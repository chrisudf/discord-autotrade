"""8/14 夜复盘回归（US RTH 2026-08-13，AEST 8/13 23:15 → 8/14 07:00）。

当晚 4 次开仓全部成交、4 次断线全部 620-827ms 内 RESUME、Telegram 0 失败。
两个缺陷，一个差点花钱，一个让复盘看错了事实：

1. **ZH 平仓管线把"仍持有"的复盘判成减仓**（parser 层，唯一的真钱路缺陷）。
   06:13:53（16:13 ET）中英孪生分道扬镳：

     EN  "nice drop on SPCX into end of day, still in the puts after the
          profit trims this morning ✅"          → no signal ✅
     ZH  "临收盘SPCX跌得漂亮，早间利润减仓后仍持有看跌期权✅"
                                                 → **CLOSE 33%** ❌

   三个因素叠出来的：`减仓` 在 ZH_ACTION_VERBS；`早间` 不在 ZH_RECAP_MARKERS
   （里面有 今早 / 今天早些 / 早些时候，唯独缺它）；整句用逗号不用句号，
   `_zh_action_sentences` 切不开，而 SPCX 是拉丁 ticker 所以 symbol 照样抽得到
   —— 不像同晚另外两条中文复盘靠 `[zh_unrecognized]` 侥幸拦下。

   **唯一挡住卖单的是 runner-preserve**（SPCX 当时剩 1 张，pct=33 取整为 0）。
   剩 2 张就会真减掉 1 张。这是当晚离误平最近的一次。

   修法两处一起落：`早间` 补进 ZH_RECAP_MARKERS 的过去时间锚组；`仍持有`
   系补进两侧 —— close 路径的 ZH_RECAP_MARKERS 和开仓路径的
   `signal_parser.SKIP_KEYWORDS`。只修一边就是 8/5 已经学过一次的"拦住一边
   另一边照样平掉"。

2. **复盘素材系统性少报持仓**（ops 层）。`ops/morning_collect.sh` 的
   "停机时仍未平的持仓" 用 `WHERE status = 'OPEN'`，而部分平仓过的仓位
   status 是 **PARTIAL** —— 整行漏掉。当晚真实过夜 6 个合约 10 张，摘要只
   显示 4 个 8 张，SPCX 120P 在两张持仓表里完全隐身。
   漏掉的恰恰是被 trim 过的仓，也就是最该盯的那些。本文件只钉住 parser 侧；
   SQL 口径的回归靠 `WHERE status IN ('OPEN','PARTIAL')` 与 position_mgr
   选仓口径对齐（那边一直是两个状态都算）。
"""
import pytest

from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import parse_signal

# 逐字原文（去掉 Discord 的 @everyone 前缀，解析器不关心它）
ZH_STILL_IN = "临收盘SPCX跌得漂亮，早间利润减仓后仍持有看跌期权✅"
EN_STILL_IN = ("nice drop on SPCX into end of day, still in the puts "
               "after the profit trims this morning ✅")

# 当晚实际持有的标的白名单（parse_close 的第二个入参）
_HELD = {"SPCX", "ASTS", "AMD", "UBER", "HOOD", "TSLA", "NVDA"}


# ============================================================
# 1. "仍持有" 型复盘不得被判成平仓
# ============================================================

def test_zh_still_holding_recap_is_not_a_close():
    """8/14 SPCX：这条明说"仍持有"的中文复盘曾被判成 CLOSE 33%。"""
    assert parse_close(ZH_STILL_IN, _HELD) is None


def test_en_twin_stays_correct():
    """英文孪生当晚就是对的 —— 修 ZH 侧不能把它带坏。"""
    assert parse_close(EN_STILL_IN, _HELD) is None


@pytest.mark.parametrize("text", [
    # `早间` 这个时间锚（今早 / 今天早些 / 早些时候 的同族，一直缺）
    "早间减仓SPCX看跌期权",
    "早间在 40% 时卖出了 NVDA",
    # `仍持有` 系：比时间锚更硬的证据 —— 明说仓位还在
    "SPCX 减仓后仍持有看跌期权",
    "ASTS 仍在持有",
    "减仓了一部分，还持有 TSLA 看涨期权",
    "还在持有 HOOD 的仓位",
])
def test_recap_markers_block_close(text):
    assert parse_close(text, _HELD) is None


@pytest.mark.parametrize("text", [
    "$ASTS 仍在持有",
    "仍持有我的 $HOOD 7/24 $125 看涨期权",
    "还在持有 $TSLA 380 看涨期权",
])
def test_holding_recap_is_not_an_open_signal(text):
    """开仓路径同批补 —— 双语双向都要拦，只修一侧等于白修（8/5 教训）。

    主动 skip 返回 `{'skip': ...}` 而不是 None（见 test_folded_skip_returns_dict）：
    两者都不会下单，但 skip 会带上原因、让 open_flow 的 triage 有机会发告警。
    """
    assert parse_signal(text) == {"skip": "holding_or_remaining"}


# ============================================================
# 2. 反向保护：真的平仓指令不能被这批新词误伤
# ============================================================

@pytest.mark.parametrize("text", [
    "减仓SPCX看跌期权 @ 3.60",
    "$SPCX 此处减仓 33%",
    "平仓 NVDA 看涨期权",
    "砍掉一半 TSLA 仓位",
])
def test_real_close_signals_still_parse(text):
    """新加的 早间 / 仍持有 系是短语级匹配，不该波及不含这些词的真指令。"""
    parsed = parse_close(text, _HELD)
    assert parsed is not None, f"真平仓指令被误伤: {text}"


def test_buy_and_hold_phrasing_still_opens():
    """裸"持有"没进表，"买入…打算持有到 9 月" 这类真开仓不受影响
    （SKIP_KEYWORDS 注释里点名要避免的形状）。"""
    sig = parse_signal("$UBER 8/21 $78 看涨期权 $0.56，打算持有到 9 月")
    assert sig is not None
    assert sig["symbol"] == "UBER"
