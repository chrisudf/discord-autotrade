"""分批止盈守护：后台 asyncio task

设计基于用户策略（对话定稿）：
- weekly: +50% trim 50% → +100% trim 剩余 50% → 剩 ~25% 跑（靠 EOD/expiry/手动）
- swing:  +100% trim 50% → +200% trim 剩余 50% → 剩 ~25% 跑
- 0dte / lotto: 不挂 TP（前者靠 EOD，后者放飞）

档位 = 阈值是 avg_entry_price * (1 + threshold_pct)，
按 ladder 顺序触发，每档卖固定比例。

张数前提（2026-08-11 起）：channels.json 的 default_qty = **2**。qty=1 时
"卖剩余的 50%" 在整数上无解——要么全平（老代码的 max(1,...)，8/10 XOM 的
$185/张学费）要么不卖，阶梯形同虚设。qty=2 让 T1 卖 1 留 1 数学上成立，
T2 则落到 runner-preserve（见 _plan）。**实盘前 qty 改回 1 时，这条阶梯
会退化回"只有 T1、且必须靠 runner-preserve 兜"，见 ROADMAP §0.0。**

防重复触发：positions.tp_hits 位掩码持久化，T1=1 / T2=2 / T3=4。
- T1 触发后置位 → 下次扫描不再考虑 T1
- bot 重启不丢，跟 _triggered 内存 set 比更稳

拒单熔断（2026-08-13 起，见 position/retry_guard）：卖单被 broker 拒 / 下单抛
异常不再原地 5s 一轮硬打，改成退避 → 熔断 → 一声大告警。熔断**不置 tp_hits
位**（那等于把没落袋的止盈静默标记成已完成），只停手并喊人，重启即恢复。

环境变量：
- TP_POLL_INTERVAL   : 默认 5 秒（同 SL）
- TP_SELL_SLIP       : 默认 0.05（卖出限价 = last * 0.95，TP 不需要 SL 那么激进）
- SELL_REJECT_MAX_FAILS / SELL_REJECT_BACKOFF_{BASE,CAP}_SEC : 熔断阈值与退避
- LOG_DEDUP_WINDOW_SEC : 重复日志收敛窗口（默认 60s，见 utils/logdedup）

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
from autotrade.position import retry_guard
from autotrade.position.sell_executor import Outcome, SellPlan, SkipSell, execute_sell
from autotrade.storage import positions_db
from autotrade.notify.transport import send_telegram
# format_close_filled 已随成交 TG 收进 sell_executor（0015），此处只剩错误文案
from autotrade.notify.messages import format_error
from autotrade.notify.watchdog import notify_tick_error, notify_tick_ok
from autotrade.utils import logdedup
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


def _guard_key(code: str, tier_bit: int) -> str:
    """熔断 / 日志收敛的公共 key。粒度 = (合约, 档位)：同标的的其它档不受牵连。"""
    return f"tp:{code}:t{tier_bit}"


async def _trigger_tp(pos: dict, last_price: float, threshold_pct: float,
                      trim_pct: int, tier_bit: int, sell_slip: float):
    """对单个仓位触发 TP 单档。

    [0015] 执行骨架合并进 sell_executor.execute_sell；TP 专属差异保留在钩子：
      - 锁内重读后先查 tp_hits 位掩码（同档已触发 → 静默跳过）
      - qty 用 round() 而非 manager.calc_qty_to_sell 的 ceil()——契约点名
        **原样保留**的口径差异：TP 阶梯"卖剩余的 50%"倾向少卖（round(2.5)=2，
        banker's rounding），让 runner 多跑；KC trim 的 ceil 倾向跟足信号比例。
        两者不是笔误，是两种策略语义，合并执行器时不做"顺手统一"。
      - round() 结果为 0 张 → runner-preserve 跳过并置位该档（8/10 XOM，见 _plan 内注释）
      - 先 mark_tp_hit 再记账（pre_record 钩子）：即使 on_close_filled 出错，
        下轮也不会重复触发同档
      - 5% 卖出 slip（价格在涨，不需要 SL 那么激进）
      - [8/13] 拒单熔断（retry_guard）：退避期 / 已熔断的档位在此直接返回，
        连锁和 broker 都不碰
    """
    code = pos["option_code"]
    key = (code, tier_bit)
    if key in _triggered_this_tick:
        return
    _triggered_this_tick.add(key)

    # 拒单熔断：退避期内静默跳过。这里**不打日志**——每 5s 一轮的跳过如果
    # 逐条落盘，8/13 那一夜就是把"刷屏"从拒单行换成了跳过行，等于没治。
    # 熔断/退避的进入与解除各有一条日志（下方 _sell_rejected / on_success），
    # 中间这段沉默是有意的。
    guard = _guard_key(code, tier_bit)
    blocked_reason = retry_guard.blocked(guard)
    if blocked_reason is not None:
        logger.debug(f"[tp] T{tier_bit} {code} 跳过：{blocked_reason}")
        return

    async def _plan(fresh: dict):
        """锁内决策：同档位掩码防护 + runner-preserve + round() 口径的分档张数。"""
        if fresh["tp_hits"] & tier_bit:
            return SkipSell(Outcome.SKIPPED_SILENT)
        # [8/10 XOM] 单张仓位的非全平档：老代码 max(1, round(1*50/100)) = max(1, 0) = 1
        # = 整仓平掉，"卖 50% 留 runner" 被这层兜底直接推翻，T2 永远走不到。
        # 实测代价：XOM T1 落袋 +92%，KC 同标的 +300%（~$185/张）。
        # 同一教训 close 路径早已用 ~$330/合约学过（policy/positions.calc_qty_to_sell
        # 的 `remaining == 1 → return 0`），这里对齐它的语义。
        # 判定用"算出来是 0 张"而不是硬编码 qty_remaining==1：ladder 将来加
        # 25% 档时，剩 2 张同样会 round 成 0，走的是同一条 runner-preserve 语义。
        qty_to_sell = round(fresh["qty_remaining"] * trim_pct / 100)
        if qty_to_sell <= 0:
            # 置位后不再重复评估此档（qty_remaining 只减不增，判定不会翻转），
            # 否则每 5s 一轮 tick 都要重算并刷屏
            positions_db.mark_tp_hit(code, tier_bit)
            logger.info(
                f"[tp] 🏃 T{tier_bit} HIT {code} 但剩 {fresh['qty_remaining']} 张，"
                f"trim {trim_pct}% 取整后为 0 张 → 保留 runner 不卖；本档标记为已触发，"
                f"后续交给 SL / EOD / 喊单员平仓信号"
            )
            await send_telegram(format_error(
                f"TP T{tier_bit} 保留 runner（未卖出）",
                f"{code} 剩 {fresh['qty_remaining']} 张，+{int(threshold_pct*100)}% 档位 "
                f"trim {trim_pct}% 取整后为 0 张（卖了就是全平）→ 跳过。"
                f"last={last_price:.2f}\n"
                f"这张只剩 SL / EOD / 手工平仓兜底，请留意。"
            ))
            return SkipSell(Outcome.RUNNER_PRESERVED)
        qty_to_sell = min(qty_to_sell, fresh["qty_remaining"])
        limit = round(last_price * (1 - sell_slip), 2)
        if limit <= 0:
            limit = 0.01
        # 收敛：正常情况下这行一档只出现一次（成交即置位），会重复的恰恰是
        # 8/13 那种"下单失败 → 位掩码没置 → 下轮重来"的形状。熔断已经把轮次
        # 压到个位数，这层是兜底：万一将来出现熔断接不住的循环，日志不再被淹。
        logdedup.log_throttled(
            f"tp-hit:{guard}",
            f"[tp] 🎯 T{tier_bit} HIT {code}: last={last_price:.2f} "
            f">= entry*({1+threshold_pct:.2f})={fresh['avg_entry_price']*(1+threshold_pct):.2f}, "
            f"selling {qty_to_sell}/{fresh['qty_remaining']} @ {limit}",
        )
        return SellPlan(
            qty=qty_to_sell, limit=limit, remark=f"tp_t{tier_bit}",
            notify_pct=trim_pct,
            note=f"TP T{tier_bit} +{int(threshold_pct*100)}%: last={last_price:.2f}",
        )

    def _already_closed(fresh: dict):
        logger.debug(f"[tp] {code} already closed while waiting for lock, skip")

    async def _on_failed_attempt(title: str, err: str, qty_desc: str):
        """拒单与下单异常共用的收尾：熔断登记 + 收敛日志 + 按需 TG。

        `_triggered_this_tick.discard(key)` 保留原语义（本 tick 内允许别的
        路径再试一次），真正的止血在 retry_guard —— 下一轮 tick 会被
        blocked() 挡在门外，不再是 5s 一轮的硬打。
        """
        _triggered_this_tick.discard(key)
        d = retry_guard.on_reject(guard, err)

        level = "ERROR" if (d.tripped or d.fails == 1) else "WARNING"
        if d.tripped:
            tail = ("确定性拒单，重试无意义" if d.deterministic
                    else f"连续 {d.fails} 次失败")
            logger.log(level, f"[tp] ⛔ T{tier_bit} {code} 熔断（{tail}）：{err}")
        else:
            logdedup.log_throttled(
                f"tp-reject:{guard}",
                f"[tp] sell rejected: {err} → 第 {d.fails} 次，退避 {d.retry_after:.0f}s",
                level=level,
            )

        if not d.alert:
            return
        suffix = f"\n（上次告警以来另有 {d.suppressed} 次同类失败未单独告警）" if d.suppressed else ""
        if d.tripped:
            hint = (
                "\n\n⛔ **本档已熔断，本进程内不再重试**（tp_hits 未置位，"
                "重启即恢复）。"
            )
            if d.deterministic:
                # 8/13 的形状：本地 DB 说有仓、broker 说没有。退避解决不了，
                # 只有对账能解决 —— 告警里直接给出下一步动作。
                hint += (
                    "\n本地 DB 与 broker 可能已脱钩，请跑 "
                    "`python -m autotrade.ops.sync_positions --dry-run` 对账，"
                    "修完重启 listener。"
                )
            hint += "\n在此之前这张只剩 SL / EOD / 手工平仓兜底。"
        else:
            hint = f"\n\n将在 {d.retry_after:.0f}s 后重试（连续第 {d.fails} 次失败）。"
        await send_telegram(format_error(title, f"{code} {qty_desc}\n{err}{suffix}{hint}"))

    async def _sell_error(e: Exception, plan):
        logger.exception("[tp] place_sell_order failed")
        await _on_failed_attempt("TP sell error", f"{type(e).__name__}: {e}",
                                 f"qty={plan.qty}")

    async def _sell_rejected(result: dict, plan):
        await _on_failed_attempt("TP sell rejected",
                                 result.get("message", "unknown"),
                                 f"qty={plan.qty}")

    # 先持久化档位（即使 on_close_filled 出错也不会重复触发同档）
    def _pre_record():
        try:
            positions_db.mark_tp_hit(code, tier_bit)
        except Exception as e:
            logger.error(f"[tp] mark_tp_hit failed: {e}")

    async def _record_failure(e: Exception, result: dict):
        logger.error(f"[tp] on_close_filled failed: {e}")

    outcome, _ = await execute_sell(
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

    # 卖成了 → 清熔断状态并补报被收敛的日志条数。
    # 只认 SOLD：RUNNER_PRESERVED / SKIPPED_* 都没真的打到 broker，不构成
    # "这条路通了"的证据，不该拿来解除退避。
    if outcome is Outcome.SOLD:
        cleared = retry_guard.on_success(guard)
        if cleared:
            logger.info(f"[tp] ✅ T{tier_bit} {code} 卖出成功，清除 {cleared} 次连续失败的退避状态")
        logdedup.flush(f"tp-reject:{guard}")
        logdedup.flush(f"tp-hit:{guard}")


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
        f"ladders={ {k: [(int(t*100), p) for t,p,_ in v] for k,v in LADDER.items()} } "
        # 熔断参数进启动行：8/13 那夜要判断"是不是又在硬打"，得先知道阈值是多少
        f"reject_guard=({retry_guard.describe_config()})"
    )
    while True:
        try:
            await _tp_tick()
            notify_tick_ok("tp")
        except Exception as e:
            notify_tick_error("tp", e)
        await asyncio.sleep(_cfg()["interval"])
