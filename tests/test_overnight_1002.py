"""9/28-10/2 周复盘的三条修复（review_WEEK_0928-1002.md §6）。lotto 硬底的契约翻转在 test_0013_lotto_floor.py。

1. EOD：到期日价内合约拿不到报价时，按标的现价算出的内在价值兜底卖出（9/25 FTNT/TEM 被行权，10/2 FTNT 差 7 分钟）。
2. B2 兜底：本行写了 MM/DD（在 ticker 前面）或 NDTE 时不许兜底成本周五（9/30 RKLB、9/21/9/23 META）。
3. 同一标的多个仓位：按紧跟 ticker 的 CALLS/PUTS/LOTTOS/NEXT WEEK 过滤（10/2 TSM 把周合约一起卖了）。
"""
import os
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from autotrade.broker import quote
from autotrade.listener import close_flow, dedup
from autotrade.parsing.close_parser import (
    LOTTO_QUALIFIER_CATS,
    SWING_QUALIFIER_CATS,
    extract_position_qualifiers,
)
from autotrade.parsing.signal_parser import parse_signal
from autotrade.position import eod_watcher
from autotrade.storage import positions_db

ET_TZ = ZoneInfo("America/New_York")


def _uniq(symbol: str, side: str, strike: float) -> str:
    return f"US.{symbol}{datetime.now().strftime('%H%M%S%f')}{side[0]}{int(strike * 1000)}"


# ============================================================
# 1. EOD 到期日价内兜底
# ============================================================

_PROBE = {"transport_ok": True, "row": True, "bid": None, "last": None,
          "age_sec": 1.0, "detail": "bid=None last=None"}


def _trading_now_et() -> datetime:
    now = datetime.now(ET_TZ).replace(hour=15, minute=51, second=0, microsecond=0)
    while now.weekday() >= 5:
        now -= timedelta(days=1)
    return now


def _open_expiring(symbol: str, strike: float, side: str, expiry, qty: int = 2,
                   eod_force: bool = False) -> str:
    code = _uniq(symbol, side, strike)
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side=side, expiry=expiry,
        qty=qty, fill_price=4.06, category="swing", apply_sl=False,
        eod_force_close=eod_force, tags=[], channel_name="ut", msg_id="ut",
    )
    eod_watcher._skip_until.pop(code, None)
    eod_watcher._alerted_until.pop(code, None)
    return code


@pytest.fixture
def _eod_state():
    eod_watcher._expiry_alert_stage.clear()
    eod_watcher._tick_noquote.clear()
    eod_watcher._tick_priced = 0
    yield
    eod_watcher._expiry_alert_stage.clear()


async def _eod_tick(now_et, spot: dict):
    """一轮 EOD：期权一律无报价，标的价按 spot 给。返回 (卖单列表, TG 文本列表, 标的取价 mock)。"""
    sells = []

    def fake_sell(**kw):
        sells.append(kw)
        return {"success": True, "order_id": "U", "qty": kw["qty"], "price": kw["limit_price"]}

    tg = AsyncMock(return_value=True)
    with patch.object(eod_watcher, "get_last_price", return_value=None), \
         patch.object(eod_watcher, "get_underlying_price",
                      side_effect=lambda s: spot.get(s)) as und, \
         patch.object(eod_watcher, "describe_quote", return_value=_PROBE), \
         patch.object(eod_watcher, "place_sell_order", side_effect=fake_sell), \
         patch.object(eod_watcher, "send_telegram", tg), \
         patch.object(eod_watcher, "_is_eod_window", return_value=True), \
         patch.object(eod_watcher, "sweep_expired_and_notify", new_callable=AsyncMock):
        await eod_watcher._eod_tick(now_et)
    return sells, [str(c).replace("\\", "") for c in tg.await_args_list], und


