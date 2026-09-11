"""
moomoo OpenAPI 下单封装

职责：
- 构造 option_code、计算 limit_price（分档 slippage）、调用 SDK
- 不做风控（已迁移到 risk_manager）
- 同步函数，调用方需 asyncio.to_thread 包装

设计：
- ctx 单例懒加载（首次 place_order 才连 OpenD）
- account_id 从 .env 读，写死 MOOMOO_ACC_ID
- unlock_trade 只解一次（模拟盘其实不需要，留着兼容真实盘）

Slippage 分档（基于 6/17 HOOD 4-bagger 实战教训）：
- price <  $1.5  → 12%   # lotto / 低价单 spread 宽
- price <  $3.0  →  8%
- price >= $3.0  →  5%
TODO: 用 Polygon 回测后用真实 fill 数据校准这三档
（分档实现已拆到 autotrade.policy.pricing，本模块裸名 import 调用）

返回格式约定（成功 / 失败统一字段）：
{
    "success": bool,
    "message": str,
    "order_id": str | None,
    "code": str,
    "qty": int,
    "price": float,
}
"""
import re
import threading

from autotrade.utils.logger import logger
from autotrade.broker import inflight  # 在飞买单登记（naked-short 是否可豁免）
from autotrade.broker import quote  # close_ctx 关行情 ctx 用（跨模块 global 只能经模块属性操作）
from autotrade.broker.common import (
    ACC_ID,
    DEFAULT_QTY,
    OPEND_HOST,
    OPEND_PORT,
    OpenSecTradeContext,
    OrderType,
    RET_OK,
    SDK_AVAILABLE,
    SecurityFirm,
    TRADE_PWD,
    TRD_ENV_STR,
    TrdMarket,
    TrdSide,
    _get_trd_env,
    _is_dry_run,
    _is_stale_session,
)
from autotrade.broker.quote import validate_option_codes
from autotrade.policy.pricing import (
    _get_slippage_pct,
    build_option_code,
    calc_limit_price,
)

# ---- 模块级单例 ----
_ctx = None
_unlocked = False
# [refactor-change](a) trade ctx 生命周期加 threading.Lock，镜像 quote 侧
# _quote_lock：SDK 同步调用经 asyncio.to_thread 跑在真线程上，懒加载/reset/close
# 并发时可能重复建连、或把并发调用正在用的 ctx 从脚下关掉。
# 只锁生命周期（_get_ctx / _reset_ctx / close_ctx），不锁下单调用本身。
_ctx_lock = threading.Lock()


def _reset_ctx():
    """连接异常后重置单例，下次调用会重连。
    OpenD 重启或网络抖动会让旧 ctx 永久变坏，必须显式丢弃。
    [refactor-change](a) 持 _ctx_lock 执行，镜像 _reset_quote_ctx。"""
    global _ctx, _unlocked
    with _ctx_lock:
        if _ctx is not None:
            try:
                _ctx.close()
            except Exception:
                pass
        _ctx = None
        _unlocked = False
    logger.warning("[broker] ctx reset, will reconnect on next call")


def _get_ctx():
    """懒加载 ctx 单例。失败抛异常由调用方 catch。"""
    global _ctx
    # [refactor-change](a) 懒加载持 _ctx_lock，避免多线程并发首连建出多个 ctx
    with _ctx_lock:
        if _ctx is None:
            if not SDK_AVAILABLE:
                raise RuntimeError("moomoo SDK not installed")
            logger.info(f"[broker] 连接 OpenD {OPEND_HOST}:{OPEND_PORT}")
            _ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=OPEND_HOST,
                port=OPEND_PORT,
                security_firm=SecurityFirm.FUTUINC,
            )
        return _ctx


def _ensure_account():
    """直接返回 .env 配置的账号 ID。"""
    if ACC_ID == 0:
        raise RuntimeError("MOOMOO_ACC_ID 未配置")
    return ACC_ID


def _ensure_unlocked():
    """首次调用解锁交易。

    moomoo SIMULATE 模式 **不支持** unlock_trade —— 调它必返回
    "ERROR. No one available account!"（即使 acc_status=ACTIVE）。
    因此 SIMULATE 永远跳过，无论 TRADE_PWD 是否配置。
    """
    global _unlocked
    if _unlocked:
        return
    if TRD_ENV_STR == "SIMULATE":
        logger.info("[broker] SIMULATE 模式，跳过 unlock_trade（moomoo 不支持）")
        _unlocked = True
        return
    ctx = _get_ctx()
    if not TRADE_PWD:
        raise RuntimeError("REAL 模式但 MOOMOO_TRD_PWD 未配置，无法 unlock_trade")
    ret, data = ctx.unlock_trade(password=TRADE_PWD, is_unlock=True)
    if ret != RET_OK:
        raise RuntimeError(f"unlock_trade failed: {data}")
    _unlocked = True
    logger.info("[broker] unlock_trade OK (REAL)")


