"""10/27 复盘（US RTH 2026-10-27）。当晚 Mac 合盖用电池睡了一整夜，靠补拉的频道历史重放。

KC 当晚唯一的新开仓 `SPY 770c 10/30 small day trade here at 2.92` 解析失败：
A1 / A3 的价格段只认 `@`，KC 偶尔写 `at`。这一单 KC 自己报 +70–75%。
raw_signals 里同型的还有 10/2 的 `GLW 200c 11/20 small swing at 4.60`。
"""
from datetime import date

from autotrade.parsing.signal_parser import parse_signal

# 原文逐字
KC_SPY_770C = ("@everyone\nKC Trades Bot:SPY 770c 10/30 small day trade here at 2.92, "
               "can add a little lower as well")
KC_GLW_200C = "@everyone\nKC Trades Bot:GLW 200c 11/20 small swing at 4.60"
KC_SPCX_RUNNERS = "@everyone\nKC Trades Bot:SPCX hitting the daily 9, the 120p runners at 3.20"


def _key(sig):
    return sig["symbol"], sig["strike"], sig["side"], str(sig["expiry_date"]), sig["price"]


def test_kc_at_price_spy_770c():
    sig = parse_signal(KC_SPY_770C, msg_ts=date(2026, 10, 27))
    assert _key(sig) == ("SPY", 770.0, "CALL", "2026-10-30", 2.92)
    assert "day_trade" in sig["tags"]


def test_kc_at_price_glw_200c():
    sig = parse_signal(KC_GLW_200C, msg_ts=date(2026, 10, 2))
    assert _key(sig) == ("GLW", 200.0, "CALL", "2026-11-20", 4.6)


def test_runners_at_price_is_not_an_open():
    """减仓评论：120p 前面是小写 "the"，不是 ticker，不能因为有 "at 3.20" 就开仓。"""
    assert parse_signal(KC_SPCX_RUNNERS, msg_ts=date(2026, 10, 27)) is None


def test_at_after_stop_or_target_is_not_entry():
    """at 分支和 Pattern D 一样过 _price_qualified：止损价 / 目标价不当入场价。"""
    day = date(2026, 10, 27)
    assert parse_signal("KC Trades Bot:SPY 770c 10/30 stop at 2.50", msg_ts=day) is None
    assert parse_signal("KC Trades Bot:SPY 770c 10/30 target at 6.00", msg_ts=day) is None


def test_at_without_number_is_not_a_price():
    day = date(2026, 10, 27)
    assert parse_signal("KC Trades Bot:SPY 770c 10/30 looking great at highs", msg_ts=day) is None


def test_ndte_form_accepts_at_too():
    sig = parse_signal("KC Trades Bot:SPY 745p 8DTE at 2.66", msg_ts=date(2026, 10, 27))
    assert _key(sig)[:3] == ("SPY", 745.0, "PUT") and sig["price"] == 2.66
