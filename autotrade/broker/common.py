"""
broker 共享基础（从 moomoo_client 拆分）：

- .env 配置常量（DEFAULT_QTY / TRD_ENV_STR / OPEND_HOST / OPEND_PORT /
  TRADE_PWD 含旧名 MOOMOO_TRADE_PWD 兼容 / ACC_ID）
- moomoo SDK 导入 + SDK_AVAILABLE
- _is_dry_run / _get_trd_env / _is_stale_session
- QUOTE_TZ / _quote_epoch（行情时间戳按美东本地化，见下方 postmortem 注释）

trade.py / quote.py 从这里 import 后以裸名调用，方便测试在各自模块上 monkeypatch。
"""
import os
from zoneinfo import ZoneInfo

from autotrade.utils.logger import logger
from autotrade.broker.errors import _STALE_SESSION_HINTS

# ---- 配置（从 .env 读取） ----
DEFAULT_QTY = int(os.getenv("DEFAULT_QTY", 1))
# .strip().upper() 防止 .env 写成 "simulate" / " REAL " 之类导致下面所有 == 判断失效
TRD_ENV_STR = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()
OPEND_HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("MOOMOO_PORT", 11111))
# .env 里统一 MOOMOO_TRD_* 前缀（TRD_ENV / TRD_PWD），跟原 MOOMOO_TRADE_PWD 对齐
# 模拟盘 SIMULATE 不需要密码所以历史没发现这个 typo，上真盘前必修
# 兼容老 .env 里的 MOOMOO_TRADE_PWD：旧名字命中时打个 warn 提示迁移
_legacy_pwd = os.getenv("MOOMOO_TRADE_PWD")
TRADE_PWD = os.getenv("MOOMOO_TRD_PWD") or _legacy_pwd or ""
if _legacy_pwd and not os.getenv("MOOMOO_TRD_PWD"):
    logger.warning(
        "[broker] 检测到旧 env 名 MOOMOO_TRADE_PWD，建议改为 MOOMOO_TRD_PWD（见 .env.example）"
    )
ACC_ID = int(os.getenv("MOOMOO_ACC_ID", 0))

# ---- SDK 导入 ----
try:
    from moomoo import (
        OpenSecTradeContext, OpenQuoteContext,
        TrdMarket, SecurityFirm,
        TrdSide, OrderType, TrdEnv, RET_OK,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    logger.warning("[broker] moomoo SDK 未安装，仅 DRY_RUN 可用")
    # 模块拆分后 trade/quote 通过 `from autotrade.broker.common import OpenQuoteContext`
    # 等方式取 SDK 名字；SDK 缺失时绑 None 占位，保证子模块 import 不炸。
    # 所有真实使用点都在 SDK_AVAILABLE / _get_*ctx 检查之后，DRY_RUN 路径行为不变。
    OpenSecTradeContext = OpenQuoteContext = None  # type: ignore[assignment]
    TrdMarket = SecurityFirm = None  # type: ignore[assignment]
    TrdSide = OrderType = TrdEnv = RET_OK = None  # type: ignore[assignment]

# moomoo snapshot 的 update_time 是**无时区的美东时间**字符串。
# 之前 pd.to_datetime(...).timestamp() 把它当 UTC：ET 落后 UTC 4-5 小时，
# 解析出来的 epoch 比真实值早 4-5h → 每条实时报价都被 60s 新鲜度检查
# 判为 stale 丢弃 → 真盘 SL/TP/EOD 永远拿不到价、全部 no-op。
# 若实测发现 OpenD 返回的是其它时区，用 MOOMOO_QUOTE_TZ 覆盖。
QUOTE_TZ = ZoneInfo(os.getenv("MOOMOO_QUOTE_TZ", "America/New_York"))


def _quote_epoch(update_time) -> float:
    """update_time（无 tz 字符串/时间戳）→ POSIX epoch 秒，按 QUOTE_TZ 本地化。"""
    import pandas as pd
    ts = pd.to_datetime(update_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize(QUOTE_TZ)
    return ts.timestamp()


def _is_dry_run() -> bool:
    """实时读取 DRY_RUN，避免 import 时锁定导致测试无法覆盖真实分支"""
    return os.getenv("DRY_RUN", "true").lower() == "true"


def _get_trd_env():
    """字符串配置 → SDK 枚举"""
    return TrdEnv.REAL if TRD_ENV_STR == "REAL" else TrdEnv.SIMULATE


def _is_stale_session(msg: str) -> bool:
    s = (msg or "").lower()
    return any(h in s for h in _STALE_SESSION_HINTS)
