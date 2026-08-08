# ============ 格式化辅助函数 ============
# 注意：所有外部输入（来自 Discord / broker / 异常文本）都必须走 escape_md，
# 否则碰到 _ * ` 之类字符会 400，虽然有 fallback 但格式会丢。
from typing import Optional

from autotrade.notify.transport import escape_md


def format_signal_alert(channel_name: str, symbol: str, strike: float,
                        expiry: str, side: str, price: float, qty: int,
                        action: str = "OPEN",
                        breakeven: Optional[tuple] = None,
                        tags: Optional[list] = None) -> str:
    """格式化信号触发通知。

    breakeven: (price, gross_pct_needed) —— 来自 broker.breakeven_exit_price
    tags: parser 抽出的标签列表 ['lotto', 'swing', 'scalp', 'day_trade']
    """
    emoji = "🟢" if side.upper() == "C" else "🔴"
    cost = price * 100 * qty
    msg = (
        f"{emoji} *新信号触发*\n"
        f"频道: `{escape_md(channel_name)}`\n"
        f"标的: *{escape_md(symbol)}* {escape_md(strike)}{escape_md(side.upper())} {escape_md(expiry)}\n"
        f"动作: {escape_md(action)}\n"
        f"价格: ${escape_md(f'{price}')}\n"
        f"数量: {qty} 张\n"
        f"成本: ${escape_md(f'{cost:.0f}')}"
    )
    if tags:
        tag_str = " ".join(f"`{escape_md(t)}`" for t in tags)
        msg += f"\n🏷️ {tag_str}"
    if breakeven:
        be_price, be_pct = breakeven
        msg += (
            f"\n📐 盈亏平衡: KC ≥ *${escape_md(f'{be_price}')}* "
            f"\\(gross \\+{escape_md(f'{be_pct:.1f}')}%\\)"
        )
    return msg


def format_order_filled(symbol: str, strike: float, side: str, expiry: str,
                        fill_price: float, qty: int, order_id: str) -> str:
    """格式化下单成功通知"""
    return (
        f"✅ *下单成功*\n"
        f"标的: *{escape_md(symbol)}* {escape_md(strike)}{escape_md(side.upper())} {escape_md(expiry)}\n"
        f"成交价: ${escape_md(f'{fill_price}')}\n"
        f"数量: {qty} 张\n"
        f"订单号: `{escape_md(order_id)}`"
    )


def format_risk_blocked(reason: str, detail: str = "") -> str:
    """格式化风控拦截通知"""
    msg = f"⚠️ *风控拦截*\n原因: {escape_md(reason)}"
    if detail:
        msg += f"\n详情: {escape_md(detail)}"
    return msg


def format_error(scope: str, error: str) -> str:
    """格式化错误通知

    长 traceback 截到 500 字符，完整 stack 通过 logger.exception 落地。
    """
    truncated = (error or "")[:500]
    return (
        f"❌ *系统错误*\n"
        f"模块: `{escape_md(scope)}`\n"
        f"错误: ```\n{escape_md(truncated)}\n```"
    )


def format_close_filled(symbol: str, strike: float, side: str, expiry: str,
                        qty_sold: int, fill_price: float, pct: int,
                        trigger: str, order_id: str) -> str:
    """卖单成交通知"""
    return (
        f"💰 *平仓成交*\n"
        f"标的: *{escape_md(symbol)}* {escape_md(strike)}{escape_md(side.upper())} {escape_md(expiry)}\n"
        f"卖出: {qty_sold} 张 @ ${escape_md(f'{fill_price}')} \\({pct}%\\)\n"
        f"触发: `{escape_md(trigger)}`\n"
        f"订单号: `{escape_md(order_id)}`"
    )


def format_close_skipped(reason: str, raw: str) -> str:
    """CLOSE 信号收到但未执行（没匹配到持仓 / 解析跳过 / parser 拒绝）"""
    return (
        f"📭 *CLOSE 未执行*\n"
        f"原因: {escape_md(reason)}\n"
        f"原文: ```\n{escape_md((raw or '')[:300])}\n```"
    )


def format_addon_alert(symbol: str, raw: str) -> str:
    """疑似加仓信号未执行（parser 不支持无 strike 的 add-on 简写）→ 提醒人工。

    背景 7/6：KC "small add SPY @ 1.86" ×4 全部静默 parse-fail，
    加仓被漏掉且无任何提醒（裸 ticker 不满足 _looks_like_open_attempt）。
    """
    return (
        f"➕ *疑似加仓信号未执行*\n"
        f"标的: *{escape_md(symbol)}* \\(已持仓\\)\n"
        f"parser 不支持无 strike 的加仓简写，如需跟加请手动下单\n"
        f"原文: ```\n{escape_md((raw or '')[:300])}\n```"
    )


def format_edited_signal_alert(channel_name: str, symbol: str, strike: float,
                               side: str, expiry: str, price: float,
                               age_sec: float, raw: str) -> str:
    """原消息被**编辑**后才成为可执行信号 → 提醒人工，不自动下单。

    背景 8/5：enrich 23:51:00 发 "跟踪 $RKLB 每周 $80 看涨期权"（无喊价，
    按规则 3 正确拒单），14s 后编辑该消息补上 "$1.35 填充 2%"。
    on_message_edit 当时只写日志不重解析，这单整个丢掉。

    刻意**不自动下单**：编辑可能发生在几分钟甚至几小时后，限价会锚在
    早已走掉的喊价上；而且 on_message 与 on_message_edit 的去重语义不同
    （_seen 按 msg_id，编辑不改 id），自动执行的竞态面比收益大。
    先让人看见，跟不跟由人定。
    """
    minutes = age_sec / 60
    age_str = f"{age_sec:.0f} 秒前" if age_sec < 90 else f"{minutes:.0f} 分钟前"
    return (
        f"✏️ *消息编辑后成为信号（未自动下单）*\n"
        f"频道: `{escape_md(channel_name)}`\n"
        f"标的: *{escape_md(symbol)}* {escape_md(strike)}"
        f"{escape_md(side.upper()[:1])} {escape_md(expiry)}\n"
        f"喊价: ${escape_md(f'{price}')}\n"
        f"原消息发出于: {escape_md(age_str)}\n"
        f"要跟请**手动**下单\n"
        f"编辑后原文: ```\n{escape_md((raw or '')[:300])}\n```"
    )


def format_daily_summary(orders: int, total_cost: float,
                         max_orders: int, max_cost: float) -> str:
    """格式化每日统计"""
    remaining = max_cost - total_cost
    return (
        f"📊 *今日统计*\n"
        f"下单次数: {orders}/{max_orders}\n"
        f"累计成本: ${escape_md(f'{total_cost:.0f}')}/${escape_md(f'{max_cost:.0f}')}\n"
        f"剩余额度: ${escape_md(f'{remaining:.0f}')}"
    )
