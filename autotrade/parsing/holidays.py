"""US Equity Options Market Holidays.

Hardcoded set 维护，每年 12 月更新下一年（提醒：2026-12 需追加 2028）。

数据来源：NYSE / Cboe 官方日历。
注意 observed 规则：节日落周末 → 前移周五或后移周一。

下次更新前置条件：
  - 2027-01 之前必须确认 2027 假日表（已预填）
  - 2027-12 之前必须追加 2028
"""
from datetime import date, timedelta

# ===== 2026 =====
# https://www.nyse.com/markets/hours-calendars
US_OPTION_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth ⚠️ 本次 QCOM/IREN 踩坑日
    date(2026, 7, 3),    # Independence Day (observed, 7/4 周六)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

# ===== 2027 =====
US_OPTION_HOLIDAYS_2027 = {
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Presidents Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed, 6/19 周六)
    date(2027, 7, 5),    # Independence Day (observed, 7/4 周日)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed, 12/25 周六)
}

US_OPTION_HOLIDAYS = US_OPTION_HOLIDAYS_2026 | US_OPTION_HOLIDAYS_2027


def is_trading_day(d: date) -> bool:
    """是否为美股期权交易日（非周末且非假日）。"""
    if d.weekday() >= 5:   # 5=Sat, 6=Sun
        return False
    if d in US_OPTION_HOLIDAYS:
        return False
    return True


def adjust_to_trading_day(d: date, direction: str = "backward") -> date:
    """把日期调整到最近的交易日。

    Args:
        d: 候选日期
        direction: "backward" 往前找（默认，期权到期日通用规则）
                   "forward"  往后找
    Returns:
        最近的交易日 date

    Safety: 最多迭代 10 次，避免假日表错误造成死循环。
    """
    if is_trading_day(d):
        return d

    step = -1 if direction == "backward" else 1
    current = d
    for _ in range(10):
        current = current + timedelta(days=step)
        if is_trading_day(current):
            return current

    # 理论上不可达（连续 10 个非交易日不可能）
    raise RuntimeError(
        f"adjust_to_trading_day: no trading day found within 10 days "
        f"of {d} (direction={direction}). 假日表可能有误。"
    )