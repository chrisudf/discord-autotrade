"""止损守护：后台 asyncio task

职责：
- 每 POLL_INTERVAL 秒一轮
- 扫所有 apply_sl=True 的活跃仓位
- 拿 broker.get_last_price，比对 avg_entry * (1 - STOP_LOSS_PCT)
- 触发即全平（激进限价确保成交），调 on_close_filled + TG

设计权衡：
- 用 in-memory `_triggered` 防止"卖出成功但落库失败"时下轮重复卖出；
  on_close_filled 成功后即释放（position 已 CLOSED，下轮不会再被选上，
  且同 code reopen 后 SL 需要重新生效——见 _triggered 定义处注释）
- 不做"先 UPDATE 占位再卖"——broker fail 后还想重试时反而麻烦
- Bot 崩溃 → in-memory set 丢失。但若 SL 已实际成交，position 也已 CLOSED；
  若 SL 卖单还在挂着没成交，重启会再发一次 sell——可接受（broker 会拒重复 or 多卖一份）
  TODO（实测调整）：要更稳就用 query_order_status 确认 fill 状态再标记

环境变量：
- STOP_LOSS_PCT       : 默认 0.50（亏 50% 触发）
- SL_POLL_INTERVAL    : 默认 5 秒
- SL_SELL_SLIP        : 默认 0.08（卖出限价相对当前价的下偏移，确保成交）

TODO（实测调整）：
- 50% 阈值经验值，看真实 fill 数据后可能要按 category 分档（weekly 紧一点）
- 8% 卖出 slip 在低流动性合约会被吃穿，要不要分档
- 行情 API 限流 / 失败时 backoff 而非死循环
- watcher 启动时要不要先打一次完整状态到 TG（"开始监控 N 个仓位"）
"""
import asyncio
import os
from typing import Optional

from autotrade.broker.trade import place_sell_order
from autotrade.broker.quote import get_last_prices
from autotrade.position import manager as position_mgr
from autotrade.position import fill_checker
from autotrade.notify.transport import send_telegram
from autotrade.notify.messages import format_close_filled, format_error
from autotrade.utils.logger import logger


def _cfg() -> dict:
    """每次调用重读 os.environ（方便测试 monkeypatch）。

    注意：这**不是** .env 热更新——load_dotenv 只在 import 时跑一次，
    运行中编辑 config/.env 不会生效，改配置需要重启进程。
    """
    return {
        "sl_pct": float(os.getenv("STOP_LOSS_PCT", "0.50")),
        "interval": int(os.getenv("SL_POLL_INTERVAL", "5")),
        "sell_slip": float(os.getenv("SL_SELL_SLIP", "0.08")),
    }


# 已触发但尚未确认落库的 option_code。
# 生命周期（非进程级！）：
#   add    → 卖单提交前（挡住 on_close_filled 失败后 DB 仍 OPEN 时下轮重复卖出）
#   discard→ 卖单失败/异常（允许下轮重试）
#   discard→ on_close_filled 成功（DB 已 CLOSED，下轮自然不会选中；
#            必须移除，否则同 code 未来 reopen 后 SL 永久失效）
# 只有"卖出成功但落库失败"会让 code 留在 set 里——此时故意冻结该 code 的 SL，
# 人工修完 DB 后重启进程恢复。
_triggered: set[str] = set()


