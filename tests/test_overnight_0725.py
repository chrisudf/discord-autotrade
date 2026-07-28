"""7/24-7/25(周五)夜盘复盘回归(第三夜,首个全 patch 夜,语料取自日志原文)。

三个发现:
  1. AVGO 415C 到期日 EOD 强平失败:no-quote 一次就 backoff 1800s,重试排到
     窗口(16:05)之后 → 整个到期日只有一次机会。重试与 TG 节流必须分离。
  2. enrich 纯 scalp 形态("$LLY $1215 scalp $1.36"/"头皮"/"$NVDA $212.50
     scalps off the 9EMA")无方向,双语六连发全程静默——sized-entry 告警接不住。
  3. enrich "Out 25% more. Down to runners." 双语双发漏路由+漏解析
     (裸 out 不在词表;当晚无持仓无损失,持仓时就是漏跟真 trim)。
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from autotrade.listener.heuristics import _looks_like_sized_entry
from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action
from autotrade.position import eod_watcher
from autotrade.storage import positions_db

ET_TZ = ZoneInfo("America/New_York")

LLY_EN = "enrich:\n$LLY $1215 scalp $1.36\n\n@everyone $alert"
LLY_ZH = "enrich:\n$LLY $1215 头皮 $1.36\n\n@everyone $alert"
NVDA_SCALP = (
    "enrich:\n$NVDA $212.50 scalps off the 9EMA on the 5min\n\n"
    "Wait for next swing alert if this doesn't resonate with you\n\n@everyone"
)
OUT25 = (
    "enrich:\n$LLY - Out 25% more. Down to runners. That's how we do it. \n\n"
    "@everyone $alert"
)


# ============================================================
# 1. EOD no-quote:每 tick 重试,TG 30min 节流(7/24 AVGO 到期实锤)
# ============================================================

def _trading_now_et():
    now_et = datetime.now(ET_TZ).replace(hour=15, minute=51, second=0, microsecond=0)
    while now_et.weekday() >= 5:
        now_et = now_et - timedelta(days=1)
    return now_et


@pytest.mark.asyncio
async def test_eod_no_quote_retries_every_tick_but_tg_throttled():
    """no-quote 不再吃掉整个强平窗口:连续 tick 每次都重新取价,
    TG 只发一次(30min 节流)。报价恢复后第三 tick 卖出成功。"""
    now_et = _trading_now_et()
    today_et = now_et.date()
    code = "US.EODRT260724C100000"
    positions_db.open_or_add(
        option_code=code, symbol="EODRT", strike=100.0, side="CALL",
        expiry=today_et, qty=1, fill_price=1.00,
        category="0dte", apply_sl=False, eod_force_close=True,
        tags=[], channel_name="ut", msg_id="ut-eodrt",
    )
    eod_watcher._skip_until.pop(code, None)
    eod_watcher._alerted_until.pop(code, None)

    quotes = [None, None, 0.50]  # 前两 tick 无报价,第三 tick 恢复
    def fake_quote(_code):
        return quotes.pop(0)

    sell_result = {"success": True, "order_id": "UT1", "qty": 1, "price": 0.45}
    with patch("autotrade.position.eod_watcher.get_last_price", side_effect=fake_quote) as q, \
         patch("autotrade.position.eod_watcher.place_sell_order", return_value=sell_result) as sell, \
         patch("autotrade.position.eod_watcher.send_telegram", new_callable=AsyncMock) as tg, \
         patch("autotrade.position.eod_watcher._is_eod_window", return_value=True), \
         patch("autotrade.position.eod_watcher.sweep_expired_and_notify",
               new_callable=AsyncMock):
        await eod_watcher._eod_tick(now_et)                       # tick1: no quote
        await eod_watcher._eod_tick(now_et + timedelta(seconds=30))  # tick2: no quote
        await eod_watcher._eod_tick(now_et + timedelta(seconds=60))  # tick3: 报价恢复

    assert q.call_count == 3          # 每 tick 都重试(老代码 tick2/3 直接被 backoff 跳过)
    sell.assert_called_once()         # 报价恢复当刻立即强平
    # no-quote TG 只发一次(节流);之后是成交通知
    noquote_calls = [c for c in tg.await_args_list if "无报价" in str(c)]
    assert len(noquote_calls) == 1

    assert positions_db.get(code)["status"] == "CLOSED"
    eod_watcher._skip_until.pop(code, None)
    eod_watcher._alerted_until.pop(code, None)


# ============================================================
# 2. enrich scalp 形态 → sized-entry 告警(不自动下单,只大声提醒)
# ============================================================

def test_scalp_forms_trigger_sized_entry_alert():
    assert _looks_like_sized_entry(LLY_EN) == "LLY"
    assert _looks_like_sized_entry(LLY_ZH) == "LLY"
    assert _looks_like_sized_entry(NVDA_SCALP) == "NVDA"   # 只有 1 个 $数字也要接住


def test_scalp_still_not_parsed_as_order():
    """无方向不猜方向:scalp 形态仍然不产生可下单信号,只走告警。"""
    from autotrade.parsing.signal_parser import parse_signal
    for t in (LLY_EN, LLY_ZH, NVDA_SCALP):
        sig = parse_signal(t)
        assert sig is None or sig.get("skip"), t


def test_scalped_recap_and_chatter_stay_silent():
    # 过去式 scalped 的词边界天然不命中;多 ticker watchlist、无 $数字闲聊不触发
    assert _looks_like_sized_entry("scalped $LLY this morning +40%, great trade") is None
    assert _looks_like_sized_entry("$MU $NBIS $RKLB scalp setups forming") is None
    assert _looks_like_sized_entry("scalp mindset: patience") is None


# ============================================================
# 3. "Out N% more" 路由 + 解析(当晚双语双发全漏)
# ============================================================

def test_out_pct_routes_and_parses():
    assert detect_action(OUT25) == "CLOSE"
    parsed = parse_close(OUT25, {"LLY"})
    assert parsed is not None
    assert parsed["kind"] == "CLOSE"
    assert parsed["symbols"] == ["LLY"]
    assert parsed["pct"] == 25


def test_knocked_out_pct_is_commentary():
    """"IV crush knocked out 25% of the premium" 是解说不是卖出。"""
    t = "IV crush knocked out 25% of the premium on those $LLY calls"
    assert detect_action(t) != "CLOSE"
    assert parse_close(t, {"LLY"}) is None


def test_out_pct_commentary_all_verb_forms():
    """[7/28 补] 解说排除必须覆盖词形变化。

    0007 只排了 knocked/knock —— "knocking out 25% of the premium on $LLY"
    照样路由成 CLOSE 并解析出 pct=25,持着 LLY 时就是一次凭空 trim。
    Python re 的 lookbehind 定长,词形只能逐个写(见 _OUT_PCT_PATTERN)。
    """
    for t in (
        "IV crush knocking out 25% of the premium on $LLY calls",
        "theta knocks out 30% of the premium overnight on $NVDA",
        "that wick was shaking out 20% of weak hands in $SPY",
        "the flush shook out 15% of longs on $SPY",
    ):
        assert detect_action(t) != "CLOSE", t
        assert parse_close(t, {"LLY", "NVDA", "SPY"}) is None, t


def test_out_pct_real_trims_survive_the_commentary_guard():
    """反向边界:排除解说不能误伤真减仓句式(-ing 一刀切会砍掉这些)。"""
    for t, pct in (
        ("scaling out 50% of $SPY here", 50),
        ("taking out 30% on $NVDA", 30),
        ("$LLY - Out 25% more. Down to runners.", 25),
    ):
        assert detect_action(t) == "CLOSE", t
        parsed = parse_close(t, {"LLY", "NVDA", "SPY"})
        assert parsed is not None and parsed["pct"] == pct, t


def test_out_pct_pattern_is_single_sourced():
    """路由(signal_parser)与解析(close_parser)必须用同一个常量。

    两边各写一份字面量 → 迟早漂移 → "路由成 CLOSE 但 parse_close 返 None"
    的漏单裂缝(或反过来的误 trim)。
    """
    from autotrade.parsing import close_parser, signal_parser
    assert signal_parser._OUT_PCT_PATTERN is close_parser._OUT_PCT_PATTERN
    assert close_parser._OUT_PCT_PATTERN in signal_parser.WEAK_CLOSE_RE.pattern


def test_out_pct_variants():
    parsed = parse_close("Out 50% here on $NVDA", {"NVDA"})
    assert parsed is not None and parsed["pct"] == 50
    # 无持仓白名单外的裸 ticker 不受影响(依赖 $ 前缀或白名单,原语义)
    assert parse_close("Out 30% more", set()) is None
