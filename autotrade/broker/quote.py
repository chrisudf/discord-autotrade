"""
moomoo 行情侧（从 moomoo_client 拆分）：quote_ctx 单例 + snapshot 封装 +
last price 批量取价 + option code 预校验 + 行情权限探测。

线程模型：SDK 同步调用经 asyncio.to_thread 跑在真线程上，
所以这里的锁必须是 threading.Lock（见 _quote_lock 注释）。

test_quote_snapshot 直接在本模块上 monkeypatch：
_quote_ctx / _quote_backoff_until / _no_perm_last_warn / SDK_AVAILABLE /
_is_dry_run / _get_quote_ctx / _reset_quote_ctx —— 这些名字必须留在本模块
命名空间；从 common/errors import 进来的名字一律裸名调用。
"""
import os
import threading
import time

from autotrade.utils.logger import logger
from autotrade.broker.common import (
    OPEND_HOST,
    OPEND_PORT,
    OpenQuoteContext,
    QUOTE_TZ,  # noqa: F401  本模块代码不直接用，但 test_quote_snapshot 读 bc.QUOTE_TZ
    RET_OK,
    SDK_AVAILABLE,
    _is_dry_run,
    _quote_epoch,
)
from autotrade.broker.errors import (
    _NO_PERMISSION_HINTS,
    _RATE_LIMIT_HINTS,
    _is_definitely_missing,
)

# Quote context（行情）独立单例：跟 trd_ctx 解耦避免互相干扰
# 必须用 threading.Lock 而非 asyncio.Lock：SDK 同步调用通过 to_thread 跑，
# 多个 watcher 并发调时是真线程并发，asyncio lock 不管用
_quote_ctx = None
_quote_lock = threading.Lock()

# 真盘行情限频 backoff（snapshot 60 次/30s 命中后整段冷却）
_quote_backoff_until: float = 0.0

# get_last_price 真盘路径日志去重（早期未实现时用，保留作为 fallback warning）
_real_quote_warned_once: bool = False
_real_quote_warned_codes: set[str] = set()

# 行情新鲜度阈值（秒）：snapshot.update_time 比 now 旧超过这个值视为 stale
# 延迟数据账户拿到的报价 ~15min 旧，会全部被这个阈值过滤掉 → 启动 probe 时会暴露
QUOTE_FRESHNESS_SEC = 60.0


def _get_quote_ctx():
    """懒加载 quote_ctx 单例。失败抛异常由调用方 catch。"""
    global _quote_ctx
    if _quote_ctx is None:
        if not SDK_AVAILABLE:
            raise RuntimeError("moomoo SDK not installed")
        logger.info(f"[broker] 连接 quote OpenD {OPEND_HOST}:{OPEND_PORT}")
        _quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    return _quote_ctx


def _reset_quote_ctx():
    """quote 链路异常时重置；下次调用会重连。
    跟 _reset_ctx 解耦——交易和行情走两条独立 socket，互不影响。
    持 _quote_lock 执行，避免把并发 snapshot 正在用的 ctx 从脚下关掉。"""
    global _quote_ctx
    with _quote_lock:
        if _quote_ctx is not None:
            try:
                _quote_ctx.close()
            except Exception:
                pass
        _quote_ctx = None
    logger.warning("[broker] quote_ctx reset, will reconnect on next call")


# no-permission 退避日志节流：每个 300s 退避周期到期后 SL/TP 各重探一次，
# 每次都打 WARNING 一夜能刷 ~200 行（7/9 实测）。状态没变化时只在
# 首次 + 每小时提醒一次，其余降为 DEBUG。
# 哨兵必须是 None 而非 0.0：monotonic 是**开机以来**的秒数，uptime < 1h 的
# 机器（重启后的生产机、CI runner）上 `now - 0.0 < 3600` 会把首条 WARNING
# 吞掉——7/14 CI 实锤（本地 uptime 数天所以测不出来）。
_no_perm_last_warn: "float | None" = None
_NO_PERM_WARN_INTERVAL = 3600.0