def _call_with_session_retry(ctx, fn_name: str, label: str, **kwargs):
    """[refactor-change](b) stale-session 统一重试 helper。

    原 place_order / place_sell_order / _get_long_qty 三处复制同一段
    "stale → reset → 重连 → unlock → 重试一次"；query_order_status 则漏了
    这层保护（中途 session 失效直接整轮失败）。统一收口：
    - 重试**恰好一次**，关键字白名单 _STALE_SESSION_HINTS 原样不变
      （白名单原因见 errors.py：避免把"价格无效""超额"等业务拒单当 stale 反复重试）；
    - query_order_status 也走这条路径（给它加上这一次 retry 即本项唯一行为变化）。

    Args:
        ctx: 首次调用用的 trade ctx（重试路径内部重新 _get_ctx 拿新连接）
        fn_name: ctx 上的 SDK 方法名（place_order / position_list_query / order_list_query）
        label: 日志前缀，保留原三处日志原文（"" / "sell " / "naked-check "）
        **kwargs: 原样透传 SDK

    Returns:
        (ret, data)：与直接调 SDK 一致
    """
    ret, data = getattr(ctx, fn_name)(**kwargs)
    if ret != RET_OK and _is_stale_session(str(data)):
        logger.warning(f"[broker] {label}stale session ({data}), reset 后重试一次")
        _reset_ctx()
        ctx = _get_ctx()
        _ensure_unlocked()
        ret, data = getattr(ctx, fn_name)(**kwargs)
    return ret, data


def place_order(signal: dict, qty: int = None) -> dict:
    """
    下单（同步函数，调用方需用 asyncio.to_thread 包装）

    参数:
        signal: dict, 必含 symbol, expiry_date, strike, side, price
        qty: 不传则用 DEFAULT_QTY

    返回: 见模块顶部约定
    """
    qty = qty if qty is not None else DEFAULT_QTY

    option_code = build_option_code(
        signal["symbol"], signal["expiry_date"],
        signal["strike"], signal["side"],
    )
    entry_price = signal["price"]
    limit_price = calc_limit_price(entry_price)
    slip_pct = _get_slippage_pct(entry_price)

    dry_run = _is_dry_run()
    logger.info(
        f"[broker] Order: {option_code} x {qty} @ {limit_price:.2f} "
        f"(entry={entry_price:.2f}, slip={slip_pct*100:.0f}%) "
        f"[env={TRD_ENV_STR}, dry_run={dry_run}] tags={signal.get('tags')}"
    )

    # ---- DRY_RUN 路径 ----
    if dry_run:
        return {
            "success": True, "message": "DRY_RUN",
            "order_id": "MOCK_001", "code": option_code,
            "qty": qty, "price": limit_price,
        }

    # ---- contract 预校验：避免 broker "Cannot find" 拒单（6/22 OSCR / 6/29 TEM/DRAM）
    # 用 quote snapshot 试取一次，找不到直接返回 rejected 不走 broker。
    # 节省一次 broker RTT，且错误消息更明确（运营能知道是 strike/expiry 不存在）。
    valid = validate_option_codes([option_code])
    if not valid.get(option_code):
        msg = f"option contract not found on OPRA: {option_code} (check strike/expiry exists)"
        logger.error(f"[broker] pre-validate rejected: {msg}")
        return {
            "success": False, "message": msg,
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }

    # ---- 真实下单 ----
    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        order_args = dict(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,  # 限价单
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark="discord_auto",
        )
        # 中途 session 失效 → 重连 + 重试一次。避免昨晚 SNOW 那种"运行半夜忽然账户挂"丢信号
        # [refactor-change](b) 原地复制的 retry 块统一走 _call_with_session_retry
        ret, data = _call_with_session_retry(ctx, "place_order", "", **order_args)
        if ret == RET_OK:
            order_id = str(data["order_id"].iloc[0])
            logger.info(f"[broker] 下单成功 order_id={order_id}")
            # [9/2] 提交 ≠ 成交。登记在飞，供下面 naked-short 判据用；
            # fill_checker 拿到终态后按 order_id 销账（同 code 可能有加仓单同时在飞）。
            inflight.mark_submitted(option_code, order_id)
            return {
                "success": True, "message": "submitted",
                "order_id": order_id, "code": option_code,
                "qty": qty, "price": limit_price,
            }
        else:
            logger.error(f"[broker] 下单失败: {data}")
            return {
                "success": False, "message": str(data),
                "order_id": None, "code": option_code,
                "qty": qty, "price": limit_price,
            }
    except Exception as e:
        logger.exception("[broker] place_order 异常")
        _reset_ctx()
        return {
            "success": False, "message": str(e),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }


