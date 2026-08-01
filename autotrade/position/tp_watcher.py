"""分批止盈守护：后台 asyncio task

设计基于用户策略（对话定稿）：
- weekly: +50% trim 50% → +100% trim 剩余 50% → 剩 ~25% 跑（靠 EOD/expiry/手动）
- swing:  +100% trim 50% → +200% trim 剩余 50% → 剩 ~25% 跑
- 0dte / lotto: 不挂 TP（前者靠 EOD，后者放飞）

档位 = 阈值是 avg_entry_price * (1 + threshold_pct)，
按 ladder 顺序触发，每档卖固定比例。

防重复触发：positions.tp_hits 位掩码持久化，T1=1 / T2=2 / T3=4。
- T1 触发后置位 → 下次扫描不再考虑 T1
- bot 重启不丢，跟 _triggered 内存 set 比更稳

环境变量：
- TP_POLL_INTERVAL   : 默认 5 秒（同 SL）
- TP_SELL_SLIP       : 默认 0.05（卖出限价 = last * 0.95，TP 不需要 SL 那么激进）

TODO（实测调整）：
- 阈值经验值，跑实盘后 PnL 复盘哪档过早 / 过晚
- T1/T2 卖出比例（现 50%/50%）也是经验值
- swing 的 +100/+200 太宽？小波动 swing 可能整周到不了
- 加 trailing TP：T1 触发后启动 trailing，从高点回撤 N% 卖剩余
- 共用 quote tick 而非独立 polling（SL + TP + future 健康检查合并一个 loop）
"""
import asyncio
import os
from typing import Optional

from autotrade.broker.trade import place_sell_order
from autotrade.broker.quote import get_last_prices
from autotrade.position import manager as position_mgr
from autotrade.position import fill_checker
from autotrade.position.sell_executor import Outcome, SellPlan, SkipSell, execute_sell
from autotrade.storage import positions_db
from autotrade.notify.transport import send_telegram
# format_close_filled 已随成交 TG 收进 sell_executor（0015），此处只剩错误文案
from autotrade.notify.messages import format_error
from autotrade.notify.watchdog import notify_tick_error, notify_tick_ok
from autotrade.utils.logger import logger


# Ladder 定义（(threshold_pct, trim_pct_of_remaining, tier_bit) 三元组，含
# "trim_pct 相对当时剩余"语义注释）已移至 autotrade.policy.positions（值不变）。
# 此处裸名 import，tests patch tp_watcher.LADDER 仍然生效。
from autotrade.policy.positions import LADDER


def _cfg() -> dict:
    return {
        "interval": int(os.getenv("TP_POLL_INTERVAL", "5")),
        "sell_slip": float(os.getenv("TP_SELL_SLIP", "0.05")),
    }


# 单 tick 内已触发的 (code, tier_bit) 避免在 broker 返回前重复触发
_triggered_this_tick: set[tuple[str, int]] = set()


