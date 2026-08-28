"""路由词表与解析词表的同步不变量。

`detect_action` 决定一条消息走 OPEN 还是 CLOSE 分支；只有判成 CLOSE 的才会
被喂给 `parse_close`。两边各有一份动作词表，**必须同进同退**：

- 路由认得、解析不认得 → `[CLOSE] parser skipped`，至少还留了痕（7/3 "all out TSLA"）；
- 解析认得、路由不认得 → **消息走 OPEN 分支落到 `[parser] no signal`，
  平仓指令静默消失**，日志里看不出这是一条被漏掉的平仓。

第二种是本文件要钉死的。`close_parser.py` 顶部早就写了这条约束，但一直没有
测试兜着，于是漂移了两个词整整七周：

    [8/25 实测] "KC Trades Bot: 减半NVDA 3.05 💰目标214.50已达成"
        detect_action  -> OPEN   ❌  （STRONG_CLOSE_RE 的 ZH 词表里没有 减半）
        parse_close    -> pct=50 ✅  （ZH_ACTION_VERBS 里 7/9 就有了）

    当晚零损失纯粹因为 EN 孪生 "out half NVDA 3.05" 早 4 秒执行了。
    ZH 孪生常常比 EN 早 2-4 秒到，那种时候就是一次漏平。

同一轮扫描还抓出 `抛了`（同样只在解析侧）。两个都已补进 STRONG_CLOSE_RE。

**不变量的写法很关键**：不能简单地"遍历词表、断言每个词都路由成 CLOSE" ——
`ZH_FULL_CLOSE_VERBS` 里有 `剩余仓位` / `剩下的仓位` 这类**名词短语**，
它们的作用是"当已经判定为平仓动作时把 pct 抬到 100"，本来就不该单独触发路由
（"减仓后剩余仓位 2 张"是陈述句）。按"每个词都必须路由"去测会报 4 个假阳性。

真正的不变量是**端到端**的那一条：

    parse_close 能解析出结果  ⇒  detect_action 必须已经判成 CLOSE

这条不依赖任何一张表的内部语义，加词、删词、改语义都不用动测试。
"""
import pytest

from autotrade.parsing.close_parser import (
    ZH_ACTION_VERBS,
    ZH_FULL_CLOSE_VERBS,
    ACTION_VERBS,
    FULL_CLOSE_VERBS,
    parse_close,
)
from autotrade.parsing.signal_parser import detect_action

OPEN_SYMBOLS = {"NVDA"}

# 两种语序都试：动词在 ticker 前 / 后。KC 的机翻两种都出现过
# （"减半NVDA 3.05" / "NVDA 减仓 3.05"）。
_ZH_TEMPLATES = ("KC交易机器人：{v}NVDA 3.05", "KC交易机器人：NVDA {v} 3.05")
_EN_TEMPLATES = ("KC Trades Bot:{v} NVDA 3.05", "KC Trades Bot:NVDA {v} 3.05")


def _gaps(verbs, templates):
    """返回 (verb, text, pct) —— parse_close 认、detect_action 不认的裂缝。"""
    out = []
    for v in sorted(set(verbs)):
        for tmpl in templates:
            text = tmpl.format(v=v)
            parsed = parse_close(text, OPEN_SYMBOLS)
            if parsed and detect_action(text) != "CLOSE":
                out.append((v, text, parsed.get("pct")))
    return out


def test_zh_parse_close_implies_close_routing():
    """ZH：解析侧认得的，路由侧必须也认得。

    失败时说明有词只加进了 close_parser 的表，没同步进
    signal_parser.STRONG_CLOSE_RE —— 那是一条静默漏平的裂缝。
    """
    gaps = _gaps(set(ZH_ACTION_VERBS) | set(ZH_FULL_CLOSE_VERBS), _ZH_TEMPLATES)
    assert not gaps, (
        "解析侧认得但路由侧不认得（消息会落到 OPEN 分支静默消失）：\n"
        + "\n".join(f"  {v!r}: {t!r} -> parse_close pct={pct}" for v, t, pct in gaps)
        + "\n修法：把这些词补进 signal_parser.STRONG_CLOSE_RE 的 ZH 词表。"
    )


# EN 侧**故意**保留的不对称：解析侧比路由侧松，且这是对的。
#
# 这几个都是**弱信号**——它们自己不构成平仓指令，只在消息里已经有别的平仓
# 上下文时才补充语义。路由侧不收它们，是为了不让情绪词单独触发 CLOSE。
#
#   bang! / bang -   KC 的情绪触发词。实测样本（8/20）里它永远和真动词同现:
#                    "BANG! Out half 2.80 💰" / "BANG! Out majority @ 3.05 🚀"
#                    —— 靠 out 短语族路由，bang 只负责在解析侧提高召回。
#                    单独放行等于每句 "BANG!" 都变平仓指令。
#   lock it/them/these
#                    带介词的形式 "lock them in" / "lock these on" **路由认得**
#                    （STRONG_CLOSE_RE 的 lock 分支要求尾随 in/on）。裸形式只在
#                    解析侧收，因为 "lock it up" / "lock these levels" 太常见。
#
# 要动这张表，先去真实语料里数一数裸形式出现过几次、有几次是真指令。
# 记在 ROADMAP P2 #5「解析器收敛：动作词表单一来源」。
_EN_INTENTIONAL_ASYMMETRY = {"bang!", "bang -", "lock it", "lock them", "lock these"}


