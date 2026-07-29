"""[0015] 卖出结局枚举 + 四路卖出共用执行骨架（SellExecutor）。

背景：四条卖出路径（kc_close / sl_polling / tp_polling / eod_force）各自
维护一份"锁→锁内重读→防护→限价→下单→记账→fill_confirm→TG"的复制粘贴，
7/23-7/28 四夜复盘里每修一个卖出 bug 都要在四处同步改（0007 的 no-quote
重试分离只落了 EOD，0008 的 META close 误路由复盘时发现 close_flow 的
布尔旗标 any_executed/any_success/any_broker_failure 三个变量各自手工维护，
漏 set 一个就是 6/18 IWM"broker 拒单误报 no matching"级别的误导告警）。

本模块两件事：

1. Outcome 枚举：一次卖出尝试的**结局**。close_flow 的
   any_executed / any_broker_failure / runner_preserved 全部从 outcomes
   列表推导，不再手工维护散落的布尔旗标（漏 set 类 bug 从结构上消灭）。

2. execute_sell：四路共用的执行骨架。**所有 I/O 依赖（下单/通知/fill
   确认/限价决策）由调用方注入**——注入的是每个 watcher 自己模块命名空间
   里的裸名（sl_watcher.place_sell_order / eod_watcher.get_last_price ...），
   这是硬约束：既有测试全部 monkeypatch 在 watcher 模块命名空间上，
   executor 若自己 import broker 裸名，382+ 条既有测试的 patch 点全部失效
   （也意味着生产里 DRY_RUN mock 注入点漂移）。禁止在本模块 import
   broker.trade / notify.transport。

有意保留的四路差异（契约点名，不做"顺手统一"）：
  - slip 分档：KC 5% / SL 8% / TP 5% / EOD 10%（在各调用方 plan_fn 里）
  - SL 记账失败 → 冻结该合约（_triggered 保留），TP/EOD/KC 只 log
  - TP 先 mark_tp_hit 再记账（pre_record 钩子），且 qty 用 round()——
    与 manager.calc_qty_to_sell 的 ceil() 口径差异**原样保留**
  - EOD 锁内取价 + 0007 的 no-quote 重试/告警分离（在 eod 的 plan_fn 里）
  - kc_close 的 runner-preserve / 策略B / twin 防护留在 close_flow 侧
"""
import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

from autotrade.notify.messages import format_close_filled
from autotrade.position import manager as position_mgr
from autotrade.utils.logger import logger


class Outcome(Enum):
    """一次卖出尝试的结局。

    close_flow 的推导规则（与 0015 之前的布尔旗标语义逐字对齐）：
      any_executed      = 任意 outcome != NOT_FOUND
                          （"处理过了"就不再发 no matching 兜底 TG——
                           6/18 IWM broker 拒单误报教训）
      any_success       = 任意 outcome == SOLD
      any_broker_failure= 任意 outcome == BROKER_FAILED
      指纹回滚          = any_broker_failure and not any_success
                          （零成交且 broker 失败 → 回滚，让 1-3s 后的
                           双语孪生充当天然重试；确定性跳过不回滚）
    """
    SOLD = "sold"                          # 卖单提交成功（乐观入账口径）
    SKIPPED_NOTIFIED = "skipped_notified"  # 确定性跳过，已发专属 TG（无价/strike/twin/频道）
    SKIPPED_SILENT = "skipped_silent"      # 确定性跳过，仅 log（等锁期间已被别人卖掉等）
    RUNNER_PRESERVED = "runner_preserved"  # runner-preserve 拦下（循环外合并 TG）
    BROKER_FAILED = "broker_failed"        # broker 异常/拒单（瞬时，值得孪生重试）
    NOT_FOUND = "not_found"                # 没找到可操作仓位（唯一不算 executed 的结局）


@dataclass
class SellPlan:
    """plan_fn 的产物：锁内敲定的卖出参数。"""
    qty: int
    limit: float
    remark: str        # broker 订单备注（kc_close_33pct / sl_polling / tp_t1 / eod_force）
    note: str          # 记账 note（进 positions 事件表）
    notify_pct: int    # 成交 TG 里展示的 pct（KC=pos_pct / SL,EOD=100 / TP=trim_pct）


@dataclass
class SkipSell:
    """plan_fn 决定不卖（runner-preserve / 无价拒卖 / no-quote / 同档已触发）。
    专属 TG / 节流 / backoff 状态由 plan_fn 自己处理完再返回。"""
    outcome: Outcome


PlanFn = Callable[[dict], Awaitable["SellPlan | SkipSell"]]


