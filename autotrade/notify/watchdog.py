"""watcher tick 异常 → TG 告警（带节流 + 恢复通知）。

[7/31 postmortem] 磁盘写满，sl/tp/eod 三个 watcher 在 22 秒里连抛 33 条
`sqlite3.OperationalError: disk I/O error`——TG 一声没吭。根因是五个 watcher 的
catch-all 清一色只有 `logger.exception(...)`，而当晚**日志本身就是写不进去的那
个东西**：app_2026-08-01.log 从 08:34 直接跳到 11:10，errors/ 当天 0 字节。
风控三路全灭，磁盘上零痕迹，全靠终端恰好还开着才被发现。

结论：告警链路不能只挂在日志上。这里给 watcher 的 except 分支补一条独立的
TG 通道，三条规则：

  1. 首次出错立刻发——别让节流窗口吃掉第一声
  2. 之后每个 scope 冷却 WATCHER_ERROR_ALERT_COOLDOWN_SEC（缺省 300s），
     冷却期内的次数攒着，下次发时一并报出。22 秒 33 条如果不节流就是 33 条
     TG，刷屏等于没有告警（参见 7/23 runner-preserve 一夜 6 连发的教训）
  3. 恢复时补一条 ✅——只报"出事了"不报"好了没"，运营还是得自己去翻日志

本模块任何异常都不得外泄：告警失败是告警的事，不能反过来放倒 watcher。
"""
import time

from autotrade.notify.messages import format_error
from autotrade.notify.transport import escape_md, notify_bg
from autotrade.utils.envcfg import env_int
from autotrade.utils.logger import logger

_DEFAULT_COOLDOWN_SEC = 300

# scope → {"last_sent": float, "suppressed": int, "failing": bool}
_state: dict[str, dict] = {}


def _cooldown() -> int:
    """per-call 读，允许运行时调参（同 watcher 的 interval 语义）。"""
    return env_int(
        "WATCHER_ERROR_ALERT_COOLDOWN_SEC", _DEFAULT_COOLDOWN_SEC, minimum=0
    )


def _slot(scope: str) -> dict:
    return _state.setdefault(
        scope, {"last_sent": 0.0, "suppressed": 0, "failing": False}
    )


def notify_tick_error(scope: str, exc: BaseException) -> None:
    """watcher tick 抛异常时调用：落 traceback + 按节流发 TG。

    必须在 except 分支内调用（logger 依赖当前异常上下文）。
    """
    # 1) 日志照旧——traceback 是排查主线索，TG 只带摘要
    try:
        logger.opt(exception=exc).error(f"[{scope}] tick error (continuing)")
    except Exception:
        pass  # 日志沟本身可能就是坏的（7/31 正是这种情况），继续走 TG

    # 2) TG 节流
    try:
        slot = _slot(scope)
        now = time.monotonic()
        first_failure = not slot["failing"]
        slot["failing"] = True

        if not first_failure and (now - slot["last_sent"]) < _cooldown():
            slot["suppressed"] += 1
            return

        detail = f"{type(exc).__name__}: {exc}"
        if slot["suppressed"]:
            detail += f"\n（冷却期内另有 {slot['suppressed']} 次同类错误未单独告警）"

        slot["last_sent"] = now
        slot["suppressed"] = 0
        notify_bg(format_error(f"{scope} watcher tick", detail))
    except Exception as e:
        logger.warning(f"[watchdog] {scope} 告警发送失败: {type(e).__name__}: {e}")


def notify_tick_ok(scope: str) -> None:
    """watcher tick 正常完成时调用。健康态下只有一次 dict 查找，热路径安全。"""
    slot = _state.get(scope)
    if slot is None or not slot["failing"]:
        return

    try:
        slot["failing"] = False
        missed = slot["suppressed"]
        slot["suppressed"] = 0
        tail = f"（期间共 {missed} 次同类错误未单独告警）" if missed else ""
        logger.info(f"[{scope}] tick 已恢复{tail}")
        # scope/tail 走 escape_md：MarkdownV2 的 reserved 字符表含 ASCII 括号和
        # 句点，拼进去不转义会让整条消息 400（虽有纯文本 fallback，但告警不该
        # 每次都退化成兜底路径）。`*` 是我们自己的结构字符，不转义。
        notify_bg(f"✅ *{escape_md(scope)} watcher 已恢复*{escape_md(tail)}")
    except Exception as e:
        logger.warning(f"[watchdog] {scope} 恢复通知失败: {type(e).__name__}: {e}")


def reset_state() -> None:
    """测试用：清空跨用例残留的节流状态。"""
    _state.clear()
