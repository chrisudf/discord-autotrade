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
            if dealt > 0 and abs(dealt - limit_price) > 1e-9:
                if positions_db.adjust_entry_price(option_code, qty, dealt):
                    logger.info(
                        f"[fill] buy {option_code} dealt_avg={dealt:.2f} "
                        f"(limit {limit_price:.2f}) → avg_entry 已回填"
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
