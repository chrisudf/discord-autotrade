"""EOD 强平守护：后台 asyncio task

职责：
- 每 EOD_CHECK_INTERVAL 秒检查 ET 时间
- 到 EOD_HOUR:EOD_MIN（默认 15:50 ET）后，平掉 status IN (OPEN, PARTIAL) 且满足
  以下**任一**条件的仓位：
    a. expiry == today_et —— 今天到期，不平就是听任过期/被自动行权；
    b. eod_force_close == 1 —— 开仓时就定性为"当日了结"（DTE==0，或信号
       原文写了 day trade），与到期日无关。
  两个条件是独立来源，缺一不可：只看 (a) 会漏掉 8/3 那笔 4DTE 的 day_trade；
  只看 (b) 会漏掉周初开、周五到期的 weekly（开仓时 flag 就是 False）。
- 是工作日才执行（避免周末本地测试误触发）

设计：
- 幂等：每轮都筛 status IN (OPEN, PARTIAL)。成功平掉的会 → CLOSED 自然剔除，
  失败的会下一轮重试 —— 在 15:50–close 之间不停尝试，越接近收盘越要平掉
- 用 in-memory `_failed_codes` 加 backoff 避免单 code 每秒重试
- 不去重 trigger-once：依赖 status 转移做幂等
- 跨日：每天 0:00 ET 后 _failed_codes 自动清空（实现：日变更检测）

ET 时区用 ZoneInfo，自动处理 DST。

环境变量：
- EOD_HOUR             : 默认 15（ET）
- EOD_MIN              : 默认 50
- EOD_CHECK_INTERVAL   : 默认 30 秒
- EOD_SELL_SLIP        : 默认 0.10（比 SL 更激进，临近收盘 spread 更宽）

TODO（实测调整）：
- 15:50 时点是经验值，实测 fill 质量后调（过早 miss gamma / 过晚 spread 跳水）
- 0DTE 收盘前几分钟可能完全卖不动（ITM 容易出，OTM 几乎归零）→
  要不要 OTM 直接放弃挂单、自动归零
- 把"今日是否已强平过"持久化到 DB，跨重启更稳

[0017] 早收盘日（black friday / xmas eve / 独立日前一天）已支持：
holidays.EARLY_CLOSE_DATES + _is_eod_window 内整体前移 3h（12:50~13:05）。
"""
import asyncio
import os
from datetime import datetime, timezone, date as date_cls
from zoneinfo import ZoneInfo

from autotrade.broker.trade import place_sell_order
from autotrade.broker.quote import get_last_price
from autotrade.parsing.holidays import is_early_close
from autotrade.position import manager as position_mgr
from autotrade.position import fill_checker
from autotrade.position.sell_executor import Outcome, SellPlan, SkipSell, execute_sell
from autotrade.notify.transport import send_telegram
# format_close_filled 已随成交 TG 收进 sell_executor（0015），此处只剩错误文案
from autotrade.notify.messages import format_error
from autotrade.notify.watchdog import notify_tick_error, notify_tick_ok
from autotrade.utils.logger import logger

ET_TZ = ZoneInfo("America/New_York")


def _cfg() -> dict:
    return {
        "hour": int(os.getenv("EOD_HOUR", "15")),
        "minute": int(os.getenv("EOD_MIN", "50")),
        "interval": int(os.getenv("EOD_CHECK_INTERVAL", "30")),
        "sell_slip": float(os.getenv("EOD_SELL_SLIP", "0.10")),
    }


# 时窗上界：16:00 ET 收盘 + 5 分钟迟到成交余量。过点后合约命运已定
# （到期日的过不了夜），继续尝试/告警纯属噪音——7/15 对已到期的 GOOGL/MU
# 每 30 分钟"手动平仓"告警到 18:50 ET，直到人工 Ctrl-C。
_WINDOW_END_HOUR, _WINDOW_END_MIN = 16, 5

