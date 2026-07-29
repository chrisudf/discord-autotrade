"""US Equity Options Market Holidays + Early Close（半日市）.

Hardcoded set 维护，每年 12 月更新下一年。

数据来源：NYSE / Cboe 官方日历。
注意 observed 规则：节日落周末 → 前移周五或后移周一。

下次更新前置条件：
  - 2028-12 之前必须追加 2029（假日表 + EARLY_CLOSE_DATES 两个都要，
    并对照 NYSE 官方日历逐条核对 observed 平移）
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

# ===== 2028 =====
# [0017] 按 NYSE 官方节日规则推导（https://www.nyse.com/markets/hours-calendars），
# 每个日期注明规则来源，2027-12 官方表出齐后逐条对照即可。
US_OPTION_HOLIDAYS_2028 = {
    # ⚠️ New Year's Day 2028-01-01 落周六，本表**没有**元旦条目——
    # NYSE Rule 7.2：节日落周六通常前移到周五休市，但该周五若是月末/年末
    # 结算日则照常开市。2027-12-31（周五）正是年末结算日 → 2028 元旦不观察。
    # 先例：2022-01-01 同为周六，2021-12-31 照常交易，2022 全年无元旦假日。
    # 若误把 2027-12-31 当假日：年末最后一个交易日的 0DTE/到期仓位会被
    # eod_watcher 视为"非交易日"跳过强平 → 直接过期（AVGO 415C 7/24 的结局）。
    date(2028, 1, 17),   # MLK Day（1 月第 3 个周一）
    date(2028, 2, 21),   # Presidents Day（2 月第 3 个周一）
    date(2028, 4, 14),   # Good Friday（2028 复活节 4/16 前的周五，computus 推算已复核）
    date(2028, 5, 29),   # Memorial Day（5 月最后一个周一）
    date(2028, 6, 19),   # Juneteenth（恰为周一，无 observed 平移；2026 同名踩坑日）
    date(2028, 7, 4),    # Independence Day（周二，无平移；前一天 7/3 周一为半日市）
    date(2028, 9, 4),    # Labor Day（9 月第 1 个周一）
    date(2028, 11, 23),  # Thanksgiving（11 月第 4 个周四；次日 11/24 黑五半日市）
    date(2028, 12, 25),  # Christmas（周一，无平移；12/24 落周日 → 无平安夜半日市）
}

US_OPTION_HOLIDAYS = (
    US_OPTION_HOLIDAYS_2026 | US_OPTION_HOLIDAYS_2027 | US_OPTION_HOLIDAYS_2028
)


# ===== 半日市（13:00 ET 提前收盘）=====
# [0017] 兑现 eod_watcher._is_eod_window 的 TODO。不修的后果是钱路事故：
# 半日市当天 eod watcher 15:50 才进窗 = 收盘后 2.5h 才开始挂卖单，
# 当日到期仓位 100% 过期——AVGO 415C（7/23-24 夜）从 +50% 拿到归零的
# 同款结局，只是触发原因从"窗口内无报价"换成"整个窗口都错过"。
#
# NYSE 半日市规则（逐年对照官方日历核对）：
#   1. 独立日前一天（7/3）为交易日时 → 13:00 收盘。
#      7/3 本身是 observed 假日（2026：7/4 周六前移）或周末（2027：7/3 周六）
#      时**无**半日市——先例 2020/2021 同构，7/2 均为全日交易。
#   2. 感恩节次日（黑五）恒为半日市（感恩节定义为周四，次日必为周五交易日）。
#   3. 平安夜 12/24 为交易日时 → 13:00 收盘。12/24 落周末（2028：周日）或
#      本身是 observed 假日（2027：12/25 周六前移到 12/24）时**无**半日市
#      ——先例 2016/2021/2022 同构，12/23 均为全日交易。
EARLY_CLOSE_DATES = {
    # --- 2026 ---
    date(2026, 11, 27),  # 黑五（感恩节 11/26 周四次日）
    date(2026, 12, 24),  # 平安夜（周四；12/25 周五为假日）
    # 2026 无 7 月半日市：7/4 周六 → 7/3 周五本身就是 observed 假日
    # --- 2027 ---
    date(2027, 11, 26),  # 黑五（感恩节 11/25 周四次日）
    # 2027 无 7 月半日市：7/3 落周六；无平安夜半日市：12/24 周五本身是
    # observed 假日（12/25 周六前移）
    # --- 2028 ---
    date(2028, 7, 3),    # 独立日前一天（周一；7/4 周二为假日）
    date(2028, 11, 24),  # 黑五（感恩节 11/23 周四次日）
    # 2028 无平安夜半日市：12/24 落周日
}


def is_early_close(d: date) -> bool:
    """是否为半日市（13:00 ET 提前收盘）。

    注意：半日市**是**交易日（is_trading_day 返回 True），只是收盘从
    16:00 提前到 13:00。消费方各自调整时点：
    - eod_watcher：强平窗口整体前移 3h（12:50 ~ 13:05）
    - broker.quote：_is_rth / _last_rth_close_utc 按 13:00 收盘计算
    """
    return d in EARLY_CLOSE_DATES


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