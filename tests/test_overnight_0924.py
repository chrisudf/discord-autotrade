"""9/23-9/24 复盘：对账器不看在挂的买单 + 重新开仓沿用旧止损。

1. RKLB 72C 下单 4 分钟就被判 db_only，挂够两轮就落账 CLOSED；之后才成交的话，
   broker 上就是一张没人看护的仓（MU 1105C 差一小时就是这样）。
2. 从未成交的单被记成"捏造价"，pnl 写"若按 0 计会虚记 $-1,532"，其实真实盈亏是 0。
3. 9/18 MU 1000C 第二轮进场没声明止损，却沿用了第一轮的 2.10。
"""
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.ops import pnl
from autotrade.position import reconciler
from autotrade.storage import logger_db, positions_db


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    reconciler._last_signature = None
    reconciler._db_only_seen = set()
    yield
    reconciler._last_signature = None
    reconciler._db_only_seen = set()


def _uniq(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open_with_order(code: str, order_id: str = "OID1"):
    positions_db.open_or_add(
        option_code=code, symbol=code[3:7], strike=72.0, side="CALL",
        expiry=date(2026, 10, 2), qty=4, fill_price=2.05, category="weekly",
        apply_sl=True, eod_force_close=False, tags=[],
        channel_name="enrich", msg_id="m1")
    logger_db.log_order("m1", {"symbol": code[3:7]},
                        {"code": code, "success": True, "order_id": order_id, "qty": 4})


async def _two_rounds(code: str, status: dict, other: str):
    """broker 只剩另一张仓（veto 要求 broker 侧非空），连跑两轮对账。"""
    with patch.object(reconciler, "list_open_option_positions", return_value={other: 1}), \
         patch.object(reconciler, "query_order_status", return_value=status), \
         patch.object(reconciler, "send_telegram", AsyncMock(return_value=True)):
        await reconciler._reconcile_tick()
        await reconciler._reconcile_tick()


def _keep_alive():
    other = _uniq("LIVE")
    positions_db.open_or_add(
        option_code=other, symbol="LIVE", strike=1.0, side="CALL",
        expiry=date(2026, 10, 2), qty=1, fill_price=1.0, category="weekly",
        apply_sl=True, eod_force_close=False, tags=[], channel_name="ashley", msg_id="m0")
    return other


# ============================================================
# 1. 买单还在挂 → 不是漂移
# ============================================================

@pytest.mark.asyncio
async def test_working_buy_is_not_closed_by_the_reconciler():
    """契约翻转：当晚 RKLB 72C 的形状，两轮 db_only 就被落账。"""
    code, other = _uniq("RKLB"), _keep_alive()
    _open_with_order(code)

    await _two_rounds(code, {"success": True, "status": "SUBMITTED", "filled_qty": 0}, other)

    assert positions_db.get(code)["status"] == "OPEN", "买单还在挂，不许落账"
    assert code not in reconciler._db_only_seen, "在挂的买单也不许攒成闸门 4 的证据"


# ============================================================
# 2. 买单已死、0 张成交 → 照常落账，但写明"从未成交"
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["CANCELLED_ALL", "SOME_NEW_STATUS"])
async def test_dead_unfilled_buy_closes_as_never_filled(status):
    """陌生状态也要能落账 —— 只认明确"在挂"的几个，否则仓位会永远挂在 DB 里。"""
    code, other = _uniq("PANW"), _keep_alive()
    _open_with_order(code)

    await _two_rounds(code, {"success": True, "status": status, "filled_qty": 0}, other)

    assert positions_db.get(code)["status"] == "CLOSED"
    close = [e for e in positions_db.get_events(code) if e["event_type"] == "CLOSE"][-1]
    assert "never filled" in close["note"]


def test_pnl_says_never_filled_legs_are_zero_not_fabricated_loss():
    legs = pnl.exit_legs(
        [{"option_code": "US.X", "event_type": "CLOSE", "qty_delta": -4, "price": None,
          "trigger_source": "broker_sync", "ts": "2026-09-24T02:38:48+00:00",
          "note": "reconcile auto-close: buy order never filled"}],
        {"US.X": {"avg_entry_price": 3.83, "channel_name": "ashley", "category": "weekly"}})
    assert legs and legs[0]["fabricated"] and legs[0]["never_filled"]


# ============================================================
# 3. 反向：查不到单 / 查询失败 → 行为不变
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    None,                                                           # orders 表里没有
    {"success": False, "status": None, "message": "order not found"},  # 隔天的单查不到
])
async def test_unknown_order_state_keeps_old_behaviour(status):
    code, other = _uniq("MU"), _keep_alive()
    if status is None:
        positions_db.open_or_add(
            option_code=code, symbol="MU", strike=1.0, side="CALL",
            expiry=date(2026, 10, 2), qty=4, fill_price=2.0, category="weekly",
            apply_sl=True, eod_force_close=False, tags=[], channel_name="ashley", msg_id="m")
    else:
        _open_with_order(code)

    await _two_rounds(code, status or {}, other)

    assert positions_db.get(code)["status"] == "CLOSED"
    close = [e for e in positions_db.get_events(code) if e["event_type"] == "CLOSE"][-1]
    assert "no longer has this position" in close["note"]


# ============================================================
# 4. 重新开仓不继承上一轮的声明止损
# ============================================================

def test_reopen_drops_the_previous_rounds_declared_stop():
    """9/18 MU 1000C：第一轮止损 2.10，平掉后第二轮 2.95 进场，止损被原样沿用。"""
    code = _uniq("MU")
    kw = dict(option_code=code, symbol="MU", strike=1000.0, side="CALL",
              expiry=date(2026, 9, 18), qty=4, category="weekly", apply_sl=True,
              eod_force_close=False, tags=[], channel_name="ashley", msg_id="m")
    positions_db.open_or_add(fill_price=2.50, **kw)
    positions_db.set_manual_stop(code, 2.10)
    positions_db.record_close(code, 4, 2.0, "sl_polling", note="ut")

    positions_db.open_or_add(fill_price=2.95, **kw)

    assert positions_db.get(code)["manual_stop"] is None
