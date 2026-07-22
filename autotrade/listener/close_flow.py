"""CLOSE 信号编排（拆自 src/listener/discord_client.py 的 _handle_close_signal，
逐字搬运，仅改名 handle_close_signal 并从新模块 import）。

卖出限价用 autotrade.policy.pricing.calc_sell_limit
（原 listener 的 _calc_sell_limit/SELL_SLIP 已随定价内核集中到 policy.pricing）。
"""
import asyncio

from autotrade.broker.trade import place_sell_order
from autotrade.listener.dedup import _is_duplicate_close, _unregister_close_fp
from autotrade.listener.heuristics import _close_is_open_twin, _looks_like_close_attempt
from autotrade.notify.messages import (
    format_close_filled,
    format_close_skipped,
    format_error,
)
from autotrade.notify.transport import _safe_notify
from autotrade.parsing.close_parser import parse_close
from autotrade.policy.pricing import calc_sell_limit
from autotrade.position import fill_checker
from autotrade.position import manager as position_mgr
from autotrade.utils.logger import logger


# ============================================================
# CLOSE 信号处理
# ============================================================
async def handle_close_signal(
    raw: str, msg_id: int, channel_name: str = None, channel_id: int = None,
):
    """detect_action==CLOSE 时调用。

    流程：
      1. 查当前活跃持仓 symbols → 给 parser 做白名单消歧
      2. parse_close → dict | None
      3. 多 symbol 循环：每个 symbol 可能对应多 strike，全部按 pct 卖
         （channel_name 传入时只平**同频道来源**的仓位——7/10 enrich 的
         "$NVDA all out" 差点平掉我们跟 KC 开的 NVDA swing，两个频道是
         两个独立 trader，close 不能跨源）
      4. broker.place_sell_order（to_thread 包同步调用）
      5. position_mgr.on_close_filled 扣减
      6. Telegram 通知

    channel_name=None（测试/旧调用方）时不做频道过滤，保持旧行为。
    """
    open_symbols = position_mgr.get_open_symbols()
    if not open_symbols:
        logger.info("[CLOSE] no open positions, ignoring close signal")
        return

    parsed = parse_close(raw, open_symbols)
    if parsed is None:
        logger.info(f"[CLOSE] parser skipped: {raw[:80]}")
        # 只对"含 ticker + 价格 hint"的发 TG：捕获真漏检（如 ZH 公司名映射失败）
        # 过滤无 ticker 的 follow-up close（如 "trim runners here at 3.45"）
        if _looks_like_close_attempt(raw):
            await _safe_notify(format_close_skipped("parser skipped (recap/no-symbol)", raw))
        return

    # CLOSE dedup —— 双语双发 / 同信号重发拦截（查即登记，原子堵双发窗口；
    # broker 失败时末尾回滚指纹，孪生版本充当重试）
    is_dup, ago = _is_duplicate_close(parsed)
    if is_dup:
        logger.info(
            f"🔁 [CLOSE] dup skipped (lang={parsed.get('lang')}): "
            f"{parsed['kind']} symbols={parsed.get('symbols')} pct={parsed['pct']} "
            f"prev {ago:.1f}s ago"
        )
        return

    if parsed["kind"] == "BULK_TRIM":
        # 全仓 trim：遍历所有 open symbols，扣掉信号里的例外
        # （7/8 "Closing all positions outside of the $IBM $310 lotto"——
        # 不能连人家明确保留的仓位一起卖）
        excluded = set(parsed.get("exclude_symbols") or [])
        targets = [s for s in open_symbols if s not in excluded]
        if excluded:
            logger.info(f"[CLOSE] BULK exclude: {sorted(excluded)}")
    else:
        targets = parsed["symbols"]

    pct = parsed["pct"]
    any_executed = False
    any_success = False  # 至少一笔卖单成功提交 → 登记 CLOSE 指纹
    runner_preserved: list[str] = []  # 被 runner-preserve 跳过的仓位，循环外合并 TG
    # broker 侧失败（异常/拒单）——这类失败是瞬时的，值得让 1-3s 后的
    # 双语孪生版本重试；确定性跳过（runner-preserve / 无价格参照 /
    # strike 不匹配）重试也是同样结果，不算在内
    any_broker_failure = False

    hint_strike = parsed.get("hint_strike")
    hint_side = parsed.get("hint_side")
    # hint 是从 symbols[0] 附近抽的（见 close_parser._extract_strike_hint），
    # 只能约束**那一个** symbol。用 TSLA 的 420c 去过滤 MSFT 的持仓，
    # 会把 MSFT 的平仓静默跳过（"Trimmed TSLA 420c and MSFT here" 案例）。
    hint_source_symbol = (parsed.get("symbols") or [None])[0]

    for symbol in targets:
        # 翻译孪生防护：刚开仓的 OPEN 消息被机翻出 close 动词
        # （7/15 "smaller size"→"小规模减仓"），不平仓，TG 留痕
        twin_reason = _close_is_open_twin(channel_id, symbol, parsed)
        if twin_reason:
            logger.warning(f"[CLOSE] open-twin guard: {twin_reason} — skip")
            await _safe_notify(format_close_skipped(
                f"疑似开仓消息的翻译孪生，不平仓：{twin_reason}", raw,
            ))
            any_executed = True  # 已发专属 TG，不再让外层报 "no matching"
            continue

        positions = position_mgr.find_by_symbol(symbol)
        if not positions:
            logger.warning(f"[CLOSE] {symbol} not found in open positions")
            continue

        # 频道来源过滤：只平信号来源频道开的仓位（channel_name 为空的历史
        # 仓位放行）。7/10 实测：enrich "$NVDA - I'm practically all out"
        # 匹配到我们跟 KC 开的 NVDA 230c —— enrich 平的是他自己的 0DTE，
        # 两个频道是独立 trader，靠"无价格参照拒卖"才躲过 100% 误平。
        if channel_name:
            same_ch = [
                p for p in positions
                if not p.get("channel_name") or p["channel_name"] == channel_name
            ]
            if not same_ch:
                logger.info(
                    f"[CLOSE] {symbol}: {len(positions)} position(s) opened from "
                    f"other channel(s) "
                    f"({sorted({p.get('channel_name') for p in positions})}), "
                    f"signal from '{channel_name}' — skip"
                )
                if parsed["kind"] == "CLOSE":
                    # 定向 close 才提示（BULK 会扫到一堆别频道仓位，全提示会刷屏）
                    await _safe_notify(format_close_skipped(
                        f"频道不匹配：{symbol} 仓位来自 "
                        f"{sorted({p.get('channel_name') or '?' for p in positions})}，"
                        f"信号来自 {channel_name}，不跨源平仓",
                        raw,
                    ))
                    any_executed = True  # 已发专属 TG，不再让外层报 "no matching"
                continue
            positions = same_ch

        # strike-aware filter：close 文本里显式给了 strike+side 时只关匹配的仓位。
        # 背景见 [docs/lessons.md](docs/lessons.md) #11：6/30 KC 平 TSLA 420c
        # 触发我们平 TSLA 425c，这次运气好两个 strike 价差小，下次未必。
        # 只对 hint 所属的 symbol 生效（多 symbol close 的其余 symbol 不受约束）。
        if hint_strike is not None and hint_side is not None and symbol == hint_source_symbol:
            matched = [
                p for p in positions
                if p["strike"] == hint_strike and p["side"] == hint_side
            ]
            if not matched:
                logger.warning(
                    f"[CLOSE] {symbol} {hint_strike}{hint_side[0]} hinted but "
                    f"no matching position (have: "
                    f"{[(p['strike'], p['side'][0]) for p in positions]}). Skipping."
                )
                await _safe_notify(format_close_skipped(
                    f"strike 不匹配（KC 平 {symbol} {hint_strike}{hint_side[0]} 但我们持仓不同 strike）",
                    raw,
                ))
                any_executed = True  # 已发专属 TG，不再让外层报 "no matching"
                continue
            logger.info(
                f"[CLOSE] strike-filter: {symbol} {hint_strike}{hint_side[0]} → "
                f"{len(matched)}/{len(positions)} positions selected"
            )
            positions = matched

        for pos in positions:
            # 卖出串行化：同一 option_code 上 SL/TP/EOD/CLOSE 四条路径互斥。
            # 锁内重读仓位——等锁期间 watcher 可能已经卖过了。
            async with position_mgr.sell_lock(pos["option_code"]):
                fresh = position_mgr.get(pos["option_code"])
                if fresh is not None:
                    pos = fresh
                if pos["status"] not in ("OPEN", "PARTIAL") or pos["qty_remaining"] <= 0:
                    logger.info(
                        f"[CLOSE] {pos['option_code']} already closed "
                        f"while waiting for lock, skip"
                    )
                    any_executed = True  # 有人处理过了，不报 "no matching"
                    continue
                qty_to_sell = position_mgr.calc_qty_to_sell(pos, pct)
                if qty_to_sell <= 0:
                    # runner-preserve（策略 A）：remaining=1 且 pct<100 故意跳过 trim。
                    # 收集起来循环外合并成一条 TG——多 strike 时逐仓位发会刷屏
                    # （7/17 一夜 12+ 条，SPY/NVDA 各两个 strike × 每次 trim）。
                    # 仍标记"已处理"，否则落到外层 "no matching" 兜底文案
                    # （7/6 IBM 两次实锤，半夜看到会以为仓位状态错乱）
                    runner_preserved.append(
                        f"{pos['symbol']} {pos['strike']}{pos['side'][0]}"
                    )
                    any_executed = True
                    continue
                limit = calc_sell_limit(
                    pos["avg_entry_price"], parsed.get("signal_price"),
                )
                if limit is None:
                    # 信号没喊价 + OPRA 不可用 → 拒绝执行，TG 警报让人工接管
                    logger.warning(
                        f"[CLOSE] no price ref for {pos['option_code']}, "
                        f"skipping sell ({pct}%)"
                    )
                    await _safe_notify(format_error(
                        "CLOSE 跳过：无价格参照",
                        f"{pos['option_code']} qty={qty_to_sell} ({pct}%)\n"
                        f"原因：信号无价 + OPRA 报价不可用\n"
                        f"请在 moomoo 手动平仓\n\n"
                        f"原文: {raw[:200]}"
                    ))
                    any_executed = True  # 算"处理过"，不让外层再发 "no matching" 提示
                    continue
                logger.info(
                    f"[CLOSE] sell {pos['option_code']} qty={qty_to_sell} "
                    f"limit={limit} ({pct}%, ref=signal)"
                )
                try:
                    result = await asyncio.to_thread(
                        place_sell_order,
                        option_code=pos["option_code"],
                        qty=qty_to_sell,
                        limit_price=limit,
                        remark=f"kc_close_{pct}pct",
                    )
                except Exception as e:
                    logger.exception("place_sell_order failed")
                    await _safe_notify(format_error("Sell order error", str(e)))
                    any_executed = True  # 持仓找到了只是 broker 异常，不再报 "no matching"
                    any_broker_failure = True
                    continue

                if not result.get("success"):
                    err = result.get("message", "unknown")
                    logger.error(f"[CLOSE] sell rejected: {err}")
                    await _safe_notify(format_error(
                        "Sell rejected by broker",
                        f"{pos['option_code']} qty={qty_to_sell}\n{err}",
                    ))
                    any_executed = True  # 持仓找到了只是 broker 拒单，不再报 "no matching"
                    any_broker_failure = True
                    continue

                try:
                    position_mgr.on_close_filled(
                        option_code=pos["option_code"],
                        qty_sold=result.get("qty", qty_to_sell),
                        fill_price=result.get("price", limit),
                        trigger_source="kc_signal",
                        ref_msg_id=str(msg_id),
                        order_id=result.get("order_id"),
                        note=f"pct={pct} matched={parsed['matched'][:60]}",
                    )
                except Exception as e:
                    logger.error(f"on_close_filled failed: {e}")

                # 卖单成交确认：DB 已按已平处理，若限价单实际没成交必须告警
                fill_checker.spawn(fill_checker.confirm_sell_fill(
                    result.get("order_id") or "", pos["option_code"],
                    result.get("qty", qty_to_sell), "kc_close",
                ))

            await _safe_notify(format_close_filled(
                pos["symbol"], pos["strike"], pos["side"], pos["expiry"],
                result.get("qty", qty_to_sell), result.get("price", limit),
                pct, "kc_signal", result.get("order_id", "N/A"),
            ))
            any_executed = True
            any_success = True

    if runner_preserved:
        await _safe_notify(format_close_skipped(
            f"runner-preserve：{'、'.join(runner_preserved)} 各剩 1 张，"
            f"跳过 {pct}% trim（策略 A，等 100% 全平信号）",
            raw,
        ))

    if any_broker_failure and not any_success:
        # 指纹已在 _is_duplicate_close 查重时登记（原子，堵双发竞态）。
        # 零成交且出现过 broker 失败（异常/拒单）→ 回滚指纹，
        # 让 1-3s 后到达的另一语言版本充当天然重试（0005 的目的）。
        # 确定性结果（runner-preserve / 无价格参照 / strike 不匹配）不回滚——
        # 孪生重试也是同样结果，只会重复刷 TG。
        _unregister_close_fp(parsed)

    if not any_executed:
        await _safe_notify(format_close_skipped(
            "no matching open positions",
            f"parsed: kind={parsed['kind']} symbols={targets} pct={pct}\n\n{raw}",
        ))
