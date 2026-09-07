"""在飞买单登记表：**"broker 说 0 张"到底是"还没到"还是"永远不会"。**

[9/2 实锤 -$166] TSLA 345P 那四分钟：

    23:38:46  买单 2104813 提交成功，DB 乐观入账 qty=2 @2.59
    23:41:47  fill_checker 轮询 180s 没等到 FILLED_ALL → TG「买单超时未确认成交」
    23:43:52  "trimmed Tesla 2.95" 解析完全正确 → 卖 1 张
              → broker: naked-short refused: broker has only 0 long
              → is_deterministic_reject 命中 → retry_guard 立刻熔断
    23:43:54  ZH 孪生"特斯拉减仓2.95"解析同样正确 → 被熔断挡掉
    05:50:21  EOD 强平 2 张 @1.76 —— 这时 broker 手里是有货的

熔断的前提（见 errors.py `_DETERMINISTIC_REJECT_HINTS`）是"naked-short 的根因
是 DB 与 broker 脱钩，退避多久都不会自愈"。8/13 MU 那次确实如此。但这一次的
根因是**买单还在飞**：限价挂上去没成交，broker 端此刻真的是 0 张，几分钟后
它成交了。这种 0 长仓是"还没到"，不是"永远不会"。

errors.py 自己写着「宁可漏判成"瞬时"，也不要把真瞬时的失败错判成确定性而
永久停掉一条止盈路径」——这个模块就是那句话缺的判据。

为什么单独一个模块：`broker/trade.py` 要读它、`position/fill_checker.py` 要写它，
而 fill_checker 本身 import broker.trade —— 放任何一边都是循环 import。
所以这里和 retry_guard 同一个形状：**纯状态，不 import 任何兄弟模块**。

只在内存里，重启即清。重启后所有买单要么已成交要么已死，本来就不该再被豁免。
"""
import time

from autotrade.utils.envcfg import env_int

# option_code → 提交时刻（time.monotonic）
_pending: dict[str, float] = {}

# 豁免窗口：买单提交后多久之内，naked-short 拒单仍按"瞬时"处理。
# 缺省 900s —— 9/2 那次拒单发生在提交后 5.5 分钟，留三倍余量。
# 判错的代价不对称：判成瞬时 = 退避后重试一次（最坏是再被拒一次，有 backoff 兜着）；
# 判成确定性 = 这个合约当晚所有喊单员平仓信号永久失效（9/2 就是这么丢的 $166）。
_DEFAULT_GRACE_SEC = 900


def _grace_sec() -> int:
    return env_int("INFLIGHT_BUY_GRACE_SEC", _DEFAULT_GRACE_SEC, minimum=0)


def mark_submitted(option_code: str) -> None:
    """买单提交成功（≠ 成交）时登记。调用点：broker.place_order 的成功分支。"""
    if option_code:
        _pending[option_code] = time.monotonic()


def clear(option_code: str) -> None:
    """买单到终态（成交 / 确认失败）时销账。调用点：fill_checker。

    **超时不销账** —— 超时的语义是"仍然不知道"，正是要豁免的那个状态；
    它由 _grace_sec() 的时间窗兜底，不会永久豁免。
    """
    _pending.pop(option_code, None)


def is_pending(option_code: str) -> bool:
    """该合约是否有"提交了、还没确认成交"的买单（且在豁免窗口内）。"""
    ts = _pending.get(option_code)
    if ts is None:
        return False
    if time.monotonic() - ts > _grace_sec():
        # 过窗即销账：再往后 broker 还说 0 张，那就是真脱钩了，该熔断就熔断。
        _pending.pop(option_code, None)
        return False
    return True


def reset_state() -> None:
    """测试用：清空跨用例残留。"""
    _pending.clear()
