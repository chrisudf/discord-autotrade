"""[8/13 MU] TP 拒单熔断 + 重复日志收敛。

事故形状：MU 945C 的 TP T1 打在 broker 的 naked-short 防护上（broker 0 长仓，
本地 DB 说还有 2 张）。拒单不置 tp_hits 位（位掩码在下单成功之后才置），
`_sell_rejected` 又把 `_triggered_this_tick` 撤了，于是每轮 5s tick 原样重来：
00:11 → 06:00，1918 次，5754 行日志，1918 条 TG。

这里钉三件事：
  1. 确定性拒单（naked-short）**第一次就熔断**，第二轮 tick 不再打 broker；
  2. 瞬时拒单退避重试，连续 SELL_REJECT_MAX_FAILS 次后熔断；
  3. 熔断**不置 tp_hits 位** —— 停手 + 喊人，不是把没落袋的止盈静默标记成完成；
  4. 告警只在首次失败与熔断两个时刻发，中间静默（同 watchdog 的节流形状）。
"""
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.position import manager, retry_guard, tp_watcher
from autotrade.storage import positions_db
from autotrade.utils import logdedup

_NAKED = ("naked-short refused: broker has only 0 long of US.MU260814C945000, "
          "asked to sell 1. This would open a naked short — refusing.")


def _open_pos(qty: int = 2, entry: float = 2.0) -> str:
    code = f"US.MU{datetime.now().strftime('%H%M%S%f')}C945000"
    positions_db.open_or_add(
        option_code=code, symbol="MU", strike=945.0, side="CALL",
        expiry=date(2026, 8, 14), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_0813",
    )
    return code


def _pos(code: str) -> dict:
    return dict(manager.get(code))


async def _tick(code: str, sell_mock, tg_mock, *, last: float = 14.80):
    """跑一轮 TP T1 触发（模拟一次 tick），tick 边界的去重集合照例重置。"""
    tp_watcher._triggered_this_tick.clear()
    with patch("autotrade.position.tp_watcher.place_sell_order", sell_mock), \
         patch("autotrade.position.tp_watcher.send_telegram", tg_mock):
        await tp_watcher._trigger_tp(_pos(code), last, 0.50, 50, 1, 0.05)


@pytest.mark.asyncio
async def test_naked_short_reject_trips_after_one_attempt(monkeypatch):
    """确定性拒单：第 1 轮打一次 broker，之后 100 轮一次都不打。"""
    code = _open_pos()
    sells = []

    def sell(**kw):
        sells.append(kw)
        return {"success": False, "message": _NAKED, "order_id": None}

    tg = AsyncMock()
    for _ in range(100):
        await _tick(code, sell, tg)

    assert len(sells) == 1, f"确定性拒单被重试了 {len(sells)} 次（8/13 是 1918 次）"
    assert retry_guard.is_tripped(f"tp:{code}:t1")
    # 熔断不等于"这一档做完了"：位掩码必须保持干净，否则重启后止盈永久消失
    assert manager.get(code)["tp_hits"] & 1 == 0
    assert manager.get(code)["qty_remaining"] == 2


@pytest.mark.asyncio
async def test_naked_short_alert_is_sent_once_with_reconcile_hint():
    """1918 次拒单只该换来 1 条 TG，且要给出下一步动作（对账）。"""
    code = _open_pos()
    tg = AsyncMock()

    def sell(**kw):
        return {"success": False, "message": _NAKED, "order_id": None}

    for _ in range(50):
        await _tick(code, sell, tg)

    assert tg.await_count == 1, f"发了 {tg.await_count} 条 TG"
    body = tg.await_args[0][0]
    assert "熔断" in body
    # format_error 走 MarkdownV2 转义，下划线会变成 \_ —— 只钉住可读部分
    assert "sync" in body and "positions" in body, "确定性拒单的告警必须带对账指引"
    assert "SL / EOD / 手工平仓" in body, "得说清熔断后这张仓还剩什么兜底"


@pytest.mark.asyncio
async def test_transient_reject_backs_off_then_trips(monkeypatch):
    """瞬时拒单：退避重试，连续 max_fails 次后熔断；退避期内不打 broker。"""
    monkeypatch.setenv("SELL_REJECT_MAX_FAILS", "3")
    monkeypatch.setenv("SELL_REJECT_BACKOFF_BASE_SEC", "30")
    code = _open_pos()
    sells = []

    def sell(**kw):
        sells.append(kw)
        return {"success": False, "message": "order rejected: price too far",
                "order_id": None}

    tg = AsyncMock()
    now = [1000.0]
    with patch("autotrade.position.retry_guard.time.monotonic", lambda: now[0]):
        await _tick(code, sell, tg)
        assert len(sells) == 1

        # 退避期内（30s）连打 10 轮，一次都不该到 broker
        for _ in range(10):
            now[0] += 2.0
            await _tick(code, sell, tg)
        assert len(sells) == 1, "退避期内仍在打 broker"

        # 退避到期 → 第 2 次尝试；再到期 → 第 3 次，达到 max_fails 熔断
        now[0] += 60.0
        await _tick(code, sell, tg)
        assert len(sells) == 2
        now[0] += 300.0
        await _tick(code, sell, tg)
        assert len(sells) == 3
        assert retry_guard.is_tripped(f"tp:{code}:t1")

        now[0] += 10_000.0
        await _tick(code, sell, tg)
        assert len(sells) == 3, "熔断后仍在重试"

    # 告警只在首次失败与熔断两端，中间的第 2 次静默
    assert tg.await_count == 2, f"发了 {tg.await_count} 条 TG，应为首次 + 熔断"
    assert "熔断" in tg.await_args[0][0]


