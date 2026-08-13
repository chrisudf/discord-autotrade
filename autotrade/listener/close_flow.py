"""CLOSE 信号编排（拆自 src/listener/discord_client.py 的 _handle_close_signal，
逐字搬运，仅改名 handle_close_signal 并从新模块 import）。

卖出限价用 autotrade.policy.pricing.calc_sell_limit
（原 listener 的 _calc_sell_limit/SELL_SLIP 已随定价内核集中到 policy.pricing）。
"""
import asyncio
import os

from autotrade.broker.quote import get_sell_ref_price
from autotrade.broker.trade import place_sell_order
from autotrade.listener.dedup import (
    _is_duplicate_close,
    _unregister_close_fp,
    runner_preserve_should_alert,
)
from autotrade.listener.heuristics import (
    _close_is_open_twin,
    _close_is_zh_twin_of_skipped_en,
    _looks_like_close_attempt,
    _record_close_skip,
)
from autotrade.notify.messages import (
    format_close_skipped,
    format_error,
)
from autotrade.notify.transport import _safe_notify
from autotrade.parsing.close_parser import parse_close
from autotrade.policy.positions import strategy_b_decision
from autotrade.utils.envcfg import env_float
from autotrade.policy.pricing import calc_sell_limit
from autotrade.position import fill_checker
from autotrade.position import manager as position_mgr
from autotrade.position import retry_guard
from autotrade.position.sell_executor import (
    Outcome,
    SellPlan,
    SkipSell,
    execute_sell,
)
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
        # EN 原文判定"不是平仓指令"要留痕，供 60s 内的 ZH 机翻孪生查
        # （8/3 SPY 误平，见 heuristics._close_is_zh_twin_of_skipped_en）
        _record_close_skip(channel_id, raw, open_symbols)
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
    # [0015] 结局清单：每个仓位/每个 symbol 级防护各记一条 (Outcome, label)。
    # 旧版的 any_executed / any_success / any_broker_failure / runner_preserved
    # 四个旗标各自在 10+ 处手工维护，漏 set 一个就是 6/18 IWM"broker 拒单
    # 误报 no matching"级别的误导——现在全部由 outcomes 循环末尾推导。
    outcomes: list[tuple[Outcome, str]] = []
    # [0011] 策略B（STRATEGY_B=true 才有内容）：label → 保留原因，
    # 循环外合并 TG 时附在对应仓位后面。默认关时恒为空 dict → 文案分支
    # 走老路，与今天逐字一致。
    strategy_b_reasons: dict[str, str] = {}

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
            # 已发专属 TG，不再让外层报 "no matching"
            outcomes.append((Outcome.SKIPPED_NOTIFIED, symbol))
            continue

        # 机翻孪生防护：同一条消息的 EN 原文刚被判定为"非平仓指令"
        # （8/3 SPY "I personally am swinging them" → ZH 版把仓位卖了）
        zh_twin_reason = _close_is_zh_twin_of_skipped_en(channel_id, symbol, parsed)
        if zh_twin_reason:
            logger.warning(f"[CLOSE] zh-twin guard: {zh_twin_reason} — skip")
            await _safe_notify(format_close_skipped(
                f"英文原文未判为平仓指令，中文孪生不执行：{zh_twin_reason}", raw,
            ))
            outcomes.append((Outcome.SKIPPED_NOTIFIED, symbol))
            continue

        positions = position_mgr.find_by_symbol(symbol)
        if not positions:
            logger.warning(f"[CLOSE] {symbol} not found in open positions")
            outcomes.append((Outcome.NOT_FOUND, symbol))
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
                    # 已发专属 TG，不再让外层报 "no matching"
                    outcomes.append((Outcome.SKIPPED_NOTIFIED, symbol))
                else:
                    # BULK 静默跳过：不发 TG 也不算"处理过"，全场都这样时
                    # 落到外层 "no matching" 兜底（与 0015 之前行为一致）
                    outcomes.append((Outcome.NOT_FOUND, symbol))
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
                # 已发专属 TG，不再让外层报 "no matching"
                outcomes.append((Outcome.SKIPPED_NOTIFIED, symbol))
                continue
            logger.info(
                f"[CLOSE] strike-filter: {symbol} {hint_strike}{hint_side[0]} → "
                f"{len(matched)}/{len(positions)} positions selected"
            )
            positions = matched

        for pos in positions:
            # TG 展示/节流 key 用的仓位标签（字段不可变，锁外算安全）
            pos_label = f"{pos['symbol']} {pos['strike']}{pos['side'][0]}"
            # [0015] 执行骨架（锁→锁内重读→防护→下单→记账→fill_confirm→TG）
            # 走 sell_executor；KC 专属的 runner-preserve / 策略B /
            # quote-fallback 决策留在本模块（_kc_sell 的 plan 闭包，锁内执行）。
            outcome, _ = await _kc_sell(
                pos, pos_label, pct, parsed, raw, msg_id, strategy_b_reasons,
            )
            outcomes.append((outcome, pos_label))

    # [0015] 旗标推导：语义与旧手工旗标逐字对齐（见 Outcome docstring）。
    # NOT_FOUND 是唯一不算"处理过"的结局 → 全场 NOT_FOUND 才发兜底 TG；
    # runner_preserved 保持 append 顺序（TG 文案的仓位顺序不变）。
    any_executed = any(o is not Outcome.NOT_FOUND for o, _ in outcomes)
    any_success = any(o is Outcome.SOLD for o, _ in outcomes)
    any_broker_failure = any(o is Outcome.BROKER_FAILED for o, _ in outcomes)
    runner_preserved = [
        label for o, label in outcomes if o is Outcome.RUNNER_PRESERVED
    ]

    if runner_preserved:
        # 7/23 降噪：同一仓位窗口期内（默认 1h）只发一次 runner-preserve TG，
        # 其余只 log——单张仓遇上 KC 连环 trim 时一夜 6 条相同提醒（AVGO 实测）。
        # [0011] 节流 key 保持裸 pos_label、**不带 reason**：策略B 的 reason 里
        # 有实时浮盈数字，每次报价都不同，进 key 会让节流形同虚设、
        # 一夜退回 7/23 的 6 连发。
        fresh = [p for p in runner_preserved if runner_preserve_should_alert(p)]
        if fresh:
            if strategy_b_reasons:
                # [0011] 策略B 开启：文案附保留原因——半夜看 TG 能直接分辨
                # 是"浮盈还没到阈值"还是"报价断了退回死拿"，不用翻 log。
                detail = "、".join(
                    f"{p}（{strategy_b_reasons[p]}）" if p in strategy_b_reasons
                    else p
                    for p in fresh
                )
                await _safe_notify(format_close_skipped(
                    f"runner-preserve：{detail} 各剩 1 张，"
                    f"跳过 {pct}% trim（策略 B 未达标保留；窗口期内不重复提醒）",
                    raw,
                ))
            else:
                # 默认（STRATEGY_B 关）：文案与 0011 之前逐字一致
                await _safe_notify(format_close_skipped(
                    f"runner-preserve：{'、'.join(fresh)} 各剩 1 张，"
                    f"跳过 {pct}% trim（策略 A，等 100% 全平信号；窗口期内不重复提醒）",
                    raw,
                ))
        else:
            logger.info(
                f"[CLOSE] runner-preserve TG throttled "
                f"(window 内已提醒过): {runner_preserved} pct={pct}"
            )

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


