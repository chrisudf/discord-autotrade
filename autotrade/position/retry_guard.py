"""卖出拒单熔断器：退避 → 熔断 → 一声大告警，替掉"5 秒一轮硬打到天亮"。

[8/13 postmortem] MU 945C 的 TP T1 打到 broker 的 naked-short 防护上
（broker 0 长仓，本地 DB 说还有 2 张）。拒单路径上 `_sell_rejected` 只做了
两件事：log + TG，然后 `_triggered_this_tick.discard(key)` 把这一 tick 的
去重也撤了 —— 而 tp_hits 位掩码是在**下单成功之后**才置位的（见
sell_executor 的 pre_record 钩子顺序），所以拒单不留任何痕迹，下一轮 5s
tick 原样再来一遍。00:11 到 06:00，1918 次，5754 行日志，1918 条 TG。

这里补上四路卖出都缺的那层状态：

  1. **确定性拒单立刻熔断**（broker/errors.is_deterministic_reject）。
     naked-short 拒单的根因是 DB 与 broker 脱钩，退避多久都不会自愈，
     重试一百次是一百次一样的答案。
  2. **瞬时拒单指数退避**：30s → 60s → 120s → 240s，封顶
     SELL_REJECT_BACKOFF_CAP_SEC（默认 300s）。退避期内连价都不查、
     单也不下，直接静默跳过。
  3. **连续失败 SELL_REJECT_MAX_FAILS 次（默认 5）后熔断**，本进程内不再
     重试该 (合约, 档位)。
  4. **告警只在"第一次失败"和"熔断"两个时刻发**，中间的退避轮次静默。
     （节流形状同 notify/watchdog：第一声绝不吞，中间攒着，末了报总数。）

熔断的边界 —— 刻意选的保守面：

  - 熔断**不写 DB、不置 tp_hits 位**。置位等于"这一档就算完成了"，会让本该
    落袋的止盈永久消失在一个静默的位掩码里；这条路径上钱的去向必须有人知道，
    所以是"停手 + 大声喊人"，不是"标记已完成"。
  - 熔断只活在内存里，重启即清。运营修完对账（ops/sync_positions）重启
     listener 就恢复，不需要额外的解除开关。
  - 熔断**只挡这一个 (合约, 档位)**：同标的的其它档、其它标的照常走。

本模块不 import broker/notify（monkeypatch 兼容硬约束，见 sell_executor
模块 docstring），只吃字符串、吐决策，I/O 全留给调用方。
"""
import time
from dataclasses import dataclass

from autotrade.broker.errors import is_deterministic_reject
from autotrade.utils.envcfg import env_int

_DEFAULT_MAX_FAILS = 5
_DEFAULT_BACKOFF_BASE_SEC = 30
_DEFAULT_BACKOFF_CAP_SEC = 300

# key → {"fails": int, "blocked_until": float, "tripped": bool, "last_err": str}
# key 由调用方拼（TP 用 "tp:{code}:t{tier}"），粒度就是熔断粒度。
_state: dict[str, dict] = {}


@dataclass
class RejectDecision:
    """一次拒单之后该怎么办。调用方按 alert 决定发不发 TG、按 tripped 决定文案。"""
    fails: int          # 连续失败次数（含本次）
    tripped: bool       # 是否已熔断（本进程内不再重试该 key）
    retry_after: float  # 距下次可重试的秒数；tripped 时为 0（不会再有下次）
    deterministic: bool # 是否确定性拒单（决定了是"立刻熔断"还是"退避"）
    alert: bool         # 本次是否值得发 TG（首次失败 / 熔断那一刻）
    suppressed: int     # 上次告警以来被压掉的失败次数（补在告警文案里）


def _max_fails() -> int:
    # minimum=1：0 会让"第一次失败即熔断"，虽然可用但更像手滑（想要那个效果
    # 的人应该直接把 base 调大），退回默认值更安全。
    return env_int("SELL_REJECT_MAX_FAILS", _DEFAULT_MAX_FAILS, minimum=1)


def _backoff_sec(fails: int) -> float:
    """指数退避：base * 2^(fails-1)，封顶 cap。"""
    base = env_int("SELL_REJECT_BACKOFF_BASE_SEC", _DEFAULT_BACKOFF_BASE_SEC, minimum=1)
    cap = env_int("SELL_REJECT_BACKOFF_CAP_SEC", _DEFAULT_BACKOFF_CAP_SEC, minimum=1)
    # fails 可能很大（配置把 max_fails 调高时），2**n 先夹一层免得溢出成天文数字
    return float(min(cap, base * (2 ** min(fails - 1, 16))))