# [0017] 半日市（黑五/平安夜/独立日前一天）13:00 ET 收盘，EOD 各时点整体
# 前移 3 小时：默认 cutoff 15:50→12:50、上界 16:05→13:05（正好落在原 TODO
# 指定的时点）。用"平移收盘差 3h"而不是硬编码 12:50，是为了保住
# env EOD_HOUR/EOD_MIN 的真实语义——"收盘前 N 分钟"——在半日市同样成立
# （比如有人调到 15:30 求更早强平，半日市自动变 12:30，而不是回退默认）。
# 不修的后果是钱路事故：半日市当天 15:50 才进窗 = 收盘后 2.5h 才开始挂单，
# 当日到期仓位 100% 过期——AVGO 415C（7/23-24 夜）+50% 拿到归零的同款结局，
# 只是触发原因从"窗口内无报价"换成"整个窗口都错过"。
_EARLY_CLOSE_SHIFT_HOURS = 3


# 无效配置**关窗**，不扩窗（PR#2 review）。
# 老写法是 max(hour - shift, 0)，注释写的是"宁可窗口从 0 点开也不能让 tick 炸掉"——
# 但那是个假二选一：第三条路（关窗 + 告警）既不抛异常也不开大窗。
# 扩窗的具体后果：EOD_HOUR 被配成 <3 时，半日市当天窗口变成 00:50～13:05，
# 十二个小时里每 30s 一轮，对**所有**当日到期/day_trade 仓位真下卖单。
# EOD 是唯一会主动清仓的 watcher，误配的代价是钱不是噪音，必须 fail closed。
#
# 三类无效，处理一致（return False + 每种配置只告警一次；本函数每 30s 被调用）：
#   a. hour/minute 越界        —— replace() 会抛 ValueError，老代码只挡了 hour<0；
#   b. hour - shift < 0        —— 前移后落到当天 0 点之前，没有交易含义；
#   c. cutoff > window_end     —— 窗口是空集，EOD **静默失效**。老代码这里也返回
#      False，但一声不吭 = 风控悄悄下线，比 (a)(b) 更隐蔽。
_bad_window_warned: set = set()


def _warn_bad_window_once(key: tuple, msg: str) -> None:
    if key in _bad_window_warned:
        return
    _bad_window_warned.add(key)
    logger.error(msg)


def _is_eod_window(now_et: datetime, hour: int, minute: int) -> bool:
    """是否在 EOD 强平时窗内。

    全日市：工作日 hour:minute ～ 16:05 ET。
    半日市（holidays.is_early_close）：整体前移 3h → 默认 12:50 ～ 13:05 ET。
    配置无效时返回 False 并告警（见上方注释），绝不扩大时窗。
    """
    if now_et.weekday() >= 5:  # 周六/日
        return False
    shift = _EARLY_CLOSE_SHIFT_HOURS if is_early_close(now_et.date()) else 0
    cutoff_hour = hour - shift

    if not (0 <= cutoff_hour <= 23) or not (0 <= minute <= 59):
        _warn_bad_window_once(
            ("range", hour, minute, shift),
            f"[eod] 配置无效：EOD_HOUR={hour} EOD_MIN={minute}"
            f"（半日市前移 {shift}h 后为 {cutoff_hour}:{minute}）"
            f"——本轮不进 EOD 时窗，强平关闭。请修 .env 后重启",
        )
        return False

    cutoff = now_et.replace(hour=cutoff_hour, minute=minute, second=0, microsecond=0)
    window_end = now_et.replace(
        hour=_WINDOW_END_HOUR - shift, minute=_WINDOW_END_MIN, second=0, microsecond=0
    )
    if cutoff > window_end:
        _warn_bad_window_once(
            ("empty", hour, minute, shift),
            f"[eod] 配置无效：cutoff {cutoff:%H:%M} 晚于窗口上界 "
            f"{window_end:%H:%M}（EOD_HOUR={hour} EOD_MIN={minute} shift={shift}h）"
            f"——时窗为空，EOD 强平永不触发",
        )
        return False

    return cutoff <= now_et <= window_end


# 单进程 backoff：连续失败的 code → 下次 tick 跳过
# {option_code: 下次允许重试的 epoch 秒}
_skip_until: dict[str, float] = {}
_skip_until_date: date_cls = None  # 跨日清空

# [7/25 事故] no-quote 的 TG 告警节流,与重试**分离**。
# 原实现把 _skip_until 同时当"防 TG 刷屏"和"防重试"用(no-quote 一次就
# backoff 1800s)——7/24 AVGO 415C 到期日:15:50-15:55 窗口被僵尸连接吃掉,
# 15:55 首试 no-quote 后下次重试排到 16:25,早过 16:05 窗口上限,
# 整个到期日强平只有一次机会,仓位直接过期。
# 现在:no-quote 每个 tick(30s)都重试(临近收盘迟到的报价还能接住),
# 只有 TG 按 30min/code 节流。broker 异常/拒单仍走 60s _skip_until。
_alerted_until: dict[str, float] = {}