def _get_long_qty(option_code: str) -> int:
    """查 broker 里持有的 long qty。用于 naked-short 防护。

    Returns:
        long qty（>= 0）。broker 确认无此 code 或方向为 SHORT → 返 0。

    Raises:
        RuntimeError: position_list_query 失败（含 stale-session 重试一次后仍失败）。
        之前把查询失败静默当 qty=0 处理，会用误导性的 naked-short 拒单
        挡掉所有卖出（含 SL 止损单），且绕过了 stale-session 恢复逻辑。
    """
    ctx = _get_ctx()
    # [refactor-change](b) 原地复制的 retry 块统一走 _call_with_session_retry
    # （"naked-check " 日志前缀保留原文）
    ret, df = _call_with_session_retry(
        ctx, "position_list_query", "naked-check ",
        code=option_code,
        trd_env=_get_trd_env(),
        acc_id=_ensure_account(),
    )
    if ret != RET_OK:
        raise RuntimeError(f"position_list_query failed: {df}")
    if df is None or len(df) == 0:
        return 0
    # position_side: LONG / SHORT
    row = df.iloc[0]
    side = str(row.get("position_side", "")).upper()
    qty = int(row.get("qty", 0))
    if side != "LONG" or qty <= 0:
        return 0
    return qty


# US 期权代码判定：US.SYMBOL + YYMMDD + C|P + strike×1000。
#
# [7/29 修正] 原判据 `[CP]\d{6,}$` 要求 strike 字段 **至少 6 位**，但 moomoo
# 的 strike×1000 **不补零** —— strike < $100 就只有 5 位甚至更少：
#     US.SOFI270115C20000  ($20)  → 20000  5 位 → 判成正股 ❌
#     US.NIO260731C5500    ($5.5) → 5500   4 位 → 判成正股 ❌
#     US.AMD260731C100000  ($100) → 100000 6 位 → 正确     ✅
# 即 **所有 strike < $100 的期权全部漏判**（实测你账户里的 SOFI 20C 就中招，
# 被列进 sync_positions 的"孤儿正股"并建议手工清掉）。
#
# 危险链条：reconciler 拿不到这类仓 → 本地有而 broker "没有" → 误报
# db_only「疑似已行权/场外平仓」→ 报告指引去跑 ops/sync_positions →
# 那边同一个 bug → record_close(fill_price=0) 把**活仓**错标 CLOSED →
# 掉出 SL/TP/EOD 选仓，裸放且无人知道。
#
# 改为按结构锚定（日期段恰好 6 位 + 行权价至少 1 位），而不是数 strike 位数。
# 正股不会误命中：股票代码里没有数字（BRK.B 之类含点的也不匹配）。
# ops/sync_positions._looks_like_option 是独立人工脚本、各自自包含，
# 已同步修同一个 bug（两处都改，只改一处等于留着另一条路踩雷）。
_OPTION_CODE_RE = re.compile(r"^[A-Z]+\d{6}[CP]\d+$")


def _looks_like_option_code(code: str) -> bool:
    return code.startswith("US.") and bool(_OPTION_CODE_RE.match(code[3:]))


