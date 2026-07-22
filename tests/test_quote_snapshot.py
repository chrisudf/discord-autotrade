"""quote_ctx / get_last_prices / validate_option_codes 行为锁定。

不依赖真实 moomoo 连接：mock OpenQuoteContext.get_market_snapshot。
"""
import os
import time
from unittest.mock import MagicMock, patch
import pytest

import autotrade.broker.quote as bc


@pytest.fixture(autouse=True)
def _reset_quote_state(monkeypatch):
    """每个测试独立 quote_ctx / backoff，避免前一个测试污染状态。"""
    # 清掉 quote 单例和 backoff
    monkeypatch.setattr(bc, "_quote_ctx", None)
    monkeypatch.setattr(bc, "_quote_backoff_until", 0.0)
    # 强制走真盘路径
    monkeypatch.setattr(bc, "_is_dry_run", lambda: False)
    # SDK_AVAILABLE 假设为 True
    monkeypatch.setattr(bc, "SDK_AVAILABLE", True)
    yield


def _make_snapshot_row(code, last_price, update_offset_s=0):
    """构造一行 mock snapshot 数据。update_offset_s 负数代表多久之前更新过。

    模拟生产实际：moomoo update_time 是**无 tz 的美东时间**字符串，
    broker 端用 _quote_epoch 按 QUOTE_TZ（默认 America/New_York）本地化。
    """
    import pandas as pd
    ts = (
        pd.Timestamp.now(tz=bc.QUOTE_TZ).tz_localize(None)
        + pd.Timedelta(seconds=update_offset_s)
    )
    return {
        "code": code,
        "last_price": last_price,
        "update_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _mock_ctx_returning(snapshot_rows, ret=None):
    """构造一个 mock OpenQuoteContext，其 get_market_snapshot 返回指定 rows。"""
    import pandas as pd
    df = pd.DataFrame(snapshot_rows) if snapshot_rows else pd.DataFrame()
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (ret if ret is not None else bc.RET_OK, df)
    return ctx


# ===== get_last_prices =====

def test_get_last_prices_normal(monkeypatch):
    rows = [
        _make_snapshot_row("US.AAPL260117C200000", 5.20),
        _make_snapshot_row("US.NVDA260117P800000", 12.30),
    ]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.get_last_prices(["US.AAPL260117C200000", "US.NVDA260117P800000"])
    assert out["US.AAPL260117C200000"] == 5.20
    assert out["US.NVDA260117P800000"] == 12.30


def test_get_last_prices_stale_filtered(monkeypatch):
    """update_time > QUOTE_FRESHNESS_SEC 旧的应返回 None"""
    rows = [_make_snapshot_row("US.AAPL", 5.20, update_offset_s=-300)]  # 5 分钟前
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.get_last_prices(["US.AAPL"])
    assert out["US.AAPL"] is None  # 太旧


def test_get_last_prices_zero_price_filtered(monkeypatch):
    """last_price=0 或 NaN 视为没成交，返回 None"""
    import pandas as pd
    rows = [
        _make_snapshot_row("US.A", 0),
        _make_snapshot_row("US.B", float("nan")),
        _make_snapshot_row("US.C", 1.50),
    ]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.get_last_prices(["US.A", "US.B", "US.C"])
    assert out["US.A"] is None
    assert out["US.B"] is None
    assert out["US.C"] == 1.50


def test_get_last_prices_empty_input():
    out = bc.get_last_prices([])
    assert out == {}


def test_get_last_prices_snapshot_failure_returns_all_none(monkeypatch):
    """ret != RET_OK 时全部 None"""
    monkeypatch.setattr(
        bc, "_get_quote_ctx",
        lambda: _mock_ctx_returning([], ret=-1),
    )
    out = bc.get_last_prices(["US.X", "US.Y"])
    assert out == {"US.X": None, "US.Y": None}


def test_get_last_prices_exception_resets_ctx(monkeypatch):
    """SDK 抛异常 → reset ctx → 返回全 None"""
    ctx = MagicMock()
    ctx.get_market_snapshot.side_effect = RuntimeError("disconnected")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    reset_called = []
    monkeypatch.setattr(bc, "_reset_quote_ctx", lambda: reset_called.append(True))

    out = bc.get_last_prices(["US.X"])
    assert out == {"US.X": None}
    assert reset_called == [True]


def test_get_last_prices_quota_triggers_backoff(monkeypatch):
    """ret != RET_OK 含 'quota' 触发 60s backoff"""
    monkeypatch.setattr(
        bc, "_get_quote_ctx",
        lambda: _mock_ctx_returning([], ret=-1),
    )
    # 让 get_market_snapshot 返回 quota 错误
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (-1, "request quota exceeded")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)

    out = bc.get_last_prices(["US.X"])
    assert out == {"US.X": None}
    assert bc._quote_backoff_until > time.monotonic() + 30  # 至少 30s 后


def test_get_last_prices_dry_run_uses_mock_env(monkeypatch):
    monkeypatch.setattr(bc, "_is_dry_run", lambda: True)
    monkeypatch.setenv("MOCK_LAST_PRICE_US.A", "9.99")
    monkeypatch.setenv("MOCK_LAST_PRICE", "1.23")
    out = bc.get_last_prices(["US.A", "US.B"])
    assert out["US.A"] == 9.99
    assert out["US.B"] == 1.23


