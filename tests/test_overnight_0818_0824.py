"""8/18～8/24 七晚复盘回归（上一份复盘停在 8/17，这一周是一次性补的）。

七晚跑的都是同一份构建 `54b9453 + 工作区有未提交改动`，所以下面几条缺陷
是在同一份代码上反复复发的。本文件按"一条缺陷一组用例 + 一条反向护栏"组织。

1. **`out the rest` / `平掉剩余` 全平指令双语双漏**（8/18 AMZN、8/19 SPY，连吃两晚）

     EN  "out the rest of AMZN to secure small green trade ✅ …"  → Parse failed
     ZH  "平掉剩余亚马逊仓位，锁定小幅盈利 ✅"                      → CLOSE **33%**

   EN 侧：`_OUT_BARE_SYM_PATTERN` 认得 "out <TICKER>"，但 out 后面紧跟的是
   `the`，而 `THE` 就在 `_OUT_BARE_SYM_STOPWORDS` 里（那张表挡的是
   "out of the money" 这类行情解说）→ 不匹配，也没有别的 ACTION_VERBS 命中。
   ZH 侧：`平掉` 既不在 ZH_ACTION_VERBS 也不在 ZH_FULL_CLOSE_VERBS ——
   8/18 那条靠句尾的"锁定"侥幸进了 CLOSE 路径但 pct 掉回 33，被 runner-preserve
   吃掉；8/19 那条连 detect_action 都没路由成 CLOSE。
   两晚的账：AMZN 1 张 $33-50，SPY 2 张 $106-163。

2. **修 1 之后踩响的雷：裸 "everything" 当 bulk marker**。AMZN 那条原文句尾是
   "…Price has on just about everything outside memory stocks has been slow and
   boring." —— 一旦它终于路由成 CLOSE，`_has_bulk_marker` 就为真 →
   **BULK_TRIM pct=100 = 把全部持仓清光**。原来解析失败所以没炸。
   现在 `everything` 必须是平仓动词的宾语才算 bulk。

3. **`runners only` / `仅持仓 @ 价格`**（8/20 一晚 3 次全漏）。KC 的固定离场句式，
   语义是"我已经减到只剩 runner 了"，不是"我还拿着"。ZH 机翻成"仅持仓X @ 3.28"
   更糟 —— `持仓` 在 `signal_parser.SKIP_KEYWORDS` 里，OPEN 路径直接判 holding 跳过。
   两侧都要求跟着 @价格：带价格的是离场播报，不带的是持仓陈述。

4. **`took another off at 1.98`**（8/18 AMZN 同晚另一条）。裸 "took off" 是行情
   黑话（"the stock took off"），所以必须跟 another 或百分比。

5. **ZH 时间状语从句被当成当前动作 → 真误平**（8/20 PLTR）。
     ZH "在我大部分减仓后，我会交易 $PLTR $TSLA 和一些 $LLY 的涨幅股。" → CLOSE 33%
     EN "I'll be swinging $PLTR $TSLA & a few $LLY runners after I scale out most." → no signal
   真卖了 1 张 PLTR 180C @0.90（入场 1.09）。

6. **`$MRVL $252.50 SCALP***** calls $1.69 weekly` 因五个星号整条消失**（8/21）。
   `p_weekly` 的填充段只收字母数字；实测一个星号就够。当晚 KC 报 two baggers。
"""
from datetime import date as _date, timedelta as _timedelta

import pytest

from autotrade.listener.heuristics import _looks_like_close_attempt
from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action, parse_signal
from autotrade.policy.positions import categorize

OPEN = {"AMZN", "SPY", "MSFT", "UBER", "PLTR", "TSLA", "LLY", "MRVL"}

