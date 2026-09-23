"""9/23 复盘（ET 2026-09-22）：PR#16 的两个洞 + GILD 下周到期 + 对冲语气误平。

1. MU 1105C 在 fill_checker 超时之后才成交 → 成本未知标记永不清除，SL/TP 整晚停摆。
2. 开仓时的"止损高于成本"检查拿的是限价：GILD 限价 3.15 / 止损 2.50 / 成交 1.92，4 秒全卖。
3. "CALLS NEXT WEEK $3.00" / "看涨期权下周 $3.00" 被解析成本周。
4. "AVGO 我可能会进行平仓" 被当成指令全平。
"""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.broker import inflight
from autotrade.listener import open_flow
from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import parse_signal
from autotrade.position import fill_checker, sl_watcher
from autotrade.storage import positions_db

TUE = date(2026, 9, 22)          # 本周五 9/25，下周五 10/2


def _uniq(p):
    return f"US.{p}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open_at_limit(code, limit, qty=4):
    """复现生产：on_order_filled 记的是限价，并打上成本未知标记。"""
    positions_db.open_or_add(
        option_code=code, symbol=code[3:7], strike=100.0, side="CALL",
        expiry=date(2026, 9, 25), qty=qty, fill_price=limit, category="weekly",
        apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ashley", msg_id="m")
    positions_db.mark_entry_unconfirmed(code, why="ut: 限价")
    sl_watcher._triggered.discard(code)
    return code


def _confirm(code, qty, limit, outcomes):
    """按顺序吐出 _poll_until_terminal 的结果，返回 TG 调用。"""
    seq = iter(outcomes)

    async def fake_poll(order_id, late=False):
        return next(seq)

    tg = AsyncMock()
    with patch.object(fill_checker, "_poll_until_terminal", fake_poll), \
         patch.object(fill_checker, "send_telegram", tg):
        asyncio.run(fill_checker.confirm_buy_fill("ORD", code, qty, limit))
    return tg


def _filled(price):
    return {"outcome": "filled", "success": True, "status": "FILLED_ALL",
            "filled_qty": 4, "filled_avg_price": price}


TIMEOUT = {"outcome": "timeout", "status": "SUBMITTED", "filled_qty": 0}


# ============================================================
# 1. 超时后成交：续查拿到成交，照常回填并解除标记
# ============================================================

def test_fill_after_timeout_still_backfills_and_clears_the_flag():
    """当晚 MU 1105C：05:04 超时，05:11-06:11 之间成交，SL 跳过它 114 次。"""
    code = _open_at_limit(_uniq("MU"), 7.88)
    positions_db.set_manual_stop(code, 6.50)
    inflight.mark_submitted(code, "ORD")

    tg = _confirm(code, 4, 7.88, [TIMEOUT, _filled(7.50)])

    pos = positions_db.get(code)
    assert pos["entry_unconfirmed"] == 0, "超时后成交也必须解除成本未知"
    assert pos["avg_entry_price"] == pytest.approx(7.50)
    assert pos["manual_stop"] == 6.50, "低于成本的声明止损照常保留"
    assert inflight.is_pending(code) is False
    assert tg.await_count == 2, "超时告警 + 超时后成交的更正"
    positions_db.record_close(code, 4, 7.5, "manual", note="ut")


def test_still_no_fill_after_late_window_keeps_the_flag():
    """续查到上限仍无终态：仍然不知道，标记与在飞登记都保持。"""
    code = _open_at_limit(_uniq("MUX"), 7.88)
    inflight.mark_submitted(code, "ORD")

    _confirm(code, 4, 7.88, [TIMEOUT, TIMEOUT])

    assert positions_db.get(code)["entry_unconfirmed"] == 1
    assert inflight.is_pending(code) is True
    inflight.clear(code, "ORD")
    positions_db.record_close(code, 4, 7.5, "manual", note="ut")


# ============================================================
# 2. 声明止损要和**真实成交**比，比较发生在回填那一刻
# ============================================================

GILD_ZH = ":RedAlert: GILD - $152.5 看涨期权下周 $3.00，止损位在 $2.50 @everyone"
RKLB_EN = ":RedAlert: RKLB - $71 CALLS THIS WEEK $2.60, STOP LOSS AT $2.20 @everyone"


def test_open_path_check_cannot_see_the_fill():
    """生产的输入是限价：开仓那一刻 2.20 < 2.81，检查必然放行（PR#16 的测试传的是成交价）。"""
    code = _open_at_limit(_uniq("RKLB"), 2.81)
    open_flow._apply_declared_stop(RKLB_EN, code, 2.81)
    assert positions_db.get(code)["manual_stop"] == 2.20
    positions_db.record_close(code, 4, 1.5, "manual", note="ut")


def test_stop_above_real_fill_is_dropped_on_backfill():
    """9/22 RKLB 原样重放：限价 2.81、止损 2.20、成交 1.51。"""
    code = _open_at_limit(_uniq("RKLB"), 2.81)
    open_flow._apply_declared_stop(RKLB_EN, code, 2.81)

    _confirm(code, 4, 2.81, [_filled(1.51)])

    pos = positions_db.get(code)
    assert pos["avg_entry_price"] == pytest.approx(1.51)
    assert pos["manual_stop"] is None, "高于真实成本的止损是即时卖单"
    positions_db.record_close(code, 4, 1.5, "manual", note="ut")


@pytest.mark.asyncio
async def test_gild_addon_replay_no_longer_dumps_all_eight(monkeypatch):
    """当晚逐字：老仓 4 @ 1.83，加仓 4 @ 限价 3.15、止损 2.50，回填 1.92 → 3 秒后 8 张全卖在 1.77。"""
    monkeypatch.setenv("SL_OBSERVE_SWING", "0")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    code = _uniq("GILD")
    positions_db.open_or_add(
        option_code=code, symbol="GILD", strike=152.5, side="CALL",
        expiry=date(2026, 9, 25), qty=4, fill_price=1.83, category="weekly",
        apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ashley", msg_id="m1")
    _open_at_limit(code, 3.15)                     # 加仓那一腿
    open_flow._apply_declared_stop(GILD_ZH, code, 3.15)
    assert positions_db.get(code)["manual_stop"] == 2.50

    async def fake_poll(order_id, late=False):
        return _filled(1.92)

    with patch.object(fill_checker, "_poll_until_terminal", fake_poll), \
         patch.object(fill_checker, "send_telegram", new_callable=AsyncMock):
        await fill_checker.confirm_buy_fill("ORD", code, 4, 3.15)
    assert positions_db.get(code)["manual_stop"] is None

    sold = []

    def fake_sell(option_code, qty, limit_price, **kw):
        sold.append((option_code, qty, limit_price))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": "UT", "code": option_code}

    with patch.object(sl_watcher, "get_last_prices",
                      side_effect=lambda codes: {c: 1.92 for c in codes}), \
         patch.object(sl_watcher, "place_sell_order", side_effect=fake_sell), \
         patch.object(sl_watcher, "send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()
    assert not sold, "当晚这里卖了 8 张"
    positions_db.record_close(code, 8, 1.9, "manual", note="ut")


def test_stop_below_real_fill_is_kept():
    """反向：AAPL 347.5C 限价 1.40、止损 1.00、成交 1.31 —— 正常止损不许动。"""
    code = _open_at_limit(_uniq("AAPL"), 1.40)
    open_flow._apply_declared_stop(
        ":RedAlert: AAPL - $347.5 CALLS 9/25 $1.25, STOP LOSS AT $1.00 @everyone",
        code, 1.40)

    _confirm(code, 4, 1.40, [_filled(1.31)])

    assert positions_db.get(code)["manual_stop"] == 1.00
    positions_db.record_close(code, 4, 1.0, "manual", note="ut")


# ============================================================
# 3. "CALLS NEXT WEEK $x" / "看涨期权下周 $x" 是下周到期
# ============================================================

@pytest.mark.parametrize("text", [
    ":RedAlert: GILD - $152.5 CALLS NEXT WEEK $3.00, STOP LOSS AT $2.50 @everyone",
    GILD_ZH,
])
def test_calls_next_week_is_next_friday(text):
    assert parse_signal(text, msg_ts=TUE)["expiry_date"] == date(2026, 10, 2)


def test_calls_next_week_in_commentary_does_not_shift():
    """反向：期权词 + next week 后面没紧跟 $价格，是评论不是到期日。"""
    r = parse_signal(":RedAlert: SKHY - $195 CALLS $2.90, adding more calls next week", msg_ts=TUE)
    assert r["expiry_date"] == date(2026, 9, 25)


# ============================================================
# 4. 对冲语气不是指令（ZH 当晚真卖了，EN 只是碰巧没卖）
# ============================================================

@pytest.mark.parametrize("text", [
    "AVGO 我可能会进行平仓，Theta 正在消耗溢价 @everyone",
    "AVGO I MIGHT DO A BREAKEVEN CLOSE, THETA KILLING PREMIUMS @everyone",
    "I MIGHT TRIM AVGO HERE @everyone",
    "MAYBE TRIMMING AVGO SOON @everyone",
    "AVGO 考虑减仓 @everyone",
])
def test_hedged_close_is_not_an_instruction(text):
    assert parse_close(text, {"AVGO"}) is None


@pytest.mark.parametrize("text", [
    "AVGO 在这里减仓，可能还会回落 @everyone",
    "AVGO MIGHT CLOSE GREEN TODAY, TRIMMING 50% HERE @everyone",
    "TRIMMED AVGO 50% HERE, MIGHT HOLD THE REST @everyone",
])
def test_real_close_next_to_a_hedge_still_fires(text):
    """反向：语气词只抹它自己那个从句，旁边的真指令照常执行。"""
    r = parse_close(text, {"AVGO"})
    assert r and r["symbols"] == ["AVGO"]