def _gc_skip(today_et: date_cls):
    """跨日清空 skip/alert set，避免昨天的失败影响今天。"""
    global _skip_until_date, _skip_until
    if _skip_until_date != today_et:
        _skip_until.clear()
        _alerted_until.clear()
        _skip_until_date = today_et


async def _force_close(pos: dict, sell_slip: float, ts_now: float):
    """单仓位强平。

    [0015] 执行骨架合并进 sell_executor.execute_sell；EOD 专属差异保留在钩子：
      - **锁内取价**（plan_fn 在锁内执行）：报价与下单在同一把锁的同一视图下，
        等锁期间被 CLOSE/SL 卖掉的仓位不会再吃一次 snapshot 配额
      - 0007 的 no-quote 重试/告警分离：无报价每 tick(30s) 照常重试
        （迟到的报价还能接住），只有 TG 按 30min/code 节流（_alerted_until）；
        broker 异常/拒单仍走 60s _skip_until backoff
      - 10% 卖出 slip（临近收盘 spread 跳水，比 SL 更激进）
    """
    code = pos["option_code"]

    async def _plan(fresh: dict):
        """锁内决策：先取价（无报价拒绝 entry-fallback 自残卖），再定限价。"""
        qty = fresh["qty_remaining"]
        last = await asyncio.to_thread(get_last_price, code)
        if last is None:
            # 没 quote 时不挂 entry-based 卖单——0DTE ITM 会被自残卖在远低于真实市价。
            # [7/25 事故] 不再进重试 backoff(见 _alerted_until 注释):下个 tick
            # 继续试,迟到的报价还能接住;只有 TG 按 30min 节流。
            if _alerted_until.get(code, 0) <= ts_now:
                _alerted_until[code] = ts_now + 1800
                logger.warning(
                    f"[eod] no quote for {code}, refusing entry-fallback sell, "
                    f"manual close required (每 tick 重试中,TG 30min 一次)"
                )
                ok = await send_telegram(format_error(
                    "EOD 强平跳过：无报价",
                    f"{code} qty={qty} entry=${fresh['avg_entry_price']:.2f}\n"
                    f"原因：OPRA 不可用，避免 entry × 0.9 自残卖\n"
                    f"收盘前每 30s 继续重试；若一直无报价请在 moomoo 手动平仓"
                ))
                # 裸 send_telegram 成功只记 debug,出过"告警到底发没发"说不清的账
                # (7/24 夜这条告警在日志里完全隐形)——安全关键路径把结果提到 INFO
                logger.info(f"[eod] no-quote TG {'sent' if ok else 'FAILED'} for {code}")
                return SkipSell(Outcome.SKIPPED_NOTIFIED)
            return SkipSell(Outcome.SKIPPED_SILENT)

        limit = max(0.01, round(last * (1 - sell_slip), 2))
        logger.warning(
            f"[eod] 🕒 force-close {code}: qty={qty} last={last:.2f} limit={limit}"
        )
        return SellPlan(
            qty=qty, limit=limit, remark="eod_force", notify_pct=100,
            note=f"EOD force close (last={last:.2f})",
        )

    def _already_closed(fresh: dict):
        logger.debug(f"[eod] {code} already closed while waiting for lock, skip")

    async def _sell_error(e: Exception, plan):
        logger.exception("[eod] place_sell_order failed")
        _skip_until[code] = ts_now + 60  # 1 分钟后再试
        await send_telegram(format_error("EOD sell error", f"{code}\n{e}"))

    async def _sell_rejected(result: dict, plan):
        err = result.get("message", "unknown")
        logger.error(f"[eod] sell rejected: {err}")
        _skip_until[code] = ts_now + 60
        await send_telegram(format_error(
            "EOD sell rejected", f"{code} qty={plan.qty}\n{err}"))

    async def _record_failure(e: Exception, result: dict):
        logger.error(f"[eod] on_close_filled failed: {e}")

    await execute_sell(
        pos,
        trigger_source="eod",
        notify_trigger="eod",
        plan_fn=_plan,
        place_sell_order=place_sell_order,
        notify=send_telegram,
        # 卖单成交确认：收盘前 spread 跳水，限价卖单挂不上必须立刻知道
        fill_confirm=lambda order_id, qty_sold: fill_checker.spawn(
            fill_checker.confirm_sell_fill(order_id, code, qty_sold, "eod")),
        on_already_closed=_already_closed,
        on_sell_error=_sell_error,
        on_sell_rejected=_sell_rejected,
        on_record_failure=_record_failure,
    )


