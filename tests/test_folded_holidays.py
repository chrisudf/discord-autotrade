"""验证假日表 + adjust_to_trading_day 行为。

（原 scripts/test_holidays.py 搁浅 assert 脚本，折叠为 pytest；期望值原样保留。）
"""
from datetime import date

from autotrade.parsing.holidays import is_trading_day, adjust_to_trading_day


def test_juneteenth_2026():
    # Juneteenth 2026
    assert not is_trading_day(date(2026, 6, 19)), "6/19/26 Juneteenth 应非交易日"
    assert adjust_to_trading_day(date(2026, 6, 19)) == date(2026, 6, 18), \
        "6/19 → 应前移到 6/18 周四"


def test_weekend_chains_through_holiday():
    # 周末
    assert not is_trading_day(date(2026, 6, 20)), "周六应非交易日"
    assert adjust_to_trading_day(date(2026, 6, 20)) == date(2026, 6, 19) or \
           adjust_to_trading_day(date(2026, 6, 20)) == date(2026, 6, 18), \
        "周六 6/20 → 应找前一交易日"
    # 注意 6/20 周六 backward → 6/19 (Juneteenth) → 再 backward → 6/18
    assert adjust_to_trading_day(date(2026, 6, 20)) == date(2026, 6, 18), \
        "6/20 周六 backward 应连跳到 6/18（6/19 是假日）"


def test_good_friday_2026():
    # Good Friday 2026
    assert not is_trading_day(date(2026, 4, 3))
    assert adjust_to_trading_day(date(2026, 4, 3)) == date(2026, 4, 2)


def test_regular_trading_day_unchanged():
    # 普通交易日
    assert is_trading_day(date(2026, 6, 18))
    assert is_trading_day(date(2026, 6, 22))  # Monday
    assert adjust_to_trading_day(date(2026, 6, 18)) == date(2026, 6, 18)


def test_juneteenth_2027_observed():
    # 2027 Juneteenth observed
    assert not is_trading_day(date(2027, 6, 18))
    assert adjust_to_trading_day(date(2027, 6, 18)) == date(2027, 6, 17)