def test_en_parse_close_implies_close_routing():
    """EN 侧同一条不变量（扣掉上面那组故意的不对称）。

    EN 的 out 短语族（out half / out N% / out <TICKER> / out the rest …）已经靠
    「两边 import 同一份 pattern 常量」保证同步；本用例覆盖的是**词表**那一半
    （ACTION_VERBS / FULL_CLOSE_VERBS），它们是各写各的。

    [8/26 扫描抓出] cut / cutting / dumped / dumping 全在 FULL_CLOSE_VERBS 里
    （命中即 pct=100 全平），却一个都不在路由表里 —— "dumping NVDA here"
    整条走 OPEN 分支静默消失。已补进 WEAK_CLOSE_RE。
    """
    gaps = [
        g for g in _gaps(set(ACTION_VERBS) | set(FULL_CLOSE_VERBS), _EN_TEMPLATES)
        if g[0] not in _EN_INTENTIONAL_ASYMMETRY
    ]
    assert not gaps, (
        "解析侧认得但路由侧不认得：\n"
        + "\n".join(f"  {v!r}: {t!r} -> parse_close pct={pct}" for v, t, pct in gaps)
        + "\n修法：把这些词补进 signal_parser.STRONG_CLOSE_RE / WEAK_CLOSE_RE；"
        "\n若确认是故意的不对称，加进 _EN_INTENTIONAL_ASYMMETRY 并写清理由。"
    )


def test_intentional_asymmetry_list_stays_honest():
    """allowlist 不许当垃圾桶：里面每一条都必须**确实**还是裂缝。

    某个词后来被补进路由表了，就该从 allowlist 里删掉 —— 否则这张表会慢慢
    变成"当年不想修的东西"的坟场，下一个人无从判断哪些还成立。
    """
    still_gaps = {g[0] for g in _gaps(set(ACTION_VERBS) | set(FULL_CLOSE_VERBS), _EN_TEMPLATES)}
    stale = _EN_INTENTIONAL_ASYMMETRY - still_gaps
    assert not stale, f"这些词已经能路由了，请从 _EN_INTENTIONAL_ASYMMETRY 删除：{sorted(stale)}"


@pytest.mark.parametrize("text,expect_pct", [
    ("@everyone\nKC Trades Bot:dumping NVDA here", 100),
    ("@everyone\nKC Trades Bot:cutting NVDA 3.05", 100),
])
def test_en_full_close_verbs_now_route(text, expect_pct):
    """cut/cutting/dumped/dumping 是 pct=100 的全平动词，漏路由 = 整仓留在场上。"""
    assert detect_action(text) == "CLOSE"
    assert parse_close(text, OPEN_SYMBOLS)["pct"] == expect_pct


@pytest.mark.parametrize("text", [
    "the Fed cut rates today",              # 宏观评论：路由可能命中，但抽不到持仓 symbol
    "nice haircut on the circuit board scout",  # \b 边界：不许命中 haircut/circuit/scout
])
def test_cut_addition_does_not_widen_recall(text):
    assert parse_close(text, OPEN_SYMBOLS) is None


def test_open_intent_veto_still_wins_over_cut():
    """放 WEAK 而非 STRONG 的理由：混排句宁可判 OPEN（误平代价 > 漏平）。"""
    assert detect_action("KC Trades Bot:cutting NVDA, adding SPY here") == "OPEN"


# ---- 8/25 当晚原文的定点回归（扫描测试万一被改坏，这两条还在）----

@pytest.mark.parametrize("text,expect_pct", [
    ("@everyone\nKC Trades Bot: 减半NVDA 3.05 💰目标214.50已达成，日线21均线在此 🚀", 50),
    ("@everyone\nKC交易机器人：减半仓NVDA于2.45", 50),
    ("@everyone\nKC交易机器人：抛了NVDA 3.05", 33),
])
def test_zh_verbs_that_had_drifted(text, expect_pct):
    assert detect_action(text) == "CLOSE"
    parsed = parse_close(text, OPEN_SYMBOLS)
    assert parsed is not None and parsed["pct"] == expect_pct


def test_en_twin_of_the_nvda_case_unchanged():
    """反向不变量：修 ZH 侧不许把 EN 孪生带坏（8/25 当晚是它兜住的）。"""
    text = "@everyone\nKC Trades Bot:out half NVDA 3.05 💰 214.50 target hit, daily 21 ema here 🚀"
    assert detect_action(text) == "CLOSE"
    assert parse_close(text, OPEN_SYMBOLS)["pct"] == 50


@pytest.mark.parametrize("text", [
    # 名词短语单独出现是陈述句，不该被判成平仓动作
    "@everyone\nKC交易机器人：减仓后剩余仓位 2 张",
    # 宏观评论里的"减半"（symbol 白名单是第二道保险，这里连动作都不该成立）
    "美联储把加息幅度减半了",
])
def test_routing_fix_does_not_widen_recall(text):
    assert parse_close(text, OPEN_SYMBOLS) is None
