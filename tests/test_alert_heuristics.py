"""listener 报警启发式测试（7/13 复盘新增的两个）

1. _looks_like_sized_entry：enrich 风格"带仓位比例、无 C/P 方向"的入场，
   parser 不能下单（不猜方向）但必须 TG 提醒。
2. _twin_of_recent_exec：EN 信号执行成功后 ~1s 的 ZH 翻译孪生 parse-fail
   不该触发 "Parse failed (looks like signal)" 报警。
"""
from datetime import datetime, timedelta, timezone

import pytest

from autotrade.listener import dedup, heuristics
from autotrade.policy import guards


# ============ sized entry（enrich 无方向入场） ============

def test_sized_entry_hits_enrich_ibm_en_and_zh():
    # 7/13 00:22-00:24 实际漏掉的 4 条（中英×2）
    en = "enrich:\n$IBM weekly $310 $1.33\n\n2% position \n\n@everyone $alert"
    zh = "enrich:\n$IBM 每周 $310 $1.33\n\n2% 头寸 \n\n@everyone $alert"
    assert heuristics._looks_like_sized_entry(en) == "IBM"
    assert heuristics._looks_like_sized_entry(zh) == "IBM"


def test_sized_entry_ignores_levels_message():
    # enrich 每日 levels：一个 ticker + 一堆 $数字，但没有 "N% position"
    text = (
        "enrich:\n$SPY levels for the day 7/13/2026:\n\n"
        "Blue zone= $751.53, $753.85\n"
        "Green targets = $754.88, $756.24, $757.51\n"
        "Red targets = $750.37, $749.13, $746.99\n\n@everyone $alert"
    )
    assert heuristics._looks_like_sized_entry(text) is None


def test_sized_entry_ignores_holding_update():
    # 有 "2% position" 但没有 strike/price（$数字 < 2）——纯持仓状态
    text = "$IBM - Holding my 2% position - rest of the portfolio is cash today."
    assert heuristics._looks_like_sized_entry(text) is None


def test_sized_entry_ignores_multi_ticker_watchlist():
    text = "holding into tomorrow: $CIFR 20% position $1.50, $ASTS 20% position $2.50"
    assert heuristics._looks_like_sized_entry(text) is None


def test_sized_entry_ignores_plain_commentary():
    text = "$IBM - Hourly wedge still looks A+ and intact. I'm in for now."
    assert heuristics._looks_like_sized_entry(text) is None


# ============ ZH/EN 孪生 parse-fail 报警抑制 ============

@pytest.fixture(autouse=True)
def _clean_recent_exec():
    heuristics._recent_exec.clear()
    yield
    heuristics._recent_exec.clear()


def _mu_signal():
    """_record_recent_exec 需要的最小开仓信号快照。"""
    return {"symbol": "MU", "strike": 1050.0, "side": "CALL", "price": 2.60}


def test_twin_suppressed_same_channel_same_symbol():
    heuristics._record_recent_exec(123, _mu_signal())
    zh = "@everyone\nKC Trades Bot：MU 1050c 7月15日 @ 2.60 日内交易彩票"
    assert heuristics._twin_of_recent_exec(zh, 123) == "MU"


def test_twin_not_suppressed_other_channel():
    heuristics._record_recent_exec(123, _mu_signal())
    zh = "KC Trades Bot：MU 1050c 7月15日 @ 2.60"
    assert heuristics._twin_of_recent_exec(zh, 456) is None


def test_twin_not_suppressed_different_symbol():
    # 60s 内的**另一个**标的 parse-fail 是真漏检，必须照常报警
    heuristics._record_recent_exec(123, _mu_signal())
    assert heuristics._twin_of_recent_exec("NFLX 80c 7/17 @ 1.38 day trade", 123) is None


def test_twin_suppression_expires_after_window():
    heuristics._recent_exec[(123, "MU")] = {
        "ts": datetime.now(timezone.utc) - heuristics._TWIN_SUPPRESS_WINDOW - timedelta(seconds=1),
        "strike": 1050.0, "side": "CALL", "price": 2.60,
    }
    assert heuristics._twin_of_recent_exec("MU 1050c @ 2.60", 123) is None


