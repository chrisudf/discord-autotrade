"""SQLite persistence: raw signals / orders

时区策略：所有时间统一存 UTC ISO with 'Z' 后缀。
复盘时用 scripts/show_today.py 自动转 ET 显示。
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# 绝对路径（跟 positions_db 一致）。之前是相对路径 Path("data/trades.db")，
# 从非 repo 根目录启动时会在 CWD 下另建一个 trades.db，
# 与 positions_db 写的库分裂成两个文件。
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "trades.db"
# [refactor-change f] import-time mkdir 移除,移入显式 init()(入口/conftest 调用)


def _utc_iso(dt: datetime = None) -> str:
    """统一 UTC ISO 格式：2026-06-16T00:16:42.258Z"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        # naive datetime 是上游 bug（典型：datetime.now() 返回本地时区裸时间）
        # 早期版本"防御性"地视作 UTC，导致 AEST 数字被贴 Z 后缀错位 10h（实测 6/16-6/17 received_at 全错）
        # 现在直接抛错，强制上游传 tz-aware
        raise ValueError(
            f"_utc_iso() received naive datetime: {dt!r}. "
            f"Caller must pass tz-aware datetime (use datetime.now(timezone.utc))."
        )
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # WAL：读写不互斥 + 崩溃恢复更稳。listener/watcher 从事件循环和
        # to_thread 线程并发访问同一库，默认 journal 模式易出 'database is locked'
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_signals (
                msg_id TEXT PRIMARY KEY,
                author TEXT,
                content TEXT,
                received_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT,
                symbol TEXT,
                side TEXT,
                strike REAL,
                expiry TEXT,
                entry_price REAL,
                qty INTEGER,
                option_code TEXT,
                success INTEGER,
                message TEXT,
                order_id TEXT,
                placed_at TEXT
            )
        """)


def init():
    """[refactor-change f] 显式初始化(建目录 + 建表)。

    原先 import 时执行 DB_PATH.parent.mkdir(...) + _init_db(),库代码 import 即有
    副作用;现由入口(autotrade.app.main / ops / diag 脚本)与 tests/conftest 显式调用。
    conftest 沿用现状:monkeypatch DB_PATH 后直接调 _init_db()。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _init_db()


def log_raw_signal(msg_id, author, content, received_at):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO raw_signals VALUES (?, ?, ?, ?)",
            (str(msg_id), author, content, _utc_iso(received_at)),
        )


def processed_msg_ids_since(since: datetime) -> set:
    """[7/28] 回补用的**跨进程**去重集合：since 之后已落库的 msg_id。

    dedup._seen() 只活在进程内存里，重启即清零。startup_backfill 会重放最近
    N 分钟的频道历史——上一次运行**已经执行过**的 CLOSE（trim）会被当成新信号
    再跑一遍（OPEN 有年龄闸门挡着，CLOSE 按设计不受年龄限制）。
    raw_signals.msg_id 是 PRIMARY KEY 且在 handler 早期就写，正好当持久化水位线。

    注意 msg_id 列是 TEXT（写入侧 str(msg_id)），比较必须用字符串。
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT msg_id FROM raw_signals WHERE received_at >= ?",
            (_utc_iso(since),),
        ).fetchall()
    return {r[0] for r in rows}


def log_order(msg_id, signal, result):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO orders
               (msg_id, symbol, side, strike, expiry, entry_price, qty,
                option_code, success, message, order_id, placed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(msg_id),
                signal.get("symbol"),
                signal.get("side"),
                signal.get("strike"),
                str(signal.get("expiry")),
                signal.get("price"),
                result.get("qty"),
                result.get("code"),
                int(result.get("success", False)),
                result.get("message"),
                result.get("order_id"),
                _utc_iso(),
            ),
        )