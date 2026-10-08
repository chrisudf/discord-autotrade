"""10/6 夜（review_2026-10-07.md §5 ②③）。

1. enrich「这是我明天持有的内容：\\n\\n$SMCI …\\n\\n目前已锁定我所有的 $NBIS $RKLB」：中文版把留着的 SMCI 也减了，
   平掉的 RKLB 按默认 33% 被 last-spare 留下。持有清单跨空行、「锁定我所有的」= 全平。
2. 下单前拿合约报价比数量级：`ANET … $90`（.90 打错）、9/30 RKLB 买错到期日（报价 0.20 / 喊价 1.00）。
3. 风控拦截的信号不再先发「🟢 新信号触发」，合约写进拦截通知。
"""
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autotrade.listener import close_flow, dedup, heuristics, open_flow
from autotrade.parsing.close_parser import parse_close
from autotrade.storage import positions_db

ZH = ("enrich:\n这是我明天持有的内容：\n\n$SMCI 10/6 $45 看涨期权\n\n"
      "目前已锁定我所有的 $NBIS $RKLB。我们会重新审视这些。\n\n重点关注：$BE\n\n@everyone $alert")


# ============================================================
# 1. 持有清单 + 锁定所有
# ============================================================

def test_holding_list_keeps_the_listed_ticker_and_lock_all_is_full():
    """契约翻转：当晚解析成 ['SMCI','NBIS','RKLB'] 33%。"""
    r = parse_close(ZH, {"SMCI", "NBIS", "RKLB"})
    assert r["symbols"] == ["NBIS", "RKLB"] and r["pct"] == 100


def test_lock_wording_without_all_my_is_unchanged():
    """反向：「全部锁定」仍按 7/17 语料契约 33%；「锁定这些收益 - 对所有开放头寸」不是全平。"""
    assert parse_close("丰富：\n$XOM 全部锁定\n\n@everyone", {"XOM"})["pct"] == 33
    r = parse_close("enrich:\n锁定这些收益 - 对所有开放头寸进行跑步。\n\n$XOM 也在 90% 的位置\n\n@everyone", {"XOM"})
    assert r is None or r["pct"] != 100


def _open_pos(symbol: str, strike: float) -> str:
    code = f"US.{symbol}{datetime.now().strftime('%H%M%S%f')}C{int(strike * 1000)}"
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side="CALL", expiry=date(2026, 10, 16),
        qty=2, fill_price=2.05, category="weekly", apply_sl=True, eod_force_close=False,
        tags=[], channel_name="ut", msg_id="m")
    return code


