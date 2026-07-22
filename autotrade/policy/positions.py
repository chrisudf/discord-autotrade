"""仓位策略（纯函数层：不 import broker/storage/listener/notify）。

来源（函数体/常量逐字搬运，只改模块归属）：
- src/storage/positions_db.py 的 categorize()（原模块 re-export 兼容旧调用点）
- src/position/manager.py 的 calc_qty_to_sell()（原模块 re-export）
- src/position/tp_watcher.py 的 LADDER（tier_bit 位掩码：T1=1 / T2=2 / T3=4，
  位掩码值内嵌在 LADDER 各档三元组的第三个元素，无独立命名常量）
"""
import math
from datetime import date

from autotrade.utils.logger import logger


# ============ 类目判定 ============

def categorize(
    expiry_d: date, today_et: date, tags: list[str]
) -> tuple[str, bool, bool]:
    """根据 DTE + tags 决定 category / apply_sl / eod_force_close。

    Args:
        expiry_d: 实际下单的到期日（已过假日调整）
        today_et: 当前 ET 日期
        tags: 信号 tags（lotto / swing / scalp 等）

    Returns:
        (category, apply_sl, eod_force_close)

    决策矩阵：
        DTE  | lotto | category    | apply_sl | eod_force
        -----+-------+-------------+----------+----------
        0    | no    | 0dte        | False    | True
        0    | yes   | 0dte_lotto  | False    | True   ← 必过期，强平
        1-7  | no    | weekly      | True     | False
        1-7  | yes   | lotto       | False    | False  ← 彩票放飞
        8+   | any   | swing       | False    | False

    TODO: lotto 实测后看要不要加 max_loss_pct（比如 -80% 硬底）
    TODO: swing 8-20 是否细分，独立测一段时间数据
    """
    dte = (expiry_d - today_et).days
    if dte < 0:
        logger.warning(f"[positions] negative DTE {dte}, treat as 0dte")
        dte = 0

    is_lotto = "lotto" in tags
    eod_force = (dte == 0)

    if dte == 0:
        return ("0dte_lotto" if is_lotto else "0dte"), False, eod_force
    if is_lotto:
        return "lotto", False, False
    if dte <= 7:
        return "weekly", True, False
    return "swing", False, False


def calc_qty_to_sell(position: dict, pct: int) -> int:
    """根据 close 信号给的 % 算实际卖出张数。

    规则（v2, 2026-07-02 开始）：
    - pct >= 100 → 全平剩余（"closed all" / "out full" / KC 明确清仓）
    - remaining == 1 且 pct < 100 → **不卖，保留 runner**
      理由：跟单单张持仓时，任何 <100% 的 trim 数学上都会被 math.ceil 拉到 1，
      即等于全平。历史损失：
        - 6/30 SPY 748c：$2.59 → 我们 33% 平在 $2.71，KC 后续 4.00 (+40%)
        - 7/1 MSFT 390c：$2.48 → 我们 33% 平在 $2.56，KC 后续 4.60 (+100%)
      合计放弃 ~$330+/合约。选项 A（保留 runner）优于全平退出。
      100% 明确清仓仍然会正常执行——不影响真反转信号。
    - remaining > 1 → 向上取整（math.ceil 而非 round，round 是 banker's rounding，
      remaining=5/pct=50 会误算 2 而非 3）

    TODO（等 OPRA 权限）：升级到策略 B —— 用报价判断"我们已经到 +X%"再选择性
    响应 trim 信号（早期跟单，中后期变 runner）。见 docs/TODO.md 中 P0。
    """
    remaining = position["qty_remaining"]
    if pct >= 100:
        return remaining
    if remaining == 1:
        # 策略 A：保留 runner，等真正的 100% close 信号
        logger.info(
            f"[calc_qty_to_sell] runner-preserve: remaining=1 pct={pct}, "
            f"skipping trim (option={position.get('option_code', '?')})"
        )
        return 0
    qty = max(1, math.ceil(remaining * pct / 100))
    return min(qty, remaining)


# Ladder 定义：(threshold_pct, trim_pct_of_remaining, tier_bit)
# 注意 trim_pct 是相对"当时剩余"，所以 T1 卖 50% → 剩 50%；T2 再卖 50% → 剩 25%
LADDER = {
    "weekly": [
        (0.50, 50, 1),    # T1: +50% → 卖剩余的 50%
        (1.00, 50, 2),    # T2: +100% → 卖剩余的 50%
    ],
    "swing": [
        (1.00, 50, 1),    # T1: +100% → 卖剩余的 50%
        (2.00, 50, 2),    # T2: +200% → 卖剩余的 50%
    ],
    # 0dte / lotto / 0dte_lotto 不在表里 = 不挂 TP
}
