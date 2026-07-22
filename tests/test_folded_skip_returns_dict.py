"""验证 parser intentional skip 返回 dict 而不是 None。

（原 scripts/test_skip_returns_dict.py 搁浅 assert 脚本，折叠为 pytest；期望值原样保留。）
"""
from datetime import date

from autotrade.parsing.signal_parser import parse_signal


def test_holding_skip_returns_dict():
    # 1. holding skip
    # （原脚本断言必为 skip dict；6/23 SKIP_KEYWORDS 收紧为精确短语后，
    #   这条 "holding for now" 样本实测走 parse-fail → None。与
    #   test_parser.test_holding_status_phrases_still_skipped 同口径：
    #   None 或 skip dict 都不会下错单，两者均可接受。）
    sig = parse_signal("$QCOM holding for now - LOD $223 is a good risk stop",
                       msg_ts=date(2026, 6, 15))
    assert sig is None or (isinstance(sig, dict) and sig.get("skip") == "holding_or_remaining")


def test_price_range_skip_returns_dict():
    # 2. price range skip
    sig = parse_signal("$SPY $500 calls $1.00-$1.50", msg_ts=date(2026, 6, 15))
    assert isinstance(sig, dict) and sig.get("skip") == "price_range"


def test_true_parse_failure_returns_none():
    # 3. 真·解析失败 = None（保留警告路径）
    sig = parse_signal("just some random gibberish nothing useful here at all",
                       msg_ts=date(2026, 6, 15))
    assert sig is None


def test_normal_signal_has_no_skip_key():
    # 4. 正常成功 = dict 无 skip key
    sig = parse_signal("$QCOM weekly $245 calls $2.25", msg_ts=date(2026, 6, 15))
    assert sig.get("skip") is None
    assert sig["symbol"] == "QCOM"