# ---- 逐字原文（Discord 的 @everyone 前缀保留，解析器要能穿过它）----
EN_OUT_REST_AMZN = (
    "@everyone\nKC Trades Bot:out the rest of AMZN to secure small green trade ✅ "
    "Price has on just about everything outside memory stocks has been slow and boring."
)
ZH_OUT_REST_AMZN = "@everyone\nKC Trades Bot：平掉剩余亚马逊仓位，锁定小幅盈利 ✅ 除记忆体股外，几乎所有品种价格走势缓慢且乏味。"
EN_OUT_REST_SPY = "@everyone\nKC Trades Bot:out the rest of SPY -9.5% here, I am bored in this chop box 😂"
ZH_OUT_REST_SPY = "@everyone\nKC交易机器人：平掉剩余SPY仓位，这里-9.5%，我在这个震荡区间里无聊死了😂"
EN_TOOK_ANOTHER = "@everyone\nKC Trades Bot:took another off at 1.98 AMZN"
EN_RUNNERS_SPY = "@everyone\nKC Trades Bot:runners only SPY @ 3.28 🚀💰"
EN_RUNNERS_MSFT = "@everyone\nKC Trades Bot:runners only MSFT @ 3.40 😎💰🚀"
ZH_RUNNERS_SPY = "@everyone\nKC交易机器人：仅持仓SPY @ 3.28 🚀💰"
ZH_AFTER_CLAUSE = (
    "enrich:\n这是我现在的情况。在我大部分减仓后，我会交易 $PLTR $TSLA 和一些 $LLY 的涨幅股。"
    "\n\n未来会更好，继续努力\n\n@everyone"
)
EN_AFTER_CLAUSE = (
    "enrich:\nHere's what I have right now. I'll be swinging $PLTR $TSLA & a few $LLY "
    "runners after I scale out most.\n\nBetter days ahead, keep pushing\n\n@everyone"
)
MRVL_SIGNAL = (
    "enrich:\n$MRVL $252.50 SCALP***** calls $1.69 weekly\n\n"
    "These are super volatile - small position \n\n@everyone"
)


# ============================================================
# 1. out the rest / 平掉剩余 —— 必须是 100%，不是 33%
# ============================================================
@pytest.mark.parametrize("text,symbol", [
    (EN_OUT_REST_AMZN, "AMZN"),
    (ZH_OUT_REST_AMZN, "AMZN"),
    (EN_OUT_REST_SPY, "SPY"),
    (ZH_OUT_REST_SPY, "SPY"),
])
def test_out_the_rest_is_full_close(text, symbol):
    """pct 必须是 100 —— 落到 33 就会被 runner-preserve 吃掉（8/18 AMZN 实测）。"""
    assert detect_action(text) == "CLOSE"
    parsed = parse_close(text, OPEN)
    assert parsed is not None
    assert parsed["kind"] == "CLOSE", "不能退化成 BULK_TRIM（见用例 2）"
    assert parsed["symbols"] == [symbol]
    assert parsed["pct"] == 100


def test_out_the_rest_does_not_become_bulk_trim():
    """AMZN 那条句尾闲聊里有 everything —— 判成 BULK 就是把全部持仓清光。"""
    parsed = parse_close(EN_OUT_REST_AMZN, OPEN)
    assert parsed["kind"] == "CLOSE"
    assert parsed["symbols"] == ["AMZN"]


# ============================================================
# 2. bulk marker 收紧后，真 bulk 仍然要认
# ============================================================
@pytest.mark.parametrize("text,expect_bulk", [
    ("Closing all positions outside of the $IBM $310 lotto", True),
    ("Selling everything here", True),
    ("trimming everything", True),
    # 闲聊里的 everything 不是 bulk（8/18 AMZN 句尾就是这个形状）
    ("Price action on just about everything has been boring", False),
])
def test_bulk_marker_requires_close_verb_object(text, expect_bulk):
    parsed = parse_close(text, OPEN | {"IBM"})
    is_bulk = bool(parsed) and parsed["kind"] == "BULK_TRIM"
    assert is_bulk is expect_bulk