async def execute_sell(
    pos: dict,
    *,
    trigger_source: str,
    notify_trigger: str,
    plan_fn: PlanFn,
    place_sell_order: Callable[..., dict],
    notify: Callable[[str], Awaitable],
    fill_confirm: Callable[[str, int], None],
    on_already_closed: Optional[Callable[[dict], None]] = None,
    on_sell_error: Optional[Callable] = None,      # async (exc, plan) -> None
    on_sell_rejected: Optional[Callable] = None,   # async (result, plan) -> None
    pre_record: Optional[Callable[[], None]] = None,
    on_record_success: Optional[Callable[[], None]] = None,
    on_record_failure: Optional[Callable] = None,  # async (exc, result) -> None
    ref_msg_id: Optional[str] = None,
) -> tuple[Outcome, Optional[dict]]:
    """四路共用卖出骨架：锁 → 锁内重读 → OPEN/PARTIAL+qty 防护 →
    plan_fn（限价/张数决策，注入）→ 下单（注入）→ [pre_record] → 记账 →
    fill_confirm（注入）→ **锁外**成交 TG（注入）。

    Args:
        pos:             锁外读到的仓位 dict（锁内必重读，见 manager.sell_lock 注释）
        trigger_source:  on_close_filled 的 trigger_source（kc_signal/sl_polling/...）
        notify_trigger:  成交 TG 里的触发标签（TP 是 tp_t{bit}，与 trigger_source 不同）
        plan_fn:         async(fresh)->SellPlan|SkipSell。**在锁内执行**——EOD 的
                         锁内取价、KC 的 runner/策略B/quote-fallback 决策都放这里，
                         保证"决定卖"与"按什么价卖"在同一把锁的同一视图下敲定。
        place_sell_order/notify/fill_confirm: 调用方模块命名空间的裸名注入
                         （monkeypatch 兼容硬约束，见模块 docstring）。
        on_*:            各触发器的差异钩子（错误 TG 文案、SL 冻结、backoff 状态）。
        pre_record:      记账**前**执行（TP 的 mark_tp_hit：即使记账失败也不会
                         下轮重复触发同档）。
        ref_msg_id:      kc_close 记账时回链原始 Discord msg。

    Returns:
        (Outcome, broker result | None)。BROKER_FAILED 且是拒单时带 result，
        异常时为 None。watcher 调用方可忽略返回值（行为由钩子完成）。
    """
    code = pos["option_code"]
    async with position_mgr.sell_lock(code):
        # 锁内重读：等锁期间可能已被其它路径卖掉（部分或全部）
        fresh = position_mgr.get(code) or pos
        if fresh["status"] not in ("OPEN", "PARTIAL") or fresh["qty_remaining"] <= 0:
            if on_already_closed is not None:
                on_already_closed(fresh)
            return Outcome.SKIPPED_SILENT, None

        plan = await plan_fn(fresh)
        if isinstance(plan, SkipSell):
            return plan.outcome, None

        try:
            result = await asyncio.to_thread(
                place_sell_order,
                option_code=code, qty=plan.qty,
                limit_price=plan.limit, remark=plan.remark,
            )
        except Exception as e:
            if on_sell_error is not None:
                await on_sell_error(e, plan)
            return Outcome.BROKER_FAILED, None

        if not result.get("success"):
            if on_sell_rejected is not None:
                await on_sell_rejected(result, plan)
            return Outcome.BROKER_FAILED, result

        if pre_record is not None:
            # TP：先持久化档位（即使下面 on_close_filled 出错也不会重复触发同档）
            pre_record()

        try:
            position_mgr.on_close_filled(
                option_code=code,
                qty_sold=result.get("qty", plan.qty),
                fill_price=result.get("price", plan.limit),
                trigger_source=trigger_source,
                ref_msg_id=ref_msg_id,
                order_id=result.get("order_id"),
                note=plan.note,
            )
            if on_record_success is not None:
                on_record_success()
        except Exception as e:
            # 记账失败的分档处理在钩子里：SL 冻结 + 大声 TG（DB 与 broker
            # 已脱钩，失去自动止损必须有人知道）；TP/EOD/KC 只 log。
            if on_record_failure is not None:
                await on_record_failure(e, result)
            else:
                logger.error(f"[sell_executor] on_close_filled failed: {e}")

        # 卖单成交确认：success 只代表限价单已提交不代表成交，
        # DB 已按已平处理，实际未成交必须告警（fill_checker 语义）
        fill_confirm(result.get("order_id") or "",
                     result.get("qty", plan.qty))

    # 成交 TG 在锁外发：TG RTT 不占卖出锁（7/8 双发竞态窗口教训的反向约束）
    await notify(format_close_filled(
        fresh["symbol"], fresh["strike"], fresh["side"], fresh["expiry"],
        result.get("qty", plan.qty), result.get("price", plan.limit),
        plan.notify_pct, notify_trigger, result.get("order_id", "N/A"),
    ))
    return Outcome.SOLD, result
