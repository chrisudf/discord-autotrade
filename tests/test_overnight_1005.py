"""10/5 夜的四个解析缺口（review_2026-10-06.md §5）。

1. KC `out the rest of Apple, only leaving Tesla`：Tesla 是留着的，却一起平了（hold-context 不认 leaving、EN 不查公司名）。
2. 中文先到时英文孪生的标签丢失：`风险日交易` 不是 day_trade（MU 拿过夜）、`乐透` 不是 lotto。
3. RedAlert `SPX - 7400 CALLS .35 0DTE`：NDTE 写在价格后面，兜底成周五。
4. `SPY 1DTE TRIMMED MORE` 连 0DTE 也卖：NDTE 当限定词，按开仓时的类目分两档。
"""
import os
from datetime import date, datetime
from unittest.mock import patch

import pytest

from autotrade.listener import close_flow, dedup
from autotrade.parsing.close_parser import (
    MULTI_DAY_QUALIFIER_CATS,
    ZERO_DTE_QUALIFIER_CATS,
    extract_position_qualifiers,
    parse_close,
)
from autotrade.parsing.signal_parser import parse_signal
from autotrade.storage import positions_db

D = date(2026, 10, 5)


# ============================================================
# 1. only leaving X = 留着 X
# ============================================================

def test_only_leaving_tesla_keeps_tesla():
    """契约翻转：当晚原文逐字，旧解析平 AAPL + TSLA，TSLA 2 张卖在 4.80，KC 的 runner 当天到 7.25。"""
    en = "@everyone\nKC Trades Bot:out the rest of Apple, only leaving Tesla to stay green overall now ✅"
    r = parse_close(en, {"AAPL", "TSLA"})
    assert r["symbols"] == ["AAPL"] and r["pct"] == 100


def test_leaving_also_works_with_the_ticker():
    r = parse_close("trimmed NVDA here @ 3.20, leaving TSLA runners", {"NVDA", "TSLA"})
    assert r["symbols"] == ["NVDA"]


def test_plain_close_of_a_company_name_still_closes():
    """反向：没写 leaving 的公司名平仓照旧（7/7 `all out apple` 那条兜底）。"""
    r = parse_close("@everyone\nKC Trades Bot:out the rest of Apple ✅", {"AAPL", "TSLA"})
    assert r["symbols"] == ["AAPL"]


# ============================================================
# 2. 中文先到时的标签
# ============================================================

def _tags(text: str) -> list:
    return parse_signal(text, msg_ts=D)["tags"]


def test_zh_risky_day_trade_is_day_trade():
    """契约翻转：当晚 MU 中文先到 1 秒，tags=[] → eod_force=False，`RISKY DAY TRADE` 拿过了夜。"""
    assert "day_trade" in _tags(":RedAlert: MU - $1100 看涨期权 10/9 $9.80，风险日交易 @everyone")


def test_zh_letou_is_lotto():
    """契约翻转：当晚 SPY 773C 中文先到 → 归 0dte 而非 0dte_lotto；周内乐透会被归成 weekly。"""
    assert "lotto" in _tags(":RedAlert: SPY - $773 看涨期权 0DTE .77，便宜的乐透玩法，严格滚动利润 @everyone")


def test_today_trading_is_not_day_trade():
    """反向：「今日交易」「每日交易」不是 day trade。"""
    assert "day_trade" not in _tags("enrich:\n今日交易：$RKLB 每周 $75 看涨期权 $1.32\n\n@everyone")


# ============================================================
# 3. RedAlert 模板：价格后面的 NDTE
# ============================================================

def test_ndte_after_price_is_today():
    """契约翻转：当晚 SPX 被解析成 10/9；这次靠 EXPECT 0 没下单，不写 EXPECT 0 的就会买成周五。"""
    sig = parse_signal(":RedAlert: SPX - 7400 CALLS .35 0DTE, SUPER LOTTOS, EXPECT 0 @everyone", msg_ts=D)
    assert sig["expiry_date"] == D


def test_ndte_before_price_and_weekly_are_unchanged():
    """不变量：NDTE 在价格前、写本周到期的，结果不变。"""
    a = parse_signal(":RedAlert: SPY - $774 CALLS 1DTE $1.20, STOP LOSS AT .95 @everyone", msg_ts=D)
    b = parse_signal(":RedAlert: TSM - $497.5 CALLS EXPIRATION THIS WEEK $1.35, STRICTLY ROLLING PROFITS @everyone", msg_ts=D)
    assert a["expiry_date"] == date(2026, 10, 6)
    assert b["expiry_date"] == date(2026, 10, 9)


# ============================================================
# 4. NDTE 当限定词
# ============================================================

def test_ndte_qualifier_maps_to_two_buckets():
    assert extract_position_qualifiers("SPY 1DTE TRIMMED MORE @everyone", "SPY") == (None, MULTI_DAY_QUALIFIER_CATS)
    assert extract_position_qualifiers("SPY 0DTE LOTTOS OUT 50% @everyone", "SPY") == (None, ZERO_DTE_QUALIFIER_CATS)
    # 自相矛盾（0DTE 又 NEXT WEEK）= 不过滤
    assert extract_position_qualifiers("SPY 0DTE NEXT WEEK OUT", "SPY") == (None, None)


def _open_pos(symbol: str, strike: float, category: str, expiry: date) -> str:
    code = f"US.{symbol}{datetime.now().strftime('%H%M%S%f')}C{int(strike * 1000)}"
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side="CALL", expiry=expiry,
        qty=4, fill_price=1.00, category=category, apply_sl=(category == "weekly"),
        eod_force_close=category.startswith("0dte"), tags=[], channel_name="ut", msg_id="m")
    return code


@pytest.mark.asyncio
async def test_1dte_trim_leaves_the_0dte_rounds_alone():
    """契约翻转：当晚 `SPY 1DTE TRIMMED MORE` 把 0DTE 的 773C 也卖了 2 张（约 $170）。"""
    r1 = _open_pos("QSPA", 771.0, "0dte", date(2026, 10, 5))
    r3 = _open_pos("QSPA", 773.0, "0dte", date(2026, 10, 5))
    r2 = _open_pos("QSPA", 774.0, "weekly", date(2026, 10, 6))
    os.environ["DRY_RUN"] = "true"
    dedup._close_fps.clear()
    sold = {}

    def fake_sell(option_code, qty, *a, **kw):
        sold[option_code] = qty
        return {"success": True, "order_id": "X", "code": option_code, "qty": qty, "price": 1.5}

    async def noop(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal("QSPA 1DTE TRIMMED MORE @ 1.50 @everyone",
                                             msg_id=int(datetime.now().timestamp() * 1e6))
    assert set(sold) == {r2}
    for c in (r1, r3):
        positions_db.record_close(c, 4, 0.01, "manual", note="ut")
