"""per-call 读数值型 env 的容错解析。

settings.py 在启动时收齐 app 层的配置错误后 exit,但 watcher / listener / risk
里还有一批**每次调用都重读** os.getenv 的旋钮(本期保留的语义)。那些站点
拿裸 int()/float() 解析,.env 里一个手滑(`OPEN_SIGNAL_MAX_AGE_SEC=5min`、
末尾多个逗号)就抛 ValueError——而它们身处消息处理路径上,异常一路冒到
handle_message 的兜底,表现为"信号被静默丢掉 / 每次重连刷一屏错误",
配置写错的人反而看不出是自己写错了。

这里统一成"坏值退回文档化的默认值 + 明确告警":交易路径上的安全闸门宁可
按默认值继续跑,也不要因为一个 typo 整条链路瘫掉。

同一个坏值只告警一次(按 name+原文去重),否则 per-call 语义会让每条消息
都刷一行同样的 warning。
"""
import os
from typing import Optional

from autotrade.utils.logger import logger

# (name, raw) → 已告警过。坏值通常整轮进程不变,去重后只留第一次那行。
_warned: set[tuple[str, str]] = set()


def _warn_once(name: str, raw: str, reason: str, default) -> None:
    key = (name, raw)
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(f"[envcfg] {name}={raw!r} {reason},按默认值 {default} 处理")


def env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    """读 float 型 env;解析失败或低于 minimum 一律退回 default。

    低于 minimum 的值**不做 clamp 而是退回 default**:配置写成 0 或负数几乎
    都是笔误(或单位搞错),clamp 到边界值会得到一个"能跑但没意义"的配置
    (比如 backfill 只拉 1 条),不如落回文档写明的默认值,行为可预期。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        _warn_once(name, raw, "不是合法数字", default)
        return default
    if val != val:  # NaN:float("nan") 能解析,但任何比较都是 False,会悄悄废掉闸门
        _warn_once(name, raw, "是 NaN", default)
        return default
    if minimum is not None and val < minimum:
        _warn_once(name, raw, f"小于下限 {minimum}", default)
        return default
    return val


def env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    """读 int 型 env;语义同 env_float。

    只接受整数写法:"3600.0" 视为坏值退默认,不做静默截断——秒数写成小数
    多半是抄错了单位,截断会把错误藏起来。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        _warn_once(name, raw, "不是合法整数", default)
        return default
    if minimum is not None and val < minimum:
        _warn_once(name, raw, f"小于下限 {minimum}", default)
        return default
    return val
