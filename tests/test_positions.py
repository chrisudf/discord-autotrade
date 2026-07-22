"""positions_db + position manager 冒烟测试

注意：用真实 DB 文件（data/trades.db）—— 测试前后用唯一 option_code 隔离，
不清表避免污染线上数据。TODO: 后续切到 tmpdir + monkeypatch DB_PATH 干净隔离。
"""
from datetime import date, datetime, timezone
import pytest

from autotrade.storage import positions_db
from autotrade.position import manager


def _uniq(prefix: str = "TEST") -> str:
    """唯一 option_code，避免测试间污染。"""
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def test_categorize_weekly_has_sl():
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(date(2026, 6, 20), today, [])
    assert (cat, sl, eod) == ("weekly", True, False)


def test_categorize_0dte_no_sl_but_eod_force():
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(today, today, [])
    assert (cat, sl, eod) == ("0dte", False, True)


def test_categorize_0dte_lotto_still_eod_force():
    """0DTE + lotto 标签：仍然必须 EOD 强平（合约要过期）。"""
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(today, today, ["lotto"])
    assert (cat, sl, eod) == ("0dte_lotto", False, True)


def test_categorize_weekly_lotto_no_sl_no_eod():
    """周内 lotto：放飞到 expiry，不挂 SL、不 EOD 强平。"""
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(date(2026, 6, 20), today, ["lotto"])
    assert (cat, sl, eod) == ("lotto", False, False)


def test_categorize_swing_no_sl():
    today = date(2026, 6, 17)
    cat, sl, eod = positions_db.categorize(date(2026, 7, 17), today, [])
    assert (cat, sl, eod) == ("swing", False, False)


def test_open_and_partial_close():
    code = _uniq("OPN")
    pos = positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=4, fill_price=1.50,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    assert pos["qty_remaining"] == 4
    assert pos["status"] == "OPEN"
    assert pos["apply_sl"] is True

    # 部分卖 2 张
    pos = positions_db.record_close(
        option_code=code, qty_sold=2, fill_price=2.50,
        trigger_source="kc_signal", ref_msg_id="m2",
    )
    assert pos["qty_remaining"] == 2
    assert pos["status"] == "PARTIAL"

    # 全平剩下
    pos = positions_db.record_close(
        option_code=code, qty_sold=2, fill_price=3.00,
        trigger_source="tp_polling",
    )
    assert pos["qty_remaining"] == 0
    assert pos["status"] == "CLOSED"
    assert pos["closed_at"] is not None


def test_add_on_weighted_average():
    code = _uniq("ADD")
    positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=2, fill_price=1.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    pos = positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=2, fill_price=2.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m2",
    )
    assert pos["qty_total"] == 4
    assert pos["qty_remaining"] == 4
    assert pos["avg_entry_price"] == pytest.approx(1.50)


def test_over_close_clamps():
    code = _uniq("OVR")
    positions_db.open_or_add(
        option_code=code, symbol="TEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=1, fill_price=1.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    pos = positions_db.record_close(
        option_code=code, qty_sold=5, fill_price=2.00,  # 卖多了
        trigger_source="manual",
    )
    assert pos["qty_remaining"] == 0
    assert pos["status"] == "CLOSED"


