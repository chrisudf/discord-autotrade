"""
风险熔断管理器
4 道防线：
  1. 单张合约价格上限
  2. 单笔订单总成本上限
  3. 当日累计成本上限（触发后当日全停）
  4. 当日下单次数上限（触发后当日全停）

时区：
  - trading_date: 美东自然日 YYYY-MM-DD（00:00 ET 切换）
  - ts / triggered_at: UTC ISO with 'Z' 后缀（跟 trades.db 对齐）
持久化：SQLite，重启不丢失
"""
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional
from loguru import logger

# [refactor-change f] import-time load_dotenv(ENV_PATH) 移除:.env 只在
# autotrade.app.main / ops / diag 脚本的 main() 里加载一次。
# 本模块 per-call os.getenv 语义(_effective_max_cost_per_order 等)原样保留。

# 风控参数
MAX_PRICE_PER_CONTRACT = float(os.getenv("MAX_PRICE_PER_CONTRACT", "5.0"))
MAX_DAILY_COST = float(os.getenv("MAX_DAILY_COST", "2000"))
MAX_DAILY_ORDERS = int(os.getenv("MAX_DAILY_ORDERS", "10"))

# 单笔订单成本上限：env-aware 硬卡
#   REAL  → 无论 env 怎么设，强制 ≤ $1000（用户硬性要求："实盘单笔 <1000"）
#   SIMULATE / DRY_RUN → 用 env 值，缺省给一个高数，让模拟盘可以测任何信号
# 改这个常量请同步 docs/，并明确：这个上限**晚于 channel max_price**生效，
# 是真盘最后一道防线。
_REAL_HARD_CAP = 1000.0
_SIMULATE_DEFAULT = 100000.0


def _effective_max_cost_per_order() -> float:
    """REAL: min(env, $1000)；SIMULATE: env or $100000。

    每次调用都重新读 env / TRD_ENV，避免运行中切环境时拿到过期值（虽然实际不切，
    但这个变量是真盘最后一道防线，不要 cache）。
    """
    trd_env = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()
    if trd_env == "REAL":
        env_val = float(os.getenv("MAX_COST_PER_ORDER", str(_REAL_HARD_CAP)))
        return min(env_val, _REAL_HARD_CAP)
    return float(os.getenv("MAX_COST_PER_ORDER", str(_SIMULATE_DEFAULT)))


# 启动时读取一次用于 banner 显示；运行时每次 check_order 重新调 _effective_max_cost_per_order
MAX_COST_PER_ORDER = _effective_max_cost_per_order()

# 数据库路径
# (模块归属变化:src/risk/risk_manager.py → autotrade/risk.py 少了一层目录,
#  parent 层级相应减一,仍指向 repo_root/data/risk.db)
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "risk.db"
# [refactor-change f] import-time mkdir 移除,移入显式 init()(入口/conftest 调用)

# 美东时区（用 ZoneInfo 自动处理夏/冬令时）
try:
    from zoneinfo import ZoneInfo
    US_EASTERN = ZoneInfo("America/New_York")
except ImportError:
    # Python < 3.9 兜底
    US_EASTERN = timezone(timedelta(hours=-5))
    logger.warning("[Risk] zoneinfo 不可用，用固定 UTC-5（无夏令时）")


# ============ 数据结构 ============

@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool
    reason: str = ""           # 拦截原因（passed=False 时）
    detail: str = ""           # 详细信息
    block_rest_of_day: bool = False  # 是否触发当日全停


# ============ 时间工具 ============

def get_trading_date() -> str:
    """
    返回当前交易日字符串 YYYY-MM-DD（美东时间）
    切换时点：美东 00:00（ZoneInfo 自动处理 DST）
    """
    now_et = datetime.now(US_EASTERN)
    return now_et.strftime("%Y-%m-%d")


