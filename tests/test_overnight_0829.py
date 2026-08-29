"""8/29 夜复盘回归（US RTH 2026-08-28，AEST 8/28 23:45 → 8/29 07:00）。

当晚 0 条 ERROR，但**零开仓、零主动平仓** —— 三件事各自独立地把动作挡掉了：

1. **listener 晚起 30 分钟**（23:45:16 而不是 23:15），错过开盘后头 15 分钟。
   根因不是 launchd 没跑、也不是没设唤醒：`pmset repeat wakepoweron` 在
   23:10 **唤醒成功了**，但机器 86 秒后就空闲入睡，23:15 那次触发落在睡眠里，
   一直等到 23:45 有人碰机器才补跑。修法在 launchd plist（多个触发点，
   关键是 23:11 落在唤醒窗口内），不在本仓库代码里，故本文件不测。

2. **中继机器人前缀污染 ZH symbol 抽取**（本文件第 1 节）。

3. **AAPL 330c 四条真 trim 全被 runner-preserve 挡住**（本文件第 2 节）——
   parser 全对、strike-filter 精确命中，卡在"单张仓 pct=33 取整为 0"。
   它只剩 1 张是 8/28 那条 UNG 战报误卖造成的：那个 bug 的第二笔账。
"""
import pytest

from autotrade.parsing.close_parser import parse_close, strip_relay_prefix
from autotrade.parsing.signal_parser import parse_signal
from autotrade.policy.positions import strategy_b_decision

_HELD = {"AAPL", "ALAB", "AVGO", "IONQ", "NVDA", "TSLA", "UNG"}

# 8/28 23:45 起 KC 频道的新格式（逐字）
NEW_ZH = "核心期权交易综合BOT: :red_circle:KC交易机器人：减仓340c @ 4.30 🚀"
NEW_EN = "核心期权交易综合BOT: KC Trades Bot:trimmed AAPL 330 @ 6.85 for +$185 per contract gain"
OLD_EN = "@everyone\nKC Trades Bot:trimmed SPY @ 2.15"


# ============================================================
# 1. 中继机器人前缀
# ============================================================

def test_new_relay_prefix_no_longer_leaks_bot_as_ticker():
    """8/29 实测：symbols=['BOT', 'KC'] —— 两个都来自发言人标签，不是标的。"""
    parsed = parse_close(NEW_ZH, _HELD | {"BOT", "KC"})
    # 剥掉标签后只剩 "减仓340c @ 4.30" —— 没有 ticker，正确地什么都不做
    assert parsed is None


def test_old_format_also_stopped_leaking_kc():
    """老格式 `@everyone\\nKC Trades Bot:` 一样会漏 KC，只是没人注意。"""
    assert "KC" not in strip_relay_prefix(OLD_EN)
    assert strip_relay_prefix(OLD_EN) == "trimmed SPY @ 2.15"


@pytest.mark.parametrize("text,expected", [
    (NEW_ZH, "减仓340c @ 4.30 🚀"),
    (NEW_EN, "trimmed AAPL 330 @ 6.85 for +$185 per contract gain"),
    (OLD_EN, "trimmed SPY @ 2.15"),
    ("@everyone\n:red_circle:KC交易机器人：减仓SPY @ 2.15", "减仓SPY @ 2.15"),
])
def test_relay_prefix_shapes(text, expected):
    assert strip_relay_prefix(text) == expected


@pytest.mark.parametrize("text", [
    # 反向护栏：不含 BOT/机器人 的正文一个字都不许被吃掉
    "trimmed SPY @ 2.15",
    "enrich:\n$ALAB - Scale out.",
    "丰富：\n$UBER - BANG",
    "out half NVDA 3.05 💰 214.50 target hit",
])
def test_relay_prefix_leaves_real_content_alone(text):
    assert strip_relay_prefix(text) == text


def test_prefixed_close_still_executes():
    """剥前缀不能把真指令一起剥掉 —— 带新前缀的 AAPL trim 仍要解析出来。"""
    parsed = parse_close(NEW_EN, _HELD)
    assert parsed is not None
    assert parsed["symbols"] == ["AAPL"]


def test_relay_prefix_does_not_affect_open_path():
    """open 侧锚在 $TICKER+strike+calls 上，前缀本来就不影响它 ——
    这条钉住"只在 close 路径剥"这个决定的前提（lesson #24 要求检查另一侧）。"""
    old = "@everyone\nKC Trades Bot:AAPL 330c 10/16 starter swing @ 5.00"
    new = "核心期权交易综合BOT: KC Trades Bot:AAPL 330c 10/16 starter swing @ 5.00"
    a, b = parse_signal(old), parse_signal(new)
    assert a is not None and b is not None
    for k in ("symbol", "strike", "side", "price"):
        assert a[k] == b[k], f"前缀改变了 open 解析的 {k}"


# ============================================================
# 2. 策略B：昨晚那个被动变单张的仓位
# ============================================================

def test_strategy_b_would_have_sold_last_nights_aapl():
    """AAPL 330c 入场 5.05，KC 两次喊 trim 报 6.85 / 7.50。
    策略A（runner-preserve）四次全部死拿；策略B 两次都该全出。"""
    for quote in (6.85, 7.50):
        decision, reason = strategy_b_decision(5.05, quote, None, 25.0)
        assert decision == "SELL_ALL", f"quote={quote} 应该全出，实际 {decision}（{reason}）"


@pytest.mark.parametrize("quote,expected", [
    (None, "PRESERVE"),   # 拿不到新鲜报价 → 退回死拿（底线不变）
    (5.50, "PRESERVE"),   # 只涨 8.9% < 25% 阈值
    (6.32, "SELL_ALL"),   # 恰好 +25.1%
])
def test_strategy_b_threshold_boundaries(quote, expected):
    decision, _ = strategy_b_decision(5.05, quote, None, 25.0)
    assert decision == expected
