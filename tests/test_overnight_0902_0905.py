"""9/2 与 9/5 两晚的回归（8/31–9/4 周复盘的第 1、2、5 条）。

两晚两个独立故障，共同点是**"停手 + 喊人"里的"喊人"那一半失灵了**：

1. **9/2 TSLA 345P，-$166。** 23:38:46 买单提交成功、DB 乐观入账，
   23:41:47 fill_checker 轮询 180s 没等到成交（TG 告警），23:43:52
   "trimmed Tesla 2.95" 解析**完全正确**，卖单撞上 broker 的 0 长仓 →
   `naked-short refused` → `is_deterministic_reject` 命中 → 立刻熔断，
   2 秒后的 ZH 孪生也被挡掉。05:50 EOD 强平 2 张 —— 那时 broker 手里是有货的。
   即：那次 0 长仓的成因是**买单还在飞**，不是 DB 脱钩。

2. **9/5 断网，三张当日到期合约烂在手里 -$714。** EOD 15:50 ET 准时进窗、
   每 30s 重试到收盘，全部卡在 no-quote（OpenD 跟着断网一起死），
   90 条"需要人工处理"的告警外加 101 次 ConnectError —— 全部只剩日志里的
   WARNING 行，而日志要等第二天早上才有人读。

本文件三节，各钉一条修复：
  §1 broker/inflight —— 在飞买单让 naked-short 归瞬时组
  §2 fill_checker —— 超时**不**销账（超时的语义是"仍然不知道"）
  §3 notify/transport —— 送不出去的告警落盘，等早间摘要
"""
import asyncio

import pytest

from autotrade.broker import inflight
from autotrade.broker.errors import is_deterministic_reject
from autotrade.position import retry_guard

CODE = "US.TSLA260904P345000"


@pytest.fixture(autouse=True)
def _clean_state():
    inflight.reset_state()
    retry_guard.reset_state()
    yield
    inflight.reset_state()
    retry_guard.reset_state()


# ============================================================
# 1. 在飞买单 → naked-short 归瞬时组，不再一次拒单就永久熔断
# ============================================================

def test_pending_buy_is_tracked_until_terminal():
    assert inflight.is_pending(CODE) is False
    inflight.mark_submitted(CODE)
    assert inflight.is_pending(CODE) is True
    inflight.clear(CODE)
    assert inflight.is_pending(CODE) is False


def test_pending_expires_after_grace(monkeypatch):
    """豁免是有窗口的：过了窗口 broker 还说 0 张，那就是真脱钩，该熔断就熔断。"""
    monkeypatch.setenv("INFLIGHT_BUY_GRACE_SEC", "0")
    inflight.mark_submitted(CODE)
    assert inflight.is_pending(CODE) is False


def test_refused_message_still_trips_the_breaker():
    """契约不变的那一半：没有在飞买单时，8/13 MU 那种真脱钩照旧立刻熔断。"""
    msg = (f"naked-short refused: broker has only 0 long of {CODE}, "
           f"asked to sell 1. This would open a naked short — refusing.")
    assert is_deterministic_reject(msg) is True
    d = retry_guard.on_reject(f"kc:{CODE}", msg, backoff=False)
    assert d.tripped is True


def test_deferred_message_does_not_trip_the_breaker():
    """契约翻转：买单在飞时 broker 换一套措辞，四个 on_reject 调用点自动走瞬时组。"""
    msg = (f"naked-short deferred: broker has only 0 long of {CODE}, "
           f"asked to sell 1, but a submitted buy is still unconfirmed — "
           f"treating as transient, will retry.")
    assert is_deterministic_reject(msg) is False
    d = retry_guard.on_reject(f"kc:{CODE}", msg, backoff=False)
    assert d.tripped is False
    # 9/2 的实际后果：2 秒后的 ZH 孪生被 blocked() 挡掉。现在它进得来。
    assert retry_guard.blocked(f"kc:{CODE}") is None


def test_sell_order_picks_the_deferred_branch_while_buy_in_flight(monkeypatch):
    """走一遍 place_sell_order 的 naked-short 分支本身，而不只是措辞。"""
    from autotrade.broker import trade

    monkeypatch.setattr(trade, "_get_long_qty", lambda code: 0)
    monkeypatch.setattr(trade, "_is_dry_run", lambda: False)

    inflight.mark_submitted(CODE)
    res = trade.place_sell_order(CODE, qty=1, limit_price=2.80)
    assert res["success"] is False
    assert "naked-short deferred" in res["message"]
    assert is_deterministic_reject(res["message"]) is False

    inflight.clear(CODE)
    res2 = trade.place_sell_order(CODE, qty=1, limit_price=2.80)
    assert res2["success"] is False
    assert "naked-short refused" in res2["message"]
    assert is_deterministic_reject(res2["message"]) is True