def _utc_iso() -> str:
    """统一 UTC ISO 格式带 Z 后缀，跟 logger_db.py 对齐
    例: 2026-06-16T14:30:00.123Z
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ============ 数据库 ============

def _init_db():
    """初始化数据库表"""
    with sqlite3.connect(DB_PATH) as conn:
        # WAL：读写不互斥 + 崩溃恢复更稳（并发访问见 logger_db 同注释）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trading_date TEXT NOT NULL,
                ts TEXT NOT NULL,
                symbol TEXT,
                strike REAL,
                side TEXT,
                expiry TEXT,
                price REAL NOT NULL,
                qty INTEGER NOT NULL,
                cost REAL NOT NULL,
                channel_id TEXT,
                channel_name TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_trading_date 
            ON daily_orders(trading_date)
        """)
        # 熔断锁表：哪天被全停了
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_circuit_breaker (
                trading_date TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                triggered_at TEXT NOT NULL
            )
        """)
        conn.commit()


def init():
    """[refactor-change f] 显式初始化(建目录 + 建表)。

    原先 import 时执行 DB_PATH.parent.mkdir(...) + _init_db(),库代码 import 即有
    副作用;现由入口(autotrade.app.main / ops / diag 脚本)与 tests/conftest 显式调用。
    conftest 沿用现状:monkeypatch DB_PATH 后直接调 _init_db()。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _init_db()


# ============ 查询 ============

def get_daily_stats(trading_date: Optional[str] = None) -> dict:
    """返回当日统计"""
    if trading_date is None:
        trading_date = get_trading_date()
    
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(cost), 0)
            FROM daily_orders WHERE trading_date = ?
        """, (trading_date,))
        count, total_cost = cur.fetchone()
        
        cur = conn.execute("""
            SELECT reason, triggered_at FROM daily_circuit_breaker
            WHERE trading_date = ?
        """, (trading_date,))
        cb = cur.fetchone()
    
    return {
        "trading_date": trading_date,
        "order_count": count,
        "total_cost": total_cost,
        "circuit_broken": cb is not None,
        "circuit_reason": cb[0] if cb else None,
        "circuit_triggered_at": cb[1] if cb else None,
        "max_orders": MAX_DAILY_ORDERS,
        "max_cost": MAX_DAILY_COST,
        "remaining_orders": MAX_DAILY_ORDERS - count,
        "remaining_cost": MAX_DAILY_COST - total_cost,
    }


def is_circuit_broken(trading_date: Optional[str] = None) -> bool:
    """当日是否已熔断"""
    if trading_date is None:
        trading_date = get_trading_date()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            SELECT 1 FROM daily_circuit_breaker WHERE trading_date = ?
        """, (trading_date,))
        return cur.fetchone() is not None


