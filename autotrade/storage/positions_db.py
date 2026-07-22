"""持仓追踪 + 流水

设计：
- positions: 当前仓位状态，以 option_code 为 PK
  - 同 symbol+strike+side+expiry 累计入一行（加仓 → qty_total+, qty_remaining+）
  - 分批卖出 → qty_remaining-，status 在 OPEN/PARTIAL/CLOSED 之间转移
- position_events: 不可变流水，所有变更都追加一行
  - 用于复盘、对账、PnL 估算
  - 含触发源（kc_signal/sl_polling/tp_polling/eod/manual）

时区策略：同 logger_db / risk_manager，UTC ISO 带 Z 后缀。
expiry 列存 ISO date 字符串（YYYY-MM-DD），category 决定 SL 策略。

category 是纯展示标签（用于日志/TG/复盘），行为靠两个独立 flag：

- apply_sl         = 是否挂止损（DTE 1-7 且非 lotto 才挂）
- eod_force_close  = 是否当日 EOD 强平（只看 DTE==0，不管 lotto 标签——
                     0DTE 当天必过期，必须平；周内 lotto 要放飞到 expiry）

category 命名（信息性）：
- 0dte          DTE == 0 且非 lotto
- 0dte_lotto    DTE == 0 且 lotto 标签
- lotto         DTE >= 1 且 lotto 标签
- weekly        DTE 1-7 且非 lotto（唯一挂 SL 的类目）
- swing         DTE >= 8 且非 lotto

apply_sl / eod_force_close 都冗余存到 positions 表，
避免后续规则调整污染历史仓位。

TODO（测试调整）：
- swing 8-20 DTE 区间是否要细分加一个 'mid' category 套宽松 SL
- 加仓场景（同 option_code 第二次 OPEN）的 avg_entry_price 加权平均逻辑测试
- 同 symbol 多 strike 的查询接口（find_by_symbol）排序规则（按 opened_at? expiry?）
"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, date, timezone
from typing import Optional

from autotrade.utils.logger import logger
# categorize() 已移至 autotrade.policy.positions(纯函数层);
# 此处 re-export 兼容旧调用点(from ...positions_db import categorize)。
from autotrade.policy.positions import categorize  # noqa: F401

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "trades.db"
# [refactor-change f] import-time mkdir 移除,移入显式 init()(入口/conftest 调用)


# ============ 时间工具 ============

def _utc_iso(dt: datetime = None) -> str:
    """统一 UTC ISO 带 Z 后缀。naive datetime 抛错（同 logger_db 策略）。"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        raise ValueError(f"_utc_iso() received naive datetime: {dt!r}")
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


# ============ 类目判定 ============
# categorize() 函数体逐字移至 autotrade.policy.positions,
# 本模块顶端 re-export,旧调用点与测试不受影响。


# ============ DB 初始化 ============

def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # WAL：读写不互斥 + 崩溃恢复更稳（并发访问见 logger_db 同注释）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                option_code      TEXT PRIMARY KEY,
                symbol           TEXT NOT NULL,
                strike           REAL NOT NULL,
                side             TEXT NOT NULL,
                expiry           TEXT NOT NULL,
                qty_total        INTEGER NOT NULL,
                qty_remaining    INTEGER NOT NULL,
                avg_entry_price  REAL NOT NULL,
                category         TEXT NOT NULL,
                apply_sl         INTEGER NOT NULL,
                eod_force_close  INTEGER NOT NULL DEFAULT 0,
                tp_hits          INTEGER NOT NULL DEFAULT 0,
                tags             TEXT,
                channel_name     TEXT,
                open_msg_id      TEXT,
                opened_at        TEXT NOT NULL,
                last_action_at   TEXT NOT NULL,
                closed_at        TEXT,
                status           TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pos_symbol ON positions(symbol)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS position_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                option_code      TEXT NOT NULL,
                event_type       TEXT NOT NULL,
                qty_delta        INTEGER NOT NULL,
                price            REAL,
                pct              REAL,
                trigger_source   TEXT NOT NULL,
                ref_msg_id       TEXT,
                order_id         TEXT,
                ts               TEXT NOT NULL,
                note             TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_evt_code ON position_events(option_code)
        """)
        # 迁移：早期版本可能缺新列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
        if "eod_force_close" not in cols:
            conn.execute(
                "ALTER TABLE positions ADD COLUMN eod_force_close INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("[positions] migrated: added eod_force_close column")
        if "tp_hits" not in cols:
            conn.execute(
                "ALTER TABLE positions ADD COLUMN tp_hits INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("[positions] migrated: added tp_hits column")


def init():
    """[refactor-change f] 显式初始化(建目录 + 建表)。

    原先 import 时执行 DB_PATH.parent.mkdir(...) + _init_db(),库代码 import 即有
    副作用;现由入口(autotrade.app.main / ops / diag 脚本)与 tests/conftest 显式调用。
    conftest 沿用现状:monkeypatch DB_PATH 后直接调 _init_db()。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _init_db()


