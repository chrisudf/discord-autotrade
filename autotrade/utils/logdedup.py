"""重复日志收敛：同 key 的同类行在窗口内只落一条，抑制数补在行尾。

[8/13 postmortem] MU 945C 的 TP T1 在 broker 侧 naked-short 拒单后没有任何
退避，`T1 HIT → SELL → sell rejected` 三行一组从 00:11 一路刷到 06:00，
1918 组、5754 行——当晚终端日志 8183 行 / 1.09 MB，前一晚只有 363 行。
真正的止血在 position/retry_guard（不再重试），但**日志侧也要有独立的一层**：
熔断器管的是"要不要再打 broker"，这里管的是"同一句话要不要再写一遍"，
将来任何一条热路径出新的循环都不至于再把日志淹掉。

与 notify/watchdog 的节流是同一形状（首条立刻发 + 冷却期计数 + 恢复补报），
差别只在出口：那边是 TG，这边是 logger。两边都遵守"第一次绝不吞"。

用法：
    log_throttled(f"tp-reject:{code}:{tier}", "……", level="ERROR")
    ...条件解除时：
    flush(f"tp-reject:{code}:{tier}")   # 补一条"期间还有 N 条"并清状态

注意 key 要带上标的/档位等区分维度：key 撞了就是把两个不同故障压成一条。
本模块任何异常都不得外泄——日志收敛出问题不能反过来放倒调用方。
"""
import time

from autotrade.utils.envcfg import env_int
from autotrade.utils.logger import logger

_DEFAULT_WINDOW_SEC = 60

# 有界：同 dedup.py 的惰性 GC 语义，避免长跑进程里 key 无限累积
_STATE_MAX = 500

# key → {"last_emit": float, "suppressed": int, "level": str}
_state: dict[str, dict] = {}


def _window() -> int:
    """per-call 读，允许运行时调参（同 watcher interval 语义）。
    minimum=0 是合法配置：0 = 不收敛，每条都写（排查时临时打开）。"""
    return env_int("LOG_DEDUP_WINDOW_SEC", _DEFAULT_WINDOW_SEC, minimum=0)


def _sweep(now: float, window: int) -> None:
    """惰性 GC：超过上限时清掉已过窗口且无待报计数的死 key。"""
    if len(_state) <= _STATE_MAX:
        return
    dead = [
        k for k, s in _state.items()
        if not s["suppressed"] and (now - s["last_emit"]) > window
    ]
    for k in dead:
        del _state[k]


def log_throttled(key: str, message: str, *, level: str = "INFO",
                  window: "int | None" = None) -> bool:
    """同 key 的日志在窗口内只落一条。返回是否真的写了。

    第一次调用立刻写（窗口不吃第一声）；窗口内的后续调用只累加计数；
    窗口过后的第一条把攒下的计数补在行尾一起写。
    """
    try:
        win = _window() if window is None else window
        now = time.monotonic()
        _sweep(now, win)
        slot = _state.get(key)

        if slot is not None and (now - slot["last_emit"]) < win:
            slot["suppressed"] += 1
            return False

        suppressed = slot["suppressed"] if slot else 0
        _state[key] = {"last_emit": now, "suppressed": 0, "level": level}
        if suppressed:
            message = f"{message}（另有 {suppressed} 条同类被收敛）"
        logger.log(level, message)
        return True
    except Exception as e:  # 收敛层自身不许放倒调用方
        try:
            logger.log(level, message)
            logger.warning(f"[logdedup] {key} 收敛失败，已原样输出: {type(e).__name__}: {e}")
        except Exception:
            pass
        return True


def flush(key: str) -> None:
    """条件解除时补报窗口内被压掉的条数并清状态。

    不调用也不会漏日志（下一条同 key 的 log_throttled 会带上计数），
    但故障恢复后往往不再有下一条——那时候"期间还有 N 条"就永远不会出现，
    所以恢复路径上应显式 flush（同 watchdog.notify_tick_ok 的补报语义）。
    """
    try:
        slot = _state.pop(key, None)
        if slot and slot["suppressed"]:
            logger.log(slot["level"],
                       f"[logdedup] {key} 期间另有 {slot['suppressed']} 条同类被收敛")
    except Exception as e:
        logger.warning(f"[logdedup] {key} flush 失败: {type(e).__name__}: {e}")


def reset_state() -> None:
    """测试用：清空跨用例残留的收敛状态。"""
    _state.clear()
