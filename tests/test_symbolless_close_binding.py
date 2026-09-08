"""无 ticker 平仓喊话的绑定（9/2 TSLA 345P，-$166）。

当晚 KC 在开仓 90 秒内连喊三次平仓，一条都没执行：

    23:38:46  TSLA 345p 9/4 @ 2.40 day trade lotto   → 开仓 2 张 @2.59
    23:40:03  out half 2.82                           → parser skipped（无 ticker）
    23:40:05  2.82减半仓                              → [zh_unrecognized]（同上）
    23:43:52  trimmed Tesla 2.95, stop is at 2.35 now → 解析正确，撞上熔断
    05:50:21  EOD 强平 2 张 @1.76

KC 的习惯就是**开仓带 ticker、后续减仓不带**。`parse_close` 对这两条返回 None
是对的 —— 它手里只有一句话。缺的判据（哪个频道、哪个仓刚开、喊价对不对得上）
全在 close_flow 那一层。

本文件两节：
  §1 parse_symbolless_close —— 只做抽取，重点是**必须返回 None** 的那些
  §2 _bind_symbolless_close —— 绑定的四道闸门 + 端到端重放

**parse_close 的 None 契约一个字节都没动**（那 48 条断言仍然全绿），
本次是 additive 的第二个入口。
"""
import os
import sqlite3
from datetime import date, datetime
from unittest.mock import patch

import pytest

from autotrade.listener import close_flow, dedup
from autotrade.parsing.close_parser import parse_close, parse_symbolless_close
from autotrade.storage import positions_db

KC = "KC-期权-波段"


def _open(code, symbol, strike, side="PUT", entry=2.59, qty=2,
          channel=KC, opened_at=None):
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side=side,
        expiry=date(2026, 9, 4), qty=qty, fill_price=entry,
        category="lotto", apply_sl=False, eod_force_close=True, tags=["lotto"],
        channel_name=channel, msg_id="m1",
    )
    if opened_at:
        # 直接改 opened_at：open_or_add 只会写"现在"，而这里要的正是一条陈年仓位
        with sqlite3.connect(positions_db.DB_PATH) as c:
            c.execute("UPDATE positions SET opened_at=? WHERE option_code=?",
                      (opened_at, code))
    return code


# ============================================================
# 1. 抽取：这是不是一条"没写标的的平仓指令"
# ============================================================

@pytest.mark.parametrize("text,pct,price,lang", [
    ("out half 2.82", 50, 2.82, "en"),                    # 9/2 EN 原文
    ("2.82减半仓", 50, 2.82, "zh"),                        # 9/2 ZH 孪生
    ("KC Trades Bot:out half 2.82", 50, 2.82, "en"),      # 带中继标签
    ("trimmed more 2.82", 33, 2.82, "en"),                # 8/25 NVDA 同型
    ("out 25% more at 2.82", 25, 2.82, "en"),
])
def test_symbolless_instructions_are_extracted(text, pct, price, lang):
    r = parse_symbolless_close(text)
    assert r is not None, text
    assert (r["pct"], r["signal_price"], r["lang"]) == (pct, price, lang)
    assert r["symbols"] == []


@pytest.mark.parametrize("text,why", [
    ("out half TSLA 2.82", "有 ticker → 归 parse_close 管"),
    ("out half 2.82 of META", "ticker 不在持仓里 = 无仓可平，不是无标的"),
    ("out half", "没喊价 → 绑定没有凭据"),
    ("closing all positions here at 2.82", "bulk 有 BULK_TRIM 自己的路"),
    ("trimmed half at 2.82 yesterday, up 200%", "recap"),
    ("if you want to trim half here 2.82", "建议句"),
    ("I am holding half here 2.82", "作者自述持有"),
    ("2.82", "没有动作动词"),
    ("$SPY levels for the day: blue zone 2.82", "根本不是平仓"),
])
def test_these_must_stay_none(text, why):
    assert parse_symbolless_close(text) is None, why


def test_parse_close_contract_is_untouched():
    """本次改动是 additive 的第二个入口 —— 主解析器对这两条仍然返回 None。"""
    assert parse_close("out half 2.82", {"TSLA"}) is None
    assert parse_close("2.82减半仓", {"TSLA"}) is None