@pytest.mark.asyncio
async def test_success_clears_backoff_state():
    """一次失败后卖成了 → 熔断状态清空，不留退避残留影响后续档位。"""
    code = _open_pos()
    calls = {"n": 0}

    def sell(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": False, "message": "transient glitch", "order_id": None}
        return {"success": True, "message": "ok", "order_id": "OID1",
                "qty": kw["qty"], "price": kw["limit_price"]}

    tg = AsyncMock()
    now = [1000.0]
    with patch("autotrade.position.retry_guard.time.monotonic", lambda: now[0]):
        await _tick(code, sell, tg)
        now[0] += 60.0
        with patch("autotrade.position.tp_watcher.fill_checker.spawn"):
            await _tick(code, sell, tg)

    assert calls["n"] == 2
    assert retry_guard.blocked(f"tp:{code}:t1") is None
    assert not retry_guard.is_tripped(f"tp:{code}:t1")
    assert manager.get(code)["tp_hits"] & 1 == 1  # 这一档真做完了才置位


@pytest.mark.asyncio
async def test_trip_is_scoped_to_one_contract_and_tier():
    """熔断只挡这一个 (合约, 档位)，别的仓位照常走。"""
    bad = _open_pos()
    good = _open_pos()

    def sell(**kw):
        if kw["option_code"] == bad:
            return {"success": False, "message": _NAKED, "order_id": None}
        return {"success": True, "message": "ok", "order_id": "OID2",
                "qty": kw["qty"], "price": kw["limit_price"]}

    tg = AsyncMock()
    await _tick(bad, sell, tg)
    with patch("autotrade.position.tp_watcher.fill_checker.spawn"):
        await _tick(good, sell, tg)

    assert retry_guard.is_tripped(f"tp:{bad}:t1")
    assert not retry_guard.is_tripped(f"tp:{good}:t1")
    assert manager.get(good)["tp_hits"] & 1 == 1


# ============================================================
# 日志收敛（第二层兜底：万一将来出现熔断接不住的循环）
# ============================================================

def test_log_throttled_first_line_never_swallowed(monkeypatch):
    monkeypatch.setenv("LOG_DEDUP_WINDOW_SEC", "60")
    lines = []
    with patch("autotrade.utils.logdedup.logger.log",
               side_effect=lambda lvl, msg: lines.append(msg)):
        assert logdedup.log_throttled("k", "第一声") is True
        for _ in range(500):
            logdedup.log_throttled("k", "重复")
    assert lines == ["第一声"], "窗口内应只落第一条"


def test_log_throttled_reports_suppressed_count_after_window(monkeypatch):
    monkeypatch.setenv("LOG_DEDUP_WINDOW_SEC", "60")
    lines = []
    now = [500.0]
    with patch("autotrade.utils.logdedup.time.monotonic", lambda: now[0]), \
         patch("autotrade.utils.logdedup.logger.log",
               side_effect=lambda lvl, msg: lines.append(msg)):
        logdedup.log_throttled("k", "A")
        for _ in range(9):
            logdedup.log_throttled("k", "A")
        now[0] += 61.0
        logdedup.log_throttled("k", "A")
    assert len(lines) == 2
    assert "另有 9 条同类被收敛" in lines[1]


def test_log_throttled_flush_reports_tail(monkeypatch):
    """故障恢复后不会再有下一条同 key 日志——被压掉的条数靠 flush 补报。"""
    monkeypatch.setenv("LOG_DEDUP_WINDOW_SEC", "60")
    lines = []
    with patch("autotrade.utils.logdedup.logger.log",
               side_effect=lambda lvl, msg: lines.append(msg)):
        logdedup.log_throttled("k", "A")
        for _ in range(4):
            logdedup.log_throttled("k", "A")
        logdedup.flush("k")
    assert len(lines) == 2
    assert "另有 4 条同类被收敛" in lines[1]


def test_log_throttled_keys_are_independent(monkeypatch):
    monkeypatch.setenv("LOG_DEDUP_WINDOW_SEC", "60")
    lines = []
    with patch("autotrade.utils.logdedup.logger.log",
               side_effect=lambda lvl, msg: lines.append(msg)):
        logdedup.log_throttled("a", "A")
        logdedup.log_throttled("b", "B")
        logdedup.log_throttled("a", "A2")
    assert lines == ["A", "B"]