async def sweep_expired_and_notify() -> list[dict]:
    """过期仓位清扫 + TG 通知。幂等（清过的 status=EXPIRED 不会再选中）。

    listener 启动时调一次（watchers 起来之前），之后 eod watcher 每轮
    tick 兜底跨日。TG 用 parse_mode=None 免转义。
    """
    swept = position_mgr.sweep_expired()
    if not swept:
        return swept
    lines = "\n".join(
        f"  • {p['option_code']} x{p['qty_remaining']} "
        f"(entry ${p['avg_entry_price']:.2f}, expired {p['expiry']})"
        for p in swept
    )
    try:
        await send_telegram(
            f"🧹 过期仓位清扫\n"
            f"{len(swept)} 张合约已过期未平仓，标记 EXPIRED（移出 watcher 轮询"
            f"和 close 白名单）：\n{lines}\n"
            f"ITM 可能已被自动行权，请核对 moomoo 持仓"
            f"（必要时跑 scripts/sync_positions.py）",
            parse_mode=None,
        )
    except Exception as e:
        logger.warning(f"[eod] expiry sweep TG notify failed: {e}")
    return swept


async def _eod_tick(now_et: datetime):
    """单轮：判断是否在 EOD 时窗、找待平仓位、依次强平。"""
    cfg = _cfg()
    # 过期清扫放时窗判断之前——凌晨跨日后就要清，不能等到 15:50
    await sweep_expired_and_notify()
    if not _is_eod_window(now_et, cfg["hour"], cfg["minute"]):
        return

    today_iso = now_et.date().isoformat()
    _gc_skip(now_et.date())
    ts_now = now_et.timestamp()

    # 两个独立入选条件（见模块 docstring）：
    #
    # a. expiry == today —— 这一条当初是**替换** flag 加进来的，因为
    #    eod_force_close 在开仓时一次性算出、之后不重算：周一买的 weekly
    #    到周五到期时 flag 仍是 False，只看 flag 会让它直接过期
    #    （ITM 被自动行权，变成一笔没打算持有的正股/保证金头寸）。
    # b. eod_force_close —— 8/3 实测：替换掉 flag 之后，这个字段就**没有任何
    #    行为消费者了**（全仓库只剩写入点）。于是 day_trade → eod_force 的
    #    修复写进 DB 却什么都没发生，正是 lessons #22 在低一层的复现。
    #    day_trade 是"当日了结"的显式声明，与到期日无关，必须自己有一条入选路径。
    #
    # 幂等性不受影响：仍靠 status 转移（成功平掉 → CLOSED 自然剔除）。
    # flag 是持久的，所以万一 15:50 之后才启动/重试失败，次日 15:50 会继续尝试——
    # 对一笔本该当日了结的仓位，这就是想要的行为。
    positions = [
        p for p in position_mgr.get_open_positions()
        if (p["expiry"] == today_iso or p.get("eod_force_close"))
        and p["qty_remaining"] > 0
        and _skip_until.get(p["option_code"], 0) <= ts_now
    ]
    if not positions:
        return

    logger.info(
        f"[eod] in window @ {now_et.strftime('%H:%M')} ET, "
        f"closing {len(positions)} position(s)"
    )
    for pos in positions:
        await _force_close(pos, cfg["sell_slip"], ts_now)


async def run_eod_watcher():
    """后台主循环。"""
    cfg = _cfg()
    logger.info(
        f"[eod] watcher started: cutoff={cfg['hour']:02d}:{cfg['minute']:02d} ET "
        f"interval={cfg['interval']}s sell_slip={cfg['sell_slip']*100:.0f}%"
    )
    while True:
        try:
            now_et = datetime.now(timezone.utc).astimezone(ET_TZ)
            await _eod_tick(now_et)
            notify_tick_ok("eod")
        except Exception as e:
            notify_tick_error("eod", e)
        await asyncio.sleep(_cfg()["interval"])