def list_open_option_positions() -> dict[str, int]:
    """[0016] 查 broker 当前全部 US 期权持仓（qty>0）。对账 reconciler 用。

    与 _get_long_qty 的区别：那是单 code 的 naked-short 防护；这里拉全量
    （position_list_query 不带 code 过滤），并用同一条 stale-session 重试
    路径（_call_with_session_retry），中途 session 失效不至于整轮对账挂掉
    ——正是 SNOW"运行半夜忽然账户挂"那类故障的恢复通道。

    正股不收：期权行权换来的"孤儿正股"由人工 ops/sync_positions 处理
    （见其 strays_stock 告警），reconciler v1 只对齐期权。

    Returns:
        {option_code: qty}

    Raises:
        RuntimeError: 查询失败（重试一次后仍失败）。调用方决定重试节奏
        （reconciler 主循环 catch 后下一轮再试），不在这里吞掉——
        静默把失败当"空仓"会让对账误报所有 DB 仓位为漂移
        （与 _get_long_qty docstring 里"查询失败不当 qty=0"同一教训）。
    """
    ctx = _get_ctx()
    ret, df = _call_with_session_retry(
        ctx, "position_list_query", "reconcile ",
        trd_env=_get_trd_env(),
        acc_id=_ensure_account(),
    )
    if ret != RET_OK:
        raise RuntimeError(f"position_list_query failed: {df}")
    out: dict[str, int] = {}
    if df is None or len(df) == 0:
        return out
    for _, row in df.iterrows():
        code = str(row["code"])
        qty = int(row["qty"])
        if qty <= 0 or not _looks_like_option_code(code):
            continue
        out[code] = qty
    return out


def place_sell_order(
    option_code: str,
    qty: int,
    limit_price: float,
    remark: str = "auto_close",
) -> dict:
    """卖单（限价，同步，调用方需 to_thread 包装）

    Args:
        option_code: 标的代码（同 build_option_code 输出）
        qty: 卖出张数
        limit_price: 限价。SL/EOD 场景建议传 bid * 0.95 偏激进确保成交；
                     CLOSE 信号正常 trim 可传 bid 附近。
        remark: 标记触发源（kc_close / sl_polling / tp_polling / eod_force）

    返回:
        {success, message, order_id, code, qty, price}

    TODO（测试调整）：
    - 实测后看是否要支持 OrderType.MARKET（SL 紧急情况）
    - 模拟盘 SIMULATE 卖单是否需要先有真实持仓，没有的话 SDK 会拒
    - 部分成交（dealt_qty < qty）的处理 —— 当前只看 RET_OK，不轮询 fill
    """
    if qty <= 0:
        return {
            "success": False, "message": f"invalid qty={qty}",
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }

    dry_run = _is_dry_run()
    logger.info(
        f"[broker] SELL: {option_code} x {qty} @ {limit_price:.2f} "
        f"[env={TRD_ENV_STR}, dry_run={dry_run}, remark={remark}]"
    )

    if dry_run:
        return {
            "success": True, "message": "DRY_RUN sell",
            "order_id": f"MOCK_SELL_{option_code[-6:]}",
            "code": option_code, "qty": qty, "price": limit_price,
        }

    # ---- Naked-short 防护 ----
    # 系统只允许卖出已持有的 long option。不能挂 SELL 让 broker 视作开裸空仓。
    # 背景：7/2 发现本地 DB 与 broker 严重脱钩（自动 exercise 后本地仍 OPEN），
    # 如果后续 close 信号误触发 SELL，broker 会当"开裸空 call/put"处理 —— 无限风险。
    # 见 docs/lessons.md #15。
    try:
        available = _get_long_qty(option_code)
    except Exception as e:
        logger.exception(f"[broker] naked-check position_list_query failed for {option_code}")
        return {
            "success": False,
            "message": f"naked-short check failed (position_list_query exception): {e}",
            "order_id": None, "code": option_code, "qty": qty, "price": limit_price,
        }
    if available < qty:
        # [9/2 实锤 -$166] 同一个 "0 长仓" 有两种成因，代价天差地别：
        #   a. DB 与 broker 真脱钩（8/13 MU）→ 退避多久都不自愈，该熔断；
        #   b. **买单还在飞**（9/2 TSLA）→ 几分钟后就成交了，熔断等于把这个
        #      合约当晚所有喊单员平仓信号永久掐掉。
        # 措辞必须分叉：`is_deterministic_reject` 认的是 "naked-short refused"
        # 这个串，换成 deferred 就自动落到瞬时组走退避 —— 判据在 broker 这层，
        # 不必让四个 on_reject 调用点各自感知（见 broker/inflight.py）。
        if inflight.is_pending(option_code):
            # [9/11] 措辞改过一次。原文结尾是 "treating as transient, will retry."，
            # 而"会重试"只对**自带循环**的调用方成立（TP/SL watcher 每 5s 再来一轮）。
            # 喊单员喊的平仓是一次性消息：9/10 夜 00:30 的 `LITE OUT 40%` 撞上
            # 00:28:38 那张还没确认的买单（FILL_ADJUST 直到 00:31:38 才回来，隔了
            # 3 分钟），那 40% 的离场就此丢失，而日志和 TG 都在说"会重试"。
            # 半夜看 TG 的人据此判断"不用管"，是被这句话误导的。
            msg = (
                f"naked-short deferred: broker has only {available} long of {option_code}, "
                f"asked to sell {qty}, but a submitted buy is still unconfirmed — "
                f"transient, so no circuit-break. NOTE: watcher-driven sells (TP/SL) "
                f"retry on their next tick; a caller-driven close is one-shot and will "
                f"NOT be retried — decide manually whether to re-exit."
            )
            logger.warning(f"[broker] {msg}")
            return {
                "success": False, "message": msg,
                "order_id": None, "code": option_code, "qty": qty, "price": limit_price,
            }
        msg = (
            f"naked-short refused: broker has only {available} long of {option_code}, "
            f"asked to sell {qty}. This would open a naked short — refusing."
        )
        logger.error(f"[broker] {msg}")
        return {
            "success": False, "message": msg,
            "order_id": None, "code": option_code, "qty": qty, "price": limit_price,
        }

    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        order_args = dict(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.NORMAL,  # 限价
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark=remark,
        )
        # [refactor-change](b) 原地复制的 retry 块统一走 _call_with_session_retry
        # （"sell " 日志前缀保留原文）
        ret, data = _call_with_session_retry(ctx, "place_order", "sell ", **order_args)
        if ret == RET_OK:
            order_id = str(data["order_id"].iloc[0])
            logger.info(f"[broker] 卖单成功 order_id={order_id}")
            return {
                "success": True, "message": "submitted",
                "order_id": order_id, "code": option_code,
                "qty": qty, "price": limit_price,
            }
        logger.error(f"[broker] 卖单失败: {data}")
        return {
            "success": False, "message": str(data),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }
    except Exception as e:
        logger.exception("[broker] place_sell_order 异常")
        _reset_ctx()
        return {
            "success": False, "message": str(e),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }


