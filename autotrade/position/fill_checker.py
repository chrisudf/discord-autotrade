"""订单成交确认（fill confirmation）后台任务

背景：broker.place_order / place_sell_order 的 success 只代表限价单**已提交**
（ret==RET_OK），不代表成交。主链路目前把"提交"当"成交"处理：
- 买入：positions 表 avg_entry_price 记的是 limit price，不是真实 fill
- 卖出：on_close_filled 立即扣减 qty / 标 CLOSED —— 若限价卖单实际没成交
  （SL 触发时价格快速下跌、限价挂不上很常见），DB 认为仓位没了，
  SL/TP/EOD 全部停止保护，broker 端却还裸奔着一张正在归零的合约

本模块不改主流程语义（保持"提交即入账"的乐观路径），而是在提交成功后
fire-and-forget 起一个确认任务：
- 买单：确认成交后用 dealt_avg_price 回填 avg_entry_price（SL/TP 阈值更准）；
  超时未成交 / 终态失败 → TG 告警（DB 可能高估持仓，提示对账）
- 卖单：超时未成交 / 终态失败 → TG 告警（DB 已按已平处理，broker 端仓位
  可能还在——提示手动处理或跑 scripts/sync_positions.py）

DRY_RUN 下 query_order_status 直接返回 FILLED_ALL，任务瞬时完成，零噪音。

环境变量：
- FILL_POLL_SEC     : 轮询间隔，默认 15 秒
- FILL_TIMEOUT_SEC  : 超时告警阈值，默认 180 秒

TODO（实测调整）：
- 卖单超时后可以更进一步：自动撤单 + 更激进限价重挂（当前只告警，人工接管）
- FILLED_PART 长时间停留的处理（当前按未成交对待，超时告警里带 filled_qty）
"""
import asyncio
import os

from autotrade.broker.trade import query_order_status
from autotrade.notify.transport import send_telegram
from autotrade.notify.messages import format_error
from autotrade.storage import positions_db
from autotrade.utils.logger import logger

# 成交价合理性闸门。
# [8/20 TSLA 实锤] US.TSLA260828C470000 限价 2.97 提交成功，broker 回报
# filled_avg_price=**0.13**（偏离 95.6%），本函数此前只判 `dealt > 0` 就照单回填
# → DB 里躺着一个"成本 $26、实际花了 $594、无止损、无有效报价"的仓位，
# 任何基于 DB 的盈亏统计都会把它算成暴赚。同一晚 04:41 该合约还出现
# `[CLOSE] no price ref, skipping sell` —— 两条现象同指一个根因：这个合约
# 拿不到 OPRA 报价，回来的成交价是垃圾值。
#
# 上界 1.05：限价单不该成交在限价之上，留 5% 给手续费/报价口径差异。
# 下界 0.50：真实的好成交（8/21 UBER 限价 0.50 实际吃到 0.37，-26%）必须放过，
# 但 -95% 这种数量级的必须拦下。越界 = **拒绝回填 + 告警**，不是照收。
_DEALT_MAX_RATIO = 1.05
_DEALT_MIN_RATIO = 0.50