def _slot(key: str) -> dict:
    return _state.setdefault(
        key, {"fails": 0, "blocked_until": 0.0, "tripped": False,
              "last_err": "", "suppressed": 0}
    )


def blocked(key: str) -> "str | None":
    """当前是否该跳过这次卖出尝试。返回 None = 放行，否则是给日志用的原因串。

    调用点在"决定触发"之前 —— 退避期内连报价比较、锁、broker 都不该碰。
    """
    slot = _state.get(key)
    if slot is None:
        return None
    if slot["tripped"]:
        return f"已熔断（连续 {slot['fails']} 次拒单：{slot['last_err'][:80]}），等人工处理"
    remain = slot["blocked_until"] - time.monotonic()
    if remain > 0:
        return f"退避中（还有 {remain:.0f}s，已连续失败 {slot['fails']} 次）"
    return None


def on_reject(key: str, err: str, *, backoff: bool = True) -> RejectDecision:
    """登记一次拒单/下单异常，返回该退避还是该熔断。

    backoff=False：**只熔断确定性拒单，瞬时拒单原样放过**（不退避、不累计到
    max_fails）。给两类调用方用：
      - kc_close：信号驱动。瞬时拒单的重试是 0005 设计好的——指纹回滚 +
        1-3s 后的双语孪生天然重试一次；在这里插退避等于把那条设计打断。
      - eod_force：自己已有 0007 调好的 60s `_skip_until`，且强平窗口只有
        十分钟，再叠一层指数退避会把仅有的几次机会吃掉。
    两者共同点是"重试有人管"，缺的只是对确定性拒单的止损。
    """
    slot = _slot(key)
    slot["fails"] += 1
    slot["last_err"] = err or "unknown"
    first_failure = slot["fails"] == 1

    deterministic = is_deterministic_reject(err)
    tripped = deterministic or (backoff and slot["fails"] >= _max_fails())

    if not backoff and not tripped:
        # 瞬时拒单在这个模式下不留退避痕迹：调用方自己的重试节奏说了算。
        # fails 仍然累加（诊断用），但不设 blocked_until。
        # alert 恒 True —— 这两条路径原本每次拒单都发 TG，节流不是本次要动的
        # 东西（kc_close 的一条拒单对应喊单员的一条信号，压掉就成了静默失败）。
        return RejectDecision(
            fails=slot["fails"], tripped=False, retry_after=0.0,
            deterministic=False, alert=True, suppressed=0,
        )

    if tripped:
        slot["tripped"] = True
        slot["blocked_until"] = 0.0
        retry_after = 0.0
    else:
        retry_after = _backoff_sec(slot["fails"])
        slot["blocked_until"] = time.monotonic() + retry_after

    # 告警只在两端：第一声（别让节流吃掉）+ 熔断（钱路上的止盈停了，必须有人知道）
    alert = first_failure or tripped
    suppressed = slot["suppressed"]
    if alert:
        slot["suppressed"] = 0
    else:
        slot["suppressed"] += 1

    return RejectDecision(
        fails=slot["fails"], tripped=tripped, retry_after=retry_after,
        deterministic=deterministic, alert=alert, suppressed=suppressed,
    )


def on_success(key: str) -> int:
    """卖出成功 → 清状态。返回清掉的连续失败数（0 = 本来就没失败过）。"""
    slot = _state.pop(key, None)
    return slot["fails"] if slot else 0


def describe_config() -> str:
    """给 watcher 启动行用的一句话配置摘要（per-call 读，反映当前 env）。"""
    return (f"max_fails={_max_fails()}, "
            f"backoff={_backoff_sec(1):.0f}s→{_backoff_sec(99):.0f}s")


def is_tripped(key: str) -> bool:
    slot = _state.get(key)
    return bool(slot and slot["tripped"])


def clear_prefix(prefix: str) -> int:
    """清掉某一路（"tp:" / "sl:" / "eod:" ...）的全部状态，返回清掉的条数。

    给 eod_watcher 的跨日 GC 用：昨天那次强平的熔断不该影响今天
    （同 _skip_until/_alerted_until 的跨日清空语义）。
    """
    dead = [k for k in _state if k.startswith(prefix)]
    for k in dead:
        del _state[k]
    return len(dead)


def reset_state() -> None:
    """测试用：清空跨用例残留的熔断状态。"""
    _state.clear()
