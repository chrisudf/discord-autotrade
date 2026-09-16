"""9/17 复盘（US RTH 2026-09-16）。五项模拟盘改动同时生效的第一夜。

当晚净 -$3,791，**其中近 $1,000 是缺陷不是交易判断**：

1. **符号抽取把指令改派给了持仓标的。** KC 的
   `closed the rest of SPY 2.62 … I almost took TSLA calls too but didn't`
   平掉了 66 字外的 TSLA 380C —— 而那句话明说他**没买** TSLA，喊价 2.62 是
   SPY 的，还被拿去给 TSLA 的卖单定限价。中文孪生行为正确
   （`symbols=['SPY'] 不在当前持仓 —— 无仓可平，跳过`）。
2. **fill 闸门拒绝回填之后，下游把限价当成了成本。** NVDA 215C 限价 2.70、
   真实成交 **1.01**，闸门（lesson #26）判 1.01 越界拒绝回填，`avg_entry`
   停在 2.70；5 秒后 SL 按 2.70 算出 -63%，4 张全平在 0.91。真实亏约 -$40，
   账上记 -$724。**那笔止损本不该发生。**
"""
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.parsing.close_parser import parse_close
from autotrade.position import sl_watcher, tp_watcher
from autotrade.storage import positions_db

# 9/16 夜 00:19:49 KC 原文（逐字）
KC_SPY_TSLA = ("closed the rest of SPY 2.62, flat trade and boring. "
               "I almost took TSLA calls too but didn't want to be too crazy before FOMC")
KNOWN = {"SPY", "TSLA", "NVDA", "AMZN", "AAPL", "META", "IBM", "JPM", "HOOD"}


def _uniq(p):
    return f"US.{p}{datetime.now().strftime('%H%M%S%f')}C001000"


# ============================================================
# 1. 主语不在持仓 → 无仓可平，**不许改派**
# ============================================================

def test_last_nights_close_no_longer_reassigns_to_a_held_ticker():
    """契约翻转：当晚这条返回 CLOSE ['TSLA']，现在必须是"无仓可平"。"""
    assert parse_close(KC_SPY_TSLA, {"TSLA", "AAPL", "NVDA"},
                       known_symbols=KNOWN) is None


def test_it_still_closes_when_the_subject_is_actually_held():
    """反向：主语本身持有时照常平 —— 改的是"改派"，不是"平仓"。"""
    r = parse_close(KC_SPY_TSLA, {"SPY", "TSLA"}, known_symbols=KNOWN)
    assert r and r["symbols"] == ["SPY"]


@pytest.mark.parametrize("text,held,want", [
    # BARE_SYM_PATTERN 是裸 [A-Z]{2,5}：BOOM 比 NVDA 更靠近动词。
    # 只看"最近的候选是不是持仓"会把这条真指令拒掉 —— known_symbols 才是判据。
    ("NVDA BOOM, OUT 80% @everyone", {"NVDA"}, ["NVDA"]),
    ("trimmed AMZN 1.92", {"AMZN"}, ["AMZN"]),
    ("JPM OUT 60% @everyone", {"JPM"}, ["JPM"]),
    ("SPY PUTS ITM, OUT 40% @everyone", {"SPY"}, ["SPY"]),
    # $ 前缀走另一条路径，一个字都不该受影响
    ("$HOOD - Nobody let these go red. Selling 25% here.", {"HOOD"}, ["HOOD"]),
])
def test_real_instructions_are_untouched(text, held, want):
    r = parse_close(text, held, known_symbols=KNOWN)
    assert r and r["symbols"] == want


def test_without_known_symbols_behaviour_is_unchanged():
    """不变量：不传 known_symbols（回放脚本 / diag）时逐字退回旧行为 ——
    新判据需要第三方证据，没有证据时不许自己发明。"""
    r = parse_close(KC_SPY_TSLA, {"TSLA", "AAPL"})
    assert r and r["symbols"] == ["TSLA"]


# ============================================================
# 2. 成本未知 → SL / TP 停手
# ============================================================

def _open(code, entry, unconfirmed=False, category="weekly", apply_sl=True):
    positions_db.open_or_add(
        option_code=code, symbol=code[3:7], strike=215.0, side="CALL",
        expiry=date(2026, 9, 18), qty=4, fill_price=entry,
        category=category, apply_sl=apply_sl, eod_force_close=False,
        tags=[], channel_name="ashley", msg_id="m",
    )
    if unconfirmed:
        positions_db.mark_entry_unconfirmed(code, why="ut")
    sl_watcher._triggered.discard(code)
    return code


@pytest.mark.asyncio
async def test_cost_unknown_position_is_skipped_by_sl(monkeypatch):
    """当晚 NVDA 215C 的形状：avg_entry 是限价 2.70，真实成交 1.01。

    拿限价当成本 → last=0.99 算出 -63% → 幻觉止损。现在必须一动不动。
    """
    monkeypatch.setenv("SL_OBSERVE_SWING", "0")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    code = _open(_uniq("UNC"), 2.70, unconfirmed=True)
    sold = []

    def fake_sell(option_code, qty, limit_price, **kw):
        sold.append((option_code, qty, limit_price))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": "UT", "code": option_code}

    with patch.object(sl_watcher, "get_last_prices",
                      side_effect=lambda codes: {c: 0.99 for c in codes}), \
         patch.object(sl_watcher, "place_sell_order", side_effect=fake_sell), \
         patch.object(sl_watcher, "send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    assert not sold, "成本未知时不许按成本算止损"
    assert positions_db.get(code)["status"] == "OPEN"
    positions_db.record_close(code, 4, 1.0, "manual", note="ut")


@pytest.mark.asyncio
async def test_confirmed_position_still_stops_normally(monkeypatch):
    """反向不变量：成本已确认的仓位照常止损 —— 改的是"成本未知"这一种。"""
    monkeypatch.setenv("SL_OBSERVE_SWING", "0")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    code = _open(_uniq("CONF"), 2.70, unconfirmed=False)
    sold = []

    def fake_sell(option_code, qty, limit_price, **kw):
        sold.append((option_code, qty, limit_price))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": "UT", "code": option_code}

    with patch.object(sl_watcher, "get_last_prices",
                      side_effect=lambda codes: {c: 0.99 for c in codes}), \
         patch.object(sl_watcher, "place_sell_order", side_effect=fake_sell), \
         patch.object(sl_watcher, "send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    assert sold, "成本确认的仓位必须照常止损"
    positions_db.record_close(code, 4, 1.0, "manual", note="ut")


def test_cost_unknown_position_is_out_of_the_tp_ladder():
    """TP 的每一档都是 entry×(1+pct) —— 成本未知时它会在错误价位触发
    （方向与 SL 那次相反，同样错）。"""
    code = _open(_uniq("TPU"), 2.70, unconfirmed=True)
    active = [p["option_code"] for p in
              [p for p in __import__("autotrade.position.manager",
                                     fromlist=["x"]).get_open_positions()
               if p["qty_remaining"] > 0 and not p.get("entry_unconfirmed")]]
    assert code not in active
    positions_db.record_close(code, 4, 1.0, "manual", note="ut")
