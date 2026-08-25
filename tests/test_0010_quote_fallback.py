"""[0010] CLOSE 无价参照 → 实时报价 fallback 回归。

7/25 夜实锤：KC "Trimmed AVGO +20%"（只报盈利不喊价）→ calc_sell_limit
无 signal_price → 拒卖 + TG 人工接管——半夜没人盯，trim 全漏。
0010 起：CLOSE_QUOTE_FALLBACK（缺省开）时先取实时 bid（优先）/last 做参照；
拿不到新鲜报价 → 拒卖底线原样保留（7/10 enrich "$NVDA all out" 误匹配
就是靠无价拒卖挡下来的）；env 关 → 行为与 0010 之前逐字一致。

分三层：
  1. broker.quote.get_sell_ref_price 单元（mock snapshot，验证 bid/last
     优先级、60s 新鲜度门、backoff 继承——不开第二条 snapshot 路径）
  2. policy.pricing.calc_sell_limit 纯函数（signal 优先级不变 + quote_ref 档）
  3. close_flow 端到端（7/25 语料逐字回放：有报价卖出 / 无报价拒卖 / env 关拒卖）
"""
import time
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import autotrade.broker.quote as bc
from autotrade.listener import close_flow, dedup
from autotrade.policy.pricing import calc_sell_limit
from autotrade.storage import positions_db

# 7/25 夜语料原文（无 signal_price，pct 默认 33）
AVGO_TRIM_RAW = "@everyone\nKC Trades Bot:Trimmed AVGO +20% 💰"
# 7/24 夜带价孪生形态（signal_price=2.20，fallback 不得介入）
AVGO_TRIM_WITH_PRICE = "BANG! Trimmed AVGO @ 2.20"


# ============================================================
# 1. get_sell_ref_price 单元（mock snapshot，风格对齐 test_quote_snapshot）
# ============================================================

@pytest.fixture(autouse=True)
def _reset_quote_state(monkeypatch):
    """每个测试独立 quote_ctx / backoff，避免前后测试互相污染。"""
    monkeypatch.setattr(bc, "_quote_ctx", None)
    monkeypatch.setattr(bc, "_quote_backoff_until", 0.0)
    monkeypatch.setattr(bc, "_is_dry_run", lambda: False)
    monkeypatch.setattr(bc, "SDK_AVAILABLE", True)
    yield


def _make_ref_row(code, bid_price, last_price, update_offset_s=0,
                  with_update_time=True):
    """构造含 bid 的 mock snapshot 行（update_time 无 tz 美东时间，
    与生产 moomoo 返回一致，见 test_quote_snapshot._make_snapshot_row）。"""
    import pandas as pd
    row = {"code": code, "bid_price": bid_price, "last_price": last_price}
    if with_update_time:
        ts = (
            pd.Timestamp.now(tz=bc.QUOTE_TZ).tz_localize(None)
            + pd.Timedelta(seconds=update_offset_s)
        )
        row["update_time"] = ts.strftime("%Y-%m-%d %H:%M:%S")
    return row


def _mock_ctx(rows, ret=None):
    import pandas as pd
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (ret if ret is not None else bc.RET_OK, df)
    return ctx


def test_sell_ref_prefers_bid_over_last(monkeypatch):
    """bid>0 时用 bid：卖单要吃穿 bid，last 可能是几分钟前的成交高点。"""
    rows = [_make_ref_row("US.AVGO250725C415000", bid_price=1.90, last_price=2.10)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx(rows))
    assert bc.get_sell_ref_price("US.AVGO250725C415000") == 1.90


def test_sell_ref_falls_back_to_last_when_bid_zero(monkeypatch):
    """无人出价（bid=0）的清淡合约退回 last。"""
    rows = [_make_ref_row("US.X", bid_price=0, last_price=2.10)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx(rows))
    assert bc.get_sell_ref_price("US.X") == 2.10


def test_sell_ref_falls_back_to_last_when_bid_nan(monkeypatch):
    rows = [_make_ref_row("US.X", bid_price=float("nan"), last_price=1.55)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx(rows))
    assert bc.get_sell_ref_price("US.X") == 1.55