# ===== get_last_price (single code wrapper) =====

def test_get_last_price_single(monkeypatch):
    rows = [_make_snapshot_row("US.X", 7.50)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    assert bc.get_last_price("US.X") == 7.50


# ===== validate_option_codes =====

def test_validate_option_codes_found(monkeypatch):
    """snapshot 里出现的 code 视为 valid"""
    rows = [_make_snapshot_row("US.AAPL", 5.20)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.validate_option_codes(["US.AAPL", "US.MISSING"])
    assert out["US.AAPL"] is True
    assert out["US.MISSING"] is False


def test_validate_option_codes_zero_price_still_valid(monkeypatch):
    """contract 存在但 last_price=0（今日无成交）仍然 valid，能下单"""
    rows = [_make_snapshot_row("US.NEWLISTED", 0)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.validate_option_codes(["US.NEWLISTED"])
    assert out["US.NEWLISTED"] is True


def test_validate_option_codes_dry_run_always_true(monkeypatch):
    monkeypatch.setattr(bc, "_is_dry_run", lambda: True)
    out = bc.validate_option_codes(["US.ANYTHING"])
    assert out == {"US.ANYTHING": True}


def test_validate_option_codes_definitely_missing_is_false(monkeypatch):
    """明确 'Unknown stock' 类错误 → False（真不存在，拒单合理）"""
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (-1, "Unknown stock. US.X")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    out = bc.validate_option_codes(["US.X"])
    assert out == {"US.X": False}


def test_validate_option_codes_transient_error_fails_open(monkeypatch):
    """瞬时失败（quota/超时/未知错误）→ fail-open 放行，让 broker 判定。

    回归：之前 fail-closed，snapshot 限频 backoff 期间 60s 内所有合法买单
    都被 'contract not found' 误拒。预校验是优化不是闸门。
    """
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (-1, "request timeout, try again later")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    out = bc.validate_option_codes(["US.X"])
    assert out == {"US.X": True}


def test_validate_option_codes_no_permission_falls_back_to_true(monkeypatch):
    """账户没期权行情权限时降级：返全 True 让 broker 自己判，否则会把所有信号 reject"""
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (
        -1,
        "No permission to get quotes for US.HOOD260702C108000. "
        "Please check US MarketOptions quote permissions.",
    )
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    out = bc.validate_option_codes(["US.A", "US.B"])
    assert out == {"US.A": True, "US.B": True}


def test_validate_option_codes_empty():
    assert bc.validate_option_codes([]) == {}


# ===== backoff =====

def test_snapshot_respects_backoff(monkeypatch):
    """_quote_backoff_until 大于 now 时不调 SDK，直接返回 backoff 错误"""
    monkeypatch.setattr(bc, "_quote_backoff_until", time.monotonic() + 60)
    ctx_called = []
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx_called.append(True) or _mock_ctx_returning([]))
    ret, msg = bc._snapshot(["US.X"])
    assert ret != bc.RET_OK
    assert "backoff" in str(msg).lower()
    assert ctx_called == []  # 完全没调 SDK


# ===== probe_quote_access =====

def _mock_ctx_for_probe(stock_ok=True, chain_ok=True, opt_ret=None, opt_df=None):
    """构造一个 mock ctx，让 probe_quote_access 走完 5 步"""
    import pandas as pd
    ctx = MagicMock()

    def snapshot_dispatch(codes):
        if codes == ["US.SPY"]:
            if not stock_ok:
                return -1, "stock snapshot failed"
            return bc.RET_OK, pd.DataFrame([_make_snapshot_row("US.SPY", 600.0)])
        # 期权 snapshot
        if opt_ret is not None:
            return opt_ret, opt_df
        return bc.RET_OK, pd.DataFrame([_make_snapshot_row(codes[0], 5.0)])

    ctx.get_market_snapshot.side_effect = snapshot_dispatch

    if chain_ok:
        ctx.get_option_chain.return_value = (
            bc.RET_OK,
            pd.DataFrame([{"code": "US.SPY260710C600000", "strike_price": 600.0}]),
        )
    else:
        ctx.get_option_chain.return_value = (-1, "chain failed")
    return ctx


def test_probe_quote_access_ok(monkeypatch):
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_for_probe())
    status, msg = bc.probe_quote_access()
    assert status == bc.QUOTE_OK


def test_probe_quote_access_no_permission(monkeypatch):
    """OPRA 拒绝 → NO_PERMISSION 状态码"""
    ctx = _mock_ctx_for_probe(
        opt_ret=-1,
        opt_df="No permission to get quotes for US.SPY260710C600000. "
               "Please check US MarketOptions quote permissions.",
    )
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    status, msg = bc.probe_quote_access()
    assert status == bc.QUOTE_NO_PERMISSION
    assert "US MarketOptions" in msg


def test_probe_quote_access_delayed(monkeypatch):
    """update_time > 15 分钟旧 → DELAYED"""
    import pandas as pd
    delayed_row = _make_snapshot_row(
        "US.SPY260710C600000", 5.0, update_offset_s=-1200,  # 20 分钟前
    )
    ctx = _mock_ctx_for_probe(
        opt_ret=bc.RET_OK, opt_df=pd.DataFrame([delayed_row]),
    )
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    status, msg = bc.probe_quote_access()
    assert status == bc.QUOTE_DELAYED
    assert "delayed" in msg.lower() or "滞后" in msg


def test_probe_quote_access_chain_fail(monkeypatch):
    """无法拿 SPY 期权链 → ERROR"""
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_for_probe(chain_ok=False))
    status, _ = bc.probe_quote_access()
    assert status == bc.QUOTE_ERROR