async def _kc_sell(
    pos: dict, pos_label: str, pct: int, parsed: dict, raw: str,
    msg_id: int, strategy_b_reasons: dict[str, str],
) -> "tuple[Outcome, dict | None]":
    """单仓位 KC close 卖出（sell_executor 骨架 + KC 专属 plan/钩子）。

    [0015] 契约边界：runner-preserve / 策略B / quote-fallback 的**决策**全部
    留在本模块（plan 闭包在 executor 的卖出锁内执行——"决定卖"与"按什么价卖"
    在同一把锁的同一仓位视图下敲定）；twin 防护 / 频道过滤 / strike hint
    在 handle_close_signal 的 symbol 层，根本不进本函数。broker / TG 注入
    用本模块命名空间裸名（place_sell_order / _safe_notify /
    get_sell_ref_price），既有测试的 patch.object(close_flow, ...) 点全部不动。
    """
    code = pos["option_code"]

    # [8/13] 确定性拒单熔断后不再对同一合约下单。结局取 SKIPPED_NOTIFIED：
    # 熔断那一刻已经发过专属 TG，这里算"处理过了"，不能落到外层
    # "no matching open positions" 兜底文案（6/18 IWM 的误导形状）。
    if retry_guard.is_tripped(f"kc:{code}"):
        logger.warning(f"[CLOSE] {code} 跳过：{retry_guard.blocked(f'kc:{code}')}")
        return Outcome.SKIPPED_NOTIFIED, None

    async def _plan(fresh: dict):
        qty_to_sell = position_mgr.calc_qty_to_sell(fresh, pct)
        # [0011] 该仓位实际执行口径的 pct：默认=信号 pct；策略B SELL_ALL
        # 时改成 100（remark/记账 note/TG 都如实说"全出"，别让半夜看到
        # "33%卖了1张"以为还剩 2 张）。默认关时恒等于 pct → 输出逐字不变。
        pos_pct = pct
        # 策略B 分支已取到的实时参照，供下方限价计算复用（不重复吃
        # snapshot 配额）。默认关/未取到时为 None → 老路不受影响。
        stratb_quote_ref = None
        if qty_to_sell <= 0:
            # runner-preserve（策略 A）：remaining=1 且 pct<100 故意跳过 trim。
            # RUNNER_PRESERVED 结局由调用方循环外合并成一条 TG——多 strike 时
            # 逐仓位发会刷屏（7/17 一夜 12+ 条，SPY/NVDA 各两个 strike ×
            # 每次 trim）。仍算"已处理"，否则落到外层 "no matching"
            # 兜底文案（7/6 IBM 两次实锤，半夜看到会以为仓位状态错乱）
            # [0011] 策略B（默认关 [ship-dark]）：死拿（策略A）的反面教材
            # 是 AVGO 415c（7/23-24 夜）从 +50% 一路拿到过期归零——KC 连环
            # trim 我们一律跳过，最后连残值都没接住。开启后：锁内取实时
            # 参照价（与 0010 fallback 同一 helper，bid 优先/60s 新鲜度门/
            # 共享配额退避），我方浮盈达标 → 该仓位全部剩余卖出（走下方
            # 既有卖出流程，限价/下单/记账/fill_confirm/TG 一条路不分叉）；
            # 未达标/无新鲜报价 → 现行为（节流 TG，文案附 reason）。
            # 开关 per-call 重读，对齐 watcher 现风格。
            strategy_b = (
                os.getenv("STRATEGY_B", "false").strip().lower() == "true"
            )
            if strategy_b:
                # 阈值走 envcfg：写坏了告警一次退 25，不崩平仓流程
                min_pnl_pct = env_float("STRATEGY_B_MIN_PNL_PCT", 25.0, minimum=0.0)
                stratb_quote_ref = await asyncio.to_thread(
                    get_sell_ref_price, code,
                )
                decision, reason = strategy_b_decision(
                    fresh["avg_entry_price"], stratb_quote_ref,
                    parsed.get("signal_pnl_pct"), min_pnl_pct,
                )
                if decision == "SELL_ALL":
                    # 单张仓（remaining==1）没有"卖 33%"的最小单位，
                    # 全出是唯一可行响应——策略A/策略B 的分野就在这里。
                    qty_to_sell = fresh["qty_remaining"]
                    pos_pct = 100
                    logger.info(
                        f"[CLOSE] strategy-B SELL_ALL: "
                        f"{code} qty={qty_to_sell} "
                        f"quote_ref={stratb_quote_ref}（{reason}）"
                    )
                    # 不 return：落回下方既有卖出流程
                else:
                    logger.info(
                        f"[CLOSE] strategy-B PRESERVE: {code}（{reason}）"
                    )
                    strategy_b_reasons[pos_label] = reason
                    return SkipSell(Outcome.RUNNER_PRESERVED)
            else:
                return SkipSell(Outcome.RUNNER_PRESERVED)
        limit = calc_sell_limit(
            fresh["avg_entry_price"], parsed.get("signal_price"),
        )
        price_ref = "signal"
        # [0010] CLOSE 无价 fallback：KC 只报盈利不喊价时
        # （7/25 夜 "Trimmed AVGO +20%" 实锤——无 signal_price 直接
        # 走拒卖 + TG 人工接管，半夜没人盯，trim 全漏），改为先取
        # 实时 bid/last 做参照（get_sell_ref_price：bid 优先、60s
        # 新鲜度门、与 watcher 共享 snapshot 配额/退避）。
        # 开关每次重读（对齐 watcher 现风格）；默认开——拿不到
        # 新鲜报价仍落到下面的拒卖底线，"是否拒卖"的语义只会因
        # 有了可靠参照而放行，不会反向放松。
        quote_fallback = (
            os.getenv("CLOSE_QUOTE_FALLBACK", "true").strip().lower()
            == "true"
        )
        if limit is None and quote_fallback:
            # [0011] 策略B SELL_ALL 落下来时同一把锁内刚取过一次新鲜
            # 报价（60s 新鲜度窗口内）——直接复用：省一次 snapshot 配额，
            # 且"决定卖"和"按什么价卖"用的是同一个参照，不会出现决策用
            # 2.20、挂单用 1.80 的撕裂。默认关时恒为 None → 行为不变。
            quote_ref = stratb_quote_ref
            if quote_ref is None:
                quote_ref = await asyncio.to_thread(
                    get_sell_ref_price, code,
                )
            if quote_ref is not None:
                limit = calc_sell_limit(
                    fresh["avg_entry_price"], None, quote_ref,
                )
                price_ref = "quote"
                logger.info(
                    f"[CLOSE] quote fallback: {code} "
                    f"quote_ref={quote_ref} → limit={limit}"
                )
        if limit is None:
            # 信号没喊价 + 实时参照也拿不到 → 拒绝执行，TG 警报让
            # 人工接管（拒卖语义与 0010 之前一字不差；env 关闭时
            # 连报价都不试，行为=今天）
            logger.warning(
                f"[CLOSE] no price ref for {code}, "
                f"skipping sell ({pos_pct}%)"
            )
            if quote_fallback:
                no_ref_reason = (
                    "信号无价 + 已尝试实时报价参照仍不可得"
                    "（bid/last 均无或 stale）"
                )
            else:
                no_ref_reason = "信号无价 + OPRA 报价不可用"
            await _safe_notify(format_error(
                "CLOSE 跳过：无价格参照",
                f"{code} qty={qty_to_sell} ({pos_pct}%)\n"
                f"原因：{no_ref_reason}\n"
                f"请在 moomoo 手动平仓\n\n"
                f"原文: {raw[:200]}"
            ))
            # 算"处理过"，不让外层再发 "no matching" 提示
            return SkipSell(Outcome.SKIPPED_NOTIFIED)
        logger.info(
            f"[CLOSE] sell {code} qty={qty_to_sell} "
            f"limit={limit} ({pos_pct}%, ref={price_ref})"
        )
        return SellPlan(
            qty=qty_to_sell, limit=limit,
            remark=f"kc_close_{pos_pct}pct",
            notify_pct=pos_pct,
            note=f"pct={pos_pct} matched={parsed['matched'][:60]}",
        )

    def _already_closed(fresh: dict):
        # 有人处理过了（SKIPPED_SILENT 仍算 executed），不报 "no matching"
        logger.info(
            f"[CLOSE] {code} already closed while waiting for lock, skip"
        )

    async def _on_failed_attempt(title: str, err: str, qty_desc: str):
        """[8/13] 只吃 retry_guard 的熔断那一半（backoff=False）。

        瞬时拒单原样放过：0005 的设计是"指纹回滚 + 1-3s 后的双语孪生天然重试
        一次"，在这里插退避会把那条链打断。要挡的只有 naked-short 这类确定性
        拒单 —— 喊单员一夜喊 7 次 trim（7/23 AVGO 的形状），本地仓位却早已在
        broker 侧消失，那就是 7 次同样的拒单 + 7 条同样的 TG。
        """
        d = retry_guard.on_reject(f"kc:{code}", err, backoff=False)
        hint = ""
        if d.tripped:
            logger.error(f"[CLOSE] ⛔ {code} 熔断（确定性拒单）：{err}")
            hint = (
                "\n\n⛔ **本合约的自动平仓已熔断**，后续同标的的 CLOSE 信号不再下单"
                "（重启即恢复）。"
                "\nbroker 侧很可能已经没有这张仓、本地 DB 陈旧：跑 "
                "`python -m autotrade.ops.sync_positions --dry-run` 对账。"
                "\n在此之前请在 moomoo 手动确认。"
            )
        else:
            logger.error(f"[CLOSE] sell rejected: {err}")
        await _safe_notify(format_error(title, f"{code} {qty_desc}\n{err}{hint}"))

    async def _sell_error(e: Exception, plan):
        logger.exception("place_sell_order failed")
        await _on_failed_attempt("Sell order error", f"{type(e).__name__}: {e}",
                                 f"qty={plan.qty}")

    async def _sell_rejected(result: dict, plan):
        await _on_failed_attempt("Sell rejected by broker",
                                 result.get("message", "unknown"),
                                 f"qty={plan.qty}")

    async def _record_failure(e: Exception, result: dict):
        logger.error(f"on_close_filled failed: {e}")

    return await execute_sell(
        pos,
        trigger_source="kc_signal",
        notify_trigger="kc_signal",
        plan_fn=_plan,
        place_sell_order=place_sell_order,
        notify=_safe_notify,
        # 卖单成交确认：DB 已按已平处理，若限价单实际没成交必须告警
        fill_confirm=lambda order_id, qty_sold: fill_checker.spawn(
            fill_checker.confirm_sell_fill(order_id, code, qty_sold, "kc_close")),
        on_already_closed=_already_closed,
        on_sell_error=_sell_error,
        on_sell_rejected=_sell_rejected,
        on_record_failure=_record_failure,
        ref_msg_id=str(msg_id),
    )