def query_order_status(order_id: str) -> dict:
    """
    查单状态（同步）

    返回:
        {success, status, filled_qty, filled_avg_price, message}
    """
    if _is_dry_run():
        return {
            "success": True, "status": "FILLED_ALL",
            "filled_qty": 1, "filled_avg_price": 0.0,
            "message": "DRY_RUN",
        }
    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        # [refactor-change](b) query_order_status 之前没有 stale-session retry，
        # 中途 session 失效只能整轮失败；现在与下单/持仓查询共用
        # _call_with_session_retry，恰好重试一次。
        ret, data = _call_with_session_retry(
            ctx, "order_list_query", "query ",
            order_id=order_id,
            trd_env=_get_trd_env(),
            acc_id=acc_id,
        )
        if ret != RET_OK:
            return {"success": False, "message": str(data),
                    "status": None, "filled_qty": 0, "filled_avg_price": 0.0}
        if len(data) == 0:
            return {"success": False, "message": "order not found",
                    "status": None, "filled_qty": 0, "filled_avg_price": 0.0}
        row = data.iloc[0]
        dealt_price = row.get("dealt_avg_price", 0)
        return {
            "success": True,
            "status": str(row["order_status"]),
            "filled_qty": int(row["dealt_qty"]),
            "filled_avg_price": float(dealt_price) if dealt_price else 0.0,
            "message": "ok",
        }
    except Exception as e:
        logger.exception("[broker] query_order_status 异常")
        _reset_ctx()
        return {"success": False, "message": str(e),
                "status": None, "filled_qty": 0, "filled_avg_price": 0.0}