# ============================================================
# 3. runners only / 仅持仓 @ 价格
# ============================================================
@pytest.mark.parametrize("text,symbol", [
    (EN_RUNNERS_SPY, "SPY"),
    (EN_RUNNERS_MSFT, "MSFT"),
    (ZH_RUNNERS_SPY, "SPY"),
])
def test_runners_only_with_price_is_a_trim(text, symbol):
    assert detect_action(text) == "CLOSE"
    parsed = parse_close(text, OPEN)
    assert parsed is not None
    assert parsed["symbols"] == [symbol]
    assert parsed["pct"] == 33


@pytest.mark.parametrize("text", [
    # 无 @价格 —— 是持仓陈述，不是离场播报（8/20 01:56 实测，当晚跳过是对的）
    "@everyone\nKC Trades Bot:MSFT calls +80%, 1 runner left for 490 in the money 💰",
    "@everyone\nKC交易机器人：微软 calls +80%，490行权价剩余1个持仓 💰 祝愉快！",
    "Holding runners on $MRVL.",
])
def test_runners_without_price_is_not_a_close(text):
    assert parse_close(text, OPEN) is None


# ============================================================
# 4. took another off —— 裸 "took off" 必须仍然是行情黑话
# ============================================================
def test_took_another_off_is_a_trim():
    assert detect_action(EN_TOOK_ANOTHER) == "CLOSE"
    parsed = parse_close(EN_TOOK_ANOTHER, OPEN)
    assert parsed["symbols"] == ["AMZN"]
    assert parsed["pct"] == 33


def test_took_off_with_pct_is_a_trim():
    """8/21 UBER 实测 "$UBER - took off 10% more." """
    parsed = parse_close("$UBER - took off 10% more.", OPEN)
    assert parsed is not None
    assert parsed["symbols"] == ["UBER"]


@pytest.mark.parametrize("text", [
    "The stock took off after earnings",
    "$TSLA really took off today",
])
def test_bare_took_off_is_not_a_close(text):
    assert parse_close(text, OPEN) is None


# ============================================================
# 5. ZH 时间状语从句 —— 8/20 PLTR 误平
# ============================================================
def test_zh_after_clause_is_not_an_instruction():
    """"在我大部分减仓后…" 是时间参照；EN 孪生本来就 no signal，两侧必须一致。"""
    assert parse_close(ZH_AFTER_CLAUSE, OPEN) is None
    assert parse_close(EN_AFTER_CLAUSE, OPEN) is None


@pytest.mark.parametrize("text,expect_close", [
    # 从句动词是"加仓"不在表里，真正的减仓在从句之外 → 照常执行
    ("@everyone\nKC交易机器人：在2.20加仓SPY后，于2.60进行小幅安全减仓以降低风险。", True),
    # 不带"在…后"的祈使句不受影响
    ("@everyone\nKC交易机器人：减仓AMZN，止损移到保本", True),
])
def test_zh_after_clause_mask_does_not_eat_real_trims(text, expect_close):
    parsed = parse_close(text, OPEN)
    assert (parsed is not None) is expect_close


# ============================================================
# 6. MRVL —— 强调星号不能吃掉整条信号
# ============================================================
def test_mrvl_scalp_asterisks_parse():
    sig = parse_signal(MRVL_SIGNAL)
    assert sig is not None, "五个星号让整条信号消失（8/21 实测，KC 当天报 two baggers）"
    assert sig["symbol"] == "MRVL"
    assert sig["strike"] == 252.5
    assert sig["price"] == 1.69
    assert sig["side"] == "CALL"
    assert "scalp" in sig["tags"]


@pytest.mark.parametrize("filler", ["SCALP*", "SCALP**", "SCALP*****", "SCALP", "weekly"])
def test_strike_side_gap_tolerates_emphasis(filler):
    """实测一个星号就足以让 p_weekly 落空。"""
    sig = parse_signal(f"$MRVL $252.50 {filler} calls $1.69 weekly")
    assert sig is not None and sig["strike"] == 252.5


