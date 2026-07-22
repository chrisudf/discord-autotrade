"""回填本地 raw_signals 历史，提取 parse 成功的 OPEN / CLOSE，写入两个 CSV

为什么不直接拉 Discord 历史：KC 付费频道关闭了 READ_MESSAGE_HISTORY
权限——bot 能 on_message 收新消息，REST `fetch_channel`/`history` 一律 404。
所以靠 listener 持续运行落入 data/trades.db 的 raw_signals 表反推。

跑法：
    python -m autotrade.ops.backfill_history

输出：
    data/backfill_open.csv    所有成功 parse 的开仓信号
    data/backfill_close.csv   所有成功 parse 的平仓信号（含 BULK_TRIM）

频道归属：
    raw_signals 表当前没存 channel_id（log_raw_signal 没记），
    按内容前缀启发式分类：
      'enrich:' / 'enrich：' / '丰富' → enrich
      'KC Trades Bot' / 'KC交易机器人' → KC-期权-波段
      其它 → other（直接 author 测试数据等）

CLOSE 白名单（全局）：
    两遍扫描，先汇总所有成功 OPEN 的 symbol，再用全集做 CLOSE 白名单。
    比按时间累计更宽松，跨频道也能召回。

TODO: 等 KC 频道开放 READ_HISTORY 或换成有 mod 权限的账号，
      再加 Discord 直拉路径（git history 有早期版本）。
"""
import sys
import csv
import sqlite3
from pathlib import Path

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from loguru import logger

from autotrade.parsing.signal_parser import parse_signal, detect_action
from autotrade.parsing.close_parser import parse_close

# 同 listener 实时去重逻辑，避免双语 / 翻译重发 / 价格修正同时入 csv
DEDUP_WINDOW = timedelta(minutes=5)


ET_TZ = ZoneInfo("America/New_York")
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DB_PATH = DATA_DIR / "trades.db"

OPEN_CSV = DATA_DIR / "backfill_open.csv"
CLOSE_CSV = DATA_DIR / "backfill_close.csv"

OPEN_COLS = [
    "ts_utc", "channel", "author", "msg_id",
    "symbol", "side", "strike", "expiry", "expiry_date",
    "entry_price", "tags", "raw",
]
CLOSE_COLS = [
    "ts_utc", "channel", "author", "msg_id",
    "lang", "kind", "symbols", "pct", "signal_price", "signal_pnl_pct",
    "raw",
]


def classify_channel(content: str) -> str:
    """根据内容前缀启发式判定 channel。"""
    if not content:
        return "other"
    if "enrich:" in content or "enrich：" in content or content.lstrip().startswith("丰富"):
        return "enrich"
    if "KC Trades Bot" in content or "KC交易机器人" in content:
        return "KC-期权-波段"
    return "other"


