"""仓位策略（纯函数层：不 import broker/storage/listener/notify）。

来源（函数体/常量逐字搬运，只改模块归属）：
- src/storage/positions_db.py 的 categorize()（原模块 re-export 兼容旧调用点）
- src/position/manager.py 的 calc_qty_to_sell()（原模块 re-export）
- src/position/tp_watcher.py 的 LADDER（tier_bit 位掩码：T1=1 / T2=2 / T3=4，
  位掩码值内嵌在 LADDER 各档三元组的第三个元素，无独立命名常量）
"""
import math
from datetime import date

from autotrade.utils.envcfg import env_int
from autotrade.utils.logger import logger


# ============ 类目判定 ============

# weekly 的 DTE 上界。**这是本仓库唯一决定"这笔仓位有没有止损"的数字。**
#
# [8/22 实锤 -$976] ASTS 80C 和 AMD 520C 都是 8/13 开仓、8/21 到期，
# **DTE = 8** —— 比原来的上界 7 多一天，于是归到 swing、apply_sl=False，
# 接下来八天里没有止损、没有 TP 触发、没有喊单员的离场信号，一路走到归零
# （ASTS -$436 卖在 0.01，AMD -$540 连报价都取不到直接过期）。
# 这两笔加起来超过那一周其余所有盈亏之和，而亏的原因不是看错方向，
# 是**没有任何规则去管它们**。
#
# 一个 8 天后到期的合约不是 swing，是"提前一天买的 weekly"。上界抬到 10 天，
# 让"下周五到期"这一整类回到 50% 止损的保护范围内
# （周一开、下周五到期 = DTE 11 仍算 swing；周三开、下周五到期 = DTE 9 算 weekly）。
#
# ---------------------------------------------------------------------------
# **10 是拍板值，不是回测值**（2026-08-25 确认保持）。它解决的是 8/22 那两笔
# 「差一天掉出保护范围」的具体事故，**没有**任何数据说明 10 比 9 或 12 更优。
# 换句话说：这个数字现在的依据是一个样本，n=2。
#
# 什么时候该回来改它 —— 见 ROADMAP P2「WEEKLY_MAX_DTE 的取值要回测」：
#   1. 攒够两三个月的 `position_events`，按开仓时 DTE 分桶统计
#      「触发过 SL 的比例 / SL 触发后到期时的价格」，看 8-14 天这一段
#      到底是"被止损救了"还是"被止损割在地板上"；
#   2. 如果 8-14 天这段的 SL 大多是割在低点（喊单员的 weekly 常常先跌后拉），
#      那正确的修法不是调这个数字，而是给这一段**单独一条更宽的止损线**
#      （现在 weekly 与它共用 50%）；
#   3. 真正的 swing（DTE 30+，如 TSLA 380C / AVGO 450C）**依旧完全裸奔**，
#      那是 max_loss_pct 硬底的事，与本常量无关，别混在一起改。
#
# 在上面第 1 步的数据出来之前，**不要凭手感调这个值**。改它等于改风控口径。
# 需要临时试验用 env `WEEKLY_MAX_DTE=` 覆盖，别改默认值。
# ---------------------------------------------------------------------------
WEEKLY_MAX_DTE = env_int("WEEKLY_MAX_DTE", 10, minimum=0)

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

    决策矩阵（eod_force 另见下方 day_trade 覆盖）：
        DTE            | lotto | category    | apply_sl | eod_force
        ---------------+-------+-------------+----------+----------
        0              | no    | 0dte        | False    | True
        0              | yes   | 0dte_lotto  | False    | True   ← 必过期，强平
        1..WEEKLY_MAX  | no    | weekly      | True     | False
        1..WEEKLY_MAX  | yes   | lotto       | False    | False  ← 彩票放飞
        WEEKLY_MAX+1.. | any   | swing       | False    | False

        WEEKLY_MAX_DTE 默认 10（8/22 之前是硬写的 7，见该常量注释里的 $976）。

    TODO: lotto 实测后看要不要加 max_loss_pct（比如 -80% 硬底）
    TODO: swing 仍然完全没有下行保护。抬 WEEKLY_MAX_DTE 只覆盖了"临近到期"
          那一段；真正的 swing（DTE 30+，如 TSLA 380C / AVGO 450C）依旧裸奔。
          max_loss_pct 硬底需要单独回测。
    """
    dte = (expiry_d - today_et).days
    if dte < 0:
        logger.warning(f"[positions] negative DTE {dte}, treat as 0dte")
        dte = 0

    is_lotto = "lotto" in tags
    # day_trade：信号原文明说当日了结（8/3 KC "AMZN 275p 4DTE @ 1.65 day trade"）。
    # 这个 tag 一直被解析、落库、打进 TG，却只在 guards 里当 DTE 上限用——EOD
    # 完全不看它。8/3 实测后果：该单按 DTE=4 归 weekly、eod_force=False，
    # 同晚 KC "-15% 离场" 的喊话又漏接（见 close_parser._OUT_BARE_SYM_PATTERN），
    # 一笔"日内"仓位就这么过夜了。
    # 与 0DTE 同等对待：收盘前强平，不赌隔夜。只翻 eod_force——
    # category / apply_sl 及其余风控路径逐字不变（weekly 仍吃 50% 止损）。
    eod_force = (dte == 0) or ("day_trade" in tags)

    # 三个非 0DTE 分支原本硬写 False（当时 eod_force 只可能来自 dte==0，
    # 写死与传变量等价）。现在 day_trade 也能置位，必须一律回传 eod_force——
    # 否则 8/3 那笔 weekly day_trade 仍然过夜，改了等于没改。
    if dte == 0:
        return ("0dte_lotto" if is_lotto else "0dte"), False, eod_force
    if is_lotto:
        return "lotto", False, eod_force
    if dte <= WEEKLY_MAX_DTE:
        return "weekly", True, eod_force
    return "swing", False, eod_force


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


# ============ SL 棘轮：已落袋的档位把止损底抬上来 ============
#
# [9/1 实锤] COIN 190C 9/4，entry 2.08 两张。02:42 T1 命中卖 1 张 @2.99；
# 03:12 T2 命中（last ≥ 4.16）但剩 1 张、trim 50% 取整为 0 → runner-preserve
# 保留不卖，日志写着"后续交给 SL / EOD / 喊单员平仓信号"。那一夜这三条腿
# 全是虚的：eod_force_close=0，喊单员的 "Down to 1/2" 没被解析出来（同批已修），
# 而 **SL 仍然锚在 entry 上**——STOP_LOSS_PCT=50% → 触发价 1.04。
#
# 也就是说：一张已经摸到 4.16 的合约，要跌回 1.04 系统才会动手。已经确认
# 到手的 +100% 可以全部还回去，而且**没有任何一条日志会说这件事正在发生**。
#
# 这里补的是最小的一层：SL 的锚从"入场价"改成"已触发档位的上一级"——
# 回吐最多一级，不做逐 tick 的移动止损（那是另一个需要报价历史的东西）。
#   T1 命中（weekly +50%）→ 底 = 保本价（真·零风险，含卖出滑点）
#   T2 命中（weekly +100%）→ 底 = entry × 1.50 / (1-slip)，锁住 +50%
#
# 为什么除以 (1 - sell_slip)：阈值是**触发价**，真正的成交价是 last×(1-slip)
# （SL 默认 8%）。不折算的话"保本止损"实际会亏掉一个滑点，那就不叫保本。
# 方向上偏保守（早触发一点），对止盈保护是对的。
#
# **作用域限于已经在 SL watch 名单里的仓位**（apply_sl=True + lotto 硬底档）。
# swing 类目 apply_sl=False，压根不进 watcher —— 它们的 runner 命中 T1/T2
# 之后同样在裸奔，但"给 swing 上止损"是改变一整个类目有没有止损，属钱路
# 决定，要单独拍板，不在本次修改范围（见 ROADMAP P1 §16）。
SL_RATCHET_ENV = "SL_RATCHET_AFTER_TP"


def sl_ratchet_enabled() -> bool:
    """1 = 开（缺省）。出事时一个 env 关掉，行为退回"锚在 entry"。"""
    return env_int(SL_RATCHET_ENV, 1, minimum=0) > 0


def sl_ratchet_floor(category: str, tp_hits: int, avg_entry: float,
                     sell_slip: float) -> "tuple[float, str] | tuple[None, str]":
    """已触发的最高 TP 档位对应的止损底价。

    Args:
        category:   仓位类目（决定用哪张 LADDER）
        tp_hits:    positions.tp_hits 位掩码（T1=1 / T2=2 / T3=4）
        avg_entry:  我方实际成交均价
        sell_slip:  SL 的卖出限价下偏移（用于把触发价折算成净额）

    Returns:
        (floor_price, 说明串)；无阶梯 / 一档未中 / 参数异常 → (None, "")

    纯函数：不读 env、不碰 DB —— 开关与"取更高者"的决定留在 sl_watcher。
    """
    ladder = LADDER.get(category) or []
    if not ladder or not tp_hits or not avg_entry or avg_entry <= 0:
        return None, ""
    if not 0 <= sell_slip < 1:
        return None, ""

    # 已命中的最高档在 ladder 里的下标；上一级的 threshold 就是要锁住的收益。
    # 逐个比对位掩码而不是按 popcount：熔断/跳档的历史下 tp_hits 未必连续。
    hit_idx = -1
    for i, (_threshold, _trim, tier_bit) in enumerate(ladder):
        if tp_hits & tier_bit:
            hit_idx = i
    if hit_idx < 0:
        return None, ""

    locked_pct = ladder[hit_idx - 1][0] if hit_idx > 0 else 0.0
    floor = round(avg_entry * (1 + locked_pct) / (1 - sell_slip), 2)
    tier_no = hit_idx + 1
    desc = "保本" if locked_pct == 0 else f"锁 +{int(locked_pct * 100)}%"
    return floor, f"T{tier_no} 已触发 → 棘轮底 {desc}"
