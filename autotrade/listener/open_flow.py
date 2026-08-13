"""OPEN 信号编排（拆自 src/listener/discord_client.py _handle_message_inner 的
"---- 解析信号 ----" 起后半段，逐字搬运）。

parse → parse-fail 三级 triage（addon / twin 抑制 / open-attempt / sized-entry，
含节流）→ intentional skip → multi-signal 防御 → fingerprint dedup → DTE guard →
signal alert → _order_flow_lock 内 check_order / place_order / log_order /
record_order → _record_recent_exec → position_mgr.on_order_filled →
fill_checker.spawn → 成交通知 + 延迟统计。
"""
import asyncio
from datetime import datetime, timezone

from autotrade.broker.trade import place_order
from autotrade.listener.dedup import (
    _ADDON_ALERT_WINDOW,
    _ALERT_THROTTLE_MAX,
    _addon_alerted,
    _is_duplicate_signal,
    _sized_entry_alerted,
    _sweep_expired,
    parser_skip_should_alert,
    stale_open_should_alert,
)
from autotrade.listener.heuristics import (
    _TWIN_SUPPRESS_WINDOW,
    _looks_like_addon_attempt,
    _looks_like_open_attempt,
    _looks_like_sized_entry,
    _open_attempt_symbol,
    _record_recent_exec,
    _twin_of_recent_exec,
)
from autotrade.notify.messages import (
    format_addon_alert,
    format_error,
    format_order_filled,
    format_risk_blocked,
    format_signal_alert,
)
from autotrade.notify.transport import _safe_notify, notify_bg
from autotrade.parsing.signal_parser import parse_signal
from autotrade.policy.guards import _suspicious_long_dte
from autotrade.policy.pricing import breakeven_exit_price, calc_limit_price
from autotrade.position import fill_checker
from autotrade.position import manager as position_mgr
from autotrade.risk import check_order, record_order
from autotrade.storage.logger_db import log_order
from autotrade.utils.envcfg import env_float
from autotrade.utils.logger import logger

# OPEN 链路串行锁：check_order → place_order → record_order 必须原子，
# 否则两条几乎同时到达的信号都会用"旧配额"通过风控（见 handle_message 内注释）
_order_flow_lock = asyncio.Lock()


