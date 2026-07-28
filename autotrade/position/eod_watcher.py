"""EOD 强平守护：后台 asyncio task

职责：
- 每 EOD_CHECK_INTERVAL 秒检查 ET 时间
- 到 EOD_HOUR:EOD_MIN（默认 15:50 ET）后，平掉所有
  expiry==today_et 且 status IN (OPEN, PARTIAL) 的仓位
  （不看 eod_force_close flag —— 该 flag 只反映开仓时刻 DTE==0，
   周初开的 weekly 到周五到期时 flag 是 False，但同样必须在过期前平掉）
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
- 早收盘日（black friday / xmas eve）EOD_HOUR 应该是 12:50 而不是 15:50
  → 需要 holidays.py 加 EARLY_CLOSE_DATES set
"""
import asyncio
import os
from datetime import datetime, timezone, date as date_cls
from zoneinfo import ZoneInfo

from autotrade.broker.trade import place_sell_order
from autotrade.broker.quote import get_last_price
from autotrade.position import manager as position_mgr
from autotrade.position import fill_checker
from autotrade.notify.transport import send_telegram
from autotrade.notify.messages import format_close_filled, format_error
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


def _is_eod_window(now_et: datetime, hour: int, minute: int) -> bool:
    """是否在 EOD 强平时窗内（工作日 hour:minute ～ 16:05 ET）。

    TODO: 半日交易日（黑五 / 平安夜 / 独立日前夜 / 元旦前夜等）13:00 ET 收盘，
          应在 holidays.py 加 EARLY_CLOSE_DATES set，当日把 cutoff 调到 12:50、
          上界调到 13:05。实测一年才几次，不急；但漏掉那几天会变成"收盘后才挂卖单"。
    """
    if now_et.weekday() >= 5:  # 周六/日
        return False
    cutoff = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    window_end = now_et.replace(
        hour=_WINDOW_END_HOUR, minute=_WINDOW_END_MIN, second=0, microsecond=0
    )
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
    """单仓位强平。"""
    code = pos["option_code"]

    async with position_mgr.sell_lock(code):
        # 锁内重读：等锁期间可能已被 SL/TP/CLOSE 卖掉（部分或全部）
        pos = position_mgr.get(code) or pos
        if pos["status"] not in ("OPEN", "PARTIAL") or pos["qty_remaining"] <= 0:
            logger.debug(f"[eod] {code} already closed while waiting for lock, skip")
            return
        qty = pos["qty_remaining"]

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
                    f"{code} qty={qty} entry=${pos['avg_entry_price']:.2f}\n"
                    f"原因：OPRA 不可用，避免 entry × 0.9 自残卖\n"
                    f"收盘前每 30s 继续重试；若一直无报价请在 moomoo 手动平仓"
                ))
                # 裸 send_telegram 成功只记 debug,出过"告警到底发没发"说不清的账
                # (7/24 夜这条告警在日志里完全隐形)——安全关键路径把结果提到 INFO
                logger.info(f"[eod] no-quote TG {'sent' if ok else 'FAILED'} for {code}")
            return

        limit = max(0.01, round(last * (1 - sell_slip), 2))
        logger.warning(
            f"[eod] 🕒 force-close {code}: qty={qty} last={last:.2f} limit={limit}"
        )

        try:
            result = await asyncio.to_thread(
                place_sell_order,
                option_code=code, qty=qty,
                limit_price=limit, remark="eod_force",
            )
        except Exception as e:
            logger.exception("[eod] place_sell_order failed")
            _skip_until[code] = ts_now + 60  # 1 分钟后再试
            await send_telegram(format_error("EOD sell error", f"{code}\n{e}"))
            return

        if not result.get("success"):
            err = result.get("message", "unknown")
            logger.error(f"[eod] sell rejected: {err}")
            _skip_until[code] = ts_now + 60
            await send_telegram(format_error("EOD sell rejected", f"{code} qty={qty}\n{err}"))
            return

        try:
            position_mgr.on_close_filled(
                option_code=code,
                qty_sold=result.get("qty", qty),
                fill_price=result.get("price", limit),
                trigger_source="eod",
                order_id=result.get("order_id"),
                note=f"EOD force close (last={last:.2f})",
            )
        except Exception as e:
            logger.error(f"[eod] on_close_filled failed: {e}")

        # 卖单成交确认：收盘前 spread 跳水，限价卖单挂不上必须立刻知道
        fill_checker.spawn(fill_checker.confirm_sell_fill(
            result.get("order_id") or "", code, result.get("qty", qty), "eod",
        ))

    await send_telegram(format_close_filled(
        pos["symbol"], pos["strike"], pos["side"], pos["expiry"],
        result.get("qty", qty), result.get("price", limit),
        100, "eod", result.get("order_id", "N/A"),
    ))


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

    # 只看 expiry == today，不看 eod_force_close flag。
    #
    # 原因：eod_force_close 在**开仓时**由 categorize() 一次性算出（DTE==0 才 True），
    # 之后不再重算。周一买的 weekly 周五到期时，它的 flag 还是 False —— 老逻辑
    # 会让它在到期日直接过期（ITM 被自动行权，变成一笔没打算持有的正股/保证金头寸）。
    # "0DTE 当天必过期，必须平"这个理由对**任何**到期日当天的仓位都成立，
    # 所以这里用 expiry 本身判断。flag 保留在 DB 里仅作开仓时刻的信息性标注。
    positions = [
        p for p in position_mgr.get_open_positions()
        if p["expiry"] == today_iso
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
        except Exception:
            logger.exception("[eod] tick error (continuing)")
        await asyncio.sleep(_cfg()["interval"])
