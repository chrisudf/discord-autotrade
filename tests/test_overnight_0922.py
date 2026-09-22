"""9/22 复盘（ET 2026-09-21）+ 9/18 那条回归。

昨夜净 +$255（近两周第一个盈利夜），但暴露了 9/16 那批修复的两个缺口，
外加一条我自己引入的回归：

1. **`entry_unconfirmed` 只修了特例。** RKLB 71C 00:41:03 开仓记 2.81（限价）、
   00:41:06 SL 按 2.81 触发全平、00:41:18 回填才说真实成交是 **1.51**。
   lesson #53 加的标记只在"闸门**拒绝**回填"时置位，而真正的窗口是
   **OPEN 到 FILL_ADJUST 之间那 12-15 秒**，每一笔单都要穿过它。
2. **声明止损没和我们的成本比。** 他喊 `$2.60, STOP LOSS AT $2.20`，我们成交
   1.51 —— 止损比成本高 46%，那不是止损是即时卖单。
3. **`NOW` 回归**（9/18）：#52 的判据 known_symbols 里混着 ServiceNow，
   于是 `ALL OUT NOW` 里的副词被当成"真 ticker 但不在持仓"，整条指令丢掉。
"""
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.listener import open_flow
from autotrade.parsing.close_parser import parse_close
from autotrade.position import sl_watcher
from autotrade.storage import positions_db

KNOWN = {"SPY", "TSLA", "NVDA", "AAPL", "META", "NOW", "BE", "AVGO", "RKLB", "INTC"}


def _uniq(p):
    return f"US.{p}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open(code, entry, apply_sl=True):
    positions_db.open_or_add(
        option_code=code, symbol=code[3:7], strike=71.0, side="CALL",
        expiry=date(2026, 9, 25), qty=4, fill_price=entry, category="weekly",
        apply_sl=apply_sl, eod_force_close=False, tags=[],
        channel_name="ashley", msg_id="m")
    sl_watcher._triggered.discard(code)
    return code


# ============================================================
# 1. 成本未知的窗口：从"拒绝回填"扩到"回填未到"
# ============================================================

@pytest.mark.asyncio
async def test_sl_stands_down_between_open_and_fill_adjust(monkeypatch):
    """契约翻转：当晚 RKLB 在这个窗口里被按限价止损了。

    `on_order_filled` 拿到的 fill_price 是**挂单限价**，SL 每 5s 扫一次 ——
    回填到达前的每一秒，成本都是假的。
    """
    monkeypatch.setenv("SL_OBSERVE_SWING", "0")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    code = _uniq("RKLB")
    # 逐字复现：限价 2.81 建仓 → 标记成本未知
    positions_db.open_or_add(
        option_code=code, symbol="RKLB", strike=71.0, side="CALL",
        expiry=date(2026, 9, 25), qty=4, fill_price=2.81, category="weekly",
        apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ashley", msg_id="m")
    positions_db.mark_entry_unconfirmed(code, why="ut: 限价")
    sl_watcher._triggered.discard(code)
    sold = []

    def fake_sell(option_code, qty, limit_price, **kw):
        sold.append((option_code, qty, limit_price))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": "UT", "code": option_code}

    with patch.object(sl_watcher, "get_last_prices",
                      side_effect=lambda codes: {c: 1.53 for c in codes}), \
         patch.object(sl_watcher, "place_sell_order", side_effect=fake_sell), \
         patch.object(sl_watcher, "send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    assert not sold, "回填未到时不许按限价止损（当晚这里卖了 4 张）"
    # 回填到达 → 恢复看护
    assert positions_db.clear_entry_unconfirmed(code) is True
    assert positions_db.get(code)["entry_unconfirmed"] == 0
    positions_db.record_close(code, 4, 1.5, "manual", note="ut")


def test_open_marks_cost_unknown_then_fill_clears_it():
    """标记打在 on_order_filled（只有那层知道传的是限价），
    由 fill_checker 确认后清除。"""
    from autotrade.position import manager as mgr
    code = _uniq("MARK")
    with patch.object(mgr.positions_db, "categorize",
                      return_value=("weekly", True, False)):
        mgr.on_order_filled(
            signal={"symbol": "MARK", "strike": 71.0, "side": "CALL",
                    "expiry_date": date(2026, 9, 25), "tags": []},
            order_result={"code": code, "qty": 4, "price": 2.81},
            channel_name="ashley", msg_id="m")
    assert positions_db.get(code)["entry_unconfirmed"] == 1, "开仓即成本未知"
    positions_db.clear_entry_unconfirmed(code)
    assert positions_db.get(code)["entry_unconfirmed"] == 0
    positions_db.record_close(code, 4, 1.5, "manual", note="ut")


# ============================================================
# 2. 开仓自带止损必须高不过我们的成本
# ============================================================

def test_declared_stop_above_our_fill_is_refused():
    """当晚原文：他喊 STOP LOSS AT $2.20，我们成交 1.51。"""
    code = _open(_uniq("STOPU"), 1.51)
    open_flow._apply_declared_stop(
        ":RedAlert: RKLB - $71 CALLS EXPIRATION THIS WEEK $2.60, "
        "STOP LOSS AT $2.20 @everyone", code, 1.51)
    assert positions_db.get(code)["manual_stop"] is None, \
        "高于成本的'止损'是即时卖单，不许录入"
    positions_db.record_close(code, 4, 1.5, "manual", note="ut")


def test_declared_stop_below_our_fill_is_recorded():
    """反向：正常的止损照常录入。"""
    code = _open(_uniq("STOPL"), 5.20)
    open_flow._apply_declared_stop(
        ":RedAlert: IBM - $250 CALLS $5.10, STOP LOSS AT $4.50 @everyone",
        code, 5.20)
    assert positions_db.get(code)["manual_stop"] == 4.50
    positions_db.record_close(code, 4, 5.0, "manual", note="ut")


def test_trailing_stop_above_entry_is_still_allowed_later():
    """**反向不变量**：这条检查只在开仓路径。后续把止损移到成本之上是
    移动止损锁利润，完全合法（喊单员每日提醒原话），
    test_overnight_0909::test_manual_stop_only_ever_ratchets_up 钉的就是它。"""
    code = _open(_uniq("TRAIL"), 3.20)
    assert positions_db.set_manual_stop(code, 3.60) is True, \
        "涨上去之后把止损抬到成本之上是锁利润，不是即时卖单"
    positions_db.record_close(code, 4, 3.6, "manual", note="ut")


# ============================================================
# 3. NOW 回归：判据里不许混歧义词
# ============================================================

@pytest.mark.parametrize("text", [
    "ALL OUT NOW, 100% BANGER @everyone",
    "I'M OUT 80% ON THESE CALLS NOW @everyone",
])
def test_ambiguous_words_do_not_claim_subject(text):
    """`NOW` 既是副词也是 ServiceNow —— 而我们交易过 ServiceNow，于是 #52
    的判据把副词当成了"真 ticker 但不在持仓"，整条指令丢掉。"""
    r = parse_close(text + " on AAPL", {"AAPL"}, known_symbols=KNOWN)
    assert r and r["symbols"] == ["AAPL"], "歧义词不许拦下真指令"


def test_unambiguous_subject_still_blocks_reassignment():
    """不变量：#52 的核心契约不受本次修补影响。"""
    assert parse_close(
        "closed the rest of SPY 2.62, flat trade and boring. "
        "I almost took TSLA calls too but didn't",
        {"TSLA", "AAPL"}, known_symbols=KNOWN) is None