async def _trigger_sl(pos: dict, last_price: float, threshold: float, sell_slip: float):
    """对单个仓位触发止损全平。"""
    code = pos["option_code"]
    if code in _triggered:
        logger.debug(f"[sl] already triggered this run: {code}")
        return
    _triggered.add(code)

    async with position_mgr.sell_lock(code):
        # 锁内重读：等锁期间可能已被 TP/EOD/CLOSE 卖掉（部分或全部）
        pos = position_mgr.get(code) or pos
        if pos["status"] not in ("OPEN", "PARTIAL") or pos["qty_remaining"] <= 0:
            logger.debug(f"[sl] {code} already closed while waiting for lock, skip")
            _triggered.discard(code)  # 没有卖出发生，维持 set 只含"已卖未落库"的不变式
            return

        qty = pos["qty_remaining"]
        limit = round(last_price * (1 - sell_slip), 2)
        if limit <= 0:
            # 极低价兜底——0.01 起挂
            limit = 0.01

        logger.warning(
            f"[sl] 🛑 TRIGGER {code}: last={last_price:.2f} <= threshold={threshold:.2f} "
            f"(entry={pos['avg_entry_price']:.2f}), selling {qty} @ {limit}"
        )

        try:
            result = await asyncio.to_thread(
                place_sell_order,
                option_code=code, qty=qty,
                limit_price=limit, remark="sl_polling",
            )
        except Exception as e:
            logger.exception("[sl] place_sell_order failed")
            await send_telegram(format_error("SL sell error", f"{code}\n{e}"))
            _triggered.discard(code)  # 让下一轮重试
            return

        if not result.get("success"):
            err = result.get("message", "unknown")
            logger.error(f"[sl] sell rejected: {err}")
            await send_telegram(format_error("SL sell rejected", f"{code} qty={qty}\n{err}"))
            _triggered.discard(code)
            return

        try:
            position_mgr.on_close_filled(
                option_code=code,
                qty_sold=result.get("qty", qty),
                fill_price=result.get("price", limit),
                trigger_source="sl_polling",
                order_id=result.get("order_id"),
                note=f"SL: last={last_price:.2f} threshold={threshold:.2f} entry={pos['avg_entry_price']:.2f}",
            )
            # DB 已转 CLOSED —— 释放 code，同合约日后 reopen 时 SL 仍然有效
            _triggered.discard(code)
        except Exception as e:
            # 卖出成功但落库失败：DB 仍显示 OPEN。保留在 _triggered 里
            # 冻结该 code 的 SL，防止下轮对已卖出的仓位重复挂卖单。
            # 必须 TG 告警——冻结意味着该合约失去自动止损，且 DB 与 broker
            # 已脱钩，只写日志半夜没人看得到。
            logger.error(f"[sl] on_close_filled failed: {e}")
            await send_telegram(format_error(
                "SL 记账失败，已冻结该合约的 SL 自动触发",
                f"{code}: 卖单已提交（order={result.get('order_id')}）但 DB 更新失败。\n"
                f"请核对 moomoo 持仓并跑 scripts/sync_positions.py 对账，"
                f"然后重启 bot 恢复该合约的 SL。"
            ))

        # 卖单成交确认：SL 场景价格在跌，限价单挂不上很常见——未成交必须告警
        fill_checker.spawn(fill_checker.confirm_sell_fill(
            result.get("order_id") or "", code, result.get("qty", qty), "sl_polling",
        ))

    await send_telegram(format_close_filled(
        pos["symbol"], pos["strike"], pos["side"], pos["expiry"],
        result.get("qty", qty), result.get("price", limit),
        100, "sl_polling", result.get("order_id", "N/A"),
    ))


async def _sl_tick():
    """单轮检查。可独立测试。

    批量取价（7/8 改造）：之前每仓位一次 get_last_price = 一次 snapshot RTT，
    N 个仓位 × 5s tick 会打满 moomoo 60 次/30s 频率配额（连 validate 都被
    挤到限频）。现在整个 tick 只发一次 get_last_prices。
    """
    cfg = _cfg()
    positions = [
        p for p in position_mgr.get_open_positions()
        if p.get("apply_sl") and p["qty_remaining"] > 0
    ]
    if not positions:
        return

    codes = [p["option_code"] for p in positions]
    prices = await asyncio.to_thread(get_last_prices, codes)

    for pos in positions:
        last = prices.get(pos["option_code"])
        if last is None:
            continue
        threshold = pos["avg_entry_price"] * (1 - cfg["sl_pct"])
        if last <= threshold:
            await _trigger_sl(pos, last, threshold, cfg["sell_slip"])


async def run_sl_watcher():
    """后台主循环。在 start_listener 里 asyncio.create_task 启动。"""
    cfg = _cfg()
    logger.info(
        f"[sl] watcher started: pct={cfg['sl_pct']*100:.0f}% "
        f"interval={cfg['interval']}s sell_slip={cfg['sell_slip']*100:.0f}%"
    )
    while True:
        try:
            await _sl_tick()
        except Exception:
            logger.exception("[sl] tick error (continuing)")
        # 实时读 interval 让运行时调参生效
        await asyncio.sleep(_cfg()["interval"])