@pytest.mark.parametrize("text", [
    # 行情播报绝不能因为放宽填充段而变成开仓信号
    "enrich:\n$SPY levels for the day 8/17/2026:\n\nBlue zone= $776.01, $778.20\n"
    "Green targets = $779.46, $780.72, $781.72\n\n@everyone $alert",
    # 裸星号不算填充词
    "$MRVL $252.50 *** *** *** calls $1.69 weekly",
])
def test_asterisk_fix_does_not_widen_recall(text):
    sig = parse_signal(text)
    assert sig is None or "skip" in sig


# ============================================================
# 7. weekly 的 DTE 上界 —— 本周最贵的一条（$976）
# ============================================================
class TestWeeklyDteBoundary:
    """ASTS 80C / AMD 520C 都是 8/13 开、8/21 到期，DTE=8 → 旧规则归 swing、
    apply_sl=False，八天里没有任何机制碰过它们，一路走到归零。
    """

    OPEN = _date(2026, 8, 13)

    @pytest.mark.parametrize("dte,expect_sl", [
        (1, True),
        (7, True),
        (8, True),    # ← 8/22 的 $976 就死在这一天上
        (10, True),
        (11, False),  # 真波段，仍然按 swing 处理
        (35, False),
    ])
    def test_near_expiry_keeps_stop_loss(self, dte, expect_sl):
        category, apply_sl, _ = categorize(
            self.OPEN + _timedelta(days=dte), self.OPEN, [],
        )
        assert apply_sl is expect_sl
        assert category == ("weekly" if expect_sl else "swing")

    def test_asts_and_amd_would_have_had_a_stop(self):
        """8/13 开、8/21 到期的两个真实仓位。"""
        category, apply_sl, _ = categorize(_date(2026, 8, 21), self.OPEN, [])
        assert (category, apply_sl) == ("weekly", True)

    @pytest.mark.parametrize("tags,expected", [
        (["lotto"], ("lotto", False)),      # 彩票放飞不受影响
        (["day_trade"], ("weekly", True)),
    ])
    def test_tag_semantics_unchanged(self, tags, expected):
        category, apply_sl, _ = categorize(_date(2026, 8, 21), self.OPEN, tags)
        assert (category, apply_sl) == expected

    def test_zero_dte_unchanged(self):
        assert categorize(self.OPEN, self.OPEN, []) == ("0dte", False, True)


# ============================================================
# 8. 无 ticker 的 CLOSE 尝试 —— 只有一个 day_trade 活仓时也要提醒
# ============================================================
@pytest.mark.parametrize("text", [
    "@everyone\nKC Trades Bot:small safety trim @ 2.60 to de-risk after the 2.20 add",
    "@everyone\nKC Trades Bot:small trim @ 2.60",
    "@everyone\nKC Trades Bot:BANG! Out half 2.80 💰",
    "@everyone\nKC Trades Bot:BANG! Out majority @ 3.05 🚀",
])
def test_no_ticker_close_alerts_when_single_day_trade(text):
    """8/19-8/20 这四条全是对当时唯一那个 day_trade（SPY）说的，全部静默丢弃。"""
    assert _looks_like_close_attempt(text, lone_day_trade=True) is True
    # 有多个 day_trade（有歧义）时维持原行为：不提醒
    assert _looks_like_close_attempt(text, lone_day_trade=False) is False


@pytest.mark.parametrize("text", [
    "just hanging out now, market is boring",   # 无价格 hint
    "great work today 💙",
])
def test_no_price_hint_stays_silent(text):
    """放宽的是 ticker 那一侧，价格 hint 仍然是硬条件。"""
    assert _looks_like_close_attempt(text, lone_day_trade=True) is False


def test_ticker_path_unchanged():
    """原有判据（有 ticker + 价格）不依赖 day_trade 数量。"""
    assert _looks_like_close_attempt("trimmed MSFT @ 2.45", lone_day_trade=False) is True
