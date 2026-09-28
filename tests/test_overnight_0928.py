"""平仓句式（语料见 tests/corpus/close_phrasing.jsonl）。

1. "closing" 没写比例时以前按 33% 减仓："CLOSING NVDA CALLS" 只卖三分之一。
   现在进全平词表，但**同一标的有多个仓位时退回 33%** —— ashley 常分轮进同一标的，
   100% 会把她还拿着的那一轮也平掉。
2. "CLOSING ABOVE / BELOW / NEAR ENTRY" 被开仓意图词 entry 一票否决成 OPEN。
3. "REDUCING MY POSITION BY 50%"、"BREAKEVEN CLOSE ON REST" 不在词表。
"""
import os
from datetime import date, datetime
from unittest.mock import patch

import pytest

from autotrade.listener import close_flow, dedup
from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action
from autotrade.storage import positions_db


def _open(symbol: str, strike: float, qty: int = 4) -> str:
    code = f"US.{symbol}{datetime.now().strftime('%H%M%S%f')}C{int(strike * 1000)}"
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side="CALL",
        expiry=date(2026, 10, 2), qty=qty, fill_price=2.00, category="weekly",
        apply_sl=True, eod_force_close=False, tags=[], channel_name="ut", msg_id="m")
    return code


async def _close(text: str) -> dict:
    """跑一遍平仓流程，返回 {option_code: 卖出张数}。"""
    os.environ["DRY_RUN"] = "true"
    dedup._close_fps.clear()
    sold = {}

    def fake_sell(option_code, qty, *a, **kw):
        sold[option_code] = qty
        return {"success": True, "order_id": "X", "code": option_code, "qty": qty, "price": 2.5}

    async def noop(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(text, msg_id=int(datetime.now().timestamp() * 1e6))
    return sold


# ============================================================
# 1. closing：单仓位全平，多仓位退回 33%
# ============================================================

@pytest.mark.asyncio
async def test_closing_a_single_position_closes_all_of_it():
    """契约翻转：当晚 "CLOSING NVDA CALLS" 这类只卖了三分之一。"""
    code = _open("CLSA", 100.0)
    sold = await _close("CLOSING CLSA CALLS HERE @ 2.50, MARKET FLUSHING @everyone")
    assert sold == {code: 4}


@pytest.mark.asyncio
async def test_closing_with_several_positions_keeps_the_old_33pct():
    """分不清是哪一轮时，行为与改动前逐字一致（每张都卖 33%）。"""
    a, b = _open("CLSB", 100.0), _open("CLSB", 105.0)
    sold = await _close("CLOSING CLSB CALLS HERE @ 2.50, MARKET FLUSHING @everyone")
    assert sold == {a: 2, b: 2}


@pytest.mark.asyncio
async def test_explicit_full_close_still_closes_every_position():
    """反向：明说 ALL OUT 的不受多仓位退回的影响。"""
    a, b = _open("CLSC", 100.0), _open("CLSC", 105.0)
    sold = await _close("ALL OUT CLSC @ 2.50 @everyone")
    assert sold == {a: 4, b: 4}


def test_closing_half_is_half():
    r = parse_close("SPY CLOSING HALF HERE @everyone", {"SPY"})
    assert r["pct"] == 50


# ============================================================
# 2 / 3. 路由与新句式
# ============================================================

@pytest.mark.parametrize("text", [
    "NVDA CLOSING LITTLE BELOW ENTRY, PRICE ACTION TOO CHOPPY @everyone",
    "RKLB CALLS CLOSING REST ABOVE ENTRY @everyone",
    "CLOSING SPY LOTTOS NEAR ENTRY, MARKET MANIPULATION CONTINUES @everyone",
    "AVGO GREEN, REDUCING MY POSITION BY 50% @everyone",
])
def test_these_route_to_close(text):
    assert detect_action(text) == "CLOSE"


@pytest.mark.parametrize("text", [
    "@everyone\nKC Trades Bot:SPY 745c 4DTE @ 3.15 day trade, great entry here",
    "REDUCING RISK TODAY, MARKET CHOPPY @everyone",
    "SET BREAKEVEN STOP ABOVE ENTRY ON REST @everyone",
])
def test_these_still_route_to_open(text):
    """反向：开仓里的 entry、"reducing risk"、没有平仓动词的止损备注都不许变成平仓。"""
    assert detect_action(text) == "OPEN"