@pytest.mark.asyncio
async def test_lock_all_sells_rklb_fully_and_leaves_smci():
    """当晚：SMCI 卖了 2 张（他要拿着的），RKLB 被 last-spare 留了 2 张（他已经全平）。"""
    smci, rklb = _open_pos("QSMC", 45.0), _open_pos("QRKL", 80.0)
    text = ZH.replace("SMCI", "QSMC").replace("RKLB", "QRKL")
    os.environ["DRY_RUN"] = "true"
    dedup._close_fps.clear()
    sold = {}

    def fake_sell(option_code, qty, *a, **kw):
        sold[option_code] = qty
        return {"success": True, "order_id": "X", "code": option_code, "qty": qty, "price": 1.5}

    async def noop(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", return_value=1.54):
        await close_flow.handle_close_signal(text, msg_id=int(datetime.now().timestamp() * 1e6))
    assert sold == {rklb: 2}
    positions_db.record_close(smci, 2, 0.01, "manual", note="ut")


# ============================================================
# 2. 下单前报价比对
# ============================================================

def _sig(price, symbol="ANET", strike=220.0):
    return {"symbol": symbol, "strike": strike, "side": "CALL", "expiry_date": date(2026, 10, 9), "price": price}


def test_quote_guard_flags_order_of_magnitude_mismatch():
    with patch.object(open_flow, "get_buy_ref_price", return_value=0.87):
        assert "差" in open_flow._quote_mismatch(_sig(90.0))          # 10/6 ANET .90 打成 $90
    with patch.object(open_flow, "get_buy_ref_price", return_value=0.20):
        assert open_flow._quote_mismatch(_sig(1.00, "RKLB", 80.0))     # 9/30 RKLB 解析成 10/2 那张


def test_quote_guard_lets_normal_and_unknown_quotes_through():
    """反向：正常滑点放行；拿不到报价、取报价出错都放行（行情故障不能挡住下单）。"""
    with patch.object(open_flow, "get_buy_ref_price", return_value=2.47):
        assert open_flow._quote_mismatch(_sig(2.45)) is None
    with patch.object(open_flow, "get_buy_ref_price", return_value=None):
        assert open_flow._quote_mismatch(_sig(90.0)) is None
    with patch.object(open_flow, "get_buy_ref_price", side_effect=RuntimeError("boom")):
        assert open_flow._quote_mismatch(_sig(90.0)) is None


def _wire(monkeypatch, placed, alerts, bg, risk_ok=True):
    async def fake_notify(msg):
        alerts.append(str(msg).replace("\\", ""))   # TG 走 Markdown 转义（0.87 → 0\.87），去掉再比

    dedup._signal_fps.clear()
    heuristics._recent_exec.clear()
    heuristics._last_open.clear()
    monkeypatch.setattr(open_flow, "_safe_notify", fake_notify)
    monkeypatch.setattr(open_flow, "notify_bg", lambda msg: bg.append(str(msg)))
    monkeypatch.setattr(open_flow, "check_order", lambda **kw: SimpleNamespace(
        passed=risk_ok, reason="" if risk_ok else "当日预算不足", detail="" if risk_ok else "已花 $4492"))
    monkeypatch.setattr(open_flow, "place_order", lambda signal, qty=None: (
        placed.append(signal["symbol"]),
        {"success": True, "order_id": "T1", "code": "US.T", "qty": 4, "price": 0.95})[1])
    monkeypatch.setattr(open_flow, "log_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow, "record_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow.position_mgr, "on_order_filled", lambda **kw: None)
    monkeypatch.setattr(open_flow.fill_checker, "spawn", lambda coro: coro.close())


async def _open(raw, day=date(2026, 10, 6)):
    msg = SimpleNamespace(id=int(datetime.now().timestamp() * 1e6),
                          created_at=datetime.now(timezone.utc) - timedelta(seconds=2))
    cfg = SimpleNamespace(name="ashley", default_qty=4, max_price=2000.0)
    await open_flow.process_open(msg, raw, cfg, 1, datetime.now(timezone.utc), day)


@pytest.mark.asyncio
async def test_typo_price_is_not_ordered_and_says_why(monkeypatch):
    """契约翻转：当晚原文，报价 0.87 对喊价 90。当晚是被当日预算碰巧挡住的。"""
    placed, alerts, bg = [], [], []
    _wire(monkeypatch, placed, alerts, bg)
    monkeypatch.setattr(open_flow, "get_buy_ref_price", lambda code: 0.87)
    await _open(":RedAlert: ANET - $220 CALLS EXPIRATION THIS WEEK $90, CHEAP LOTTO PLAY @everyone")
    assert placed == []
    assert any("对不上" in a and "0.87" in a for a in alerts)


@pytest.mark.asyncio
async def test_matching_quote_still_orders(monkeypatch):
    placed, alerts, bg = [], [], []
    _wire(monkeypatch, placed, alerts, bg)
    monkeypatch.setattr(open_flow, "get_buy_ref_price", lambda code: 0.87)
    await _open(":RedAlert: ANET - $220 CALLS EXPIRATION THIS WEEK .90, CHEAP LOTTO PLAY @everyone")
    assert placed == ["ANET"]


# ============================================================
# 3. 风控拦截后不报「新信号触发」
# ============================================================

@pytest.mark.asyncio
async def test_risk_blocked_signal_sends_one_alert_with_the_contract(monkeypatch):
    """契约翻转：当晚「风控拦截」之后又来一条「🟢 新信号触发」，像是下单了。"""
    placed, alerts, bg = [], [], []
    _wire(monkeypatch, placed, alerts, bg, risk_ok=False)
    await _open(":RedAlert: ORCL - $147 CALLS EXPIRATION THIS WEEK $2.47, STOP LOSS AT $2.00 @everyone")
    assert placed == [] and bg == []
    assert len(alerts) == 1 and "ORCL" in alerts[0] and "147" in alerts[0]


@pytest.mark.asyncio
async def test_passed_signal_still_gets_the_trigger_alert(monkeypatch):
    placed, alerts, bg = [], [], []
    _wire(monkeypatch, placed, alerts, bg)
    await _open(":RedAlert: ORCL - $147 CALLS EXPIRATION THIS WEEK $2.47, STOP LOSS AT $2.00 @everyone")
    assert placed == ["ORCL"] and len(bg) == 1