def test_twin_matches_ticker_attached_to_hanzi():
    # ZH 文本汉字紧贴 ticker（"减仓MU"），\b 不触发，lookaround 要能命中
    heuristics._record_recent_exec(123, _mu_signal())
    assert heuristics._twin_of_recent_exec("KC交易机器人：减仓MU 1050c @ 2.60", 123) == "MU"


def test_record_recent_exec_prunes_stale_entries():
    heuristics._recent_exec[(123, "OLD")] = {
        "ts": datetime.now(timezone.utc) - heuristics._TWIN_SUPPRESS_WINDOW - timedelta(seconds=5),
        "strike": 1.0, "side": "CALL", "price": 1.0,
    }
    heuristics._record_recent_exec(123, _mu_signal())
    assert (123, "OLD") not in heuristics._recent_exec
    assert (123, "MU") in heuristics._recent_exec


# ============ 原文级消息去重（7/14 源频道每条消息双发） ============

@pytest.fixture(autouse=True)
def _clean_recent_raw():
    dedup._recent_raw.clear()
    yield
    dedup._recent_raw.clear()


def test_raw_dedup_same_channel_same_text():
    raw = "@everyone\nKC Trades Bot:PLTR 160c 8/21 starter swing @ 2.50"
    assert dedup._is_duplicate_raw(123, raw) is False  # 首条放行
    assert dedup._is_duplicate_raw(123, raw) is True   # 双发第二条挡住


def test_raw_dedup_different_channel_not_blocked():
    raw = "same text"
    assert dedup._is_duplicate_raw(123, raw) is False
    assert dedup._is_duplicate_raw(456, raw) is False


def test_raw_dedup_different_text_not_blocked():
    assert dedup._is_duplicate_raw(123, "trimmed PLTR @ 3.40") is False
    assert dedup._is_duplicate_raw(123, "trimmed PLTR @ 3.65") is False


def test_raw_dedup_expires_after_window():
    raw = "repeat me"
    dedup._recent_raw[(123, raw)] = (
        datetime.now(timezone.utc) - dedup._RAW_DEDUP_WINDOW - timedelta(seconds=1)
    )
    # 窗口外的旧记录不算重复（KC 隔几分钟重发同文本是真实场景）
    assert dedup._is_duplicate_raw(123, raw) is False


# ============ ZH 方向词进 looks-like-signal 启发式 ============

def test_open_attempt_recognizes_zh_side_word():
    """parser 因其他原因失败的 ZH 方向信号应报 "looks like signal"，
    而不是掉进 sized-entry 的"无 C/P 方向"（7/14 HOOD 误报）。"""
    assert heuristics._looks_like_open_attempt("$HOOD - 7/24 $125 看涨期权 $1.50") is True
    # 且不再被 sized-entry 分支捕获的前提成立：有方向词的文本 side RE 必命中
    assert heuristics._OPEN_SIDE_RE.search("买入看跌期权对冲") is not None


# ============ scalp/NDTE 无方向入场提醒（7/15 MSFT 0DTE 漏检） ============

def test_sized_entry_hits_scalp_dte_format():
    # 7/15 实测原文（中英双发全静默漏掉，后续 +200%）
    en = "enrich:\nScalp - $MSFT 0DTE $397.50 $.90\n\n@everyone $alert"
    zh = "enrich:\n头皮 - $MSFT 0DTE $397.50 $.90\n\n@everyone $alert"
    assert heuristics._looks_like_sized_entry(en) == "MSFT"
    assert heuristics._looks_like_sized_entry(zh) == "MSFT"


def test_sized_entry_dte_needs_two_dollar_numbers():
    # [7/25 翻转] 原判定 "$META scalp $685s 无 DTE 无价格,不值得提醒"——
    # 7/25 实锤推翻:enrich "$NVDA $212.50 scalps off the 9EMA" 同为
    # scalp+单 ticker+1 个 $数字,双语静默漏过一整晚。scalp 形态降为 ≥1 个
    # $数字即提醒(一条节流 TG 的代价换不漏);无 scalp 词的 NDTE 形态仍要 ≥2。
    assert heuristics._looks_like_sized_entry("enrich:\n$META scalp $685s") == "META"
    # NDTE 形态(无 scalp 词)维持 ≥2 门槛:只有一个 $数字不提醒
    assert heuristics._looks_like_sized_entry("enrich:\n$META 0DTE $685s") is None