# ============================================================
# 2. 绑定：四道闸门
# ============================================================

def test_binds_to_the_lone_fresh_position_in_that_channel():
    code = _open("US.TSLA260904P345000BIND1", "TSLA", 345.0)
    try:
        b = close_flow._bind_symbolless_close("out half 2.82", KC)
        assert b is not None
        assert b["kind"] == "CLOSE"
        assert b["symbols"] == ["TSLA"]
        # 钉到具体合约，不是钉到 symbol —— 见下面 test_does_not_touch_the_old_swing
        assert (b["hint_strike"], b["hint_side"]) == (345.0, "PUT")
        assert b["pct"] == 50
    finally:
        positions_db.record_close(code, 2, 2.59, "manual", note="ut")


def test_does_not_touch_the_old_swing_of_the_same_symbol():
    """9/2 那晚 KC 频道同时持有 TSLA 345P（当日）和 TSLA 380C（8/14 的 swing）。

    只按 symbol 绑会把三周前的 swing 一起 trim 掉 —— hint_strike/hint_side
    必须指向当日那张。
    """
    fresh = _open("US.TSLA260904P345000BIND2", "TSLA", 345.0)
    old = _open("US.TSLA260918C380000BIND2", "TSLA", 380.0, side="CALL",
                entry=5.30, qty=1, opened_at="2026-08-14T19:10:08.327576Z")
    try:
        b = close_flow._bind_symbolless_close("out half 2.82", KC)
        assert b is not None
        assert (b["hint_strike"], b["hint_side"]) == (345.0, "PUT")
    finally:
        positions_db.record_close(fresh, 2, 2.59, "manual", note="ut")
        positions_db.record_close(old, 1, 5.30, "manual", note="ut")


def test_refuses_when_two_positions_opened_today_in_the_channel():
    a = _open("US.TSLA260904P345000BIND3", "TSLA", 345.0)
    b_ = _open("US.HOOD260904C108000BIND3", "HOOD", 108.0, side="CALL", entry=1.16)
    try:
        assert close_flow._bind_symbolless_close("out half 2.82", KC) is None
    finally:
        positions_db.record_close(a, 2, 2.59, "manual", note="ut")
        positions_db.record_close(b_, 2, 1.16, "manual", note="ut")


def test_refuses_across_channels():
    """频道是"同源唯一新仓"这个判据的全部重量所在（7/10 enrich 差点平掉 KC 的 NVDA）。"""
    code = _open("US.TSLA260904P345000BIND4", "TSLA", 345.0, channel="enrich")
    try:
        assert close_flow._bind_symbolless_close("out half 2.82", KC) is None
        assert close_flow._bind_symbolless_close("out half 2.82", None) is None
    finally:
        positions_db.record_close(code, 2, 2.59, "manual", note="ut")


def test_refuses_when_the_quoted_price_is_a_different_order_of_magnitude():
    """喊价是绑定的唯一硬凭据：2.82 对 entry 0.54 的 UBER 是 5.2×，出局。"""
    code = _open("US.UBER260904C78000BIND5", "UBER", 78.0, side="CALL", entry=0.54)
    try:
        assert close_flow._bind_symbolless_close("out half 2.82", KC) is None
        # 同一个仓、对得上的喊价 → 放行（证明拦下的是价格不是别的）
        assert close_flow._bind_symbolless_close("out half 0.60", KC) is not None
    finally:
        positions_db.record_close(code, 2, 0.54, "manual", note="ut")


def test_kill_switch_reproduces_todays_silence(monkeypatch):
    monkeypatch.setenv("CLOSE_BIND_SYMBOLLESS", "0")
    code = _open("US.TSLA260904P345000BIND6", "TSLA", 345.0)
    try:
        assert close_flow._bind_symbolless_close("out half 2.82", KC) is None
    finally:
        positions_db.record_close(code, 2, 2.59, "manual", note="ut")


# ============================================================
# 3. 端到端：9/2 那条 "out half 2.82" 现在真的会下卖单
# ============================================================