# ============ 写入 ============

def open_or_add(
    option_code: str, symbol: str, strike: float, side: str,
    expiry: date, qty: int, fill_price: float,
    category: str, apply_sl: bool, eod_force_close: bool,
    tags: list[str], channel_name: str, msg_id: str,
) -> dict:
    """开仓 / 加仓。

    - 不存在 → INSERT 新仓位 + OPEN 事件
    - 已存在（同 option_code）→ UPDATE qty + 加权平均价 + ADD_ON 事件

    返回当前持仓 dict（含更新后的 qty_remaining）。
    """
    now = _utc_iso()
    expiry_str = expiry.isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM positions WHERE option_code = ?",
            (option_code,),
        ).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO positions (
                    option_code, symbol, strike, side, expiry,
                    qty_total, qty_remaining, avg_entry_price,
                    category, apply_sl, eod_force_close, tags, channel_name,
                    open_msg_id, opened_at, last_action_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                option_code, symbol, strike, side, expiry_str,
                qty, qty, fill_price,
                category, int(apply_sl), int(eod_force_close),
                json.dumps(tags), channel_name,
                str(msg_id), now, now, "OPEN",
            ))
            event_type = "OPEN"
        else:
            # 区分两种"同 option_code"场景：
            #
            # (1) reopen：已经平仓过（status='CLOSED' 或 qty_remaining=0），现在 KC 又
            #     发新开仓信号。如果按加权平均继续算，旧仓位的成本基会污染新均价，
            #     并且 tp_hits 不清零会让 TP watcher 误以为档位已经触发过、跳过。
            #     → 视作全新开仓：重置 qty_total/avg = 本次数据，清 tp_hits，
            #       event_type='OPEN'，closed_at = NULL。
            #     category/apply_sl/eod_force_close/tags/channel/msg_id/opened_at
            #     也必须一起刷新——caller 按"当前 DTE"重算过。若沿用旧值：
            #     DTE=5 开的 weekly 平掉后在到期日 reopen，本地仍是
            #     eod_force_close=False → EOD watcher 不强平 → ITM 自动行权
            #     （lessons.md #14 的事故链）。反向场景会丢 SL。
            #
            # (2) add-on：仓位还活着（PARTIAL 或 OPEN+qty_remaining>0），同 KC 加仓。
            #     → 加权平均，保留 tp_hits（如果 T1 已 hit，加仓后 T1 仍算 hit 过），
            #       event_type='ADD_ON'。
            #
            # 下游查询全部按 `status IN ('OPEN','PARTIAL')` 过滤（见 get_open_positions），
            # closed_at = NULL 在这里清是为了让 status+closed_at 状态一致，避免出现
            # status='OPEN' 但 closed_at 非空的矛盾。
            # existing 是 sqlite3.Row，不支持 .get()，要用索引（schema 保证两列都存在）
            existing_status = existing["status"]
            existing_remaining = existing["qty_remaining"]
            is_reopen = existing_status == "CLOSED" or existing_remaining == 0

            if is_reopen:
                conn.execute("""
                    UPDATE positions
                    SET qty_total = ?, qty_remaining = ?, avg_entry_price = ?,
                        category = ?, apply_sl = ?, eod_force_close = ?,
                        tags = ?, channel_name = ?, open_msg_id = ?,
                        opened_at = ?, last_action_at = ?, status = ?,
                        closed_at = NULL, tp_hits = 0
                    WHERE option_code = ?
                """, (
                    qty, qty, fill_price,
                    category, int(apply_sl), int(eod_force_close),
                    json.dumps(tags), channel_name, str(msg_id),
                    now, now, "OPEN", option_code,
                ))
                event_type = "OPEN"  # 流水语义：这是新开仓不是加仓
                logger.info(
                    f"[positions] reopen {option_code}: prev status={existing_status} "
                    f"remaining={existing_remaining}, new qty={qty} @ {fill_price:.2f}"
                )
            else:
                # 加权平均：(old_qty*old_avg + new_qty*new_price) / total
                old_total = existing["qty_total"]
                old_avg = existing["avg_entry_price"]
                new_total = old_total + qty
                new_avg = (old_total * old_avg + qty * fill_price) / new_total
                new_remaining = existing_remaining + qty
                conn.execute("""
                    UPDATE positions
                    SET qty_total = ?, qty_remaining = ?, avg_entry_price = ?,
                        last_action_at = ?, status = ?, closed_at = NULL
                    WHERE option_code = ?
                """, (
                    new_total, new_remaining, new_avg,
                    now, "OPEN", option_code,
                ))
                event_type = "ADD_ON"
                logger.info(
                    f"[positions] add-on {option_code}: +{qty} @ {fill_price:.2f} "
                    f"new_avg={new_avg:.2f} qty_rem={new_remaining}"
                )

        conn.execute("""
            INSERT INTO position_events (
                option_code, event_type, qty_delta, price, pct,
                trigger_source, ref_msg_id, ts, note
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            option_code, event_type, qty, fill_price, None,
            "kc_signal", str(msg_id), now,
            f"category={category} apply_sl={apply_sl} eod_force={eod_force_close}",
        ))

    return get(option_code)


def record_close(
    option_code: str, qty_sold: int, fill_price: float,
    trigger_source: str, ref_msg_id: Optional[str] = None,
    order_id: Optional[str] = None, note: str = "",
) -> Optional[dict]:
    """记录卖出（部分或全部）。

    Args:
        qty_sold: 本次卖出张数（正数）
        trigger_source: kc_signal / sl_polling / tp_polling / eod / manual
        ref_msg_id: 触发源是 discord 时填 msg_id
        order_id: broker 返回的卖单 ID

    Returns:
        更新后的 position dict，仓位不存在返回 None
    """
    if qty_sold <= 0:
        raise ValueError(f"qty_sold must be positive, got {qty_sold}")

    now = _utc_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        pos = conn.execute(
            "SELECT * FROM positions WHERE option_code = ?",
            (option_code,),
        ).fetchone()
        if pos is None:
            logger.warning(f"[positions] close ignored, no position: {option_code}")
            return None

        remaining = pos["qty_remaining"] - qty_sold
        if remaining < 0:
            # 卖多了，clamp 到 0 并 log（可能 close 信号要求卖 50% 但只剩 1 张）
            logger.warning(
                f"[positions] over-close {option_code}: had {pos['qty_remaining']}, "
                f"asked {qty_sold}, clamping"
            )
            qty_sold = pos["qty_remaining"]
            remaining = 0

        if remaining == 0:
            new_status = "CLOSED"
            closed_at = now
        else:
            new_status = "PARTIAL"
            closed_at = None

        conn.execute("""
            UPDATE positions
            SET qty_remaining = ?, status = ?, last_action_at = ?, closed_at = ?
            WHERE option_code = ?
        """, (remaining, new_status, now, closed_at, option_code))

        # event_type 区分用途：CLOSE = 全平，TRIM = 部分
        event_type = "CLOSE" if remaining == 0 else "TRIM"
        pct = round(qty_sold / pos["qty_remaining"] * 100, 2) if pos["qty_remaining"] else 0
        conn.execute("""
            INSERT INTO position_events (
                option_code, event_type, qty_delta, price, pct,
                trigger_source, ref_msg_id, order_id, ts, note
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            option_code, event_type, -qty_sold, fill_price, pct,
            trigger_source, ref_msg_id, order_id, now, note,
        ))

    logger.info(
        f"[positions] {event_type} {option_code}: -{qty_sold} @ {fill_price:.2f} "
        f"({trigger_source}) remaining={remaining}"
    )
    return get(option_code)