async def process_open(message, raw, cfg, cid, t0, msg_date_et):
    """OPEN 编排。router._handle_message_inner 在 detect_action != CLOSE 时调用。

    参数即老 _handle_message_inner 走到 "---- 解析信号 ----" 时的局部状态：
    message / raw / cfg / cid / t0，外加 router 侧提取的 msg_date_et
    （ET 日期，见 router._extract_et_date 与 [改动 Bug C]）。
    """
    # ---- 解析信号 ----
    signal = parse_signal(raw, msg_ts=msg_date_et)

    # === [改动 Bug B] 区分 intentional skip vs 真·解析失败 ===
    if signal is None:
        # 真·没匹配任何模式。只对"含 $TICKER + 侧别 + 价格"三件套的发 TG，
        # 否则视为 KC 状态评论/行情解说，仅 log（避免每晚 10+ 条 TG 噪音，见 6/24 review）
        logger.warning("Parse failed")
        # 先查 add-on（更具体）：加已持仓标的 + @price → 提醒人工跟加（7/6 SPY 漏加实锤）
        addon_sym = _looks_like_addon_attempt(raw)
        if addon_sym:
            now = datetime.now(timezone.utc)
            # [refactor-change](d) 告警节流 dict 惰性 GC（有界），与其它 registry 一致
            _sweep_expired(_addon_alerted, now, _ADDON_ALERT_WINDOW, cap=_ALERT_THROTTLE_MAX)
            prev = _addon_alerted.get(addon_sym)
            if prev is None or now - prev > _ADDON_ALERT_WINDOW:
                _addon_alerted[addon_sym] = now
                await _safe_notify(format_addon_alert(addon_sym, raw))
            else:
                logger.info(
                    f"🔁 addon alert dedup: {addon_sym} "
                    f"(prev {(now - prev).total_seconds():.0f}s ago)"
                )
        elif _looks_like_open_attempt(raw):
            twin_sym = _twin_of_recent_exec(raw, cid)
            if twin_sym:
                logger.info(
                    f"🔁 parse-fail alert suppressed: {twin_sym} executed from "
                    f"this channel <{_TWIN_SUPPRESS_WINDOW.total_seconds():.0f}s ago "
                    f"(likely ZH/EN twin)"
                )
            else:
                await _safe_notify(format_error("Parse failed (looks like signal)", raw))
        else:
            sized_sym = _looks_like_sized_entry(raw)
            if sized_sym:
                now = datetime.now(timezone.utc)
                # [refactor-change](d) 同上：sized-entry 节流 dict 惰性 GC（有界）
                _sweep_expired(_sized_entry_alerted, now, _ADDON_ALERT_WINDOW, cap=_ALERT_THROTTLE_MAX)
                prev = _sized_entry_alerted.get(sized_sym)
                if prev is None or now - prev > _ADDON_ALERT_WINDOW:
                    _sized_entry_alerted[sized_sym] = now
                    await _safe_notify(format_error(
                        "疑似入场信号（无 C/P 方向，未自动下单）", raw
                    ))
                else:
                    logger.info(
                        f"🔁 sized-entry alert dedup: {sized_sym} "
                        f"(prev {(now - prev).total_seconds():.0f}s ago)"
                    )
        return

    if signal.get("skip"):
        # parser 主动 skip（holding / price_range / no_price），绝大多数是正常过滤。
        #
        # [8/10 DELL] 但 pre-filter 也会误伤真信号："$DELL - weekly - $3.50 - $530
        # calls" 被 price_range 当成喊价区间吞掉，中英两条全跳，**一条 TG 都没发**
        # （这里原本只有 logger.debug）——人工零补救机会。parser 侧已修那一族写法
        # （见 signal_parser._INVERTED_PRICE_STRIKE_RE），但那只修好**已知**的一种；
        # 这里是安全网：下次某个 pre-filter 误伤没见过的写法时，至少有人能在几分钟内
        # 手工补单。
        #
        # 噪音闸门用与 parse-fail triage 同一套三件套启发式（$TICKER + calls/puts
        # + 喊价），闲聊/持仓状态贴发不出来——8/10 整晚 9 条语义消息里只有 DELL
        # 那条会命中。中英孪生 + 编辑重发靠 (原因, symbol) 节流收敛成 1 条。
        reason = signal["skip"]
        skip_sym = _open_attempt_symbol(raw)
        if not skip_sym:
            logger.debug(f"Parser intentional skip: {reason}")
        elif parser_skip_should_alert(f"{reason}:{skip_sym}"):
            logger.warning(
                f"Parser intentional skip on signal-shaped text: {reason} | {skip_sym}"
            )
            await _safe_notify(format_error(
                f"疑似信号被 pre-filter 跳过（{reason}，未自动下单）", raw
            ))
        else:
            logger.info(f"🔁 parser-skip alert dedup: {reason}:{skip_sym}")
        return

    # ---- 多信号（防御层，parser 当前不返 list） ----
    # TODO: parser 已重写为只返回 dict|None，此分支当前不可达。
    # 保留作为防御层；若未来 parser 改回支持多信号 list，此处自动生效。
    # 触达后请确认是否还要 Telegram 告警（用户目前规则：不做多腿）。
    if isinstance(signal, list):
        all_signals = signal
        signal = all_signals[0]
        logger.info(
            f"Multi-signal ({len(all_signals)}), taking first: "
            f"{signal['symbol']} {signal['strike']}{signal['side'][0]}"
        )
        await _safe_notify(
            f"⚠️ Multi-signal ({len(all_signals)} contracts), taking first only:\n"
            + "\n".join(
                f"  {i+1}. {s['symbol']} {s['strike']}{s['side'][0]} "
                f"{s['expiry']} @ ${s['price']}"
                for i, s in enumerate(all_signals)
            )
        )

    # ---- 信号年龄闸门(7/28)----
    # 睡眠回补/启动回补会重放几分钟~几十分钟前的消息:CLOSE 迟到也该执行
    # (还持着就想平,走 close_flow 不经此处),OPEN 迟到不能自动追——
    # 限价锚在陈旧喊价上,价格早走了(追上=没成交,跌破=接飞刀)。
    # 超龄 OPEN 降级为 TG 告警,人工决定追不追。FakeMessage 无 created_at
    # → 视为实时,不拦。
    #
    # 必须放在指纹去重**之前**:_is_duplicate_signal 是"查即登记",让一条
    # 陈旧重放先去登记指纹,等于给这个合约上了 5 分钟的静音闸——KC 常在
    # 几分钟内重喊同一张(NNE 实测 8s 重发),那条**实时**信号会被当成孪生
    # 静默丢掉。改为先判年龄:陈旧的直接告警返回,不碰指纹表;
    # 双语孪生的重复告警由 stale_open_should_alert 按 symbol 5min 节流。
    created = getattr(message, "created_at", None)
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - created).total_seconds()
        # 坏值退默认而不是抛:这行在每条消息的处理路径上,ValueError 会一路
        # 冒到 router 兜底,表现为**所有 OPEN 被静默丢掉**——闸门反成断路器。
        max_age = env_float("OPEN_SIGNAL_MAX_AGE_SEC", 300.0, minimum=1.0)
        if age_sec > max_age:
            logger.warning(
                f"[age-guard] OPEN 信号已 {age_sec:.0f}s(> {max_age:.0f}s 上限),"
                f"不自动下单: {raw[:80]}"
            )
            if stale_open_should_alert(signal["symbol"]):
                await _safe_notify(format_error(
                    f"错过的开仓信号({age_sec/60:.0f} 分钟前),未自动下单",
                    f"{signal['symbol']} {signal['strike']}{signal['side'][0]} "
                    f"{signal.get('expiry', '')} @ {signal.get('price')}\n"
                    f"来源:回补/晚启动重放,限价会锚在旧喊价上。要跟请手动。\n\n"
                    f"{raw[:200]}"
                ))
            return

    # ---- Fingerprint 去重 ----
    # 必须放在 parse 成功之后（要拿 symbol/strike/side/expiry_date 作 key）
    # 必须放在风控/TG/下单之前（拦得越早越省）
    # 不发 TG：双发场景下 TG 跟着双响会刷屏；只 log 留痕用于复盘
    is_dup, ago_sec = _is_duplicate_signal(signal)
    if is_dup:
        logger.info(
            f"🔁 Duplicate signal skipped: "
            f"{signal['symbol']} {signal['strike']}{signal['side'][0]} "
            f"{signal.get('expiry_date', '')} "
            f"(prev {ago_sec:.0f}s ago)"
        )
        return

    # ---- 短线标签 × 长 DTE 防护 ----
    # 7/16 实锤：KC 笔误 "SPY 755c June 20 @ 2.17 day trade"（June 20 已过），
    # smart_expiry 跨年滚动 → 买成 2027-06 合约（真实市场该合约根本不是 $2.17
    # 量级）。day trade / scalp / lotto / 0dte 隐含短 DTE，解析出 30 天以上
    # 只可能是喊单笔误或解析错位 → 不下单，TG 让人工确认。
    # 真 LEAPS 不受影响："SOFI 20c Jan 15 2027 starter leap swing" tags=['swing']。
    dte_guard_reason = _suspicious_long_dte(signal, msg_date_et)
    if dte_guard_reason:
        logger.warning(f"[dte-guard] {dte_guard_reason} — 不下单: {raw[:80]}")
        await _safe_notify(format_error(
            "疑似日期笔误（短线标签 + 长 DTE），未下单", f"{dte_guard_reason}\n\n{raw[:200]}",
        ))
        return

    # TODO P3: symbol blacklist

    # 解析成功立即预警，带 breakeven 提示 + KC tags
    # [7/23] 改后台发送：预警 TG round-trip（7/22 夜实测 ~1.2s）不再垫在
    # 风控→下单前面，省下的全是滑点。消息仍必发（notify_bg 持强引用），
    # 只是可能晚于"下单成功"通知到达。风控拒单/下单结果等通知保持同步 await。
    entry_p = signal.get("price", 0) or 0
    be_info = breakeven_exit_price(entry_p) if entry_p > 0 else None
    notify_bg(format_signal_alert(
        cfg.name,
        signal["symbol"],
        signal["strike"],
        signal["expiry"],
        signal["side"][0],
        entry_p,
        cfg.default_qty,
        signal.get("action", "OPEN"),
        breakeven=be_info,
        tags=signal.get("tags") or None,
    ))

    # ---- 风控 + 下单 + 配额记录：整段串行 ----
    # check_order 与 record_order 之间隔着 broker RTT（await），没有锁的话
    # 两条几乎同时到达的信号会都用"旧配额"通过 Layer 3/4 检查，
    # MAX_DAILY_COST / MAX_DAILY_ORDERS 可以被双双突破。
    # 信号频率是每天个位数，串行化整个下单段的延迟代价可以忽略。
    async with _order_flow_lock:
        # ---- 风控 ----
        # 关键参数说明：
        # - max_price_override: channel 的 max_price 覆盖全局 MAX_PRICE_PER_CONTRACT
        # - qty 来自 channel 配置，不同 channel 可以设不同张数
        # - effective_price: broker 实际会挂 signal_price × (1+5~12% slippage)，
        #   成本类风控（单笔/当日累计）必须按挂单价算，否则 REAL $1000 硬顶被滑点穿透
        qty = cfg.default_qty
        risk_result = check_order(
            price=signal["price"],
            qty=qty,
            symbol=signal["symbol"],
            strike=signal["strike"],
            side=signal["side"],
            expiry=signal.get("expiry", ""),
            channel_name=cfg.name,
            max_price_override=cfg.max_price,
            effective_price=calc_limit_price(signal["price"]),
        )

        if not risk_result.passed:
            logger.warning(
                f"🛡️  Risk blocked: {risk_result.reason} - {risk_result.detail}"
            )
            await _safe_notify(format_risk_blocked(risk_result.reason, risk_result.detail))
            return

        # ---- 下单 ----
        # 下单（broker.place_order 是同步函数，必须 to_thread 包装）
        try:
            order_result = await asyncio.to_thread(place_order, signal, qty)
        except Exception as e:
            logger.exception("place_order failed")
            await _safe_notify(format_error("Order error", str(e)))
            return

        # 落库订单（成功失败都记，作为业务日志）
        try:
            log_order(message.id, signal, order_result)
        except Exception as e:
            logger.error(f"log_order failed: {e}")

        # === [改动 Bug A] 只有 success=True 才 record_order ===
        # 实测背景：6/16 QCOM/IREN 期权代码错误（Juneteenth 未处理），
        # broker 返回 success=False，但旧逻辑仍 record_order 污染配额，
        # 导致 daily_orders 表出现"成功记录"但订单实际未成交。
        # 现在失败 → 发 TG 提示用户 + 不污染配额。
        if not order_result.get("success"):
            err_msg = order_result.get("message", "unknown error")
            logger.error(f"Order rejected by broker: {err_msg}")
            await _safe_notify(format_error(
                "Order rejected by broker",
                f"{signal['symbol']} {signal['strike']}{signal['side'][0]} "
                f"{signal.get('expiry', '')}\n{err_msg}"
            ))
            return

        # 下单成功 → 记录 (channel, symbol) + 合约快照：压制 ZH 孪生的
        # parse-fail 报警 + 挡住被机翻成 close 动词的孪生（_close_is_open_twin）
        _record_recent_exec(cid, signal)

        try:
            # 用 broker 实际挂单价计成本（含 slippage），否则 MAX_DAILY_COST 会被低估
            effective_price = order_result.get("price", signal["price"])
            record_order(
                price=effective_price,
                qty=qty,
                symbol=signal["symbol"],
                strike=signal["strike"],
                side=signal["side"],
                expiry=signal.get("expiry", ""),
                channel_id=str(cid),
                channel_name=cfg.name,
            )
        except Exception as e:
            logger.error(f"record_order failed: {e}")

    # ---- 持仓追踪（用于后续 close 信号匹配 / SL polling / EOD 强平） ----
    try:
        position_mgr.on_order_filled(
            signal=signal,
            order_result=order_result,
            channel_name=cfg.name,
            msg_id=str(message.id),
        )
    except Exception as e:
        logger.error(f"position_mgr.on_order_filled failed: {e}")

    # ---- 成交确认（fire-and-forget）----
    # broker success 只是"已提交"；确认成交后回填真实 avg_entry，
    # 超时未成交则 TG 告警提示对账。见 fill_checker 模块 docstring。
    fill_checker.spawn(fill_checker.confirm_buy_fill(
        order_result.get("order_id") or "",
        order_result.get("code") or "",
        order_result.get("qty", qty),
        order_result.get("price", 0.0) or 0.0,
    ))

    # ---- 通知 + 延迟统计 ----
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds() * 1000
    await _safe_notify(format_order_filled(
        signal["symbol"],
        signal["strike"],
        signal["side"][0],
        signal["expiry"],
        order_result.get("price", signal.get("price", 0) or 0),
        order_result.get("qty", cfg.default_qty),
        order_result.get("order_id", "N/A"),
    ))
    logger.info(f"⏱️  End-to-end latency: {elapsed:.0f}ms")