# ============================================================
# 2. fill_checker：只有终态才销账，超时不销
# ============================================================

def _run_confirm(monkeypatch, outcome: dict):
    from autotrade.position import fill_checker

    async def fake_poll(order_id):
        return outcome

    monkeypatch.setattr(fill_checker, "_poll_until_terminal", fake_poll)
    monkeypatch.setattr(fill_checker, "send_telegram", _noop_async)
    inflight.mark_submitted(CODE)
    asyncio.run(fill_checker.confirm_buy_fill("2104813", CODE, 2, 2.59))


async def _noop_async(*a, **kw):
    return True


def test_timeout_keeps_the_buy_in_flight(monkeypatch):
    """9/2 的核心：超时 ≠ 没成交，只是**还不知道**。这正是要豁免的状态。"""
    _run_confirm(monkeypatch, {"outcome": "timeout", "status": "SUBMITTED",
                               "filled_qty": 0})
    assert inflight.is_pending(CODE) is True


def test_filled_clears_the_flight(monkeypatch):
    """成交了就销账 —— 之后再报 0 长仓就是真脱钩，不该再被豁免。"""
    _run_confirm(monkeypatch, {"outcome": "filled", "success": True,
                               "status": "FILLED_ALL", "filled_avg_price": 2.59})
    assert inflight.is_pending(CODE) is False


def test_dead_order_clears_the_flight(monkeypatch):
    _run_confirm(monkeypatch, {"outcome": "dead", "success": True,
                               "status": "CANCELLED_ALL"})
    assert inflight.is_pending(CODE) is False


# ============================================================
# 3. 送不出去的告警落盘
# ============================================================

@pytest.fixture
def _tg(monkeypatch, tmp_path):
    from autotrade.notify import transport

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "BOT_TOKEN", "token")
    monkeypatch.setattr(transport, "CHAT_ID", "chat")
    monkeypatch.setattr(transport, "_undelivered_broken", False)
    return transport


def _sink(transport) -> str:
    p = transport._undelivered_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_failed_alert_lands_on_disk(_tg):
    """9/5 那 90 条 EOD 告警要在断网时留下痕迹，而不是只剩一行 WARNING。"""
    async def fail(text, parse_mode):
        return False

    _tg._send_telegram_inner = fail
    assert asyncio.run(_tg.send_telegram("🕒 到期未平\nUS.NBIS260904C215000\t需人工")) is False

    lines = _sink(_tg).splitlines()
    assert len(lines) == 1
    ts, body = lines[0].split("\t", 1)
    assert ts.endswith("Z")
    # 单行 TSV：正文换行压成 " / "、tab 压成空格，morning_collect 的 awk 才能按行归并
    assert body == "🕒 到期未平 / US.NBIS260904C215000 需人工"


def test_successful_alert_is_not_recorded(_tg):
    async def ok(text, parse_mode):
        return True

    _tg._send_telegram_inner = ok
    assert asyncio.run(_tg.send_telegram("正常送达")) is True
    assert _sink(_tg) == ""


def test_unconfigured_is_not_recorded(monkeypatch, _tg):
    """没配 token 是部署问题，不是"送不出去" —— 否则 DRY_RUN / 测试会刷垃圾。"""
    monkeypatch.setattr(_tg, "BOT_TOKEN", "")
    assert asyncio.run(_tg.send_telegram("不该落盘")) is False
    assert _sink(_tg) == ""


def test_sink_stops_growing_past_the_cap(_tg, monkeypatch):
    """7/31 是磁盘写满引发的事故。断网期间这个文件是唯一还在长的东西。"""
    monkeypatch.setattr(_tg, "_UNDELIVERED_MAX_BYTES", 10)

    async def fail(text, parse_mode):
        return False

    _tg._send_telegram_inner = fail
    for _ in range(5):
        asyncio.run(_tg.send_telegram("超过十个字节的一条告警"))
    assert len(_sink(_tg).splitlines()) == 1