# ============ 查询 ============

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["apply_sl"] = bool(d["apply_sl"])
    d["eod_force_close"] = bool(d.get("eod_force_close", 0))
    d["tp_hits"] = int(d.get("tp_hits", 0) or 0)
    try:
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    except json.JSONDecodeError:
        d["tags"] = []
    return d


def get(option_code: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM positions WHERE option_code = ?",
            (option_code,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_open_positions() -> list[dict]:
    """所有未完全平掉的仓位（OPEN + PARTIAL）。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE status IN ('OPEN', 'PARTIAL')
            ORDER BY opened_at DESC
        """).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_open_symbols() -> set[str]:
    """活跃仓位的 symbol 集合 —— 供 CLOSE parser 做白名单消歧用。"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT DISTINCT symbol FROM positions
            WHERE status IN ('OPEN', 'PARTIAL')
        """).fetchall()
    return {r[0] for r in rows}


def find_by_symbol(symbol: str) -> list[dict]:
    """查同 symbol 所有活跃仓位（可能多 strike）。

    排序：opened_at DESC（最近的在前）。
    TODO: close 信号匹配多 strike 时的策略——v1 暂时全平所有同 symbol 仓位，
          后续可能要按"最近开仓"或"strike 最接近 close 信号中提到的价"匹配。
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE symbol = ? AND status IN ('OPEN', 'PARTIAL')
            ORDER BY opened_at DESC
        """, (symbol,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def sweep_expired(today_et: date) -> list[dict]:
    """把 expiry < today（ET）的 OPEN/PARTIAL 仓位标记为 EXPIRED。

    背景（7/13 复盘）：5 张 7/10 到期合约一直挂着 OPEN——
    - SL/TP watcher 每轮先对过期 code 取快照 → 报错 → 300s backoff，
      连带 validate 整夜 fail-open（即使补了 OPRA 订阅也会复现）
    - 过期 symbol 留在 close 白名单里，"IBM trimmed" 会对过期合约挂卖单

    注意 expiry == today 不清（当天仍可交易，EOD watcher 15:50 强平）。
    ITM 过期可能被自动行权变成正股头寸——这里只管本地记账，
    行权对账靠 scripts/sync_positions.py 人工跑。

    Returns:
        被清掉的仓位 dict 列表（清扫前的快照，qty_remaining 是过期时剩的张数）
    """
    today_iso = today_et.isoformat()
    now = _utc_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE status IN ('OPEN', 'PARTIAL') AND expiry < ?
        """, (today_iso,)).fetchall()
        swept = [_row_to_dict(r) for r in rows]
        for pos in swept:
            conn.execute("""
                UPDATE positions
                SET status = 'EXPIRED', qty_remaining = 0,
                    last_action_at = ?, closed_at = ?
                WHERE option_code = ?
            """, (now, now, pos["option_code"]))
            conn.execute("""
                INSERT INTO position_events (
                    option_code, event_type, qty_delta, price, pct,
                    trigger_source, ref_msg_id, ts, note
                ) VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                pos["option_code"], "EXPIRE", -pos["qty_remaining"], None, None,
                "expiry_sweep", None, now,
                f"expired {pos['expiry']}, {pos['qty_remaining']} contract(s) "
                f"unclosed @ entry {pos['avg_entry_price']:.2f}",
            ))
            logger.warning(
                f"[positions] EXPIRE {pos['option_code']}: "
                f"{pos['qty_remaining']} contract(s) expired {pos['expiry']} "
                f"(entry={pos['avg_entry_price']:.2f})"
            )
    return swept


