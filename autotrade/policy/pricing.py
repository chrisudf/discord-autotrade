"""统一定价内核（纯函数层：不 import broker/storage/listener/notify）。

来源（函数体逐字搬运，只改模块归属）：
- src/broker/moomoo_client.py 的 _get_slippage_pct / calc_limit_price /
  breakeven_exit_price / build_option_code
- src/listener/discord_client.py 的 SELL_SLIP / _calc_sell_limit
  （改名 calc_sell_limit，close_flow 引用新名）
"""
from datetime import date


def _get_slippage_pct(price: float) -> float:
    """
    分档 slippage：低价合约 spread 更宽，需要更大偏移才追得到 fill。

    分档（v1，待回测校准）：
    - < $1.5  → 12%
    - < $3.0  →  8%
    - >= $3.0 →  5%

    例子：
    - HOOD 1.10 → 1.10 * 1.12 = 1.23   (旧版 5% 只挂 1.16，6/17 漏吃 4-bagger)
    - NOW  3.90 → 3.90 * 1.05 = 4.09   (与旧版一致)
    - IREN 0.68 → 0.68 * 1.12 = 0.76
    """
    if price < 1.5:
        return 0.12
    elif price < 3.0:
        return 0.08
    else:
        return 0.05


def calc_limit_price(price: float) -> float:
    """
    计算挂单限价 = entry_price * (1 + slippage_pct)，2 位小数。

    公开导出：listener 风控前也要调它——Layer 2/3/4 的成本必须按
    实际挂单价算，否则 REAL $1000 硬顶会被 slippage 突破最多 12%。

    TODO: 加入 Penny Pilot tick 档位对齐
    - Penny Pilot (IREN/SPY/QQQ/HOOD 等)：tick = $0.01，当前 round(2) 已对齐
    - 非 Penny：< $3 → $0.05 / >= $3 → $0.10，当前会挂出非法价位
    暂不实现，等收集到非 Penny 拒单数据再做。
    """
    pct = _get_slippage_pct(price)
    return round(price * (1 + pct), 2)


def breakeven_exit_price(entry_price: float, sell_slip: float = 0.05) -> tuple[float, float]:
    """计算"我们跟单不亏"所需的最低 KC 卖出价（毛 PnL %）。

    我们买入 ≈ entry × (1 + buy_slip)
    我们卖出 ≈ KC_exit × (1 - sell_slip)
    净 PnL = 0 → KC_exit = entry × (1 + buy_slip) / (1 - sell_slip)

    返回 (breakeven_price, breakeven_gross_pct)
    例: entry=2.70（$1.5-$3 档，buy_slip=8%）→ (3.07, +13.7%)

    实战价值：开单时 TG 提示 KC 至少要 +N% 退出我们才不亏，
    用户对照 KC 历史 trim 阈值（通常 +50%/+100%）能直观判断信号好坏。
    """
    if entry_price is None or entry_price <= 0:
        return 0.0, 0.0
    buy = _get_slippage_pct(entry_price)
    be_price = entry_price * (1 + buy) / (1 - sell_slip)
    be_pct = (be_price / entry_price - 1) * 100
    return round(be_price, 2), round(be_pct, 1)


def build_option_code(symbol: str, exp_date: date, strike: float, side: str) -> str:
    """
    构造 moomoo 期权代码

    格式：US.{SYMBOL}{YYMMDD}{C/P}{STRIKE*1000}
    例子：IREN 60C exp 2026-06-15 → US.IREN260615C60000

    注意：
    - strike * 1000 是因为 moomoo 用千分之一美元为单位
    - strike 段是**裸整数，不做前导零填充**：
      strike=2.5  → 2500
      strike=60   → 60000
      strike=400  → 400000
      7/13 复盘实锤：带前导零的 code 被 moomoo 100% 拒（"Cannot find ... in
      US Stocks"）——OSCR 30c(6/23)、TEM 65c(6/29)、RGTI 15p、NFLX 80c(7/13)
      四笔全灭；strike ≥ $100（天然 ≥6 位）的全部成功。此前 :06d 填充只是
      恰好没被大票踩到。
    - round 而非 int 截断：浮点误差下 int() 可能把 x999.9999 截成 x999
    """
    date_str = exp_date.strftime("%y%m%d")
    cp = "C" if side == "CALL" else "P"
    strike_str = str(round(strike * 1000))
    return f"US.{symbol}{date_str}{cp}{strike_str}"


# 卖单挂价相对参照价的负偏移 —— 偏移要够"吃"穿 bid，避免挂在 ask 上没人接。
# [0010] 无价 CLOSE 的实时参照已落地：close_flow 经 broker.quote.
# get_sell_ref_price 取 bid（优先）/last 传入 quote_ref。
# TODO（实测调整）：
#  - 分档：trim (pct<100) 用浅偏移、close (pct=100) 用深偏移
#  - SL/EOD 触发时用更激进偏移
SELL_SLIP = 0.05


def calc_sell_limit(
    avg_entry: float, signal_price: float = None, quote_ref: float = None,
) -> "float | None":
    """卖出限价。

    优先级：
      1. signal_price 存在 → 用 KC 喊的价 × (1 - SELL_SLIP)（现行为不变）
      2. quote_ref 存在（[0010] CLOSE 无价时调用方取实时 bid/last，
         见 broker.quote.get_sell_ref_price）→ quote_ref × (1 - SELL_SLIP)
      3. 都无 → 返回 None，**拒绝执行**

    设计原则（"宁错过不错杀"）：
    没有可靠价参照（信号没喊价 + 实时报价不可得/stale）就不卖。
    用 avg_entry 当兜底参照会**锁定 -5% 亏损**——曾在 TSLA 案例上踩坑；
    avg_entry 参数保留只为签名兼容，永不作为参照价。

    纯函数：不做 I/O。quote_ref 的获取与 60s 新鲜度门在 broker.quote 侧，
    是否启用 fallback 的开关（CLOSE_QUOTE_FALLBACK）在 close_flow 侧。
    """
    if signal_price is not None:
        return round(signal_price * (1 - SELL_SLIP), 2)
    if quote_ref is not None and quote_ref > 0:
        return round(quote_ref * (1 - SELL_SLIP), 2)
    return None
