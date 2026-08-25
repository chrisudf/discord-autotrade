"""止损守护：后台 asyncio task

职责：
- 每 POLL_INTERVAL 秒一轮
- 扫所有 apply_sl=True 的活跃仓位（全局 STOP_LOSS_PCT 档）
- 另扫 category in ("lotto", "0dte_lotto") 的活跃仓位（0013 硬底档，
  LOTTO_STOP_LOSS_PCT>0 时启用）——同一条阈值/冻结/卖出代码，只是 pct 来源分档
- 拿 broker.get_last_price，比对 avg_entry * (1 - 对应档位 pct)
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
- STOP_LOSS_PCT       : 默认 0.50（亏 50% 触发；**小数**）
- LOTTO_STOP_LOSS_PCT : 代码缺省 "0"=关 [ship-dark]，生产模板给 80；**整数百分比**
                        （80 = 亏 80% 触发，与 STOP_LOSS_PCT 单位不同，勿混）。
                        只作用于 category in ("lotto", "0dte_lotto") 的仓位。
- SL_POLL_INTERVAL    : 默认 5 秒
- SL_SELL_SLIP        : 默认 0.08（卖出限价相对当前价的下偏移，确保成交）

TODO（实测调整）：
- 50% 阈值经验值，看真实 fill 数据后可能要按 category 分档（weekly 紧一点；
  0013 已给 lotto 档加 -80% 硬底，weekly 细分仍待实测）
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
from autotrade.position import retry_guard
from autotrade.position.sell_executor import Outcome, SellPlan, execute_sell
from autotrade.notify.transport import send_telegram
# format_close_filled 已随成交 TG 收进 sell_executor（0015），此处只剩错误文案
from autotrade.notify.messages import format_error
from autotrade.notify.watchdog import notify_tick_error, notify_tick_ok
from autotrade.utils import logdedup
from autotrade.utils.envcfg import env_int
from autotrade.utils.logger import logger


def _cfg() -> dict:
    """每次调用重读 os.environ（方便测试 monkeypatch）。

    注意：这**不是** .env 热更新——load_dotenv 只在 import 时跑一次，
    运行中编辑 config/.env 不会生效，改配置需要重启进程。
    """
    return {
        "sl_pct": float(os.getenv("STOP_LOSS_PCT", "0.50")),
        # 0013 lotto 硬底：env 是整数百分比（80 = -80%），这里换算成小数与
        # sl_pct 同单位。<=0 = 关闭（完全不选 lotto 仓位，行为与 0013 之前逐字一致）。
        # 契约偏差说明：BATCH-CONTRACT WP-D 要求代码缺省 "80"，但既有测试
        # test_watchers.py::test_sl_skips_apply_sl_false 断言 lotto 仓位 -95%
        # 也不触发（铁律 3 禁改既有断言，且该文件不属于本 WP）——按铁律 2 的
        # 兜底路径改为缺省关 [ship-dark]，生产模板 config/.env.example 给 80，
        # 复制模板部署即得到契约意图的 -80% 硬底。
        # 整数百分比读走 envcfg（写坏了告警一次退默认，不崩 SL watcher）；
        # 0=关，故 minimum=0。
        "lotto_pct": env_int("LOTTO_STOP_LOSS_PCT", 0, minimum=0) / 100.0,
        "interval": int(os.getenv("SL_POLL_INTERVAL", "5")),
        "sell_slip": float(os.getenv("SL_SELL_SLIP", "0.08")),
    }


# 0013：吃 lotto 硬底的类目（policy/positions.categorize 的 TODO "max_loss_pct
# (比如 -80% 硬底)" 在 watcher 侧兑现——categorize 的 apply_sl 语义不动，
# lotto/0dte_lotto 依旧 apply_sl=False，分档在这里做）。
# 实际案例：AVGO 415C（7/23-24 夜）单张 lotto 从 +50% 一路拿到过期归零——
# runner-preserve 挡掉了 KC 的 trim，KC 又没发 100% close，最后整仓归零。
# 放飞哲学保留：lotto 照旧不挂 TP、不吃全局 SL、放到 expiry；-80% 只是
# "残值回收"（接近归零时把最后一点权利金抢回来），不是止损策略变更。
LOTTO_CATEGORIES = ("lotto", "0dte_lotto")


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
    """对单个仓位触发止损全平。

    [0015] 执行骨架（锁→重读→防护→下单→记账→fill_confirm→TG）合并进
    sell_executor.execute_sell；SL 专属差异全部保留在本函数的钩子里：
      - _triggered 冻结语义（卖出成功但落库失败 → 冻结该合约的 SL，
        人工对账后重启恢复；其余失败路径 discard 让下轮重试）
      - 8% 激进卖出 slip（价格在跌，限价必须够深才追得到 fill）
      - broker 注入用**本模块命名空间的裸名**（place_sell_order /
        send_telegram）——既有测试 patch 在 sl_watcher.* 上，注入点不能挪。
    """
    code = pos["option_code"]
    if code in _triggered:
        logger.debug(f"[sl] already triggered this run: {code}")
        return
    _triggered.add(code)

    # [8/13] 拒单熔断：SL 的失败路径全部 discard 让下轮重试，而 tick 是 5s 一轮
    # ——跟 TP 一模一样的硬打形状，只是那晚先炸的是 TP。退避期/熔断后直接返回，
    # 不打日志（每 5s 一条跳过日志就是把刷屏换个措辞，见 tp_watcher 同处注释）。
    guard = f"sl:{code}"
    blocked_reason = retry_guard.blocked(guard)
    if blocked_reason is not None:
        logger.debug(f"[sl] {code} 跳过：{blocked_reason}")
        _triggered.discard(code)  # 没有卖出发生，维持 set 的不变式
        return

    async def _plan(fresh: dict):
        """锁内决策：SL 全平剩余，限价 = last × (1-slip)，0.01 兜底。"""
        qty = fresh["qty_remaining"]
        limit = round(last_price * (1 - sell_slip), 2)
        if limit <= 0:
            # 极低价兜底——0.01 起挂
            limit = 0.01
        logger.warning(
            f"[sl] 🛑 TRIGGER {code}: last={last_price:.2f} <= threshold={threshold:.2f} "
            f"(entry={fresh['avg_entry_price']:.2f}), selling {qty} @ {limit}"
        )
        return SellPlan(
            qty=qty, limit=limit, remark="sl_polling", notify_pct=100,
            note=(
                f"SL: last={last_price:.2f} threshold={threshold:.2f} "
                f"entry={fresh['avg_entry_price']:.2f}"
            ),
        )

    def _already_closed(fresh: dict):
        logger.debug(f"[sl] {code} already closed while waiting for lock, skip")
        _triggered.discard(code)  # 没有卖出发生，维持 set 只含"已卖未落库"的不变式

    async def _on_failed_attempt(title: str, err: str, qty_desc: str):
        """拒单/异常共用收尾：熔断登记 + 收敛日志 + 按需 TG，最后 discard 让下轮重试。

        熔断了也照样 discard —— 挡住下一轮的是 blocked()，不是 _triggered。
        那个 set 的不变式（只含"已卖未落库"）必须保持干净，否则同 code 日后
        reopen 时 SL 会永久失效。
        """
        d = retry_guard.on_reject(guard, err)

        level = "ERROR" if (d.tripped or d.fails == 1) else "WARNING"
        if d.tripped:
            tail = ("确定性拒单，重试无意义" if d.deterministic
                    else f"连续 {d.fails} 次失败")
            logger.log(level, f"[sl] ⛔ {code} 熔断（{tail}）：{err}")
        else:
            logdedup.log_throttled(
                f"sl-reject:{guard}",
                f"[sl] sell rejected: {err} → 第 {d.fails} 次，退避 {d.retry_after:.0f}s",
                level=level,
            )

        if d.alert:
            suffix = (f"\n（上次告警以来另有 {d.suppressed} 次同类失败未单独告警）"
                      if d.suppressed else "")
            if d.tripped:
                # SL 熔断比 TP 熔断严重一档：这张仓从此没有自动止损。
                # 措辞按最坏情况写，别让人在半夜把它当成一条普通拒单划走。
                hint = (
                    "\n\n⛔ **本合约的 SL 已熔断，本进程内不再重试 —— "
                    "它现在没有自动止损**（重启即恢复）。"
                )
                if d.deterministic:
                    hint += (
                        "\n若是 naked-short 拒单，说明 broker 侧已经没有这张仓，"
                        "本地 DB 陈旧：跑 "
                        "`python -m autotrade.ops.sync_positions --dry-run` 对账。"
                    )
                hint += "\n否则请立刻在 moomoo 手动处理。"
            else:
                hint = f"\n\n将在 {d.retry_after:.0f}s 后重试（连续第 {d.fails} 次失败）。"
            await send_telegram(format_error(title, f"{code} {qty_desc}\n{err}{suffix}{hint}"))

        _triggered.discard(code)  # 让下一轮重试（能不能真重试由 blocked() 说了算）

    async def _sell_error(e: Exception, plan):
        logger.exception("[sl] place_sell_order failed")
        await _on_failed_attempt("SL sell error", f"{type(e).__name__}: {e}",
                                 f"qty={plan.qty}")

    async def _sell_rejected(result: dict, plan):
        await _on_failed_attempt("SL sell rejected",
                                 result.get("message", "unknown"),
                                 f"qty={plan.qty}")

    # 卖出成功但落库失败：DB 仍显示 OPEN。保留在 _triggered 里
    # 冻结该 code 的 SL，防止下轮对已卖出的仓位重复挂卖单。
    # 必须 TG 告警——冻结意味着该合约失去自动止损，且 DB 与 broker
    # 已脱钩，只写日志半夜没人看得到。
    async def _record_failure(e: Exception, result: dict):
        logger.error(f"[sl] on_close_filled failed: {e}")
        await send_telegram(format_error(
            "SL 记账失败，已冻结该合约的 SL 自动触发",
            f"{code}: 卖单已提交（order={result.get('order_id')}）但 DB 更新失败。\n"
            f"请核对 moomoo 持仓并跑 scripts/sync_positions.py 对账，"
            f"然后重启 bot 恢复该合约的 SL。"
        ))

    outcome, _ = await execute_sell(
        pos,
        trigger_source="sl_polling",
        notify_trigger="sl_polling",
        plan_fn=_plan,
        place_sell_order=place_sell_order,
        notify=send_telegram,
        # 卖单成交确认：SL 场景价格在跌，限价单挂不上很常见——未成交必须告警
        fill_confirm=lambda order_id, qty_sold: fill_checker.spawn(
            fill_checker.confirm_sell_fill(order_id, code, qty_sold, "sl_polling")),
        on_already_closed=_already_closed,
        on_sell_error=_sell_error,
        on_sell_rejected=_sell_rejected,
        # DB 已转 CLOSED —— 释放 code，同合约日后 reopen 时 SL 仍然有效
        on_record_success=lambda: _triggered.discard(code),
        on_record_failure=_record_failure,
    )

    if outcome is Outcome.SOLD:
        cleared = retry_guard.on_success(guard)
        if cleared:
            logger.info(f"[sl] ✅ {code} 卖出成功，清除 {cleared} 次连续失败的退避状态")
        logdedup.flush(f"sl-reject:{guard}")


async def _sl_tick():
    """单轮检查。可独立测试。

    批量取价（7/8 改造）：之前每仓位一次 get_last_price = 一次 snapshot RTT，
    N 个仓位 × 5s tick 会打满 moomoo 60 次/30s 频率配额（连 validate 都被
    挤到限频）。现在整个 tick 只发一次 get_last_prices。
    """
    cfg = _cfg()
    # 0013 分档选仓：apply_sl 仓位照旧吃全局 pct（行为逐字不变，且优先于
    # lotto 档——categorize 正常产物里两者互斥，手工改库出现重叠时按更紧的
    # 全局档处理，宁早不晚）；另加 lotto/0dte_lotto 仓位吃专属硬底 pct。
    # env<=0 时 lotto 仓位完全不进 watch 列表——连报价都不取，不占
    # moomoo snapshot 配额（见下方 7/8 批量取价注释）。
    watch: list[tuple[dict, float]] = []
    for p in position_mgr.get_open_positions():
        if p["qty_remaining"] <= 0:
            continue
        if p.get("apply_sl"):
            watch.append((p, cfg["sl_pct"]))
        elif cfg["lotto_pct"] > 0 and p.get("category") in LOTTO_CATEGORIES:
            watch.append((p, cfg["lotto_pct"]))
    if not watch:
        return

    codes = [p["option_code"] for p, _ in watch]
    prices = await asyncio.to_thread(get_last_prices, codes)

    for pos, pct in watch:
        last = prices.get(pos["option_code"])
        if last is None:
            continue
        # 阈值判断/冻结语义/卖出路径与全局 SL 完全同一条代码（_trigger_sl），
        # 分档只体现在 pct 来源——这是 0013 的硬要求，避免第二条卖出路径。
        threshold = pos["avg_entry_price"] * (1 - pct)
        if last <= threshold:
            await _trigger_sl(pos, last, threshold, cfg["sell_slip"])


async def run_sl_watcher():
    """后台主循环。在 start_listener 里 asyncio.create_task 启动。"""
    cfg = _cfg()
    lotto_desc = f"{cfg['lotto_pct']*100:.0f}%" if cfg["lotto_pct"] > 0 else "off"
    logger.info(
        f"[sl] watcher started: pct={cfg['sl_pct']*100:.0f}% "
        f"lotto_floor={lotto_desc} "
        f"interval={cfg['interval']}s sell_slip={cfg['sell_slip']*100:.0f}%"
    )
    while True:
        try:
            await _sl_tick()
            notify_tick_ok("sl")
        except Exception as e:
            # [7/31] 只 logger.exception 不够：磁盘写满那晚日志本身就写不进去，
            # 风控三路全灭却零告警。改走 watchdog（落日志 + 节流 TG）
            notify_tick_error("sl", e)
        # 实时读 interval 让运行时调参生效
        await asyncio.sleep(_cfg()["interval"])
