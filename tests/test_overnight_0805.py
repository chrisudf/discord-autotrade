"""8/5 夜复盘回归（US RTH 2026-08-05，AEST 8/5 23:28 → 8/6 07:00）。

当晚零下单、零持仓。唯一的真漏单是 $RKLB，两道**独立**的闸各挡了一次：

1. **编辑不重解析**（listener 层）——23:51:00 喊单员发 "跟踪 $RKLB 每周 $80
   看涨期权"，无喊价，按契约规则 3 正确拒单并 TG 告警。14 秒后他**编辑**该
   消息补上 "\\n\\n$1.35 填充 2%"。`on_message_edit` 当时只写一行日志就返回
   （router 模块 docstring 明写"不重新触发下单"），补进来的喊价从未进过解析链路。

2. **喊价跨不过换行**（parser 层）——就算重新喂进去也接不住：B0/B0.5/B1b/B2/B3
   的尾段一律是 `[^\\$\\n]*?`，字符类里的 \\n 把另起一段的 $1.35 挡在外面。
   同一句写成一行 "…$80 calls $1.35 fill" 一直解析正常，差别只有那个换行。

修 1 不修 2 无效，反之亦然 —— 本文件把两道都钉住。

顺带挖出来的两个**不是当晚触发、但同源**的缺陷（当晚零持仓所以没造成损失）：

3. **B0.5 从来没生效过**（本文件 test_b05_month_name_*）——rf 字符串里
   `{0,3}` 被当成 f-string 替换字段渲染成字面量 "(0, 3)"，多出第 7 个捕获组，
   6 元解包抛 ValueError 被 `except ValueError` 吞成"日期非法"，信号静默丢失；
   不崩的路径则落到 B2，英文月份日期被无视、expiry 悄悄退成 next Friday。

4. **限定价当入场价**（test_qualified_price_*）——改动前
   "$RKLB weekly $80 calls target $3.00" 会按 $3.00 挂买单（实测确认）。
   B 系列此前没有任何限定词护栏，现由 _b_match → _price_qualified 接管。

另有两个**误平**隐患（当晚双双落到"无持仓，忽略"，是侥幸不是规则）：

5. **未来意图当成当下指令**（本文件第 6 节）——"will trim SOFI ... closer to
   $19" 是等股价到位再减，却被判成已执行的减仓，pct 默认 33 → 有持仓就立刻
   卖掉三分之一。RECAP_MARKERS 有 "will be " 却接不住少了 be 的 "will trim"。

6. **so far 的收尾形态写死了标点**（同上）——"Controlled selling so far - "
   是复盘口径，老表只认 "so far," 和 "(so far)"，喊单员用的是破折号；漏掉后
   close_parser 还会把仓位规模 "2% in $RKLB" 读成 trim 比例返回 pct=2。

5/6 于 8/8 修在 close_parser 的 recap 闸（新增 RECAP_PATTERNS + ZH 侧
"目前为止"），路由层 detect_action 保持判 CLOSE 不动 —— 这些句子确实在谈平仓。
逐字语料见 tests/corpus/2026-08-05.jsonl 的 sofi_will_trim_en / rklb_risk_off_*。
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pytest

from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action, parse_signal

D = date(2026, 8, 5)          # 周三；_next_friday → 2026-08-07
FRI = date(2026, 8, 7)

# 当晚逐字原文（取自 autotrade_2026-08-06_overnight.txt）
RKLB_ORIG_ZH = "丰富：\n跟踪 $RKLB 每周 $80 看涨期权\n\n@everyone $alert"
RKLB_ORIG_EN = "enrich:\nTailing the $RKLB weekly $80 calls\n\n@everyone $alert"
# 23:51:15 的编辑版：只多了 "$1.35 填充 2%" 这一段
RKLB_EDITED_ZH = (
    "丰富：\n跟踪 $RKLB 每周 $80 看涨期权\n\n$1.35 填充 2%\n\n@everyone $警报"
)


# ============================================================
# 1. RKLB —— 当晚的漏单本体
# ============================================================

@pytest.mark.parametrize("raw", [RKLB_ORIG_ZH, RKLB_ORIG_EN])
def test_original_message_without_price_is_refused(raw):
    """原始版本没喊价 → 契约规则 3「无价不下单」，拒单是**正确**行为。

    这条不是缺陷回归，是把"正确的拒单"钉死：日后放宽 B 系列时不能
    顺手把无价信号也放进来。
    """
    assert detect_action(raw) == "OPEN"
    assert parse_signal(raw, msg_ts=D) is None


def test_edited_message_with_crossline_fill_parses():
    """8/5 头号事故：喊价另起一段，当晚 B 系列全落空。"""
    sig = parse_signal(RKLB_EDITED_ZH, msg_ts=D)
    assert sig is not None, "喊价另起一段不该再解析失败"
    assert (sig["symbol"], sig["strike"], sig["side"], sig["price"]) == (
        "RKLB", 80.0, "CALL", 1.35,
    )
    assert sig["expiry_date"] == FRI, "每周/weekly → 消息当日之后最近的周五"


def test_edited_message_matches_its_single_line_equivalent():
    """跨行版与单行版必须解析出**同一张合约**。

    单行版在改动前就一直正常 —— 它是本次期望值的来源（语料铁律 1：
    期望只能来自跑过 parser 确认的行为，不能手猜）。
    """
    inline = parse_signal("Tailing the $RKLB weekly $80 calls $1.35 fill", msg_ts=D)
    edited = parse_signal(RKLB_EDITED_ZH, msg_ts=D)
    keys = ("symbol", "side", "strike", "price", "expiry_date")
    assert {k: edited[k] for k in keys} == {k: inline[k] for k in keys}


# ============================================================
# 2. 跨行喊价：B 系列各变体
# ============================================================

@pytest.mark.parametrize("label,raw,expiry", [
    ("B0  MM/DD",      "$RKLB 8/7 $80 calls\n\n$1.35 fill",   FRI),
    ("B0.5 Month DD",  "$RKLB Aug 7 $80 calls\n\n$1.35 fill", FRI),
    ("B1  NDTE 前置",  "$RKLB 2DTE $80 calls\n\n$1.35",       FRI),
    ("B1b NDTE 居中",  "$RKLB $80 calls 2DTE\n\n$1.35",       FRI),
    ("B2  weekly",     "$RKLB weekly $80 calls\n\n$1.35 fill", FRI),
])
def test_crossline_price_across_b_variants(label, raw, expiry):
    """喊价另起一段在 B 全系列都要接住 —— 喊单员下次换个写法不能又漏。"""
    sig = parse_signal(raw, msg_ts=D)
    assert sig is not None, label
    assert (sig["symbol"], sig["strike"], sig["side"], sig["price"]) == (
        "RKLB", 80.0, "CALL", 1.35,
    ), label
    assert sig["expiry_date"] == expiry, label


def test_crossline_puts_direction_preserved():
    sig = parse_signal("$SPY weekly $770 puts\n\n$2.40 fill", msg_ts=D)
    assert sig is not None
    assert (sig["symbol"], sig["side"], sig["strike"], sig["price"]) == (
        "SPY", "PUT", 770.0, 2.40,
    )


# ============================================================
# 3. 限定价护栏 —— 放开换行后的主要新风险
# ============================================================
# 窗口能跨行了，就够得着下一段的"目标价/止损价"。_price_qualified
# （原 Pattern D 的护栏）现在也管 B 系列。

@pytest.mark.parametrize("raw", [
    # 改动前这两条会按 $3.00 / $9.50 真下单（实测确认），是本次顺带修掉的洞
    "$RKLB weekly $80 calls target $3.00",
    "$SPY weekly $770 calls target $9.50",
    # 跨行形态
    "$SPY weekly $770 calls\n\ntarget $9.50",
    "$SPY weekly $770 calls\n\nstop loss $2.40",
    "$SPY 每周 $770 看涨期权\n\n目标价格 $9.50",
    "$SPY 每周 $770 看涨期权\n\n止损价格 $2.40",
])
def test_qualified_price_is_never_an_entry(raw):
    """目标价/止损价不是入场价 —— 拿它当限价就是凭空溢价买入。"""
    assert parse_signal(raw, msg_ts=D) is None, raw


def test_real_fill_before_target_still_parses():
    """真喊价在前、目标价在后：按真喊价成交，不受后面那个数字影响。"""
    sig = parse_signal(
        "$RKLB weekly $80 calls\n\n$1.35 fill\n\ntarget $3.00", msg_ts=D,
    )
    assert sig is not None and sig["price"] == 1.35


# ============================================================
# 4. $ 墙 —— 跨行不等于跨标的
# ============================================================
# _B_PRICE_GAP 是 [^\$]*?：放开了 \n，但仍禁 $。$ 是本频道 ticker 的固定
# 前缀，禁掉它窗口就跨不到第二个标的去（Pattern D 的 _GAP 字母墙同款思路）。

@pytest.mark.parametrize("raw", [
    "$META $620 calls\n\n$SPY 4.20",
    "$META $620 calls\n\n$SPY 4DTE @ 3.15",
    "$META 620 calls\n\n$SPY $4.20",
])
def test_price_window_cannot_cross_into_another_ticker(raw):
    """绝不能拼出 META 的行权价 + SPY 的价格这种混合单。"""
    sig = parse_signal(raw, msg_ts=D)
    assert not (sig and not sig.get("skip") and sig.get("symbol") == "META"), raw


# ============================================================
# 5. B0.5 f-string 缺陷回归
# ============================================================

@pytest.mark.parametrize("raw,expiry", [
    # 有填充词：改动前编译出的正则会命中并抛 ValueError → 静默丢单
    ("$RKLB June 26 $80 weekly calls $1.35", date(2027, 6, 25)),
    # 无填充词：改动前落到 B2，June 26 被无视、expiry 退成 next Friday
    ("$RKLB June 26 $80 calls $1.35", date(2027, 6, 25)),
])
def test_b05_month_name_expiry_is_honoured(raw, expiry):
    """英文月份日期必须真的决定 expiry，不能悄悄退成 weekly。

    2026-06-26 相对消息日 8/5 已过 → smart_expiry 跨年取 2027-06-26（周六）
    → _adjust_expiry 回退到 2027-06-25 周五。
    """
    sig = parse_signal(raw, msg_ts=D)
    assert sig is not None, raw
    assert sig["expiry_date"] == expiry, raw


def test_b05_does_not_raise_valueerror():
    """曾经的报错是 ValueError，而 parse_signal 的 except ValueError 是给
    smart_expiry 的非法日期用的 —— 归因错误比丢单本身更难查。"""
    import autotrade.parsing.signal_parser as sp
    m = sp._try_pattern_b("$NVDA June 20 $180 calls $2.50", D)
    assert m is not None and m["symbol"] == "NVDA"


# ============================================================
# 6. 两个误平隐患 —— 当晚靠"零持仓"躲过，不是靠规则
# ============================================================
# 当晚 3 条消息被 detect_action 判成 CLOSE，全部因无持仓而空转。其中两条
# **在有持仓时会真的卖出**：
#   (a) "will trim SOFI ... closer to $19" → pct 默认 33，立刻减掉三分之一；
#   (b) "Controlled selling so far - "     → 把仓位规模 2% 读成 trim 比例。
# 两条都是复盘/未来意图，不是当下指令。RECAP_MARKERS 本该拦住，各差一点：
# (a) 老表有 "will be " 没有裸 "will trim"；(b) 老表把 so far 的收尾形态写死
# 成逗号和右括号，而喊单员用的是破折号。
#
# 路由层（detect_action）保持判 CLOSE 不动 —— 这些句子确实在谈平仓，
# 该拦的地方是 close_parser 的 recap 闸，不是路由。

SOFI_WILL_TRIM_EN = (
    "@everyone\nKC Trades Bot:will trim SOFI calls closer to $19 stock price 📈"
)
SOFI_WILL_TRIM_ZH = (
    "@everyone\nKC Trades Bot：将在股价接近19美元时减仓SOFI看涨期权📈"
)
RKLB_RISK_OFF_EN = (
    "enrich:\nRisk off for now - I’m 98% cash with 2% in $RKLB \n\n"
    "Controlled selling so far - let’s EMAs catch up\n\n@everyone"
)
RKLB_RISK_OFF_ZH = (
    "enrich:\n目前风险规避 - 我有98%的现金，2%投资于$RKLB\n\n"
    "到目前为止控制性卖出 - 让我们的EMA追赶上来\n\n@everyone"
)


@pytest.mark.parametrize("raw,held", [
    (SOFI_WILL_TRIM_EN, "SOFI"),
    # ZH 孪生走的是另一条闸（ZH_RECAP_MARKERS 的 "时减仓"，7/x 就在了）。
    # 一起钉住：双语冗余只有在两边都拦得住时才是冗余，漏一边等于没修。
    (SOFI_WILL_TRIM_ZH, "SOFI"),
    (RKLB_RISK_OFF_EN, "RKLB"),
    (RKLB_RISK_OFF_ZH, "RKLB"),
])
def test_recap_and_future_intent_never_sell(raw, held):
    """**持有该标的**时也必须一张不卖 —— 当晚是零持仓侥幸，这里补上真条件。"""
    assert detect_action(raw) == "CLOSE", "路由层不改：这些句子确实在谈平仓"
    assert parse_close(raw, {held}) is None, raw


@pytest.mark.parametrize("present,future", [
    ("trim SOFI here",     "will trim SOFI closer to $19"),
    ("cut SOFI here",      "will cut SOFI closer to $19"),
    ("selling SOFI here",  "will sell SOFI closer to $19"),
    ("closing SOFI",       "will close SOFI closer to $19"),
    ("dumping SOFI",       "will dump SOFI closer to $19"),
    ("scaling out SOFI",   "will scale out of SOFI closer to $19"),
    ("lock in SOFI",       "will lock in SOFI closer to $19"),
])
def test_future_intent_blocked_where_present_tense_executes(present, future):
    """A/B 成对：同一个动词，当下式照常平，将来式一张不动。

    先断言 present 一侧真的会平仓，否则"future 返回 None"证明不了是护栏起了
    作用（那 7 个动词里只有 trim/cut 的裸祈使在 ACTION_VERBS 里，其余靠
    -ing/-ed 词形，随手写个裸词会得到一条永远为真的空测试）。

    动词表与 close_parser.ACTION_VERBS 的词根同源，加动词要两处一起看。
    """
    assert parse_close(present, {"SOFI"}) is not None, present
    assert parse_close(future, {"SOFI"}) is None, future


@pytest.mark.parametrize("raw", [
    "will trim SOFI closer to $19",
    "will be trimming SOFI closer to $19",   # 老表的 "will be " 本来就接得住
    "I’ll trim SOFI closer to $19",           # Discord 客户端的弯引号
    "I'll trim SOFI closer to $19",           # 直引号
])
def test_future_intent_contractions(raw):
    assert parse_close(raw, {"SOFI"}) is None, raw


@pytest.mark.parametrize("tail", [",", " -", " –", " —", ".", ":", ";", ")", "\n"])
def test_so_far_is_recap_under_any_clause_ending(tail):
    """收尾符不该只认逗号 —— 8/5 漏的就是破折号那一种。"""
    assert parse_close(f"Controlled selling so far{tail} EMAs catch up",
                       {"RKLB"}) is None, repr(tail)


@pytest.mark.parametrize("raw", [
    # so far 后面跟单词 = 时间状语，不是复盘收尾（老注释里点名要放行的形态）
    "Selling $RKLB so far before FOMC",
    # 当下祈使/完成时，必须照常执行
    "Trim SPY here @ 2.54",
    "Trimmed 50% $SOFI",
    "Selling $RKLB here",
    "Out half $SOFI @ 1.20",
])
def test_present_tense_close_still_executes(raw):
    """过度拦截的另一侧：真指令被 recap 闸吞掉就是漏平。"""
    assert parse_close(raw, {"SPY", "SOFI", "RKLB"}) is not None, raw


# ============================================================
# 6. on_message_edit —— 编辑后成为信号
# ============================================================
# 当晚的第一道闸：编辑内容从未进过解析链路。现在改成"解析 + TG 告警，
# 但仍不下单"，这一节钉住"该提醒的提醒、不该提醒的别吵"两侧。

CID = 515151
TRIGGER_UID = 424242
OTHER_UID = 999999


@dataclass
class _Author:
    id: int
    name: str = "美股会员网机器人"


@dataclass
class _Channel:
    id: int
    name: str = "enrich"


@dataclass
class _Msg:
    id: int
    content: str
    channel: _Channel = field(default_factory=lambda: _Channel(CID))
    author: _Author = field(default_factory=lambda: _Author(TRIGGER_UID))
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) - timedelta(seconds=14)
    )


class _Cfg:
    name = "enrich"

    @staticmethod
    def is_trigger_user(uid):
        return uid == TRIGGER_UID


class _Registry:
    @staticmethod
    def is_monitored(cid):
        return cid == CID

    @staticmethod
    def get(cid):
        return _Cfg()


@pytest.fixture
def edit_env(monkeypatch):
    """把 router 的 registry / TG 出口换掉，返回收集到的 TG 消息列表。"""
    from autotrade.listener import dedup, heuristics, router

    sent = []

    async def _capture(msg):
        sent.append(msg)

    monkeypatch.setattr(router, "registry", _Registry())
    monkeypatch.setattr(router, "_safe_notify", _capture)
    dedup._edit_signal_alerted.clear()
    heuristics._recent_exec.clear()
    return sent


async def _edit(before_text, after_text, **kw):
    from autotrade.listener.router import handle_message_edit

    mid = kw.pop("msg_id", 777)
    before = _Msg(id=mid, content=before_text, **kw)
    after = _Msg(id=mid, content=after_text, **kw)
    await handle_message_edit(before, after)


@pytest.mark.asyncio
async def test_edit_that_adds_fill_price_alerts(edit_env):
    """8/5 逐字重演：无价 → 编辑补上 $1.35 → 必须提醒。"""
    await _edit(RKLB_ORIG_ZH, RKLB_EDITED_ZH)
    assert len(edit_env) == 1, "编辑后成为可执行信号，必须发 TG"
    # 文案是 MarkdownV2，'.' 之类保留字带反斜杠（$1\.35）——比对前先去转义
    plain = edit_env[0].replace("\\", "")
    assert "RKLB" in plain and "1.35" in plain and "80" in plain
    assert "未自动下单" in plain, "文案必须让人明确知道要手动跟"


@pytest.mark.asyncio
async def test_edit_alert_is_throttled_per_contract(edit_env):
    """连着编辑同一条（补价后又改错字）不该重复轰炸。"""
    await _edit(RKLB_ORIG_ZH, RKLB_EDITED_ZH)
    await _edit(RKLB_ORIG_ZH, RKLB_EDITED_ZH + " 补充说明")
    assert len(edit_env) == 1


@pytest.mark.asyncio
async def test_price_correction_on_parseable_message_is_silent(edit_env):
    """编辑前就能解析 = 价格修正，不是漏单。

    按"订阅第一信号"原则，原消息该下单时已经下过了，这里再提醒只是噪音。
    """
    await _edit(
        "$RKLB weekly $80 calls $1.35 fill",
        "$RKLB weekly $80 calls $1.45 fill",
    )
    assert edit_env == []


@pytest.mark.asyncio
async def test_edit_still_without_price_stays_silent(edit_env):
    """编辑了但还是没喊价 → 仍然解析失败 → 不提醒（原消息的 parse-fail
    告警已经发过一次了）。"""
    await _edit(RKLB_ORIG_ZH, RKLB_ORIG_ZH + "\n\n再看看")
    assert edit_env == []


@pytest.mark.asyncio
async def test_embed_only_edit_is_ignored(edit_env):
    """Discord 为链接预览等纯 embed 变化也发 edit 事件，content 逐字未变。"""
    await _edit(RKLB_EDITED_ZH, RKLB_EDITED_ZH)
    assert edit_env == []


@pytest.mark.asyncio
async def test_non_trigger_user_edit_ignored(edit_env):
    await _edit(RKLB_ORIG_ZH, RKLB_EDITED_ZH, author=_Author(OTHER_UID))
    assert edit_env == []


@pytest.mark.asyncio
async def test_unmonitored_channel_edit_ignored(edit_env):
    await _edit(RKLB_ORIG_ZH, RKLB_EDITED_ZH, channel=_Channel(123, "random"))
    assert edit_env == []


@pytest.mark.asyncio
async def test_edit_into_close_semantics_is_not_handled_here(edit_env):
    """编辑成平仓语义不走本路径（误平代价 > 漏平，无实测语料不放开）。"""
    await _edit(RKLB_ORIG_ZH, "$RKLB 全部平仓 @ 1.35")
    assert edit_env == []


@pytest.mark.asyncio
async def test_edit_suppressed_when_symbol_just_executed(edit_env):
    """刚从本频道成交过 → 多半是我们已经跟上的孪生，不是漏单。"""
    from autotrade.listener import heuristics

    heuristics._record_recent_exec(
        CID, {"symbol": "RKLB", "strike": 80.0, "side": "CALL", "price": 1.35},
    )
    await _edit(RKLB_ORIG_ZH, RKLB_EDITED_ZH)
    assert edit_env == []


@pytest.mark.asyncio
async def test_edit_handler_never_raises(edit_env):
    """畸形对象也不能崩 Discord 链路（handler 崩了会带走整条消息流）。"""
    from autotrade.listener.router import handle_message_edit

    class _Broken:
        @property
        def channel(self):
            raise RuntimeError("boom")

    await handle_message_edit(_Broken(), _Broken())
    assert edit_env == []


# ============================================================
# 7. 告警送达可见性（复盘 §4.4）
# ============================================================
# 8/5 夜最该让人知道的一条 TG —— "机器在 09:30 开盘钟上睡了 8 分 44 秒" ——
# 事后翻日志只能看到"发起了"，看不出送没送到：_on_alive_gap 直连
# send_telegram，而成功只记 DEBUG，`[notify] TG sent` 只有 _safe_notify 会打。
# 同一形状的还有 storm / churn 两条断线告警。三条统一改走 _safe_notify，
# 并给它加了 parse_mode 透传（这些正文没按 MarkdownV2 转义过，必须走纯文本）。

@pytest.mark.asyncio
async def test_safe_notify_forwards_parse_mode(monkeypatch):
    """透传断了就会给未转义正文套上 MarkdownV2 → 400（transport 会 fallback
    成纯文本，所以不会漏发，但每条告警都白跑一次往返且日志多一条 warning）。"""
    import autotrade.notify.transport as tp

    seen = []

    async def _fake(text, parse_mode="MarkdownV2"):
        seen.append(parse_mode)
        return True

    monkeypatch.setattr(tp, "send_telegram", _fake)
    await tp._safe_notify("plain", parse_mode=None)
    await tp._safe_notify("markdown")
    assert seen == [None, "MarkdownV2"]


@pytest.mark.asyncio
async def test_sleep_alert_goes_through_safe_notify(monkeypatch):
    """睡眠告警必须经由 _safe_notify（= 日志里能确认送达），且走纯文本。"""
    import autotrade.app.connection as conn

    sent = []

    async def _fake(msg, parse_mode="MarkdownV2"):
        sent.append((msg, parse_mode))

    monkeypatch.setattr(conn, "_safe_notify", _fake)
    monkeypatch.setattr(conn, "_last_disconnect_wall", None)
    monkeypatch.setattr(conn, "_sleep_alerted_wall", None)
    monkeypatch.setattr(conn, "client", None)

    now = datetime.now(timezone.utc)
    await conn._on_alive_gap(now - timedelta(minutes=20), now)

    assert len(sent) == 1
    msg, parse_mode = sent[0]
    assert parse_mode is None, "正文含反引号，套 MarkdownV2 会 400"
    assert "睡眠" in msg


@pytest.mark.asyncio
async def test_alive_gap_alert_failure_never_propagates(monkeypatch):
    """_safe_notify 自己吞异常；这里钉住调用方没有反过来把它包成会抛的形态
    —— 告警发不出去绝不能连带掐掉后面的回补锚逻辑。"""
    import autotrade.app.connection as conn

    async def _boom(msg, parse_mode="MarkdownV2"):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(conn, "_safe_notify", _boom)
    monkeypatch.setattr(conn, "_last_disconnect_wall", None)
    monkeypatch.setattr(conn, "_sleep_alerted_wall", None)
    monkeypatch.setattr(conn, "client", None)

    now = datetime.now(timezone.utc)
    prev = now - timedelta(minutes=20)
    with pytest.raises(RuntimeError):
        await conn._on_alive_gap(prev, now)
    # 锚在告警之前就已经回拨 —— 即使告警炸了，回补仍然拿得到正确起点
    assert conn._last_disconnect_wall == prev
