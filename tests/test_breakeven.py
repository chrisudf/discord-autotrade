"""broker.breakeven_exit_price 单元测试

公式：be_price = entry × (1 + buy_slip) / (1 - sell_slip)
buy_slip 三档：<$1.5→12% / <$3→8% / ≥$3→5%
sell_slip 固定 5%
"""
import pytest
from autotrade.policy.pricing import breakeven_exit_price


def test_breakeven_mid_range():
    """entry=2.70（$1.5-$3 档 → buy 8%）"""
    be_price, be_pct = breakeven_exit_price(2.70)
    # 2.70 * 1.08 / 0.95 = 3.07
    assert be_price == pytest.approx(3.07, abs=0.01)
    # gross +13.7%
    assert be_pct == pytest.approx(13.7, abs=0.2)


def test_breakeven_low_price():
    """entry=1.10（<$1.5 → buy 12%）"""
    be_price, be_pct = breakeven_exit_price(1.10)
    # 1.10 * 1.12 / 0.95 = 1.297
    assert be_price == pytest.approx(1.30, abs=0.01)
    assert be_pct == pytest.approx(17.9, abs=0.5)


def test_breakeven_high_price():
    """entry=5.00（≥$3 → buy 5%）"""
    be_price, be_pct = breakeven_exit_price(5.00)
    # 5.00 * 1.05 / 0.95 = 5.526
    assert be_price == pytest.approx(5.53, abs=0.01)
    assert be_pct == pytest.approx(10.5, abs=0.3)


def test_breakeven_zero_entry():
    """边界：entry=0 不该崩溃"""
    be_price, be_pct = breakeven_exit_price(0)
    assert be_price == 0.0
    assert be_pct == 0.0


def test_breakeven_none():
    """边界：entry=None"""
    be_price, be_pct = breakeven_exit_price(None)
    assert be_price == 0.0
    assert be_pct == 0.0