# moomoo order_status 终态
_FILLED_STATUSES = {"FILLED_ALL"}
# 不会再变成 FILLED 的失败终态 → 立即告警不用等超时
_DEAD_STATUSES = {"CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}


def _cfg() -> dict:
    return {
        "poll": float(os.getenv("FILL_POLL_SEC", "15")),
        "timeout": float(os.getenv("FILL_TIMEOUT_SEC", "180")),
    }


async def _poll_until_terminal(order_id: str) -> dict:
    """轮询到 成交 / 失败终态 / 超时。返回最后一次 status dict + 'outcome' 字段。

    outcome ∈ {"filled", "dead", "timeout"}
    """
    cfg = _cfg()
    waited = 0.0
    last: dict = {}
    while True:
        last = await asyncio.to_thread(query_order_status, order_id)
        status = (last.get("status") or "").upper()
        if last.get("success") and status in _FILLED_STATUSES:
            return {**last, "outcome": "filled"}
        if last.get("success") and status in _DEAD_STATUSES:
            return {**last, "outcome": "dead"}
        if waited >= cfg["timeout"]:
            return {**last, "outcome": "timeout"}
        await asyncio.sleep(cfg["poll"])
        waited += cfg["poll"]


async def confirm_buy_fill(order_id: str, option_code: str, qty: int, limit_price: float):
    """买单提交成功后 fire-and-forget 调用（asyncio.create_task）。

    成交 → dealt_avg_price 回填 avg_entry_price（仅当期间无加仓/减仓）。
    未成交 → TG 告警提示对账。
    """
    if not order_id:
        return
    try:
        res = await _poll_until_terminal(order_id)
        if res["outcome"] == "filled":
            dealt = res.get("filled_avg_price") or 0.0
            # [ROADMAP P1 #14 (a)] filled 分支无论走哪条都留一行。
            # 原来只在 dealt != limit 时才打日志，成交价正好等于限价就静默 return
            # —— "什么都没有" 和 "压根没跑" 在日志上无法区分（8/13 MU 那单全日志
            # 零 [fill] 行，同晚 ASTS/SPCX 都在 15s 内出了 FILL_ADJUST）。
            # 8/20 CRWV 88P 也踩过同一处：复盘要靠"四笔有 FILL_ADJUST、一笔没有"
            # 才能反推它是正常成交而不是任务没跑。
            logger.info(
                f"[fill] buy {option_code} filled dealt_avg={dealt:.2f} "
                f"(limit {limit_price:.2f}) order={order_id}"
            )
            if dealt > 0 and abs(dealt - limit_price) > 1e-9:
                lo = limit_price * _DEALT_MIN_RATIO
                hi = limit_price * _DEALT_MAX_RATIO
                if limit_price > 0 and not (lo <= dealt <= hi):
                    # 拒绝回填：DB 里留着限价（偏高但量级正确），
                    # 好过写进一个把成本基准打穿的垃圾值。
                    logger.error(
                        f"[fill] buy {option_code} dealt_avg={dealt:.2f} 偏离限价 "
                        f"{limit_price:.2f} 超出闸门 [{lo:.2f}, {hi:.2f}] —— "
                        f"拒绝回填 avg_entry，order={order_id}"
                    )
                    await send_telegram(format_error(
                        "成交价异常，拒绝回填成本",
                        f"{option_code} order={order_id}\n"
                        f"限价 ${limit_price:.2f} → broker 回报成交 ${dealt:.2f}"
                        f"（偏离 {abs(dealt - limit_price) / limit_price * 100:.0f}%）\n"
                        f"avg_entry 保持 ${limit_price:.2f} 未动。"
                        f"该合约很可能取不到报价 —— 请核对 moomoo 成交明细，"
                        f"并确认 SL/TP/EOD 还能不能给它取到价"
                    ))
                    return
                if positions_db.adjust_entry_price(
                    option_code, qty, dealt, assumed_price=limit_price
                ):
                    logger.info(
                        f"[fill] buy {option_code} dealt_avg={dealt:.2f} "
                        f"(limit {limit_price:.2f}) → avg_entry 已回填"
                    )
                else:
                    # [8/18 SPCX] 此前这里是静默 return —— 加仓必然走到这条分支，
                    # 日志里一个字都没有，只能靠"四笔有 FILL_ADJUST、一笔没有"
                    # 反推。成本基准没修对是要进 TP 阶梯的，不许再无声。
                    logger.warning(
                        f"[fill] buy {option_code} dealt_avg={dealt:.2f} "
                        f"(limit {limit_price:.2f}) **未回填** —— 仓位不在 "
                        f"OPEN/PARTIAL，或 qty_total < 本单张数（qty={qty}）。"
                        f"avg_entry 仍是限价，order={order_id}"
                    )
            return
        if res["outcome"] == "dead":
            await send_telegram(format_error(
                "买单终态未成交",
                f"{option_code} x{qty} order={order_id} status={res.get('status')}\n"
                f"DB 已按持仓入账但订单已死 —— 请核对 moomoo，"
                f"必要时跑 scripts/sync_positions.py 对账"
            ))
            return
        await send_telegram(format_error(
            "买单超时未确认成交",
            f"{option_code} x{qty} order={order_id} "
            f"status={res.get('status')} filled={res.get('filled_qty', '?')}/{qty}\n"
            f"DB 已按持仓入账 —— 若实际未成交，SL/TP 会盯着一个不存在的仓位。\n"
            f"请核对 moomoo 或跑 scripts/sync_positions.py"
        ))
    except Exception:
        logger.exception(f"[fill] confirm_buy_fill crashed for {order_id}")


async def confirm_sell_fill(order_id: str, option_code: str, qty: int, trigger: str):
    """卖单提交成功后 fire-and-forget 调用。

    卖单风险方向相反：DB 已经按"已平"扣减（watcher 不再保护），
    若限价卖单实际没成交，broker 端仓位还在 —— 必须让人立刻知道。
    """
    if not order_id:
        return
    try:
        res = await _poll_until_terminal(order_id)
        if res["outcome"] == "filled":
            return
        status = res.get("status")
        await send_telegram(format_error(
            f"⚠️ 卖单未成交（{trigger}）",
            f"{option_code} x{qty} order={order_id} status={status} "
            f"filled={res.get('filled_qty', '?')}/{qty}\n"
            f"DB 已按已平处理，SL/TP/EOD **不再保护这个仓位**，\n"
            f"但 broker 端可能还持有 —— 请立即在 moomoo 手动处理，\n"
            f"然后跑 scripts/sync_positions.py 对账"
        ))
    except Exception:
        logger.exception(f"[fill] confirm_sell_fill crashed for {order_id}")


# [refactor-change] c: asyncio.create_task 只在 event loop 里留弱引用，
# fire-and-forget 的确认任务可能在完成前被 GC 回收（CPython docs 明确警告）。
# spawn 现在持强引用：task 加入模块级 set，done 时回调 discard 自动清理。
_pending_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """create_task 的防御包装：调用点在同步/异步混合上下文，失败只 log。"""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError as e:
        # 无运行中的 event loop（理论上只在单测直接调用时发生）
        logger.error(f"[fill] cannot spawn confirm task: {e}")
        return
    # [refactor-change] c: 持强引用防 GC，任务结束后自动移除
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