def load_rows() -> list[dict]:
    """读所有 raw_signals 行，按 received_at 升序。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT msg_id, author, content, received_at FROM raw_signals "
            "ORDER BY received_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_ts_to_et_date(ts_str: str):
    """raw_signals.received_at 是 UTC ISO with Z → ET date（给 parser 用）"""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(ET_TZ).date()
    except Exception:
        return datetime.now(timezone.utc).astimezone(ET_TZ).date()


def _fingerprint_open(sig: dict) -> str:
    """同 listener `_signal_fingerprint`：symbol|side|strike|expiry_date。"""
    return (f"{sig['symbol']}|{sig['side']}|{sig['strike']}|"
            f"{sig.get('expiry_date', '')}")


def _parse_ts_full(ts_str: str) -> datetime:
    """raw_signals.received_at → tz-aware UTC datetime（naive 视作 UTC）"""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def extract_opens(rows: list[dict]) -> tuple[list[dict], set[str]]:
    """第一遍：识别所有成功 OPEN，返回 csv rows + 全局 symbol 集合。

    带 5min fingerprint dedup：同 symbol+side+strike+expiry_date 在窗口内只保留首条，
    避免 EN+ZH 双发 / KC 翻译机器人重发 把同一信号当成多个独立 trade。
    """
    open_rows = []
    symbols: set[str] = set()
    dedup_seen: dict[str, datetime] = {}
    dedup_skipped = 0

    for r in rows:
        raw = r["content"] or ""
        if not raw.strip():
            continue
        if detect_action(raw) != "OPEN":
            continue
        msg_date_et = _parse_ts_to_et_date(r["received_at"])
        sig = parse_signal(raw, msg_ts=msg_date_et)
        if sig is None or sig.get("skip"):
            continue

        # Fingerprint dedup
        fp = _fingerprint_open(sig)
        ts = _parse_ts_full(r["received_at"])
        prev = dedup_seen.get(fp)
        if prev is not None and (ts - prev) <= DEDUP_WINDOW:
            dedup_skipped += 1
            continue
        dedup_seen[fp] = ts

        open_rows.append({
            "ts_utc": r["received_at"],
            "channel": classify_channel(raw),
            "author": r["author"],
            "msg_id": r["msg_id"],
            "symbol": sig["symbol"],
            "side": sig["side"],
            "strike": sig["strike"],
            "expiry": sig.get("expiry", ""),
            "expiry_date": sig["expiry_date"].isoformat() if sig.get("expiry_date") else "",
            "entry_price": sig.get("price"),
            "tags": ";".join(sig.get("tags", [])),
            "raw": raw[:300].replace("\n", " "),
        })
        symbols.add(sig["symbol"])

    if dedup_skipped:
        logger.info(f"OPEN dedup: skipped {dedup_skipped} duplicate signal(s)")
    return open_rows, symbols


def extract_closes(rows: list[dict], whitelist: set[str]) -> list[dict]:
    """第二遍：用全局白名单识别 CLOSE。"""
    close_rows = []
    for r in rows:
        raw = r["content"] or ""
        if not raw.strip():
            continue
        if detect_action(raw) != "CLOSE":
            continue
        parsed = parse_close(raw, whitelist)
        if parsed is None:
            continue
        close_rows.append({
            "ts_utc": r["received_at"],
            "channel": classify_channel(raw),
            "author": r["author"],
            "msg_id": r["msg_id"],
            "lang": parsed.get("lang", "en"),
            "kind": parsed["kind"],
            "symbols": ";".join(parsed.get("symbols") or []),
            "pct": parsed.get("pct"),
            "signal_price": parsed.get("signal_price"),
            "signal_pnl_pct": parsed.get("signal_pnl_pct"),
            "raw": raw[:300].replace("\n", " "),
        })
    return close_rows


def write_csv(path: Path, cols: list[str], rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    logger.info(f"wrote {len(rows)} rows → {path}")


def main():
    # 注意：这里刻意不调 storage init() —— 本脚本只读 raw_signals，
    # 依赖下面的 exists() 检查在 DB 缺失时报错退出；先 init 会创建空库把检查废掉。
    if not DB_PATH.exists():
        logger.error(f"❌ DB 不存在: {DB_PATH}")
        sys.exit(1)

    rows = load_rows()
    logger.info(f"loaded {len(rows)} raw_signals rows")

    # Channel 分布预览
    from collections import Counter
    chs = Counter(classify_channel(r["content"] or "") for r in rows)
    logger.info(f"channel distribution: {dict(chs)}")

    open_rows, global_symbols = extract_opens(rows)
    logger.info(
        f"OPEN parsed={len(open_rows)} "
        f"global_symbols={sorted(global_symbols)}"
    )

    close_rows = extract_closes(rows, global_symbols)
    logger.info(f"CLOSE parsed={len(close_rows)}")

    write_csv(OPEN_CSV, OPEN_COLS, open_rows)
    write_csv(CLOSE_CSV, CLOSE_COLS, close_rows)

    # 简单摘要
    from collections import Counter
    open_by_ch = Counter(r["channel"] for r in open_rows)
    close_by_ch = Counter(r["channel"] for r in close_rows)
    logger.info(f"open by channel: {dict(open_by_ch)}")
    logger.info(f"close by channel: {dict(close_by_ch)}")


if __name__ == "__main__":
    main()