def test_get_open_symbols_excludes_closed():
    code = _uniq("SYM")
    positions_db.open_or_add(
        option_code=code, symbol="ZZTEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=1, fill_price=1.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    assert "ZZTEST" in positions_db.get_open_symbols()
    positions_db.record_close(code, 1, 2.0, trigger_source="manual")
    assert "ZZTEST" not in positions_db.get_open_symbols()


def test_manager_on_order_filled_routes_correctly():
    code = _uniq("MGR")
    # 用相对日期 today+30 而非硬编码：避免日历走过 2026-06-25 后
    # categorize() 把信号判成 0DTE → category 变 "0dte_lotto"
    from datetime import timedelta
    signal = {
        "symbol": "MGRTEST", "strike": 10.0, "side": "CALL",
        "expiry_date": date.today() + timedelta(days=30), "price": 1.0,
        "tags": ["lotto"],
    }
    order_result = {
        "success": True, "code": code, "qty": 2, "price": 1.10,
        "order_id": "ORD1",
    }
    pos = manager.on_order_filled(
        signal=signal, order_result=order_result,
        channel_name="ut", msg_id="m1",
    )
    assert pos is not None
    assert pos["category"] == "lotto"
    assert pos["apply_sl"] is False
    assert pos["avg_entry_price"] == pytest.approx(1.10)


def test_reopen_resets_position_and_refreshes_flags():
    """reopen（CLOSED 后同 code 再开）必须按新信号刷新全部元数据。

    场景：DTE=5 开 weekly（eod_force_close=False）→ 全平 → 到期日当天
    KC 重开同一合约 → categorize 判 0dte / eod_force_close=True。
    旧实现沿用旧 flag → EOD watcher 不强平 → ITM 自动行权（lessons #14）。
    """
    code = _uniq("REO")
    first = positions_db.open_or_add(
        option_code=code, symbol="REOTEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=2, fill_price=1.00,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="chan_a", msg_id="m1",
    )
    positions_db.mark_tp_hit(code, 1)  # 旧仓位 T1 已触发过
    positions_db.record_close(code, qty_sold=2, fill_price=2.00,
                              trigger_source="kc_signal")

    pos = positions_db.open_or_add(
        option_code=code, symbol="REOTEST", strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=1, fill_price=3.00,
        category="0dte", apply_sl=False, eod_force_close=True, tags=["lotto"],
        channel_name="chan_b", msg_id="m9",
    )
    # 数量/均价重置为本次数据（不与旧仓位加权平均）
    assert pos["qty_total"] == 1
    assert pos["qty_remaining"] == 1
    assert pos["avg_entry_price"] == pytest.approx(3.00)
    assert pos["status"] == "OPEN"
    assert pos["closed_at"] is None
    # 元数据按新信号刷新（本次修复的核心）
    assert pos["category"] == "0dte"
    assert pos["apply_sl"] is False
    assert pos["eod_force_close"] is True
    assert pos["tags"] == ["lotto"]
    assert pos["channel_name"] == "chan_b"
    assert pos["open_msg_id"] == "m9"
    assert pos["opened_at"] != first["opened_at"]
    # TP 档位清零，watcher 不会误跳过 T1
    assert pos["tp_hits"] == 0


def test_calc_qty_to_sell():
    pos = {"qty_remaining": 4}
    assert manager.calc_qty_to_sell(pos, 100) == 4
    assert manager.calc_qty_to_sell(pos, 50) == 2
    assert manager.calc_qty_to_sell(pos, 25) == 1


def test_calc_qty_to_sell_single_contract_runner_preserve():
    """规则 v2 (7/2 起): 1 张持仓 + pct<100 → 不卖，保留 runner

    历史损失驱动改动：6/30 SPY 748c、7/1 MSFT 390c 都是 1 张持仓被 KC 33% trim
    信号直接全平，然后 KC 后续走到 +100%~+150% 我们没吃到。
    """
    pos = {"qty_remaining": 1}
    # trim 系列全部跳过
    assert manager.calc_qty_to_sell(pos, 25) == 0
    assert manager.calc_qty_to_sell(pos, 33) == 0
    assert manager.calc_qty_to_sell(pos, 50) == 0
    assert manager.calc_qty_to_sell(pos, 75) == 0
    assert manager.calc_qty_to_sell(pos, 99) == 0
    # 但 100% 明确清仓仍执行
    assert manager.calc_qty_to_sell(pos, 100) == 1


def test_calc_qty_to_sell_multi_contract_unchanged():
    """规则 v2 只影响 qty=1；qty>=2 时保持向上取整行为"""
    pos = {"qty_remaining": 2}
    assert manager.calc_qty_to_sell(pos, 25) == 1  # ceil(0.5) = 1
    assert manager.calc_qty_to_sell(pos, 50) == 1  # ceil(1.0) = 1
    assert manager.calc_qty_to_sell(pos, 100) == 2


# ============ sweep_expired（7/13 复盘：过期合约残留 OPEN） ============

def _open_test_pos(code: str, symbol: str, expiry: date, qty: int = 2):
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=15.0, side="CALL",
        expiry=expiry, qty=qty, fill_price=1.50,
        category="weekly", apply_sl=True, eod_force_close=False,
        tags=[], channel_name="test", msg_id="1",
    )


def test_sweep_expired_marks_and_excludes():
    code = _uniq("EXPA")
    _open_test_pos(code, "EXPA", date(2026, 7, 10))

    swept = positions_db.sweep_expired(date(2026, 7, 13))

    assert code in [p["option_code"] for p in swept]
    pos = positions_db.get(code)
    assert pos["status"] == "EXPIRED"
    assert pos["qty_remaining"] == 0
    # 移出 watcher 轮询和 close 白名单
    assert code not in [p["option_code"] for p in positions_db.get_open_positions()]
    assert "EXPA" not in positions_db.get_open_symbols()
    # 事件流水留痕
    events = positions_db.get_events(code)
    assert events[-1]["event_type"] == "EXPIRE"
    assert events[-1]["qty_delta"] == -2
    assert events[-1]["trigger_source"] == "expiry_sweep"


def test_sweep_expired_keeps_today_and_future():
    """expiry == today 不清（当天仍可交易，EOD watcher 15:50 强平）。"""
    today_code = _uniq("EXPB")
    future_code = _uniq("EXPC")
    _open_test_pos(today_code, "EXPB", date(2026, 7, 13))
    _open_test_pos(future_code, "EXPC", date(2026, 8, 21))

    swept = positions_db.sweep_expired(date(2026, 7, 13))

    swept_codes = [p["option_code"] for p in swept]
    assert today_code not in swept_codes
    assert future_code not in swept_codes
    assert positions_db.get(today_code)["status"] == "OPEN"
    assert positions_db.get(future_code)["status"] == "OPEN"


def test_sweep_expired_idempotent():
    code = _uniq("EXPD")
    _open_test_pos(code, "EXPD", date(2026, 7, 10))

    first = positions_db.sweep_expired(date(2026, 7, 13))
    second = positions_db.sweep_expired(date(2026, 7, 13))

    assert [p["option_code"] for p in first].count(code) == 1
    assert second == []
