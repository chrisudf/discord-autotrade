"""9/9-9/25 的 :RedAlert: 漏单 + 一组「更正类」告警。

1. 35 个 :RedAlert: 信号从没解析成功过（语料见 tests/corpus/redalert_bare_price.jsonl）。
   9/28 决定：与带 $ 的彩票一视同仁地跟；写了 EXPECT 0 的只记录不跟。
2. SPX 的期权代码是 US.SPXW…（get_option_chain("US..SPX") 实测）。
3. 「疑似信号」告警报了一整晚评论，真信号（SPY 769C / SNDK / enrich $270）一条没报。
4. 9/23 RKLB 编辑改了 strike、NVDA 74 秒后更正了到期日，系统都没吭声。
5. 9/23 META 747.5C：启动回补重放出来、喊单员已经止损了才被买入。
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from autotrade.listener import dedup, heuristics, open_flow
from autotrade.parsing.signal_parser import parse_signal
from autotrade.policy.pricing import build_option_code


def _wire(monkeypatch, placed, alerts):
    async def fake_notify(msg):
        alerts.append(msg)

    dedup._signal_fps.clear()
    dedup._stale_open_alerted.clear()
    heuristics._recent_exec.clear()
    heuristics._last_open.clear()
    monkeypatch.setattr(open_flow, "_safe_notify", fake_notify)
    monkeypatch.setattr(open_flow, "notify_bg", lambda msg: None)
    monkeypatch.setattr(open_flow, "check_order",
                        lambda **kw: SimpleNamespace(passed=True, reason="", detail=""))
    monkeypatch.setattr(open_flow, "place_order", lambda signal, qty=None: (
        placed.append(signal["symbol"]),
        {"success": True, "order_id": "T1", "code": "US.T", "qty": 1, "price": 0.55})[1])
    monkeypatch.setattr(open_flow, "log_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow, "record_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow.position_mgr, "on_order_filled", lambda **kw: None)
    monkeypatch.setattr(open_flow.fill_checker, "spawn", lambda coro: coro.close())


async def _open(raw, day, age_sec=2, cid=1):
    msg = SimpleNamespace(id=int(datetime.now().timestamp() * 1e6),
                          created_at=datetime.now(timezone.utc) - timedelta(seconds=age_sec))
    cfg = SimpleNamespace(name="ashley", default_qty=4, max_price=2000.0)
    await open_flow.process_open(msg, raw, cfg, cid, datetime.now(timezone.utc), day)


# ============================================================
# 1. EXPECT 0 只记录；其余裸价彩票照跟
# ============================================================

async def test_expect_zero_lotto_is_logged_not_ordered(monkeypatch):
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    await _open(":RedAlert: SPY - $760 PUTS 0DTE .50, SUPER LOTTOS, EXPECT 0 @everyone", date(2026, 9, 9))
    await _open(":RedAlert: ANET - $215 看涨期权 0DTE .29, 超级乐透, 预计 0 @everyone", date(2026, 9, 25))
    assert placed == [] and alerts == [], "EXPECT 0：不下单，也不发 TG（复盘看日志）"


async def test_bare_price_lotto_without_expect_zero_is_followed(monkeypatch):
    """契约翻转：9/23 实时漏掉的那条。"""
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    await _open(":RedAlert: SPY - 769 CALLS 0DTE .53, SUPER LOTTOS @everyone", date(2026, 9, 23))
    assert placed == ["SPY"]


def test_expect_zero_regex_does_not_eat_prices():
    assert not open_flow._EXPECT_ZERO_RE.search(":RedAlert: SPY - $760 PUTS 0DTE .50 预计 0.50 @everyone")


# ============================================================
# 2. SPX → SPXW；RedAlert 的 NDTE 往后数交易日
# ============================================================

def test_spx_option_code_uses_the_spxw_root():
    assert build_option_code("SPX", date(2026, 10, 2), 7800, "CALL") == "US.SPXW261002C7800000"
    assert build_option_code("SPY", date(2026, 10, 2), 660, "CALL") == "US.SPY261002C660000"


def test_friday_1dte_is_monday_not_friday():
    """B1 的"日历日 + 1 再往回调"会把周五 1DTE 退回周五本身；新模式往后数交易日。"""
    sig = parse_signal(":RedAlert: SPY - 669 CALLS 1DTE .50, SUPER LOTTOS @everyone",
                       msg_ts=date(2026, 9, 25))
    assert sig["expiry_date"] == date(2026, 9, 28)


# ============================================================
# 3. 疑似信号告警：认模板，不认词
# ============================================================

@pytest.mark.parametrize("text", [
    ":RedAlert: SLV - $61 ITM 0DTE SUPER LOTTOS .55 @everyone",
    "@everyone\nKC Trades Bot:can also take SNDK 2100 9/25 @ 9.30, similar setup",
    "enrich:\nPrice action deserves some exposure \n\n$270 9/25 weekly calls $2.20\n\n@everyone $alert",
    "丰富：\n$ARM - 彩票 / 剥头皮 - $310 每周看涨期权 $1.50 这些价格会迅速上涨\n@everyone $alert",
])
def test_signal_shaped_parse_failures_alert(text):
    assert heuristics._looks_like_open_attempt(text)


@pytest.mark.parametrize("text", [
    "META CALLS AT $12.00 IF YOU'RE STILL IN @everyone",
    "PANW CALLS AT ALMOST $1K PER CONTRACT @everyone",
    "MSTR CALLS ITM, CALLS AT $6.05 @everyone",
    "@everyone\nKC Trades Bot:AMD just sideways, down 4% right now with puts @ 6.05",
])
def test_position_updates_do_not_alert(text):
    assert not heuristics._looks_like_open_attempt(text)


# ============================================================
# 4. 更正类告警
# ============================================================

NVDA = {"symbol": "NVDA", "strike": 235.0, "side": "CALL", "expiry": "9/30", "price": 1.3}


@pytest.mark.parametrize("text", ["EXPIRATION 9/28 @everyone", "到期 9/28 @everyone"])
def test_expiry_correction_after_an_open_is_flagged(text):
    heuristics._last_open.clear()
    heuristics._record_recent_exec(7, NVDA)
    msg = heuristics._expiry_correction(text, 7)
    assert msg and "9/30" in msg and "9/28" in msg


def test_expiry_correction_needs_same_channel_recent_and_different():
    heuristics._last_open.clear()
    heuristics._record_recent_exec(7, NVDA)
    assert heuristics._expiry_correction("EXPIRATION 9/30 @everyone", 7) is None, "同一天不算更正"
    assert heuristics._expiry_correction("EXPIRATION 9/28 @everyone", 8) is None, "别的频道"
    heuristics._last_open[7]["ts"] -= timedelta(minutes=11)
    assert heuristics._expiry_correction("EXPIRATION 9/28 @everyone", 7) is None, "超过 10 分钟"


async def test_expiry_correction_reaches_telegram(monkeypatch):
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    heuristics._record_recent_exec(1, NVDA)
    await _open("EXPIRATION 9/28 @everyone", date(2026, 9, 22))
    assert len(alerts) == 1 and "更正了到期日" in alerts[0]


CID, UID = 1517754725615927316, 42


@dataclass
class _Msg:
    id: int
    content: str
    channel: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(id=CID, name="enrich"))
    author: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(id=UID, bot=False, name="bot"))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) - timedelta(seconds=17))


async def test_edit_that_changes_the_strike_alerts(monkeypatch):
    """9/23 逐字：$72 编辑成 $75。原来当价格修正静默放过。"""
    from autotrade.listener import router

    sent = []

    async def capture(msg):
        sent.append(msg)

    cfg = SimpleNamespace(name="enrich", is_trigger_user=lambda uid: uid == UID)
    monkeypatch.setattr(router, "registry", SimpleNamespace(is_monitored=lambda c: c == CID, get=lambda c: cfg))
    monkeypatch.setattr(router, "_safe_notify", capture)
    dedup._edit_signal_alerted.clear()
    before = "enrich:\n$RKLB 10/2 $72 看涨期权，价格为 $1.90\n\n从 1% 开始，然后逐步增加\n\n@everyone $alert"
    await router.handle_message_edit(_Msg(9, before), _Msg(9, before.replace("$72", "$75")))
    assert len(sent) == 1 and "编辑改了合约" in sent[0]
    assert "72C" in sent[0] and "75C" in sent[0]


# ============================================================
# 5. 启动回补重放出来的开仓只告警
# ============================================================

META_7475 = ":RedAlert: META - $747.5 CALLS 0DTE $5.45, STOP LOSS AT $4.80, RISK LEVEL HIGH @everyone"


async def test_startup_replay_open_alerts_instead_of_ordering(monkeypatch):
    """当晚：00:34:21 发、00:38:59 回补收到（278 秒，没超 300 秒上限）、00:39:01 下单。"""
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    open_flow.set_startup_replay(True)
    try:
        await _open(META_7475, date(2026, 9, 23), age_sec=278)
    finally:
        open_flow.set_startup_replay(False)
    assert placed == [] and any("错过的开仓信号" in a for a in alerts)


async def test_live_message_during_startup_replay_still_orders(monkeypatch):
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    open_flow.set_startup_replay(True)
    try:
        await _open(META_7475, date(2026, 9, 23), age_sec=2)
    finally:
        open_flow.set_startup_replay(False)
    assert placed == ["META"]
