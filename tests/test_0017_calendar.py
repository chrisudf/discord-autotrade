"""[0017] 半日市 + 2028 假日表测试。

覆盖三个消费方：
1. parsing.holidays：US_OPTION_HOLIDAYS_2028（含 observed 平移与"元旦不观察"
   特例）、EARLY_CLOSE_DATES 全量成员、is_early_close helper
2. position.eod_watcher._is_eod_window：半日市窗口前移 12:50 ~ 13:05
3. broker.quote._is_rth / _last_rth_close_utc / _freshness_verdict：
   半日市 13:00 收盘语义

选 2026-11-27（黑五，本表最近的一个半日市）做主锚点：这是生产机上第一个
会真实踩到的日期——修错了当天到期仓位会在收盘后才开始挂卖单，直接过期
（AVGO 415C 7/24 归零同款结局）。
"""
from datetime import date, datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

import pytest

from autotrade.parsing.holidays import (
    EARLY_CLOSE_DATES,
    US_OPTION_HOLIDAYS_2028,
    adjust_to_trading_day,
    is_early_close,
    is_trading_day,
)
from autotrade.position import eod_watcher
from autotrade.broker.quote import (
    QUOTE_DELAYED,
    QUOTE_OK,
    _freshness_verdict,
    _is_rth,
    _last_rth_close_utc,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _utc(y, m, d, hh, mm):
    return _et(y, m, d, hh, mm).astimezone(timezone.utc)


# ============================================================
# 1. 2028 假日表
# ============================================================

@pytest.mark.parametrize("holiday", sorted(US_OPTION_HOLIDAYS_2028))
def test_2028_holidays_not_trading_days(holiday):
    assert not is_trading_day(holiday), f"{holiday} 应为 2028 假日"


def test_2028_holiday_table_exact():
    # 全量锁死：漏一个（watcher 在假日空转/误判过期）或多一个（真交易日
    # 被跳过强平 → 到期仓位过期）都是钱路问题，用集合相等而非逐条 in。
    assert US_OPTION_HOLIDAYS_2028 == {
        date(2028, 1, 17),   # MLK：1 月第 3 个周一
        date(2028, 2, 21),   # Presidents：2 月第 3 个周一
        date(2028, 4, 14),   # Good Friday：复活节 4/16 前的周五
        date(2028, 5, 29),   # Memorial：5 月最后一个周一
        date(2028, 6, 19),   # Juneteenth：恰为周一
        date(2028, 7, 4),    # Independence：周二
        date(2028, 9, 4),    # Labor：9 月第 1 个周一
        date(2028, 11, 23),  # Thanksgiving：11 月第 4 个周四
        date(2028, 12, 25),  # Christmas：周一
    }
    # 表内日期全部落工作日（observed 平移正确性的自检：
    # 假日落在周末说明推导错了）
    for h in US_OPTION_HOLIDAYS_2028:
        assert h.weekday() < 5, f"{h} 落在周末，observed 平移有误"


def test_2028_new_years_not_observed():
    """NYSE Rule 7.2 特例：2028 元旦落周六，且前一个周五 2027-12-31 是
    年末结算日 → 不前移休市，元旦整个不观察（先例 2022-01-01）。"""
    # 2027-12-31（周五）照常交易——年末最后一个交易日，0DTE 强平必须照跑
    assert is_trading_day(date(2027, 12, 31))
    # 2028-01-03（下周一）也是普通交易日
    assert is_trading_day(date(2028, 1, 3))
    # 元旦本身是周六，非交易日的原因是周末而非假日表
    assert date(2028, 1, 1) not in US_OPTION_HOLIDAYS_2028


def test_2028_expiry_adjustments():
    """到期日平移穿过 2028 假日。"""
    # 7/4 周二假日 → 前移到 7/3（半日市，但仍是交易日）
    assert adjust_to_trading_day(date(2028, 7, 4)) == date(2028, 7, 3)
    # 感恩节周四 → 前移到周三
    assert adjust_to_trading_day(date(2028, 11, 23)) == date(2028, 11, 22)
    # 圣诞周一 → 连跳周日/周六到周五 12/22
    assert adjust_to_trading_day(date(2028, 12, 25)) == date(2028, 12, 22)


def test_2028_regular_days_still_trading():
    assert is_trading_day(date(2028, 6, 20))   # Juneteenth 次日周二
    assert is_trading_day(date(2028, 11, 22))  # 感恩节前的周三


# ============================================================
# 2. EARLY_CLOSE_DATES
# ============================================================

def test_early_close_table_exact():
    # 全量锁死推导结果，特别是三个"容易想当然多加"的反例：
    # 2026/2027 无 7 月半日市、2027/2028 无平安夜半日市（详见 holidays.py 注释）
    assert EARLY_CLOSE_DATES == {
        date(2026, 11, 27),  # 黑五
        date(2026, 12, 24),  # 平安夜（周四）
        date(2027, 11, 26),  # 黑五
        date(2028, 7, 3),    # 独立日前一天（周一）
        date(2028, 11, 24),  # 黑五
    }


@pytest.mark.parametrize("d", sorted(EARLY_CLOSE_DATES))
def test_early_close_days_are_trading_days(d):
    """半日市是交易日：不能因为提前收盘就把它当假日跳过强平。"""
    assert is_trading_day(d)
    assert is_early_close(d)


def test_non_early_close_counterexamples():
    # 半日市"候选但不成立"的日子必须是 False：
    assert not is_early_close(date(2026, 7, 2))    # 7/3 本身是 observed 假日
    assert not is_early_close(date(2027, 7, 2))    # 7/3 落周六，7/2 全日
    assert not is_early_close(date(2027, 12, 23))  # 12/24 本身是 observed 假日
    assert not is_early_close(date(2028, 12, 22))  # 12/24 落周日，12/22 全日
    # 普通交易日
    assert not is_early_close(date(2026, 7, 28))


# ============================================================
# 3. eod_watcher 半日市窗口（2026-11-27 黑五，默认 cfg 15:50）
# ============================================================

def test_eod_window_black_friday_shifted():
    bf = lambda hh, mm: _et(2026, 11, 27, hh, mm)  # noqa: E731
    assert eod_watcher._is_eod_window(bf(12, 49), 15, 50) is False  # 未到前移 cutoff
    assert eod_watcher._is_eod_window(bf(12, 51), 15, 50) is True   # 前移窗口内
    assert eod_watcher._is_eod_window(bf(13, 4), 15, 50) is True    # 13:00 收盘 + 5min 余量
    assert eod_watcher._is_eod_window(bf(13, 10), 15, 50) is False  # 余量已过


def test_eod_window_black_friday_not_after_real_close():
    """核心回归：老代码黑五 15:51 返回 True → 收盘后 2.5h 才开始挂卖单，
    当日到期仓位必过期。修完必须 False。"""
    assert eod_watcher._is_eod_window(_et(2026, 11, 27, 15, 51), 15, 50) is False


def test_eod_window_custom_cutoff_shifts_too():
    """env 语义保真：EOD_HOUR/EOD_MIN 表达的是"收盘前 N 分钟"，
    半日市按收盘差 3h 平移，而不是回退硬编码默认。"""
    # 配 15:30（收盘前 30min）→ 黑五等效 12:30
    assert eod_watcher._is_eod_window(_et(2026, 11, 27, 12, 31), 15, 30) is True
    assert eod_watcher._is_eod_window(_et(2026, 11, 27, 12, 29), 15, 30) is False


@pytest.mark.parametrize("hour,minute,when,why", [
    # 半日市前移后落到 0 点之前：老代码 max(h-3,0) 把窗口撑成 00:50~13:05
    (2, 50, (2026, 11, 27, 6, 0), "EOD_HOUR<shift 不得扩窗到 0 点"),
    (2, 50, (2026, 11, 27, 1, 0), "扩窗后连凌晨 1 点都会强平"),
    # replace() 会抛 ValueError 的越界配置（老代码只挡了 hour 侧）
    (15, 99, (2026, 11, 25, 15, 51), "EOD_MIN 越界"),
    (99, 50, (2026, 11, 25, 15, 51), "EOD_HOUR 越界"),
    # cutoff 晚于 16:05 上界 → 时窗为空、EOD 静默失效（现在至少有告警）
    (17, 0, (2026, 11, 25, 17, 30), "cutoff 晚于窗口上界"),
])
def test_eod_window_invalid_config_fails_closed(hour, minute, when, why):
    """无效配置一律关窗：不扩窗、也不抛异常（PR#2 review）。

    EOD 是唯一会主动清仓的 watcher——误配的代价是真下卖单，不是日志噪音。
    """
    eod_watcher._bad_window_warned.clear()
    assert eod_watcher._is_eod_window(_et(*when), hour, minute) is False, why
    eod_watcher._bad_window_warned.clear()


def test_eod_window_invalid_config_alerts_once_per_config():
    """本函数每 30s 被调一次，告警必须去重到"每种坏配置一条"。"""
    eod_watcher._bad_window_warned.clear()
    for _ in range(5):
        eod_watcher._is_eod_window(_et(2026, 11, 27, 6, 0), 2, 50)
    assert len(eod_watcher._bad_window_warned) == 1
    # 另一种坏配置各占一格，不会被前一条压掉
    eod_watcher._is_eod_window(_et(2026, 11, 25, 17, 30), 17, 0)
    assert len(eod_watcher._bad_window_warned) == 2
    eod_watcher._bad_window_warned.clear()


def test_eod_window_normal_day_unchanged():
    """普通交易日行为与既有测试逐字一致（黑五前一周的周三）。"""
    wed = lambda hh, mm: _et(2026, 11, 25, hh, mm)  # noqa: E731
    assert eod_watcher._is_eod_window(wed(12, 51), 15, 50) is False  # 半日市时点不误触发
    assert eod_watcher._is_eod_window(wed(15, 49), 15, 50) is False
    assert eod_watcher._is_eod_window(wed(15, 51), 15, 50) is True
    assert eod_watcher._is_eod_window(wed(16, 4), 15, 50) is True
    assert eod_watcher._is_eod_window(wed(16, 10), 15, 50) is False


def test_eod_window_2028_early_closes():
    # 2028 两个半日市同样前移（独立日前一天周一 + 黑五）
    assert eod_watcher._is_eod_window(_et(2028, 7, 3, 12, 55), 15, 50) is True
    assert eod_watcher._is_eod_window(_et(2028, 7, 3, 15, 55), 15, 50) is False
    assert eod_watcher._is_eod_window(_et(2028, 11, 24, 12, 55), 15, 50) is True
    assert eod_watcher._is_eod_window(_et(2028, 11, 24, 15, 55), 15, 50) is False


# ============================================================
# 4. quote：RTH / 上次收盘 / 新鲜度（2026-11-27 黑五）
# ============================================================

def test_is_rth_black_friday_closes_at_13():
    assert _is_rth(_utc(2026, 11, 27, 12, 30)) is True    # 半日市上午照常盘中
    assert _is_rth(_utc(2026, 11, 27, 13, 0)) is False    # 13:00 整点已收盘
    assert _is_rth(_utc(2026, 11, 27, 14, 0)) is False    # 老代码这里是 True（按整日算）
    assert _is_rth(_utc(2026, 11, 25, 14, 0)) is True     # 普通日 14:00 仍盘中


def test_last_rth_close_black_friday_13et():
    # 黑五收盘后（14:00 ET）→ 当天 13:00 ET
    close = _last_rth_close_utc(_utc(2026, 11, 27, 14, 0))
    assert close.astimezone(ET).date() == date(2026, 11, 27)
    assert close.astimezone(ET).time() == dt_time(13, 0)
    # 周六 → 仍是黑五 13:00
    close = _last_rth_close_utc(_utc(2026, 11, 28, 12, 0))
    assert close.astimezone(ET).time() == dt_time(13, 0)
    assert close.astimezone(ET).date() == date(2026, 11, 27)


def test_last_rth_close_walks_back_through_thanksgiving():
    # 黑五盘中（12:00，未收盘）→ 上次收盘要跳过感恩节假日到周三 16:00
    close = _last_rth_close_utc(_utc(2026, 11, 27, 12, 0))
    assert close.astimezone(ET).date() == date(2026, 11, 25)
    assert close.astimezone(ET).time() == dt_time(16, 0)


def test_last_rth_close_normal_day_unchanged():
    close = _last_rth_close_utc(_utc(2026, 11, 25, 18, 0))
    assert close.astimezone(ET).date() == date(2026, 11, 25)
    assert close.astimezone(ET).time() == dt_time(16, 0)


def test_freshness_black_friday_afternoon_no_false_delayed():
    """回归 7/22 假告警的半日市变体：黑五 14:30 ET 探测，报价停在 13:00
    收盘附近（age≈1.5h）。老代码按盘中处理 age>900s → 误报 DELAYED；
    现在按盘外 sanity check → OK。"""
    now = _utc(2026, 11, 27, 14, 30)
    gap = (now - _last_rth_close_utc(now)).total_seconds()
    status, msg = _freshness_verdict(gap + 300, now, "US.SPY261127C680000")
    assert status == QUOTE_OK
    assert "盘外" in msg


def test_freshness_black_friday_truly_stale_still_flagged():
    """半日市下真正过旧的数据（比 13:00 收盘还旧超 4h 容差）仍要告警。"""
    now = _utc(2026, 11, 27, 14, 30)
    gap = (now - _last_rth_close_utc(now)).total_seconds()
    status, _ = _freshness_verdict(gap + 5 * 3600, now, "US.SPY261127C680000")
    assert status == QUOTE_DELAYED