def _trigger_circuit_breaker(reason: str):
    """触发当日熔断"""
    trading_date = get_trading_date()
    now = _utc_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO daily_circuit_breaker 
            (trading_date, reason, triggered_at)
            VALUES (?, ?, ?)
        """, (trading_date, reason, now))
        conn.commit()
    logger.warning(f"[Risk] 🚨 当日熔断触发: {reason} (date={trading_date})")


# ============ 核心检查 ============

def check_order(price: float, qty: int,
                symbol: str = "", strike: float = 0,
                side: str = "", expiry: str = "",
                channel_name: str = "",
                max_price_override: float = None,
                effective_price: float = None) -> RiskCheckResult:
    """
    下单前风控检查（不记录订单，只检查）

    Args:
        price: 单张合约价格（每股美元，信号价）—— Layer 1 用它比 max_price
        qty: 下单张数
        effective_price: 实际挂单价（含 slippage，见 broker.calc_limit_price）。
            Layer 2/3/4 的成本按它算——broker 会挂 price × (1+5~12%)，
            若按信号价算成本，REAL $1000 硬顶实际能被突破到 ~$1120。
            不传时退回用 price（兼容老调用方/测试，但会低估成本）。

    Returns:
        RiskCheckResult
    """
    trading_date = get_trading_date()
    cost_price = effective_price if effective_price is not None else price
    cost = cost_price * 100 * qty
    
    # ---------- Layer 0: 当日已熔断 ----------
    if is_circuit_broken(trading_date):
        stats = get_daily_stats(trading_date)
        return RiskCheckResult(
            passed=False,
            reason="当日已熔断",
            detail=f"原因: {stats['circuit_reason']} | 触发时间: {stats['circuit_triggered_at']}"
        )
    
    # ---------- Layer 1: 单张价格 ----------
    # channel 配置优先；没传则用全局 MAX_PRICE_PER_CONTRACT
    effective_max_price = max_price_override if max_price_override is not None else MAX_PRICE_PER_CONTRACT
    if price > effective_max_price:
        return RiskCheckResult(
            passed=False,
            reason="单张合约价格超限",
            detail=f"信号价 ${price} > 上限 ${effective_max_price} (channel override={max_price_override is not None})"
        )
    
    # ---------- Layer 2: 单笔成本 ----------
    # 每次 check 重新算 effective cap：REAL 总是硬卡 $1000，SIMULATE 用 env 值。
    # 这样运行时切环境 (理论上不会发生)也安全，且代码里清楚标示 cap 来源。
    effective_max_cost = _effective_max_cost_per_order()
    if cost > effective_max_cost:
        trd_env = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()
        return RiskCheckResult(
            passed=False,
            reason="单笔订单成本超限",
            detail=(
                f"本笔成本 ${cost:.0f} (挂单价 ${cost_price} × 100 × {qty}) "
                f"> 上限 ${effective_max_cost:.0f} (env={trd_env})"
            )
        )
    
    # ---------- Layer 3 & 4: 当日累计 ----------
    stats = get_daily_stats(trading_date)
    
    # 次数检查
    if stats["order_count"] >= MAX_DAILY_ORDERS:
        _trigger_circuit_breaker(f"当日下单次数达上限 ({MAX_DAILY_ORDERS})")
        return RiskCheckResult(
            passed=False,
            reason="当日下单次数达上限",
            detail=f"已下 {stats['order_count']}/{MAX_DAILY_ORDERS} 单，当日剩余全停",
            block_rest_of_day=True
        )
    
    # 累计成本检查（本笔会导致超限）
    if stats["total_cost"] + cost > MAX_DAILY_COST:
        _trigger_circuit_breaker(
            f"当日累计成本将达上限 (${stats['total_cost']:.0f} + ${cost:.0f} > ${MAX_DAILY_COST:.0f})"
        )
        return RiskCheckResult(
            passed=False,
            reason="当日累计成本达上限",
            detail=f"已花 ${stats['total_cost']:.0f}，本笔 ${cost:.0f}，上限 ${MAX_DAILY_COST:.0f}，当日剩余全停",
            block_rest_of_day=True
        )
    
    # ---------- 全部通过 ----------
    return RiskCheckResult(passed=True)


def record_order(price: float, qty: int,
                 symbol: str = "", strike: float = 0,
                 side: str = "", expiry: str = "",
                 channel_id: str = "", channel_name: str = ""):
    """
    记录已下单（必须在下单成功后调用）
    """
    trading_date = get_trading_date()
    now = _utc_iso()
    cost = price * 100 * qty
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO daily_orders 
            (trading_date, ts, symbol, strike, side, expiry, price, qty, cost, 
             channel_id, channel_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trading_date, now, symbol, strike, side, expiry, 
              price, qty, cost, channel_id, channel_name))
        conn.commit()
    
    logger.info(
        f"[Risk] 订单已记录: {symbol} {strike}{side} {expiry} "
        f"x{qty} @ ${price} (cost=${cost:.0f}) date={trading_date}"
    )


# ============ 手动管理工具 ============

def manual_reset_today() -> dict:
    """手动重置当日（清除熔断 + 清除今日订单记录）"""
    trading_date = get_trading_date()
    with sqlite3.connect(DB_PATH) as conn:
        cur1 = conn.execute("DELETE FROM daily_circuit_breaker WHERE trading_date = ?", 
                            (trading_date,))
        cur2 = conn.execute("DELETE FROM daily_orders WHERE trading_date = ?", 
                            (trading_date,))
        conn.commit()
        return {
            "trading_date": trading_date,
            "circuit_breaker_cleared": cur1.rowcount,
            "orders_cleared": cur2.rowcount,
        }


def clear_circuit_breaker_only(trading_date: Optional[str] = None) -> bool:
    """只清除熔断标记，保留订单记录（更安全）"""
    if trading_date is None:
        trading_date = get_trading_date()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM daily_circuit_breaker WHERE trading_date = ?", 
                          (trading_date,))
        conn.commit()
        return cur.rowcount > 0