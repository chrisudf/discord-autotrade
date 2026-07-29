"""7/23-7/24 夜盘复盘回归(第二夜,语料取自当晚 listener 日志原文)。

当晚头号事故:KC "META 620c 4DTE @ 4.80 little day trade **into the close**
for fun" 被 STRONG_CLOSE_RE 的 `closed?` 按裸名词 close 误路由成 CLOSE,
一个完全可解析的开仓信号丢失;ZH 孪生 "买入META 620看涨期权,4天到期,价格4.80"
解析失败且 "价格4.80" 不在启发式价格形式里 → 连大声告警都没有。
"""
from datetime import date

from autotrade.listener.heuristics import _looks_like_open_attempt
from autotrade.parsing.signal_parser import detect_action, parse_signal

META_EN = (
    "@everyone\nKC Trades Bot:META 620c 4DTE @ 4.80 "
    "little day trade into the close for fun"
)
META_ZH = (
    "@everyone\n:red_circle:KC交易机器人：买入META 620看涨期权，"
    "4天到期，价格4.80，收盘前做点日内交易玩玩。"
)


def test_into_the_close_not_routed_close():
    """"into the close" 的 close 是名词(收盘),不是平仓动词。"""
    assert detect_action(META_EN) != "CLOSE"


def test_meta_signal_parses_after_rerouting():
    """路由修好后,当晚的 META 信号必须能完整解析(快照当前 NDTE 语义)。"""
    sig = parse_signal(META_EN, msg_ts=date(2026, 7, 23))
    assert sig is not None and not sig.get("skip")
    assert sig["symbol"] == "META"
    assert sig["strike"] == 620.0
    assert sig["side"] == "CALL"
    assert sig["price"] == 4.8
    assert "day_trade" in sig["tags"]
    assert sig["expiry_date"] == date(2026, 7, 27)  # 4DTE 现行语义快照


def test_close_noun_contexts_not_close():
    """名词/副词用法的 close 一律不算平仓动词。"""
    for text in (
        "big volume into the close",
        "let's watch price action at the close",
        "SPY is close to 630 resistance",
        "decision before the close",
    ):
        assert detect_action(text) != "CLOSE", text


def test_close_verb_usages_still_route_close():
    """真平仓动词用法必须原样保留(含 7/8/7/15 语料)。"""
    for text in (
        "closed NOW small day trade -15%",
        "Close TSLA here",
        "want to close half my AAPL",
        "I'm going to close it out",
        "all out SPY -11% not adding",
        "BANG! Trimmed AVGO @ 2.20",
    ):
        assert detect_action(text) == "CLOSE", text


def test_zh_meta_twin_triggers_loud_alert():
    """ZH 孪生解析失败时,"价格4.80" 要能凑齐三件套触发 looks-like-signal 告警,
    而不是静默丢弃(ticker META + 看涨期权 + 价格N)。"""
    assert _looks_like_open_attempt(META_ZH) is True


def test_zh_price_form_meiyuan():
    assert _looks_like_open_attempt(
        "@everyone\nKC交易机器人：买入NVDA 190看涨期权，价格2.50美元"
    ) is True


def test_zh_chatter_without_price_still_silent():
    """三件套门控不放松:没有价格形态的 ZH 闲聊照旧沉默。"""
    assert _looks_like_open_attempt(
        "@everyone\nKC交易机器人：META 看涨期权今天走势不错,大家拿住"
    ) is False