def adjust_entry_price(option_code: str, expect_qty_total: int, dealt_avg: float) -> bool:
    """买单 fill 确认后，用真实成交均价回填 avg_entry_price。

    仅当 qty_total 仍等于 expect_qty_total（提交到确认之间没有加仓/reopen）
    且仓位还活着时更新；否则加权关系已变，保守跳过。

    Returns:
        True = 已更新；False = 条件不满足跳过
    """
    now = _utc_iso()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            UPDATE positions
            SET avg_entry_price = ?, last_action_at = ?
            WHERE option_code = ? AND qty_total = ?
              AND status IN ('OPEN', 'PARTIAL')
        """, (dealt_avg, now, option_code, expect_qty_total))
        if cur.rowcount == 0:
            return False
        conn.execute("""
            INSERT INTO position_events (
                option_code, event_type, qty_delta, price, pct,
                trigger_source, ref_msg_id, ts, note
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            option_code, "FILL_ADJUST", 0, dealt_avg, None,
            "fill_checker", None, now,
            f"avg_entry backfilled from dealt_avg (expect_qty={expect_qty_total})",
        ))
    logger.info(f"[positions] FILL_ADJUST {option_code}: avg_entry → {dealt_avg:.2f}")
    return True


def mark_tp_hit(option_code: str, tier_bit: int) -> None:
    """标记某档 TP 已触发。tier_bit 是位掩码（1=T1, 2=T2, 4=T3...）。

    用 OR 累加，多次调用幂等。SL/EOD 全平后 status=CLOSED，该字段不再被查询，
    所以不需要重置。
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE positions SET tp_hits = tp_hits | ? WHERE option_code = ?",
            (tier_bit, option_code),
        )


def get_events(option_code: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM position_events
            WHERE option_code = ? ORDER BY id
        """, (option_code,)).fetchall()
    return [dict(r) for r in rows]