def test_sell_ref_none_when_no_bid_no_last(monkeypatch):
    """bid/last 都无 → None：拒卖底线由调用方保持。"""
    rows = [_make_ref_row("US.X", bid_price=0, last_price=0)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx(rows))
    assert bc.get_sell_ref_price("US.X") is None


def test_sell_ref_stale_quote_returns_none(monkeypatch):
    """过不了 60s 新鲜度门（QUOTE_FRESHNESS_SEC）→ None。
    stale bid 用来定卖价比用来触发 watcher 更危险——挂几分钟前的
    低 bid 等于白送。"""
    rows = [_make_ref_row("US.X", bid_price=1.90, last_price=2.10,
                          update_offset_s=-300)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx(rows))
    assert bc.get_sell_ref_price("US.X") is None


def test_sell_ref_missing_update_time_still_trusted(monkeypatch):
    """update_time 缺失时仍信任 snapshot——与 get_last_prices 同一语义。"""
    rows = [_make_ref_row("US.X", bid_price=1.90, last_price=2.10,
                          with_update_time=False)]
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx(rows))
    assert bc.get_sell_ref_price("US.X") == 1.90


def test_sell_ref_snapshot_failure_returns_none(monkeypatch):
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: _mock_ctx([], ret=-1))
    assert bc.get_sell_ref_price("US.X") is None


def test_sell_ref_inherits_snapshot_backoff(monkeypatch):
    """backoff 期间完全不碰 SDK——证明复用 _snapshot 的配额/退避机制，
    没有开第二条 snapshot 路径（契约硬要求）。"""
    monkeypatch.setattr(bc, "_quote_backoff_until", time.monotonic() + 60)
    ctx_called = []
    monkeypatch.setattr(
        bc, "_get_quote_ctx",
        lambda: ctx_called.append(True) or _mock_ctx([]),
    )
    assert bc.get_sell_ref_price("US.X") is None
    assert ctx_called == []  # 完全没调 SDK


def test_sell_ref_quota_error_triggers_shared_backoff(monkeypatch):
    """限频报错经 _snapshot 触发的 backoff 是全局共享的（watcher 同款）。"""
    ctx = MagicMock()
    ctx.get_market_snapshot.return_value = (
        -1, "request failed due to high frequency. Maximum 60 times per 30 seconds.")
    monkeypatch.setattr(bc, "_get_quote_ctx", lambda: ctx)
    assert bc.get_sell_ref_price("US.X") is None
    assert bc._quote_backoff_until > time.monotonic()


def test_sell_ref_dry_run_uses_mock_env(monkeypatch):
    """DRY_RUN 走 get_last_price 的 mock env；不设 mock 时缺省 None
    （不假装有报价）。"""
    monkeypatch.setattr(bc, "_is_dry_run", lambda: True)
    assert bc.get_sell_ref_price("US.NOMOCK") is None
    monkeypatch.setenv("MOCK_LAST_PRICE_US.A", "2.00")
    assert bc.get_sell_ref_price("US.A") == 2.00


# ============================================================
# 2. calc_sell_limit 纯函数
# ============================================================

def test_calc_sell_limit_signal_price_still_first_priority():
    """signal_price 优先级不变：喊价在场时 quote_ref 完全不参与。"""
    assert calc_sell_limit(1.0, 2.45, 99.0) == round(2.45 * 0.95, 2)


def test_calc_sell_limit_quote_ref_fallback():
    assert calc_sell_limit(1.0, None, 2.00) == 1.90


def test_calc_sell_limit_no_ref_returns_none():
    """都无 → None（拒卖）；avg_entry 永不作为参照（TSLA -5% 锁亏教训）。"""
    assert calc_sell_limit(3.33) is None            # 旧签名单参照调用仍兼容
    assert calc_sell_limit(3.33, None, None) is None
    assert calc_sell_limit(3.33, None, 0) is None   # 0 价参照无意义


