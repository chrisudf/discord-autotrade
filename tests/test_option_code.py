"""build_option_code 格式回归（7/13 复盘）

moomoo 期权代码的 strike 段是 strike*1000 的**裸整数，无前导零填充**。
带填充的 code 被 moomoo 100% 拒（"Cannot find ... in US Stocks"）：
OSCR 30c(6/23)、TEM 65c(6/29)、RGTI 15p、NFLX 80c(7/13) 四笔实测全灭；
strike ≥ $100（天然 ≥6 位）的订单全部成功——:06d 填充此前只是没被小票踩到。
"""
from datetime import date

from autotrade.policy.pricing import build_option_code


def test_small_strike_no_zero_padding():
    # 7/13 实际被拒的两笔：曾生成 RGTI260821P015000 / NFLX260717C080000
    assert build_option_code("RGTI", date(2026, 8, 21), 15, "PUT") == "US.RGTI260821P15000"
    assert build_option_code("NFLX", date(2026, 7, 17), 80, "CALL") == "US.NFLX260717C80000"


def test_large_strike_format_unchanged():
    # 历史成功订单的格式不受影响
    assert build_option_code("MU", date(2026, 7, 15), 1050, "CALL") == "US.MU260715C1050000"
    assert build_option_code("DELL", date(2026, 7, 10), 480, "CALL") == "US.DELL260710C480000"
    assert build_option_code("HOOD", date(2026, 7, 2), 102, "CALL") == "US.HOOD260702C102000"


def test_fractional_strike():
    assert build_option_code("SOFI", date(2026, 9, 18), 2.5, "PUT") == "US.SOFI260918P2500"
    assert build_option_code("F", date(2026, 9, 18), 12.5, "CALL") == "US.F260918C12500"


def test_float_precision_uses_round_not_truncate():
    # 8.2 * 1000 = 8199.999...，int() 截断会得到 8199
    assert build_option_code("XYZ", date(2026, 1, 16), 8.2, "CALL") == "US.XYZ260116C8200"
