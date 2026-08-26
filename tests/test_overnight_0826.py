"""8/26 复盘回归（US RTH 2026-08-25，AEST 8/25 23:00 → 8/26 07:00）。

**无 ticker 的减仓告警判据下窄了 —— 8/24 那个补丁上线后第一次受检验就没接住。**

8/25 23:42 按 2.50 买入 NVDA 227.5C 9/4（成交 2.55，`FAFO LOTTO`，
tags=['lotto','fafo']）。随后 KC 连喊三次减仓：

    23:48  "trimmed a few @ 2.72, will trim out half at 214.50-215 stock price"
    23:49  "trimmed more 2.82"
    23:52  "out half NVDA 3.05 💰 214.50 target hit"   ← 只有这条执行了

前两条没写 ticker。`close_flow` 的判据是"全库恰好只有一个 **day_trade** 活仓"，
而 NVDA 这单是 lotto、`eod_force_close=False` → 命中不了 → 和补丁上线前
一模一样地静默，一条 TG 都没发。

当时全库 4 个活仓，另外 3 个是 8/14-8/22 的陈年仓位（AVGO / IONQ / TSLA）——
**只有 NVDA 是刚开 5 分钟、喊单员正在连续谈论的那个**。
判据改成「今天（ET）新开的恰好只有一个」：喊单员省略 ticker 时说的必然是
刚开的那个，不会是三周前的陈仓。

日期口径按 **ET** 不按本机：本地 8/26 00:32 开的仓，ET 还是 8/25，
喊单员当时说的就是它。
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from autotrade.listener import close_flow
from autotrade.listener.close_flow import _has_lone_fresh_position, _opened_on_et
from autotrade.listener.heuristics import _looks_like_close_attempt

# 逐字原文
NO_TICKER_TRIMS = [
    "@everyone\nKC Trades Bot:trimmed a few @ 2.72, will trim out half at 214.50-215 stock price",
    "@everyone\nKC Trades Bot:trimmed more 2.82",
]


def _pos(opened_at: str, tags=None, eod=False) -> dict:
    return {
        "option_code": "US.NVDA260904C227500",
        "opened_at": opened_at,
        "tags": tags if tags is not None else ["lotto", "fafo"],
        "eod_force_close": eod,
    }


# ============================================================
# 1. opened_at → ET 日期
# ============================================================
@pytest.mark.parametrize("opened_at,today,expected", [
    # NVDA 那单：本地 8/25 23:42 = UTC 13:42 = ET 09:42 同日
    ("2026-08-25T13:42:59.960456Z", date(2026, 8, 25), True),
    # BE 那单：本地 8/26 02:46 = UTC 16:46 = ET 12:46（仍是 8/25）
    ("2026-08-25T16:46:27.185364Z", date(2026, 8, 25), True),
    # 本地 8/26 12:00 = UTC 02:00 = ET 前一天 22:00 —— 跨日边界必须按 ET 算
    ("2026-08-26T02:00:00Z", date(2026, 8, 25), True),
    # ET 8/24 23:00，不是当天
    ("2026-08-25T03:00:00Z", date(2026, 8, 25), False),
    # 坏数据一律 False：宁可不发提醒，也不让它把 close 主链路带崩
    (None, date(2026, 8, 25), False),
    ("", date(2026, 8, 25), False),
    ("garbage", date(2026, 8, 25), False),
])
def test_opened_on_et(opened_at, today, expected):
    assert _opened_on_et(opened_at, today) is expected


# ============================================================
# 2. 判据本身
# ============================================================
def test_lotto_opened_today_counts_as_lone_fresh():
    """8/25 的真实构成：1 个当日新开的 lotto + 3 个陈仓 → 应该为 True。

    旧判据（只认 day_trade / eod_force_close）在这里返回 False，那正是漏告警的原因。
    """
    positions = [
        _pos("2026-08-25T13:42:59Z"),                      # 当日新开的 NVDA lotto
        _pos("2026-08-14T19:10:08Z", tags=[]),             # TSLA 380C，8/14
        _pos("2026-08-15T19:30:03Z", tags=[]),             # AVGO 450C，8/15
        _pos("2026-08-22T19:42:12Z", tags=[]),             # IONQ 50C，8/22
    ]
    with patch.object(close_flow.position_mgr, "get_open_positions", return_value=positions), \
         patch.object(close_flow, "today_et", return_value=date(2026, 8, 25)):
        assert _has_lone_fresh_position() is True


@pytest.mark.parametrize("fresh_count,expected", [
    (0, False),   # 当日没开过仓 —— 无从指认
    (1, True),
    (2, False),   # 两个当日新开 —— 有歧义，维持静默
    (3, False),
])
def test_only_exactly_one_fresh_position_qualifies(fresh_count, expected):
    positions = [_pos("2026-08-25T13:42:59Z") for _ in range(fresh_count)]
    positions += [_pos("2026-08-14T19:10:08Z", tags=[])]  # 陈仓不计
    with patch.object(close_flow.position_mgr, "get_open_positions", return_value=positions), \
         patch.object(close_flow, "today_et", return_value=date(2026, 8, 25)):
        assert _has_lone_fresh_position() is expected


def test_db_failure_falls_back_to_silent():
    """告警是锦上添花，不能因为它把 close 主链路带崩。"""
    with patch.object(close_flow.position_mgr, "get_open_positions",
                      side_effect=RuntimeError("db gone")):
        assert _has_lone_fresh_position() is False


# ============================================================
# 3. 端到端：当晚那两条原文该不该告警
# ============================================================
@pytest.mark.parametrize("text", NO_TICKER_TRIMS)
def test_the_two_missed_trims_now_alert(text):
    assert _looks_like_close_attempt(text, lone_day_trade=True) is True


@pytest.mark.parametrize("text", NO_TICKER_TRIMS)
def test_still_silent_when_ambiguous(text):
    """当日新开不止一个（或一个都没有）时维持原行为：不提醒。"""
    assert _looks_like_close_attempt(text, lone_day_trade=False) is False


def test_price_hint_is_still_a_hard_requirement():
    """放宽的是 ticker 那一侧，价格 hint 没放宽。"""
    assert _looks_like_close_attempt(
        "@everyone\nKC Trades Bot:just keeping this small using some profits",
        lone_day_trade=True,
    ) is False


def test_ticker_path_unaffected():
    """有 ticker + 价格时，与当日新开几个仓无关。"""
    assert _looks_like_close_attempt(
        "@everyone\nKC Trades Bot:out half NVDA 3.05 💰", lone_day_trade=False,
    ) is True