@pytest.mark.asyncio
async def test_last_nights_out_half_now_sells():
    os.environ["DRY_RUN"] = "true"
    code = _open(f"US.TSLA{datetime.now().strftime('%H%M%S%f')}P345000", "TSLA", 345.0)
    dedup._close_fps.clear()

    sells, notes = [], []

    async def capture(msg):
        notes.append(msg)

    def fake_sell(option_code, qty, limit_price, **kw):
        sells.append((option_code, qty, limit_price))
        return {"success": True, "message": "submitted", "order_id": "1",
                "code": option_code, "qty": qty, "price": limit_price}

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "KC Trades Bot:out half 2.82", msg_id=424242, channel_name=KC,
        )

    assert len(sells) == 1, ("9/2 那条应当下一张卖单", sells, notes)
    sold_code, qty, _ = sells[0]
    assert sold_code == code
    assert qty == 1                       # 2 张 trim 50%
    positions_db.record_close(code, 1, 2.59, "manual", note="ut cleanup")


# ============================================================
# 4. 与 #6（在飞买单豁免）合并后才出现的交互
# ============================================================

@pytest.mark.asyncio
async def test_bilingual_twins_only_sell_once():
    """9/2 那晚两条是**双发**：23:40:03 EN、23:40:05 ZH，相隔 2 秒。

    绑定之后两条都能解析出 CLOSE/[TSLA]/50 —— 指纹相同，必须只卖一次。
    漏网就是 7/8 那次"qty>=2 时 trim 两次"的翻版，只是这回从无 ticker 的路进来。
    """
    os.environ["DRY_RUN"] = "true"
    code = _open(f"US.TSLA{datetime.now().strftime('%H%M%S%f')}P345000", "TSLA", 345.0)
    dedup._close_fps.clear()

    sells = []

    async def capture(msg):
        pass

    def fake_sell(option_code, qty, limit_price, **kw):
        sells.append((option_code, qty))
        return {"success": True, "message": "submitted", "order_id": "1",
                "code": option_code, "qty": qty, "price": limit_price}

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "KC Trades Bot:out half 2.82", msg_id=1, channel_name=KC)
        await close_flow.handle_close_signal(
            "KC交易机器人：2.82减半仓", msg_id=2, channel_name=KC)

    assert len(sells) == 1, ("双语孪生只该卖一次", sells)
    positions_db.record_close(code, 1, 2.59, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_bound_sell_on_an_unfilled_buy_does_not_trip_the_breaker():
    """把 9/2 那一晚的两条修复接起来跑一遍。

    当晚真实顺序：买单挂着没成交 → 无 ticker 的减仓喊话进不来（本 PR 修）→
    带 ticker 的那条进来了却撞上 0 长仓被判确定性拒单而永久熔断（#6 修）。
    两条都修完之后：绑定成功 → 卖单被 broker 判为 deferred（瞬时）→
    **熔断不该触发**，后续孪生/重试仍进得来。
    """
    from autotrade.broker import inflight
    from autotrade.position import retry_guard

    os.environ["DRY_RUN"] = "true"
    code = _open(f"US.TSLA{datetime.now().strftime('%H%M%S%f')}P345000", "TSLA", 345.0)
    dedup._close_fps.clear()
    retry_guard.reset_state()
    inflight.reset_state()
    inflight.mark_submitted(code, "2104813")   # 买单还在飞

    deferred = (f"naked-short deferred: broker has only 0 long of {code}, "
                f"asked to sell 1, but a submitted buy is still unconfirmed — "
                f"treating as transient, will retry.")

    async def capture(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order",
                      return_value={"success": False, "message": deferred,
                                    "order_id": None, "code": code,
                                    "qty": 1, "price": 2.8}):
        await close_flow.handle_close_signal(
            "KC Trades Bot:out half 2.82", msg_id=3, channel_name=KC)

    assert retry_guard.is_tripped(f"kc:{code}") is False, "在飞买单期间不该熔断"
    assert retry_guard.blocked(f"kc:{code}") is None, "后续孪生/重试必须进得来"
    positions_db.record_close(code, 2, 2.59, "manual", note="ut cleanup")
