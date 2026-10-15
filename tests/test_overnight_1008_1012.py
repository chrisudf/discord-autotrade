"""10/8、10/12 夜（review_2026-10-09.md、review_2026-10-13.md）。

1. 开仓自带止损省掉前导 0（`止损位 .40` / `STOP LOSS AT .40`）：中文解析成 40 被护栏拒掉，英文认不出。
2. `PLTR WEEKLIES CLOSING … CAN USE A 25% SL`：25% 是给拿着的人的止损建议，不是平仓比例；
   WEEKLIES 点名本周到期那张（两张都是 weekly 类目，只能按到期日分）。
3. 15:51 ET 喊的 SPX 0DTE：已过 EOD 截止点，买进去下一轮就被强平。
4. DELL 615C → 46 秒后同价同到期重发成 620C：前一张是笔误，两张都下了单。
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from autotrade.listener import close_flow, dedup, heuristics, open_flow
from autotrade.parsing.close_parser import (
    NEXT_WEEK,
    THIS_WEEK,
    expiry_in_scope,
    extract_expiry_qualifier,
    parse_close,
    parse_stop_adjust,
)
from autotrade.position import eod_watcher
from autotrade.storage import positions_db
from autotrade.utils.timeutil import ET_TZ
from tests.test_overnight_1006 import _open, _wire

AAPL_ZH = ":RedAlert: AAPL - $342.5 看涨期权 0DTE .64，止损位 .40，彩票大小 @everyone"
AAPL_EN = ":RedAlert: AAPL - $342.5 CALLS 0DTE .64, STOP LOSS AT .40, LOTTO SIZE @everyone"
PLTR_EN = ("PLTR WEEKLIES CLOSING HERE ABOVE ENTRY @everyone \n\n"
           "CAN USE A 25% SL IF YOU WANT TO RISK HOLDING :verifyblue:")
SPX = ":RedAlert: SPX - 7815 CALLS 0DTE .10, SUPER LOTTOS @everyone"
DELL_615 = ":RedAlert: DELL - $615 CALLS EXPIRATION NEXT WEEK $6.50, STOP LOSS AT $5.50 @everyone"
DELL_620 = ":RedAlert: DELL - $620 CALLS EXPIRATION NEXT WEEK $6.50, STOP LOSS AT $5.50 @everyone"


def _pos(symbol: str, strike: float, expiry: date, qty: int, fill: float, category: str = "weekly") -> str:
    code = f"US.{symbol}{datetime.now().strftime('%H%M%S%f')}C{int(strike * 1000)}"
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side="CALL", expiry=expiry,
        qty=qty, fill_price=fill, category=category, apply_sl=True, eod_force_close=False,
        tags=[], channel_name="ut", msg_id="m")
    return code


# ============================================================
# 1. 止损价省掉前导 0
# ============================================================

def test_leading_dot_stop_parses_in_both_languages():
    """契约翻转：中文当晚是 40.0（被开仓护栏拒掉），英文是 None。10/8 PLTR `.95` 同形。"""
    assert parse_stop_adjust(AAPL_ZH)["stop"] == 0.40
    assert parse_stop_adjust(AAPL_EN)["stop"] == 0.40
    assert parse_stop_adjust(":RedAlert: PLTR - $205 看涨期权本周到期 $1.30 @everyone\n\n止损设定在 .95")["stop"] == 0.95
    assert parse_stop_adjust(":RedAlert: PLTR - $205 CALLS EXPIRATION THIS WEEK $1.30 @everyone\n\nSTOP LOSS AT .95")["stop"] == 0.95


def test_stops_with_a_leading_digit_are_unchanged():
    assert parse_stop_adjust("止损位在 $2.60")["stop"] == 2.60
    assert parse_stop_adjust("stop at 2.72 now")["stop"] == 2.72
    assert parse_stop_adjust("STOP LOSS AT $2.00, RISKY")["stop"] == 2.00


def test_leading_dot_stop_is_recorded_on_open():
    """当晚 AAPL 342.5C 成交 0.66：.40 应该落库，10:33 ET last 0.40 时会触发。"""
    code = _pos("QAPL", 342.5, date(2026, 10, 12), 4, 0.66, "0dte_lotto")
    open_flow._apply_declared_stop(AAPL_ZH, code, 0.66)
    assert positions_db.get(code)["manual_stop"] == 0.40
    positions_db.record_close(code, 4, 0.01, "manual", note="ut")


# ============================================================
# 2. N% SL 不是平仓比例 + WEEKLIES 按到期日点名
# ============================================================

def test_pct_sl_advice_is_not_the_close_pct():
    """契约翻转：当晚 pct=25，两张 PLTR 都被 last-spare 留下。"""
    assert parse_close(PLTR_EN, {"PLTR"})["pct"] == 100
    assert parse_close("PLTR 周期性收盘在入场价之上 \n\n如果你想冒险持有，可以使用 25% 止损", {"PLTR"}) is None


def test_real_trim_pcts_are_unchanged():
    assert parse_close("SPY trimming 25% here @everyone", {"SPY"})["pct"] == 25
    aapl = ("AAPL CLOSING HERE ABOVE ENTRY FOR NOW, WILL ADD AGAIN LATER, "
            "IF YOU WANT TO CONTINUE HOLDING SUGGEST USING A 20% SL @everyone")
    assert parse_close(aapl, {"AAPL"})["pct"] == 100


def test_week_qualifier_is_read_right_after_the_ticker():
    assert extract_expiry_qualifier(PLTR_EN, "PLTR") == THIS_WEEK
    assert extract_expiry_qualifier("MU NEXT WEEK CALLS OUT 90% @everyone", "MU") == NEXT_WEEK
    # 反向：说的是行情，不是点名哪一张
    assert extract_expiry_qualifier("Closing LRCX calls, market is not treating me well this week", "LRCX") is None
    assert extract_expiry_qualifier("I cut my weeklies $IREN $MRVL", "IREN") is None


def test_week_scope_splits_on_this_friday():
    thu = date(2026, 10, 8)
    assert expiry_in_scope("2026-10-09", THIS_WEEK, thu) and not expiry_in_scope("2026-10-16", THIS_WEEK, thu)
    assert expiry_in_scope("2026-10-16", NEXT_WEEK, thu) and not expiry_in_scope("2026-10-09", NEXT_WEEK, thu)
    assert expiry_in_scope("2026-10-09", None, thu)
    fri = date(2026, 9, 18)   # 9/18 MU NEXT WEEK 是周五喊的
    assert expiry_in_scope("2026-09-25", NEXT_WEEK, fri) and not expiry_in_scope("2026-09-18", NEXT_WEEK, fri)


@pytest.mark.asyncio
async def test_weeklies_close_sells_only_this_weeks_round():
    """当晚：205C（10/9）和 207.5C（10/16）各剩 2 张，一张没卖。她平的是本周那张，下周的两轮说了要拿着。"""
    this_wk = _pos("QPLW", 205.0, date(2026, 10, 9), 2, 1.44)
    next_wk = _pos("QPLW", 207.5, date(2026, 10, 16), 2, 2.93)
    dedup._close_fps.clear()
    sold = {}

    def fake_sell(option_code, qty, *a, **kw):
        sold[option_code] = qty
        return {"success": True, "order_id": "X", "code": option_code, "qty": qty, "price": 1.38}

    async def noop(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", return_value=1.38), \
         patch.object(close_flow, "today_et", return_value=date(2026, 10, 8)):
        await close_flow.handle_close_signal(PLTR_EN.replace("PLTR", "QPLW"),
                                             msg_id=int(datetime.now().timestamp() * 1e6))
    assert sold == {this_wk: 2}
    positions_db.record_close(this_wk, 2, 1.38, "manual", note="ut")
    positions_db.record_close(next_wk, 2, 0.01, "manual", note="ut")


# ============================================================
# 3. EOD 截止点之后不开当日到期的仓
# ============================================================

NOW_1551 = datetime(2026, 10, 12, 15, 51, tzinfo=ET_TZ)


def test_eod_window_check_needs_both_today_expiry_and_cutoff():
    assert eod_watcher.expires_inside_eod_window(date(2026, 10, 12), NOW_1551)
    assert not eod_watcher.expires_inside_eod_window(date(2026, 10, 12), NOW_1551.replace(minute=49))
    assert not eod_watcher.expires_inside_eod_window(date(2026, 10, 14), NOW_1551)


@pytest.mark.asyncio
async def test_spx_0dte_after_cutoff_is_not_ordered(monkeypatch):
    """契约翻转：当晚 15:51 ET 买了 4 张，1.8 秒后 EOD 去卖，naked-short ERROR，下一轮卖在 0.09。"""
    placed, alerts, bg = [], [], []
    _wire(monkeypatch, placed, alerts, bg)
    monkeypatch.setattr(open_flow, "get_buy_ref_price", lambda code: 0.10)
    real = eod_watcher.expires_inside_eod_window
    monkeypatch.setattr(eod_watcher, "expires_inside_eod_window", lambda exp: real(exp, NOW_1551))
    await _open(SPX, day=date(2026, 10, 12))
    assert placed == []
    assert any("EOD" in a and "SPX" in a for a in alerts)


@pytest.mark.asyncio
async def test_spx_0dte_before_cutoff_still_orders(monkeypatch):
    placed, alerts, bg = [], [], []
    _wire(monkeypatch, placed, alerts, bg)
    monkeypatch.setattr(open_flow, "get_buy_ref_price", lambda code: 0.10)
    real = eod_watcher.expires_inside_eod_window
    monkeypatch.setattr(eod_watcher, "expires_inside_eod_window",
                        lambda exp: real(exp, NOW_1551.replace(minute=40)))
    await _open(SPX, day=date(2026, 10, 12))
    assert placed == ["SPX"]


# ============================================================
# 4. 同价同到期只改了行权价 = 更正
# ============================================================

@pytest.mark.asyncio
async def test_strike_only_resend_alerts_about_the_first_order(monkeypatch):
    """当晚原文：两张都下了，615C 挂 74 分钟后成交，-$544。更正后的 620C 照常下，前一张提醒人工撤。"""
    placed, alerts, bg = [], [], []
    _wire(monkeypatch, placed, alerts, bg)
    monkeypatch.setattr(open_flow, "get_buy_ref_price", lambda code: 6.66)
    await _open(DELL_615, day=date(2026, 10, 8))
    await _open(DELL_620, day=date(2026, 10, 8))
    assert placed == ["DELL", "DELL"]
    fix = [b for b in bg if "行权价" in b]
    assert len(fix) == 1 and "615" in fix[0] and "620" in fix[0]


def test_strike_correction_needs_same_price_expiry_and_a_short_gap():
    heuristics._last_open.clear()
    sig = {"symbol": "DELL", "strike": 615.0, "side": "CALL", "expiry": "10/16", "price": 6.5}
    heuristics._record_recent_exec(9, sig)
    assert heuristics._strike_correction({**sig, "strike": 620.0}, 9)
    assert heuristics._strike_correction(sig, 9) is None                              # 同一张再喊（加仓）
    assert heuristics._strike_correction({**sig, "strike": 620.0, "price": 3.1}, 9) is None   # 不同价 = 另一单
    assert heuristics._strike_correction({**sig, "strike": 620.0, "expiry": "10/23"}, 9) is None
    assert heuristics._strike_correction({**sig, "strike": 620.0}, 8) is None         # 别的频道
    heuristics._last_open[9]["ts"] -= timedelta(seconds=121)
    assert heuristics._strike_correction({**sig, "strike": 620.0}, 9) is None