def _snapshot(codes: list) -> "tuple[int, object]":
    """单层 wrapper：处理 lock、限频 backoff、异常 reset。返回 (ret, df_or_msg)。

    设计见 docs/realtime_quote_design.md (PR 1)。
    """
    global _quote_backoff_until, _no_perm_last_warn
    now = time.monotonic()
    if now < _quote_backoff_until:
        return -1, f"backoff (limit/quota) for another {_quote_backoff_until - now:.0f}s"

    try:
        with _quote_lock:
            ctx = _get_quote_ctx()
            ret, df = ctx.get_market_snapshot(codes)
    except Exception as e:
        logger.exception("[broker] snapshot exception")
        _reset_quote_ctx()
        return -1, f"exception: {type(e).__name__}"

    if ret != RET_OK:
        msg = str(df)
        msg_lower = msg.lower()
        # 限频/配额 → 短退避。7/8 实测 moomoo 的限频报错原文是
        # "request failed due to high frequency. Maximum 60 times per 30 seconds."
        # ——不含 quota/limit 字样，旧关键词接不住 → watcher 不退避持续硬打。
        if any(k in msg_lower for k in _RATE_LIMIT_HINTS):
            _quote_backoff_until = time.monotonic() + 60.0
            logger.warning(f"[broker] snapshot quota/limit exceeded, backoff 60s: {msg[:120]}")
        # 无期权行情权限 → 长退避。权限不会在一次 tick 之间凭空出现，
        # 但每次失败的调用**照样消耗 60/30s 频率配额**（7/8 整夜被 watcher
        # 打满，validate_option_codes 全靠 fail-open 才没误拒买单）。
        # 5 分钟重试一次：中途开通订阅也能在几分钟内自动恢复。
        elif any(k in msg_lower for k in _NO_PERMISSION_HINTS):
            _quote_backoff_until = time.monotonic() + 300.0
            note = (
                f"[broker] snapshot no-permission, backoff 300s "
                f"(watcher 轮询暂停，避免打满频率配额): {msg[:120]}"
            )
            if (_no_perm_last_warn is None
                    or time.monotonic() - _no_perm_last_warn >= _NO_PERM_WARN_INTERVAL):
                _no_perm_last_warn = time.monotonic()
                logger.warning(note)
            else:
                logger.debug(note)
    return ret, df


def get_last_prices(codes: list) -> dict:
    """批量取期权最新价。SL/TP/EOD watcher 应每 tick 调用一次（而非 N×get_last_price）。

    Returns:
        {code: float | None}，找不到/stale/无成交 一律 None
    """
    out = {c: None for c in codes}
    if not codes:
        return out

    if _is_dry_run():
        for c in codes:
            per = os.getenv(f"MOCK_LAST_PRICE_{c}")
            if per:
                out[c] = float(per)
                continue
            fixed = os.getenv("MOCK_LAST_PRICE")
            if fixed:
                out[c] = float(fixed)
        return out

    ret, df = _snapshot(codes)
    if ret != RET_OK or df is None:
        return out
    if not hasattr(df, "iterrows") or len(df) == 0:
        return out

    import pandas as pd  # 仅 SDK 路径需要，pandas 是 moomoo 必装依赖
    now_ts = time.time()
    for _, row in df.iterrows():
        code = row.get("code")
        if code not in out:
            continue
        last = row.get("last_price")
        if pd.isna(last) or last is None or last <= 0:
            continue
        # freshness check：避免 OpenD 持旧 cache 或延迟数据账户。
        # update_time 是无 tz 的美东时间，必须按 QUOTE_TZ 本地化再转 epoch
        # （当 UTC 解析会整体偏早 4-5h，所有实时报价都被误判 stale）。
        try:
            ts = _quote_epoch(row["update_time"])
            if now_ts - ts > QUOTE_FRESHNESS_SEC:
                continue
        except Exception:
            pass  # update_time 缺失时仍信任 snapshot（少见）
        out[code] = float(last)
    return out


def get_last_price(option_code: str):
    """查单个期权最新成交价。SL / EOD watcher 用。

    内部走 get_last_prices([code])，统一一条码路径。

    Returns:
        float 最新价；None 表示拿不到（watcher 应跳过该仓位）

    DRY_RUN 路径：
        - 默认返回 None（不误触发 SL）
        - 设 MOCK_LAST_PRICE=0.5 强制固定价
        - 设 MOCK_LAST_PRICE_<CODE>=0.5 针对单 option_code 设价
    """
    return get_last_prices([option_code]).get(option_code)