@pytest.mark.asyncio
async def test_expiry_itm_call_without_quote_sells_at_intrinsic(_eod_state):
    """契约翻转：10/2 FTNT 177.5C 无报价 8.5 分钟，旧逻辑一路拒卖；标的 180.95 → 内在价值 3.45。"""
    now = _trading_now_et()
    code = _open_expiring("FTNT", 177.5, "CALL", now.date())
    sells, _, _ = await _eod_tick(now, {"FTNT": 180.95})
    mine = [s for s in sells if s["option_code"] == code]
    assert len(mine) == 1
    assert mine[0]["qty"] == 2
    assert mine[0]["limit_price"] == round((180.95 - 177.5) * 0.9, 2)


@pytest.mark.asyncio
async def test_expiry_itm_put_uses_strike_minus_spot(_eod_state):
    """put 的内在价值是 K - S；SPX 的标的取价走指数代码（见 quote 那条用例）。"""
    now = _trading_now_et()
    code = _open_expiring("SPX", 7650.0, "PUT", now.date())
    sells, _, _ = await _eod_tick(now, {"SPX": 7600.0})
    mine = [s for s in sells if s["option_code"] == code]
    assert mine and mine[0]["limit_price"] == 45.0


@pytest.mark.asyncio
async def test_expiry_otm_without_quote_still_refuses(_eod_state):
    """反向：9/14 否决"无报价打折卖"的理由对价外合约仍然成立（10/2 MU 1245C，标的 1074.89）。"""
    now = _trading_now_et()
    code = _open_expiring("MU", 1245.0, "CALL", now.date(), qty=4)
    sells, tgs, _ = await _eod_tick(now, {"MU": 1074.89})
    assert not [s for s in sells if s["option_code"] == code]
    assert any(code in t and "[价外]" in t for t in tgs)
    positions_db.record_close(code, 4, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_expiry_unknown_moneyness_is_flagged_not_reassured(_eod_state):
    """标的也取不到时判断不了价内价外，TG 不许和归零合约一起说"不用管"。"""
    now = _trading_now_et()
    code = _open_expiring("TEMX", 83.0, "CALL", now.date())
    sells, tgs, _ = await _eod_tick(now, {})
    assert not [s for s in sells if s["option_code"] == code]
    hit = [t for t in tgs if code in t]
    assert hit and "价内价外未知" in hit[0] and "请人工看一眼" in hit[0]
    positions_db.record_close(code, 2, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_non_expiry_no_quote_does_not_fetch_underlying(_eod_state):
    """不变量：非到期日（day_trade 强平）无报价时行为不变，也不多吃一次 snapshot。"""
    now = _trading_now_et()
    code = _open_expiring("DTRD", 50.0, "CALL", now.date() + timedelta(days=7), eod_force=True)
    sells, _, und = await _eod_tick(now, {"DTRD": 60.0})
    assert not [s for s in sells if s["option_code"] == code]
    assert ("DTRD",) not in [c.args for c in und.call_args_list]
    positions_db.record_close(code, 2, 0.01, "manual", note="ut")


def test_underlying_price_maps_index_options_to_the_index():
    """SPXW 合约的标的报价在 US..SPX，不是 US.SPX / US.SPXW。"""
    asked = []
    with patch.object(quote, "get_last_prices",
                      side_effect=lambda codes: asked.extend(codes) or {c: 1.0 for c in codes}):
        quote.get_underlying_price("SPX")
        quote.get_underlying_price("SPXW")
        quote.get_underlying_price("ftnt")
    assert asked == ["US..SPX", "US..SPX", "US.FTNT"]


# ============================================================
# 2. B2 兜底不许无视本行的日期 / NDTE
# ============================================================

def _expiry(text: str, msg_day: date):
    sig = parse_signal(text, msg_ts=msg_day)
    return sig and sig.get("expiry_date")


def test_date_before_ticker_is_used_not_this_friday():
    """契约翻转：9/30 enrich 原文逐字，旧逻辑买成 10/2（成交 0.20，喊价 1.00）。"""
    en = "enrich:\nAdding back some next weeks 10/9 $RKLB $80 calls $1.00\n\n@everyone $alert"
    zh = "enrich:\n添加回一些下周的 10/9 $RKLB $80 看涨期权 $1.00\n\n@everyone $alert"
    assert _expiry(en, date(2026, 9, 30)) == date(2026, 10, 9)
    assert _expiry(zh, date(2026, 9, 30)) == date(2026, 10, 9)


def test_ndte_between_strike_and_side_goes_to_redalert_template():
    """契约翻转：9/23 原文，旧逻辑解析成周五 9/25（9/21、9/23 两次按它下错单）。"""
    en = ":RedAlert: META - $755 0DTE CALLS $4.15, SMALL LOTTOS @everyone"
    zh = ":RedAlert: META - $755 0DTE 看涨期权 $4.15, 小额彩票 @everyone"
    assert _expiry(en, date(2026, 9, 23)) == date(2026, 9, 23)
    assert _expiry(zh, date(2026, 9, 23)) == date(2026, 9, 23)


def test_date_on_another_line_belongs_to_another_ticker():
    """反向：enrich 一条列两个合约，MU 那行的 8/19 不能套到 CRWV 头上（第一版就踩了，全历史回放抓到的）。"""
    text = "enrich:\n$MU 8/19 $895 puts \n\n$CRWV weekly $88 puts $1.55 \n\n@everyone"
    assert _expiry(text, date(2026, 8, 18)) == date(2026, 8, 21)


def test_no_date_still_falls_back_to_this_friday():
    """不变量：真没写日期的照旧兜底成本周五。"""
    assert _expiry("enrich:\n$RKLB $80 calls $1.00", date(2026, 9, 30)) == date(2026, 10, 2)


# ============================================================
# 3. 同一标的多个仓位：按紧跟 ticker 的方向/类目词过滤
# ============================================================

def test_qualifiers_read_from_the_words_right_after_the_ticker():
    assert extract_position_qualifiers("TSM PUTS TRIMMING MORE @everyone", "TSM") == ("PUT", None)
    assert extract_position_qualifiers("TSM LOTTOS TRIMMED PROFITS @everyone", "TSM") == (None, LOTTO_QUALIFIER_CATS)
    assert extract_position_qualifiers("TSM 彩票修剪利润 @everyone", "TSM") == (None, LOTTO_QUALIFIER_CATS)
    assert extract_position_qualifiers("MU NEXT WEEK CALLS OUT 90% @everyone", "MU") == ("CALL", SWING_QUALIFIER_CATS)
    assert extract_position_qualifiers("CLOSING SPY SUPER LOTTOS @everyone", "SPY") == (None, LOTTO_QUALIFIER_CATS)


def test_qualifiers_far_from_the_ticker_are_ignored():
    """反向：全历史里这两条的方向/类目词说的是别的合约（TSLA 的 calls、留着的那轮 lottos）。"""
    kc = "@everyone\nKC Trades Bot:closed the rest of SPY 2.62, flat trade and boring. I almost took TSLA calls too but didn’t"
    ibm = "Gonna slow down for rest of the day @everyone \n\nClosing IBM round 2 also, lottos open partial position."
    assert extract_position_qualifiers(kc, "SPY") == (None, None)
    assert extract_position_qualifiers(ibm, "IBM") == (None, None)
    assert extract_position_qualifiers("Closing TSM round 3, market back at chop chop", "TSM") == (None, None)


def _open_pos(symbol: str, strike: float, side: str, category: str, qty: int = 4) -> str:
    code = _uniq(symbol, side, strike)
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side=side,
        expiry=date(2026, 10, 9), qty=qty, fill_price=2.00, category=category,
        apply_sl=(category == "weekly"), eod_force_close=category.startswith("0dte"),
        tags=[], channel_name="ut", msg_id="m")
    return code


async def _close(text: str) -> tuple[dict, list]:
    """跑一遍平仓流程，返回 ({option_code: 卖出张数}, TG 文本列表)。"""
    os.environ["DRY_RUN"] = "true"
    dedup._close_fps.clear()
    sold, notes = {}, []

    def fake_sell(option_code, qty, *a, **kw):
        sold[option_code] = qty
        return {"success": True, "order_id": "X", "code": option_code, "qty": qty, "price": 2.5}

    async def note(msg):
        notes.append(str(msg))

    with patch.object(close_flow, "_safe_notify", side_effect=note), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(text, msg_id=int(datetime.now().timestamp() * 1e6))
    return sold, notes


def _tsm_book(sym: str) -> dict:
    """10/2 当晚的持仓形状：周合约 480C + 两轮 0DTE call + 0DTE put。"""
    return {
        "weekly": _open_pos(sym, 480.0, "CALL", "weekly"),
        "c472": _open_pos(sym, 472.5, "CALL", "0dte"),
        "c475": _open_pos(sym, 475.0, "CALL", "0dte_lotto"),
        "p470": _open_pos(sym, 470.0, "PUT", "0dte_lotto"),
    }


@pytest.mark.asyncio
async def test_lottos_trim_leaves_the_weekly_alone():
    """契约翻转：当晚 `TSM LOTTOS TRIMMED PROFITS` 把她说要拿过周末的 480C 也减了。"""
    book = _tsm_book("QTSA")
    sold, _ = await _close("QTSA LOTTOS TRIMMED PROFITS @ 2.50 @everyone")
    assert book["weekly"] not in sold
    assert set(sold) == {book["c472"], book["c475"], book["p470"]}


@pytest.mark.asyncio
async def test_puts_trim_only_touches_the_put():
    """契约翻转：当晚 `TSM PUTS TRIMMING MORE` 连 472.5C 也卖了 1 张。"""
    book = _tsm_book("QTSB")
    sold, _ = await _close("QTSB PUTS TRIMMING MORE @ 2.50 @everyone")
    assert set(sold) == {book["p470"]}


@pytest.mark.asyncio
async def test_qualifier_matching_nothing_alerts_instead_of_selling_everything():
    """滤空时宁漏平不误平：发 TG，一张都不卖。"""
    a = _open_pos("QTSC", 480.0, "CALL", "weekly")
    b = _open_pos("QTSC", 485.0, "CALL", "weekly")
    sold, notes = await _close("QTSC PUTS OUT 50% @ 2.50 @everyone")
    assert sold == {}
    assert any("对不上" in n for n in notes)
    for c in (a, b):
        positions_db.record_close(c, 4, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_without_qualifier_behaviour_is_unchanged():
    """不变量：没有方向/类目词时照旧对所有仓位执行（round N 先不管）。"""
    a = _open_pos("QTSD", 472.5, "CALL", "0dte")
    b = _open_pos("QTSD", 475.0, "CALL", "0dte_lotto")
    sold, _ = await _close("QTSD OUT 50% HERE @ 2.50 @everyone")
    assert set(sold) == {a, b}


# ============================================================
# 4. PR#22 review（Copilot 5 条，例子取自 review 原文）
# ============================================================

def test_qualifier_binds_to_the_action_clause_occurrence():
    """review：第一次出现的 TSM 在行情句里，动作句里的才是被平的那张。"""
    assert extract_position_qualifiers("TSM calls ran +30%. Closing TSM PUTS now", "TSM") == ("PUT", None)


def test_qualifier_window_stops_at_clause_or_another_ticker():
    """review：「TSM减仓，META看跌期权减仓」的看跌属于 META，不能算到 TSM 头上。"""
    text = "TSM减仓，META看跌期权减仓"
    assert extract_position_qualifiers(text, "TSM", ["TSM", "META"]) == (None, None)
    assert extract_position_qualifiers(text, "META", ["TSM", "META"]) == ("PUT", None)
    assert extract_position_qualifiers("Trimmed TSM CALLS and META PUTS here", "TSM", ["TSM", "META"]) == ("CALL", None)


@pytest.mark.asyncio
async def test_every_explicit_target_gets_its_own_qualifier():
    """review：只过滤第一个标的时，`… and META PUTS` 会把 META 的 call 也卖掉。"""
    a_call, a_put = _open_pos("QTSE", 480.0, "CALL", "weekly"), _open_pos("QTSE", 470.0, "PUT", "weekly")
    b_call, b_put = _open_pos("QMTE", 750.0, "CALL", "weekly"), _open_pos("QMTE", 740.0, "PUT", "weekly")
    # 多标的共用一个喊价时会丢弃喊价改用实时报价，这里给一个报价
    with patch.object(close_flow, "get_sell_ref_price", return_value=2.50):
        sold, _ = await _close("Trimmed QTSE CALLS and QMTE PUTS here @ 2.50 @everyone")
    assert set(sold) == {a_call, b_put}
    for c in (a_put, b_call):
        positions_db.record_close(c, 4, 0.01, "manual", note="ut")


def _sel(sym, side=None, cats=None):
    return {"kind": "CLOSE", "symbols": [sym], "pct": 50, "selectors": {sym: (side, cats)}}


def test_dedup_treats_a_different_named_side_as_a_new_instruction():
    """review：CALLS 之后 60s 内的 PUTS 不是孪生；没点名方向的机翻孪生仍然去重。"""
    dedup._close_fps.clear()
    assert dedup._is_duplicate_close(_sel("QDDA", "CALL"))[0] is False
    assert dedup._is_duplicate_close(_sel("QDDA", "PUT"))[0] is False
    assert dedup._is_duplicate_close(_sel("QDDA"))[0] is True          # 「QDDA 出局 50%」这种译丢了方向的孪生
    assert dedup._is_duplicate_close(_sel("QDDA", "CALL"))[0] is True  # 原样重发


def test_dedup_rollback_only_drops_its_own_registration():
    """回滚 PUTS 那条（broker 失败）不能把已经执行的 CALLS 登记一起抹掉，否则 CALLS 的孪生会再卖一遍。"""
    dedup._close_fps.clear()
    dedup._is_duplicate_close(_sel("QDDB", "CALL"))
    dedup._is_duplicate_close(_sel("QDDB", "PUT"))
    dedup._unregister_close_fp(_sel("QDDB", "PUT"))
    assert dedup._is_duplicate_close(_sel("QDDB"))[0] is True
    assert dedup._is_duplicate_close(_sel("QDDB", "PUT"))[0] is False   # PUTS 的孪生可以重试


@pytest.mark.asyncio
async def test_expiry_underlying_is_fetched_once_per_tick(_eod_state):
    """review：同一标的几张无报价合约，每张各取一次标的价会吃光 60 次/30s 的配额。"""
    now = _trading_now_et()
    codes = [_open_expiring("QFTA", k, "CALL", now.date()) for k in (190.0, 195.0, 200.0)]
    _, _, und = await _eod_tick(now, {"QFTA": 150.0})
    assert [c.args for c in und.call_args_list].count(("QFTA",)) == 1
    for c in codes:
        positions_db.record_close(c, 2, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_barely_itm_is_sold_not_labelled_otm(_eod_state):
    """review：价内 $0.04 也会被行权，不能标成[价外]说不用管。"""
    now = _trading_now_et()
    code = _open_expiring("QNIT", 100.0, "CALL", now.date())
    sells, tgs, _ = await _eod_tick(now, {"QNIT": 100.04})
    mine = [s for s in sells if s["option_code"] == code]
    assert mine and mine[0]["limit_price"] == 0.04
    assert not any(code in t and "[价外]" in t for t in tgs)