async def _trigger_tp(pos: dict, last_price: float, threshold_pct: float,
                      trim_pct: int, tier_bit: int, sell_slip: float):
    """对单个仓位触发 TP 单档。

    [0015] 执行骨架合并进 sell_executor.execute_sell；TP 专属差异保留在钩子：
      - 锁内重读后先查 tp_hits 位掩码（同档已触发 → 静默跳过）
      - qty 用 round() 而非 manager.calc_qty_to_sell 的 ceil()——契约点名
        **原样保留**的口径差异：TP 阶梯"卖剩余的 50%"倾向少卖（round(2.5)=2，
        banker's rounding），让 runner 多跑；KC trim 的 ceil 倾向跟足信号比例。
        两者不是笔误，是两种策略语义，合并执行器时不做"顺手统一"。
      - 先 mark_tp_hit 再记账（pre_record 钩子）：即使 on_close_filled 出错，
        下轮也不会重复触发同档
      - 5% 卖出 slip（价格在涨，不需要 SL 那么激进）
    """
    code = pos["option_code"]
    key = (code, tier_bit)
    if key in _triggered_this_tick:
        return
    _triggered_this_tick.add(key)

    async def _plan(fresh: dict):
        """锁内决策：同档位掩码防护 + round() 口径的分档张数。"""
        if fresh["tp_hits"] & tier_bit:
            return SkipSell(Outcome.SKIPPED_SILENT)
        qty_to_sell = max(1, round(fresh["qty_remaining"] * trim_pct / 100))
        qty_to_sell = min(qty_to_sell, fresh["qty_remaining"])
        limit = round(last_price * (1 - sell_slip), 2)
        if limit <= 0:
            limit = 0.01
        logger.info(
            f"[tp] 🎯 T{tier_bit} HIT {code}: last={last_price:.2f} "
            f">= entry*({1+threshold_pct:.2f})={fresh['avg_entry_price']*(1+threshold_pct):.2f}, "
            f"selling {qty_to_sell}/{fresh['qty_remaining']} @ {limit}"
        )
        return SellPlan(
            qty=qty_to_sell, limit=limit, remark=f"tp_t{tier_bit}",
            notify_pct=trim_pct,
            note=f"TP T{tier_bit} +{int(threshold_pct*100)}%: last={last_price:.2f}",
        )

    def _already_closed(fresh: dict):
        logger.debug(f"[tp] {code} already closed while waiting for lock, skip")

    async def _sell_error(e: Exception, plan):
        logger.exception("[tp] place_sell_order failed")
        _triggered_this_tick.discard(key)
        await send_telegram(format_error("TP sell error", f"{code}\n{e}"))

    async def _sell_rejected(result: dict, plan):
        err = result.get("message", "unknown")
        logger.error(f"[tp] sell rejected: {err}")
        _triggered_this_tick.discard(key)
        await send_telegram(format_error(
            "TP sell rejected", f"{code} qty={plan.qty}\n{err}"))

    # 先持久化档位（即使 on_close_filled 出错也不会重复触发同档）
    def _pre_record():
        try:
            positions_db.mark_tp_hit(code, tier_bit)
        except Exception as e:
            logger.error(f"[tp] mark_tp_hit failed: {e}")

    async def _record_failure(e: Exception, result: dict):
        logger.error(f"[tp] on_close_filled failed: {e}")

    await execute_sell(
        pos,
        trigger_source="tp_polling",
        notify_trigger=f"tp_t{tier_bit}",
        plan_fn=_plan,
        place_sell_order=place_sell_order,
        notify=send_telegram,
        # 卖单成交确认：未成交则 TG 告警（DB 已扣减，broker 端可能还持有）
        fill_confirm=lambda order_id, qty_sold: fill_checker.spawn(
            fill_checker.confirm_sell_fill(order_id, code, qty_sold, f"tp_t{tier_bit}")),
        on_already_closed=_already_closed,
        on_sell_error=_sell_error,
        on_sell_rejected=_sell_rejected,
        pre_record=_pre_record,
        on_record_failure=_record_failure,
    )


async def _tp_tick():
    """单轮检查。仅扫 category 在 LADDER 里的活跃仓位。

    批量取价（7/8 改造，同 sl_watcher._sl_tick）：整个 tick 只发一次
    get_last_prices，避免打满 moomoo 60 次/30s 频率配额。
    """
    cfg = _cfg()
    global _triggered_this_tick
    _triggered_this_tick = set()  # tick 边界重置（每轮独立判断）

    positions = [
        p for p in position_mgr.get_open_positions()
        if p["category"] in LADDER and p["qty_remaining"] > 0
    ]
    if not positions:
        return

    codes = [p["option_code"] for p in positions]
    prices = await asyncio.to_thread(get_last_prices, codes)

    for pos in positions:
        cat = pos["category"]
        last = prices.get(pos["option_code"])
        if last is None:
            continue

        # 检查每档：未触发过 + 价格达标 → 触发
        for threshold_pct, trim_pct, tier_bit in LADDER[cat]:
            if pos["tp_hits"] & tier_bit:
                continue  # 此档已触发过
            threshold_price = pos["avg_entry_price"] * (1 + threshold_pct)
            if last >= threshold_price:
                await _trigger_tp(pos, last, threshold_pct, trim_pct, tier_bit, cfg["sell_slip"])
                # 触发一档后 pos 数据已过时（qty_remaining 变了），中断本仓位本轮
                # 下一轮 tick 重新读取最新状态，自然处理下一档
                break


async def run_tp_watcher():
    cfg = _cfg()
    logger.info(
        f"[tp] watcher started: interval={cfg['interval']}s "
        f"sell_slip={cfg['sell_slip']*100:.0f}% "
        f"ladders={ {k: [(int(t*100), p) for t,p,_ in v] for k,v in LADDER.items()} }"
    )
    while True:
        try:
            await _tp_tick()
            notify_tick_ok("tp")
        except Exception as e:
            notify_tick_error("tp", e)
        await asyncio.sleep(_cfg()["interval"])