def _validate_one(code: str) -> bool:
    """单 code 校验。返回 True=可下单（含权限不足/瞬时失败时的"未知放行"），
    False=**确认**不存在。"""
    ret, df = _snapshot([code])
    if ret != RET_OK:
        # 只有明确 "Unknown stock" 类才判不存在；quota backoff、超时等
        # 瞬时失败一律放行，让 broker 做最终裁决
        return not _is_definitely_missing(str(df))
    if df is None or not hasattr(df, "iterrows") or len(df) == 0:
        return False
    return any(row.get("code") == code for _, row in df.iterrows())


def validate_option_codes(codes: list) -> dict:
    """下单前预校验 option_code 是否存在于 OPRA 链上。

    策略：
    1. 先一次性 batch snapshot（最省 RTT）
    2. batch 失败时若是"no permission"，全部降级返 True（让 broker 自己判）
    3. batch 失败时若是"unknown stock"类（某个 code 让整 batch 挂掉），
       拆成 per-code 重试 —— 隔离坏 code，让好的能正常 validate
    4. batch 成功，按 snapshot 出现与否标 True/False

    Returns:
        {code: True/False}。True = 存在/未知（可让 broker 试）；False = 确认不存在
    """
    out = {c: False for c in codes}
    if not codes:
        return out
    if _is_dry_run():
        return {c: True for c in codes}

    ret, df = _snapshot(codes)

    if ret == RET_OK and df is not None and hasattr(df, "iterrows"):
        # 正常：只把出现在 snapshot 的标 True
        for _, row in df.iterrows():
            code = row.get("code")
            if code in out:
                out[code] = True
        return out

    # batch 失败处理
    msg_lower = str(df).lower()
    if any(k in msg_lower for k in _NO_PERMISSION_HINTS):
        logger.warning(
            "[broker] validate skipped: no US options quote permission "
            f"({str(df)[:120]}). 让 broker 自行判断 contract 存在性。"
            " 要启用预校验，请在 moomoo app 订阅 US MarketOptions Lv1+。"
        )
        return {c: True for c in codes}

    # 瞬时失败（quota backoff / 超时 / OpenD 抖动）→ fail-open 全部放行。
    # 之前 fail-closed：backoff 期间 60s 内所有合法买单都被
    # "contract not found" 误拒 —— 预校验不能变成闸门。
    if not _is_definitely_missing(str(df)):
        logger.warning(
            f"[broker] validate transient failure ({str(df)[:100]}), "
            f"fail-open: 放行 {len(codes)} 个 code 让 broker 判定"
        )
        return {c: True for c in codes}

    # 明确 "Unknown stock" 类（一个坏 code 拖累整 batch）
    # 拆成 per-code 重查，隔离坏 code。代价 = N 次 RTT，但只在 batch 失败时才走
    if len(codes) > 1:
        logger.info(
            f"[broker] batch validate failed ({str(df)[:80]}), "
            f"falling back to per-code check for {len(codes)} codes"
        )
        for c in codes:
            out[c] = _validate_one(c)
        return out

    # 单 code 且明确不存在 → 拒
    logger.warning(f"[broker] validate rejected {codes[0]}: {str(df)[:120]}")
    return out


# Probe quote access tier，状态码用于 banner 着色和后续判断
QUOTE_OK = "ok"                # 期权 snapshot 真返价 + 新鲜
QUOTE_DELAYED = "delayed"      # snapshot 通了但行情滞后（疑似 delayed-data tier）
QUOTE_NO_PERMISSION = "no_perm"  # 账户没 US MarketOptions 订阅
QUOTE_ERROR = "error"          # 其它失败（OpenD 连不上 / chain 取不到 / etc.）