# ============ 翻译孪生 close 防护（7/15 SPY "smaller size"→"小规模减仓"） ============

def _spy_open():
    return {"symbol": "SPY", "strike": 760.0, "side": "CALL", "price": 3.0}


def test_close_twin_blocked_by_same_price():
    heuristics._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": 3.0, "hint_strike": None, "hint_side": None}
    assert heuristics._close_is_open_twin(123, "SPY", parsed) is not None


def test_close_twin_blocked_by_same_contract_hint():
    heuristics._record_recent_exec(123, _spy_open())
    # 7/15 实测 parse 结果：strike=760.0 side=CALL price=3.0
    parsed = {"signal_price": None, "hint_strike": 760.0, "hint_side": "CALL"}
    assert heuristics._close_is_open_twin(123, "SPY", parsed) is not None


def test_close_twin_not_blocked_different_price():
    # 真砍仓：价格已变（-11% 后 KC 喊 2.65 之类），必须放行
    heuristics._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": 2.65, "hint_strike": None, "hint_side": None}
    assert heuristics._close_is_open_twin(123, "SPY", parsed) is None


def test_close_twin_not_blocked_no_price_no_hint():
    # "全部平仓 SPY -11%" 无价无 strike —— 不能仅凭时间窗判定孪生
    heuristics._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": None, "hint_strike": None, "hint_side": None}
    assert heuristics._close_is_open_twin(123, "SPY", parsed) is None


def test_close_twin_not_blocked_outside_window():
    heuristics._recent_exec[(123, "SPY")] = {
        "ts": datetime.now(timezone.utc) - heuristics._TWIN_SUPPRESS_WINDOW - timedelta(seconds=1),
        "strike": 760.0, "side": "CALL", "price": 3.0,
    }
    parsed = {"signal_price": 3.0, "hint_strike": None, "hint_side": None}
    assert heuristics._close_is_open_twin(123, "SPY", parsed) is None


def test_close_twin_not_blocked_other_channel_or_none_cid():
    heuristics._record_recent_exec(123, _spy_open())
    parsed = {"signal_price": 3.0, "hint_strike": None, "hint_side": None}
    assert heuristics._close_is_open_twin(456, "SPY", parsed) is None
    assert heuristics._close_is_open_twin(None, "SPY", parsed) is None  # 旧调用方/测试


# ============ 短线标签 × 长 DTE 防护（7/16 June-20 笔误 → 2027 合约） ============

from datetime import date


def test_suspicious_long_dte_blocks_typo():
    sig = {"symbol": "SPY", "strike": 755.0, "side": "CALL",
           "expiry_date": date(2027, 6, 17), "tags": ["day_trade"]}
    assert guards._suspicious_long_dte(sig, date(2026, 7, 16)) is not None


def test_suspicious_long_dte_allows_leap_swing():
    # 真 LEAPS："SOFI 20c Jan 15 2027 starter leap swing" tags=['swing']
    sig = {"symbol": "SOFI", "strike": 20.0, "side": "CALL",
           "expiry_date": date(2027, 1, 15), "tags": ["swing"]}
    assert guards._suspicious_long_dte(sig, date(2026, 7, 17)) is None


def test_suspicious_long_dte_allows_short_dte_lotto():
    sig = {"symbol": "AMZN", "strike": 250.0, "side": "CALL",
           "expiry_date": date(2026, 7, 17), "tags": ["lotto", "day_trade"]}
    assert guards._suspicious_long_dte(sig, date(2026, 7, 14)) is None


def test_suspicious_long_dte_no_tags_not_blocked():
    sig = {"symbol": "UPS", "strike": 130.0, "side": "CALL",
           "expiry_date": date(2027, 1, 15), "tags": []}
    assert guards._suspicious_long_dte(sig, date(2026, 7, 7)) is None