def test_probe_quote_access_chain_no_permission_routes_to_no_perm(monkeypatch):
    """get_option_chain 也吃 OPRA 权限——返 'no permission' 时归 NO_PERMISSION 而非 ERROR"""
    import pandas as pd
    ctx = MagicMock()
    # stock snapshot 通过
    ctx.get_market_snapshot.return_value = (
        bc.RET_OK, pd.DataFrame([_make_snapshot_row("US.SPY", 600.0)]),
    )
    # chain 返 no permission
    ctx.get_option_chain.return_value = (
        -1, "No permission to get quotes for US.SPY. Please check US MarketOptions quote permissions.",
    )
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    status, msg = bc.probe_quote_access()
    assert status == bc.QUOTE_NO_PERMISSION
    assert "US MarketOptions" in msg


def test_probe_quote_access_stock_snapshot_fail(monkeypatch):
    """连个股 quote 都不通 → ERROR"""
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_for_probe(stock_ok=False))
    status, _ = bc.probe_quote_access()
    assert status == bc.QUOTE_ERROR


def test_probe_quote_access_dry_run_skipped(monkeypatch):
    monkeypatch.setattr(bc, "_is_dry_run", lambda: True)
    status, msg = bc.probe_quote_access()
    assert status == bc.QUOTE_OK
    assert "DRY_RUN" in msg


def test_get_last_prices_et_realtime_not_stale(monkeypatch):
    """回归：update_time 是无 tz 的 ET 字符串。当 UTC 解析会整体偏早 4-5h，
    所有实时报价都被 60s 新鲜度检查误判 stale → 真盘 SL/TP/EOD 永远拿不到价。
    现在按 QUOTE_TZ 本地化，刚更新的报价必须通过。"""
    rows = [_make_snapshot_row("US.AAPL", 5.20, update_offset_s=-5)]  # 5 秒前(ET)
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx_returning(rows))
    out = bc.get_last_prices(["US.AAPL"])
    assert out["US.AAPL"] == 5.20


# === 7/8 复盘回归：限频/无权限退避 ===

def test_snapshot_high_frequency_triggers_backoff(monkeypatch):
    """moomoo 限频报错原文不含 quota/limit 字样，旧关键词接不住 → 不退避硬打。"""
    import time as _time
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (
        -1, "Get Market Snapshot request failed due to high frequency. "
            "Maximum 60 times per 30 seconds.")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    bc.get_last_prices(["US.X"])
    assert bc._quote_backoff_until > _time.monotonic(), "限频必须触发退避"


def test_snapshot_no_permission_long_backoff(monkeypatch):
    """无 OPRA 权限 → 300s 长退避。7/8 整夜 watcher 空转打满频率配额，
    连 validate 都被挤到限频（靠 fail-open 才没误拒买单）。"""
    import time as _time
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (
        -1, "No permission to get quotes for US.X. "
            "Please check US MarketOptions quote permissions.")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    bc.get_last_prices(["US.X"])
    # 长退避：显著大于普通 60s 档
    assert bc._quote_backoff_until > _time.monotonic() + 200


def test_snapshot_no_permission_warn_throttled(monkeypatch):
    """7/9 实测：300s 退避到期后 SL/TP 各重探一次，每次都 WARNING
    一夜刷 ~200 行。同原因一小时内只 WARNING 一次，其余 DEBUG。"""
    from autotrade.utils.logger import logger as _lg
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (
        -1, "No permission to get quotes for US.X.")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    # None = "进程内没警告过"。不能用 0.0：uptime < 1h 的机器（CI runner）上
    # monotonic - 0.0 < 3600，首条 WARNING 会被节流吞掉，测试在 CI 假失败
    monkeypatch.setattr(bc, "_no_perm_last_warn", None)

    records = []
    sink = _lg.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    try:
        bc.get_last_prices(["US.X"])
        monkeypatch.setattr(bc, "_quote_backoff_until", 0.0)  # 模拟退避到期
        bc.get_last_prices(["US.X"])
    finally:
        _lg.remove(sink)

    warns = [r for r in records
             if r[0] == "WARNING" and "no-permission" in r[1]]
    debugs = [r for r in records
              if r[0] == "DEBUG" and "no-permission" in r[1]]
    assert len(warns) == 1, f"一小时内同原因只应 WARNING 一次: {warns}"
    assert len(debugs) == 1, "第二次应降为 DEBUG"