def probe_broker() -> tuple[bool, str]:
    """启动时探测 broker 健康度。供 run_listener 在 client.start 前调用。

    检查项：
      1. SDK 装好了吗
      2. OpenD 连得上吗
      3. get_acc_list 返回里有没有 MOOMOO_ACC_ID + MOOMOO_TRD_ENV 匹配的活跃账户
      4. REAL 模式：unlock_trade 测一下密码

    DRY_RUN=true 时跳过整套探测，直接返回 OK。

    Returns:
        (ok, message)：ok=False 时 message 是用户可读的诊断原因
    """
    # 启动时把生效配置打到日志，方便人眼对比 .env，避免"以为改了实际没生效"
    logger.info(
        f"[broker] config snapshot: TRD_ENV={TRD_ENV_STR} ACC_ID={ACC_ID} "
        f"TRADE_PWD={'set' if TRADE_PWD else 'empty'} "
        f"OpenD={OPEND_HOST}:{OPEND_PORT} DRY_RUN={_is_dry_run()}"
    )
    if _is_dry_run():
        return True, "DRY_RUN: 跳过 broker 探测"
    if not SDK_AVAILABLE:
        return False, "moomoo SDK 未安装（pip install moomoo-api）"
    if ACC_ID == 0:
        return False, "MOOMOO_ACC_ID 未配置或为 0"

    try:
        ctx = _get_ctx()
    except Exception as e:
        return False, f"连接 OpenD {OPEND_HOST}:{OPEND_PORT} 失败: {e}"

    try:
        ret, df = ctx.get_acc_list()
    except Exception as e:
        _reset_ctx()
        return False, f"get_acc_list 抛异常: {type(e).__name__}: {e}"

    if ret != RET_OK:
        return False, f"get_acc_list 失败: {df}"
    if df is None or len(df) == 0:
        return False, (
            "OpenD 没返回任何账户。检查：\n"
            "  1) moomoo 桌面端是否已登录\n"
            "  2) SIMULATE: 需要在桌面端启用模拟交易并选过一个账户\n"
            "  3) 试着重启 OpenD 或重登桌面端"
        )

    # 匹配账户。
    # moomoo SDK 不同版本对 trd_env 字段返回值不一致：可能是字符串 "SIMULATE"/"REAL"，
    # 也可能是 enum TrdEnv.SIMULATE / 整型。直接 == TRD_ENV_STR 比较会在 enum/int 形态下
    # 误判为不匹配，明明账户可用却阻止启动。这里把候选值都转字符串再比，覆盖所有形态。
    try:
        target = TRD_ENV_STR  # 已经 .upper() 过
        env_str = df["trd_env"].astype(str).str.upper().str.replace("TRDENV.", "", regex=False)
        matched = df[(df["acc_id"] == ACC_ID) & (env_str == target)]
    except Exception as e:
        return False, f"过滤账户列异常: {e}（df.columns={getattr(df, 'columns', '?').tolist() if hasattr(df, 'columns') else '?'}）"

    if len(matched) == 0:
        ids = df["acc_id"].tolist() if "acc_id" in df.columns else []
        envs = df["trd_env"].tolist() if "trd_env" in df.columns else []
        return False, (
            f"MOOMOO_ACC_ID={ACC_ID} env={TRD_ENV_STR} 不在可用账户列表里。\n"
            f"  OpenD 返回的 acc_id: {ids}\n"
            f"  OpenD 返回的 trd_env: {envs}\n"
            f"  → 把 .env 里 MOOMOO_ACC_ID 改成上面其中之一"
        )

    status = matched.iloc[0].get("acc_status", "UNKNOWN")
    if status != "ACTIVE":
        return False, f"账户 {ACC_ID} 状态为 {status}（非 ACTIVE），无法下单"

    # REAL 模式：试 unlock。SIMULATE 由 _ensure_unlocked 直接 skip，这里不碰
    if TRD_ENV_STR == "REAL":
        if not TRADE_PWD:
            return False, "REAL 模式但 MOOMOO_TRD_PWD 未配置"
        try:
            ret, data = ctx.unlock_trade(password=TRADE_PWD, is_unlock=True)
            if ret != RET_OK:
                return False, f"REAL unlock_trade 失败: {data}（密码错？）"
            global _unlocked
            _unlocked = True
        except Exception as e:
            return False, f"unlock_trade 抛异常: {type(e).__name__}: {e}"

    return True, (
        f"OK · env={TRD_ENV_STR} acc_id={ACC_ID} status={status} "
        f"({len(matched)} matching / {len(df)} total)"
    )


def close_ctx():
    """优雅退出时调用。同时关交易和行情两路。"""
    global _ctx, _unlocked
    # [refactor-change](a) trade 侧持 _ctx_lock 关闭，镜像 quote 侧
    with _ctx_lock:
        if _ctx is not None:
            try:
                _ctx.close()
            except Exception:
                pass
            _ctx = None
            _unlocked = False
            logger.info("[broker] trade ctx closed")
    # 行情侧：_quote_ctx 拆到 quote 模块后是跨模块 global，
    # 只能经模块属性读写（裸名 import 无法重绑对方的全局变量）
    if quote._quote_ctx is not None:
        try:
            quote._quote_ctx.close()
        except Exception:
            pass
        quote._quote_ctx = None
        logger.info("[broker] quote ctx closed")
