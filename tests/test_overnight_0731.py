"""7/31 夜复盘回归。

当晚两件事：

1. **磁盘写满**（09:50:20-42 AEST = 19:50 ET，收盘后约 3h50m）——sl/tp/eod 三个
   watcher 在 22 秒里连抛 33 条 `sqlite3.OperationalError: disk I/O error`，
   TG 全程沉默。要命的不是那 22 秒（当晚唯一持仓 TSLA 260807C340 是 swing、
   apply_sl=0、eod_force=0，收盘后本就没有敞口），而是**故障把自己的证据擦掉了**：
   日志 sink 与 sqlite 写在同一块盘上，app_2026-08-01.log 从 08:34 直接跳到
   11:10，errors/ 当天 0 字节。风控三路全灭，磁盘上零痕迹。
   对应修复：watcher 的 except 分支改走 notify/watchdog——落日志 + 独立 TG
   通道 + per-scope 节流 + 恢复通知。

2. **喊价前置语序丢单**（03:41 AEST = 13:41 ET 盘中）——
   "$AAOI scalp 0DTE $.70 $98 calls" 把喊价写在了行权价前面，Pattern B 全系列
   只认 "$STRIKE calls ... $PRICE"，中英双播两条全丢。
   对应修复：入口做一次语序归一化，改写成规范序后复用既有 B 阶梯。
"""
from datetime import date

import pytest

from autotrade.notify import watchdog
from autotrade.parsing.signal_parser import parse_signal

D = date(2026, 7, 31)


# ============================================================
# 1. 喊价前置语序（7/31 13:41 ET 实锤丢单）
# ============================================================

@pytest.mark.parametrize("raw", [
    "$AAOI scalp 0DTE $.70 $98 calls",            # 当晚 EN 原文
    "$AAOI 头皮 0DTE $.70 $98 看涨期权",            # 当晚 ZH 孪生
])
def test_inverted_price_strike_now_parses(raw):
    sig = parse_signal(raw, msg_ts=D)
    assert sig is not None, "喊价前置写法不该再丢单"
    assert sig["symbol"] == "AAOI"
    assert sig["side"] == "CALL"
    assert sig["strike"] == 98.0
    assert sig["price"] == 0.70
    assert "scalp" in sig["tags"]


@pytest.mark.parametrize("raw,strike,price,side", [
    ("$HOOD 7/24 $1.50 $125 calls", 125.0, 1.50, "CALL"),   # 倒序 + MM/DD
    ("$SPY 0DTE $2.10 $730 puts", 730.0, 2.10, "PUT"),      # 倒序 + NDTE + puts
    ("$NOW $1.35 $115 calls", 115.0, 1.35, "CALL"),         # 倒序 + 无日期(weekly)
])
def test_inverted_order_across_expiry_forms(raw, strike, price, side):
    """归一化后复用既有 B 阶梯，各种到期日形态都应继承。"""
    sig = parse_signal(raw, msg_ts=D)
    assert sig is not None
    assert (sig["strike"], sig["price"], sig["side"]) == (strike, price, side)


def test_canonical_order_unaffected():
    """规范序（喊价在后）行为必须逐字不变。"""
    sig = parse_signal("$AAOI scalp 0DTE $98 calls $.70", msg_ts=D)
    assert sig is not None
    assert (sig["strike"], sig["price"]) == (98.0, 0.70)


@pytest.mark.parametrize("raw", [
    "$SPY $740 $745 calls",     # 价差写法：两个整数，护栏1(必须带小数点)拦下
    "$XYZ $99.5 $98 calls",     # price >= strike：护栏2 拦下
])
def test_swap_guards_reject_non_premium(raw):
    """换反 strike/price = 用行权价当限价下单，宁可不接也不能猜错。"""
    assert parse_signal(raw, msg_ts=D) is None


def test_price_levels_broadcast_still_skipped():
    """当晚同频道的 SPY levels 播报含一堆 $金额，不能被倒序正则勾出信号。"""
    raw = (
        "$SPY levels for the day 7/31/2026:\n\n"
        "Blue zone= $742.81, $746.56\n"
        "Green targets = $748.20, $749.80, $751.41\n"
        "Red targets = $741.04, $739.36, $736.64\n\n"
        "@everyone $alert"
    )
    sig = parse_signal(raw, msg_ts=D)
    assert sig is None or "skip" in sig


# ============================================================
# 2. watcher tick 异常 → 节流 TG 告警
# ============================================================

@pytest.fixture(autouse=True)
def _clean_watchdog():
    watchdog.reset_state()
    yield
    watchdog.reset_state()


@pytest.fixture
def sent(monkeypatch):
    msgs = []
    monkeypatch.setattr(watchdog, "notify_bg", msgs.append)
    return msgs


def test_first_error_alerts_immediately(sent):
    """首次出错不等节流窗口——7/31 那晚一条都没发出去。"""
    watchdog.notify_tick_error("sl", RuntimeError("disk I/O error"))
    assert len(sent) == 1
    assert "sl watcher tick" in sent[0]
    assert "disk I/O error" in sent[0]


def test_burst_is_throttled_to_one_alert(monkeypatch, sent):
    """22 秒 33 条不能变成 33 条 TG——刷屏等于没有告警。"""
    monkeypatch.setenv("WATCHER_ERROR_ALERT_COOLDOWN_SEC", "300")
    for _ in range(33):
        watchdog.notify_tick_error("sl", RuntimeError("disk I/O error"))
    assert len(sent) == 1


def test_cooldown_expiry_reports_suppressed_count(monkeypatch, sent):
    monkeypatch.setenv("WATCHER_ERROR_ALERT_COOLDOWN_SEC", "0")
    for _ in range(3):
        watchdog.notify_tick_error("tp", RuntimeError("boom"))
    assert len(sent) == 3


def test_scopes_throttle_independently(monkeypatch, sent):
    """sl/tp/eod 同时挂掉时，三路都要各自报一次。"""
    monkeypatch.setenv("WATCHER_ERROR_ALERT_COOLDOWN_SEC", "300")
    for scope in ("sl", "tp", "eod"):
        for _ in range(5):
            watchdog.notify_tick_error(scope, RuntimeError("disk I/O error"))
    assert len(sent) == 3
    assert {"sl", "tp", "eod"} == {
        s.split(" watcher")[0].split("`")[-1] for s in sent
    }


def test_recovery_notifies_once_with_missed_count(monkeypatch, sent):
    monkeypatch.setenv("WATCHER_ERROR_ALERT_COOLDOWN_SEC", "300")
    for _ in range(4):
        watchdog.notify_tick_error("eod", RuntimeError("disk I/O error"))
    sent.clear()

    watchdog.notify_tick_ok("eod")
    assert len(sent) == 1
    assert "已恢复" in sent[0]
    assert "3" in sent[0]          # 冷却期内被压掉的 3 次

    watchdog.notify_tick_ok("eod")  # 健康态重复调用不该再发
    assert len(sent) == 1


def test_healthy_ticks_are_silent(sent):
    """热路径：没出过错就不该有任何通知开销。"""
    for _ in range(100):
        watchdog.notify_tick_ok("sl")
    assert sent == []


def test_alerting_failure_never_escapes(monkeypatch):
    """告警链路自己炸了也不能放倒 watcher——它是保护措施，不是新的故障源。"""
    def boom(_msg):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(watchdog, "notify_bg", boom)
    watchdog.notify_tick_error("sl", RuntimeError("disk I/O error"))
    watchdog.notify_tick_ok("sl")