# ============================================================
# 3. close_flow 端到端（7/25 语料逐字回放）
# ============================================================

def _open_avgo(qty: int = 3) -> str:
    code = "US.AVGO250725C415000"
    positions_db.open_or_add(
        option_code=code, symbol="AVGO", strike=415.0, side="CALL",
        expiry=date(2026, 7, 31), qty=qty, fill_price=1.65,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_0010",
    )
    dedup._close_fps.clear()
    return code


@pytest.mark.asyncio
async def test_noprice_trim_sells_with_quote_fallback(monkeypatch):
    """7/25 原文 + mock 实时报价 2.00 → 正常卖出，限价 = 2.00 × 0.95。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("CLOSE_QUOTE_FALLBACK", raising=False)  # 验证缺省=开
    code = _open_avgo(qty=3)

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append(kwargs)
        return {"success": True, "order_id": "Q1", "code": code,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    quote_mock = MagicMock(return_value=2.00)
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_RAW, msg_id=100101)

    quote_mock.assert_called_once_with(code)
    assert len(sell_called) == 1, "拿到实时参照后应正常卖出"
    assert sell_called[0]["limit_price"] == 1.90  # 2.00 × (1 - SELL_SLIP)
    assert sell_called[0]["qty"] == 1             # qty=3, pct=33 → ceil = 1
    text = "\n".join(notifications)
    assert "平仓成交" in text
    assert "无价格参照" not in text
    pos = positions_db.get(code)
    assert pos["qty_remaining"] == 2


@pytest.mark.asyncio
async def test_noprice_trim_rejected_when_quote_unavailable(monkeypatch):
    """报价拿不到（无 bid/last 或 stale）→ 拒卖 + TG 人工接管，语义原样保留；
    文案说明已尝试实时参照。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("CLOSE_QUOTE_FALLBACK", raising=False)
    code = _open_avgo(qty=3)

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    quote_mock = MagicMock(return_value=None)
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order") as sell_mock, \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_RAW, msg_id=100102)

    quote_mock.assert_called_once_with(code)
    sell_mock.assert_not_called()
    text = "\n".join(notifications)
    assert "无价格参照" in text, ("拒卖 TG 底线必须保留", text)
    assert "实时报价" in text, ("文案应说明已尝试实时参照仍不可得", text)
    assert "no matching" not in text.lower()
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN" and pos["qty_remaining"] == 3


@pytest.mark.asyncio
async def test_noprice_trim_env_off_behaves_like_before(monkeypatch):
    """CLOSE_QUOTE_FALLBACK=false → 完全不取报价，拒卖文案与 0010 之前逐字一致。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("CLOSE_QUOTE_FALLBACK", "false")
    code = _open_avgo(qty=3)

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    quote_mock = MagicMock(return_value=2.00)
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order") as sell_mock, \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_RAW, msg_id=100103)

    quote_mock.assert_not_called()  # env 关 = 连报价都不试
    sell_mock.assert_not_called()
    text = "\n".join(notifications)
    assert "无价格参照" in text
    assert "信号无价 \\+ OPRA 报价不可用" in text or "信号无价 + OPRA 报价不可用" in text, (
        "env 关时文案必须与 0010 之前一致", text,
    )
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN" and pos["qty_remaining"] == 3


@pytest.mark.asyncio
async def test_signal_price_close_never_touches_quote(monkeypatch):
    """带价 close（7/24 "BANG! Trimmed AVGO @ 2.20"）走 ref=signal 老路，
    fallback 完全不介入（钱路保守：现行为逐字不变）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("CLOSE_QUOTE_FALLBACK", raising=False)
    code = _open_avgo(qty=3)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append(kwargs)
        return {"success": True, "order_id": "Q2", "code": code,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    async def noop(msg):
        pass

    quote_mock = MagicMock(return_value=99.0)
    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_WITH_PRICE, msg_id=100104)

    quote_mock.assert_not_called()
    assert len(sell_called) == 1
    assert sell_called[0]["limit_price"] == round(2.20 * 0.95, 2)  # ref=signal
