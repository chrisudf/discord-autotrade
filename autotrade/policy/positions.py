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

    [0011] 策略 B 已落地（ship-dark，STRATEGY_B 默认关）：见下方
    strategy_b_decision + close_flow 的 runner-preserve 分支。本函数语义不变——
    返回 0 只表示"runner-preserve 拦下了"，要不要升级为策略 B 全出由调用方
    （close_flow）根据实时报价决定。
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


def strategy_b_decision(
    avg_entry: float,
    quote_ref: "float | None",
    kc_pnl_pct: "float | None",
    min_pnl_pct: float,
) -> tuple[str, str]:
    """[0011] 策略 B：runner-preserve 拦下的 trim，按**我方**实时浮盈决定全出或续拿。

    Args:
        avg_entry:    我方成交均价（positions_db.avg_entry_price）
        quote_ref:    实时卖出参照价（broker.quote.get_sell_ref_price：bid 优先/
                      last 兜底/60s 新鲜度门；拿不到为 None）。取价 I/O 在调用方。
        kc_pnl_pct:   KC 消息里自报的 ±N%（close_parser.signal_pnl_pct），可 None
        min_pnl_pct:  我方浮盈达标阈值（整数百分比语义，25 = +25%）

    Returns:
        ("SELL_ALL" | "PRESERVE", reason)。纯函数，不做 I/O、不读 env。

    规则 v1（契约已定）：
        our_pnl = (quote_ref - avg_entry) / avg_entry * 100
        our_pnl >= min_pnl_pct → SELL_ALL；否则 PRESERVE。
        quote_ref None（无新鲜报价）→ PRESERVE：拿不到可靠参照就维持策略 A
        的"死拿"底线——与 CLOSE 无价拒卖同一哲学（宁错过不错杀），
        绝不按盲猜的浮盈卖 runner。avg_entry 异常（None/<=0）同理 PRESERVE。

    kc_pnl_pct **只进 reason 文案、不进判断**——这是显式推迟的设计决策，
    不是遗漏：KC 的进场价和我们的实际成交价隔着买入 slippage（12/8/5% 分档）
    加 1-3s 的时间差，他喊 +20% 时我们可能只有 +8%（7/25 夜 "Trimmed AVGO
    +20%" 型消息是本功能的直接动机）。两侧口径怎么换算对齐，等 SIMULATE
    实测数据说话再调；v1 先用我方口径独立判断，KC 数字仅供半夜看 TG 时参考。

    单张仓"最小单位"问题的正面回答：runner-preserve 只在 remaining==1 时触发，
    而 1 张合约数学上不存在"卖 33%"——qty==1 时 SELL_ALL 是唯一可行的响应动作。
    这正是策略 A 与策略 B 的分野：
      策略 A（默认，STRATEGY_B=false）：死拿，只认 100% 全平信号
        （6/30 SPY 748c、7/1 MSFT 390c 提前平掉合计放弃 ~$330+/合约的教训）；
      策略 B：KC 喊 trim 且我方浮盈已达标 → 借势全出锁利
        （AVGO 415c 7/23-24 夜从 +50% 拿到过期归零——死拿的反面教材）。
    与 TP 阶梯的边界：策略 B 只在"KC 喊 trim 且被 runner-preserve 拦下"这一个
    路口介入；tp_watcher 的 LADDER 照常独立运行、两者互不感知，同一合约的
    卖出互斥由 option_code 级 sell_lock 保证（close_flow 在锁内决策+下单）。
    """
    kc_txt = (
        f"KC 自报 {kc_pnl_pct:+g}%" if kc_pnl_pct is not None else "KC 未报盈亏"
    )
    if quote_ref is None:
        return "PRESERVE", f"无新鲜报价参照（{kc_txt}），维持策略 A 死拿"
    if avg_entry is None or avg_entry <= 0:
        return "PRESERVE", f"avg_entry 异常（{avg_entry}），无法计算我方浮盈"
    our_pnl = (quote_ref - avg_entry) / avg_entry * 100
    if our_pnl >= min_pnl_pct:
        return (
            "SELL_ALL",
            f"我方浮盈 {our_pnl:+.1f}% ≥ 阈值 {min_pnl_pct:g}%（{kc_txt}）",
        )
    return (
        "PRESERVE",
        f"我方浮盈 {our_pnl:+.1f}% < 阈值 {min_pnl_pct:g}%（{kc_txt}）",
    )


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