def probe_quote_access() -> tuple[str, str]:
    """探测期权行情订阅状态，决定 SL/TP/EOD watcher 真盘是否能工作。

    步骤：
      1. quote_ctx 连得上吗
      2. 个股 snapshot（US.SPY）能拿到吗 —— 这个不要 OPRA 权限
      3. 拿一个真实存在的 SPY 期权 code（via get_option_chain）
      4. 对该 code 调 snapshot —— "No permission" 返 NO_PERMISSION
      5. 检查报价新鲜度 —— age > 15min 视为 DELAYED

    不阻塞启动，调用方根据返回决定 banner / warn / 是否启动 watcher。

    Returns:
        (status, message) where status ∈ {OK, DELAYED, NO_PERMISSION, ERROR}
    """
    if _is_dry_run():
        return QUOTE_OK, "DRY_RUN: 跳过期权行情探测"
    if not SDK_AVAILABLE:
        return QUOTE_ERROR, "moomoo SDK 未安装"

    # 1. quote ctx 连得通
    try:
        ctx = _get_quote_ctx()
    except Exception as e:
        return QUOTE_ERROR, f"quote_ctx 连接失败: {type(e).__name__}: {e}"

    # 2. 个股 snapshot —— 基本 quote 连通性 + tier 探测
    try:
        ret, df = ctx.get_market_snapshot(["US.SPY"])
    except Exception as e:
        _reset_quote_ctx()
        return QUOTE_ERROR, f"个股 snapshot 抛异常: {type(e).__name__}: {e}"
    if ret != RET_OK:
        return QUOTE_ERROR, f"个股 snapshot 失败（基本 quote 都不通）: {str(df)[:120]}"

    # 3. 取一个真实期权 code（任何 SPY 近月 ATM 附近都行，列表第一个）
    from datetime import date as _date, timedelta as _timedelta
    today = _date.today()
    # 下周三 → 让 weekly/monthly 大概率都覆盖；不论今天是周几
    target = today + _timedelta(days=(2 - today.weekday()) % 7 + 7)
    try:
        ret, chain = ctx.get_option_chain(
            code="US.SPY", start=target.isoformat(), end=target.isoformat(),
        )
    except Exception as e:
        return QUOTE_ERROR, f"get_option_chain 抛异常: {type(e).__name__}: {e}"
    if ret != RET_OK:
        # get_option_chain 本身也吃 OPRA 权限（实测 6/30）
        msg_lower = str(chain).lower()
        if any(k in msg_lower for k in _NO_PERMISSION_HINTS):
            return QUOTE_NO_PERMISSION, (
                "账户缺 US MarketOptions Lv1+ 行情订阅（get_option_chain 失败）。\n"
                "  影响：SL / TP / EOD watcher 真盘下 no-op；validate_option_codes "
                "降级到不预校验（让 broker 拒）。\n"
                "  开通方法：moomoo app → 我的 → 行情订阅 → US MarketOptions Lv1。"
            )
        return QUOTE_ERROR, f"无法获取 SPY {target} 期权链: {str(chain)[:120]}"
    if chain is None or len(chain) == 0:
        return QUOTE_ERROR, f"SPY {target} 期权链为空（可能无该到期日，换一天试）"
    sample_code = chain.iloc[0]["code"]

    # 4. 对真实期权 code 试 snapshot —— 触发 OPRA 权限检查
    try:
        ret, opt_df = ctx.get_market_snapshot([sample_code])
    except Exception as e:
        return QUOTE_ERROR, f"OPRA snapshot 抛异常: {type(e).__name__}: {e}"
    if ret != RET_OK:
        msg_lower = str(opt_df).lower()
        if any(k in msg_lower for k in _NO_PERMISSION_HINTS):
            return QUOTE_NO_PERMISSION, (
                "账户缺 US MarketOptions Lv1+ 行情订阅。\n"
                "  影响：SL / TP / EOD watcher 真盘下 no-op；validate_option_codes "
                "降级到不预校验（让 broker 拒）。\n"
                "  开通方法：moomoo app → 我的 → 行情订阅 → US MarketOptions Lv1。"
            )
        return QUOTE_ERROR, f"OPRA snapshot 异常: {str(opt_df)[:200]}"

    # 5. 新鲜度（delayed-data tier 通常滞后 15 分钟）
    try:
        update_ts = _quote_epoch(opt_df.iloc[0]["update_time"])
        age = time.time() - update_ts
        if age > 900:  # 15 分钟
            return QUOTE_DELAYED, (
                f"OPRA 行情可拿但滞后 {age:.0f}s（疑似 delayed-data tier）。\n"
                f"  影响：watcher 的 60s 新鲜度过滤会把所有报价当 stale 丢弃 → "
                f"实际 SL/TP/EOD 仍然 no-op。\n"
                f"  开通方法：升级到 US MarketOptions Lv1 实时行情。"
            )
    except Exception:
        pass  # 拿不到 update_time 时不阻断

    return QUOTE_OK, f"OPRA 行情可用 + 实时（sample {sample_code}）"
