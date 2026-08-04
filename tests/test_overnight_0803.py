"""8/3 夜复盘回归（US RTH 2026-08-03，AEST 8/3 23:09 → 8/4 06:07）。

当晚 3 开 1 平，已实现 -$30，另有两笔仓位过夜。四件事，全部围绕
**双语孪生同时降级**这一个故障模式：

1. **AMZN 全平漏接**（10:27 ET）——KC "out AMZN -15%"。EN 侧 detect_action
   判成 OPEN（STRONG 只认 "out of"，WEAK 只认 out half/full/majority + out N%，
   裸 "out <TICKER>" 两边都没有），ZH 孪生"减持亚马逊 -15%"里 减持 是 trim 动词
   → pct 落默认 33 → 单张持仓触发 runner-preserve 跳过。
   两条路各错各的，冗余归零：KC 在 -15% 走人，我们把 275p 带过夜。

2. **SPY 条件句误平**（16:06 ET，当晚唯一一笔误平）——KC "…if you don't want
   to swing you can close until 4:15pm EST. I personally am swinging them"。
   EN 正确跳过，ZH 机翻"若不想持仓过夜，可…平仓"被解析成 CLOSE 100% @1.72
   （1.72 本身还是"接近入场价"的行情描述），卖在 1.63，入场 1.93 → -$30。
   两道修复：语义层（否定条件句 + 作者自述持有）＋ 结构层（ZH 孪生守卫）。

3. **`out 1/2` 漏路由**（11:25 ET）——enrich "$TSLA out 1/2" / "$TSLA 出 1/2"。
   FRACTION_OUT_PATTERN 一直认得这个形状，但它在 pct 抽取阶段才跑，路由和
   动词判定都到不了那一步。当晚 TSLA 只剩 1 张，runner-preserve 兜住了没亏钱，
   缺口本身是真的。

4. **day_trade tag 空转**——AMZN 信号原文写了 "day trade"，tag 解析、落库、
   进 TG 一路都在，但 categorize 只按 DTE==0 决定 eod_force。DTE=4 → weekly
   → 不强平。叠加第 1 条，一笔日内单带着 4DTE 过夜。
"""

import os
from datetime import date, datetime
from unittest.mock import patch

import pytest

from autotrade.listener import close_flow, dedup, heuristics
from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action, parse_signal
from autotrade.policy.positions import categorize
from autotrade.storage import positions_db

D = date(2026, 8, 3)
# 当晚真实持仓白名单（close parser 的裸 ticker 消歧靠它）
OPEN_SYMS = {"AMZN", "DELL", "SPY", "TSLA"}


# ============================================================
# 1. 裸 "out <TICKER>" —— AMZN 全平漏接
# ============================================================

def test_bare_out_ticker_routes_and_parses_as_full_close():
    """当晚原文：路由必须进 CLOSE，且是 100% 全平（不是 trim 默认 33）。"""
    raw = "out AMZN -15%, bored and the weekly premiums are moving like butt"
    assert detect_action(raw) == "CLOSE", "裸 out <TICKER> 不该再落到 OPEN 解析"
    parsed = parse_close(raw, OPEN_SYMS)
    assert parsed is not None
    assert parsed["symbols"] == ["AMZN"]
    assert parsed["pct"] == 100, "out X 是全平语义，33 会被 runner-preserve 吞掉"
    assert parsed["lang"] == "en"
    # -15% 是 PnL 标注不是 trim 比例，既有的 PCT_PATTERN 符号排除不能被破坏
    assert parsed["signal_pnl_pct"] == -15.0
    # 该句没有喊价（"-15%" 不是价），signal_price 必须留空让卖出走实时报价
    assert parsed["signal_price"] is None


@pytest.mark.parametrize("raw,pct", [
    ("all out TSLA", 100),          # 既有形态，行为不变
    ("out $DELL here", 100),        # $ 前缀
    ("out SPY 50%", 50),            # 显式 % 优先于全平默认
    ("out half AMZN", 50),          # 既有 out half 不被新分支顶掉
])
def test_out_forms_keep_their_pct(raw, pct):
    parsed = parse_close(raw, OPEN_SYMS)
    assert parsed is not None, raw
    assert parsed["pct"] == pct, raw


@pytest.mark.parametrize("raw", [
    # 大小写是唯一的护栏：小写介词/冠词绝不能被当 ticker
    "SPY is way out of the money here",
    "just hanging out now after a green day",
    "waiting for it to shake out weak hands before adding",
    # 全大写但在停用词表里
    "out ITM and looking at next week",
    "closing this out EOD unless it rips",
])
def test_bare_out_does_not_fire_on_prose(raw):
    """裸 out 分支不得把行情解说变成平仓动作（误平代价 > 漏平）。"""
    from autotrade.parsing.close_parser import _OUT_BARE_SYM_PATTERN
    import re as _re
    assert not _re.search(f"(?-i:{_OUT_BARE_SYM_PATTERN})", raw), raw


def test_open_signal_with_day_trade_still_routes_open():
    """同一晚的 AMZN **开仓**原文必须仍然判 OPEN 并完整解析。"""
    raw = "KC Trades Bot:AMZN 275p 4DTE @ 1.65 day trade"
    assert detect_action(raw) == "OPEN"
    sig = parse_signal(raw, msg_ts=D)
    assert sig is not None
    assert (sig["symbol"], sig["strike"], sig["side"], sig["price"]) == (
        "AMZN", 275.0, "PUT", 1.65,
    )
    assert "day_trade" in sig["tags"]


def test_zh_trim_verb_still_means_trim():
    """ZH 侧不动：减持 就是 trim（pct=33）。

    "out"→"减持" 是机翻的有损降级，靠先到的 EN 孪生拿全平，不靠猜中文措辞——
    把 减持 提级成全平会波及所有真 trim 信号。
    """
    parsed = parse_close(
        "KC交易机器人：减持亚马逊 -15%，感到无聊，周度权利金像蜗牛一样爬。",
        OPEN_SYMS,
    )
    assert parsed is not None
    assert parsed["symbols"] == ["AMZN"]
    assert parsed["pct"] == 33
    assert parsed["lang"] == "zh"


# ============================================================
# 2. 条件句 / 作者自述持有 —— SPY 误平
# ============================================================

SPY_EN_0803 = (
    "KC Trades Bot:SPY puts near entry price at the close around 1.72, if you "
    "don’t want to swing you can close until 4:15pm EST. I personally am "
    "swinging them, only have 10 contracts right now."
)
SPY_ZH_0803 = (
    "KC交易机器人：SPY看跌期权临近收盘价约1.72，若不想持仓过夜，可在美东时间"
    "下午4:15前平仓。我个人选择持仓，目前仅持有10张合约。"
)


@pytest.mark.parametrize("raw", [SPY_EN_0803, SPY_ZH_0803])
def test_conditional_close_is_not_an_instruction(raw):
    """当晚原文双语都不得成交（真实后果：1.93 进、1.63 出）。"""
    assert parse_close(raw, OPEN_SYMS) is None


@pytest.mark.parametrize("raw", [
    "if you don’t want to hold over the weekend you can close SPY here",
    "若不想持有到周末，可以在这里平仓SPY",
    "如果你不想持仓过夜，可减仓SPY",
])
def test_negated_conditional_masks_the_main_clause(raw):
    """"若不想 X，可 Y" 的动作在主句里，抹除范围必须盖到句末。"""
    assert parse_close(raw, OPEN_SYMS) is None


@pytest.mark.parametrize("raw", [
    "I personally am swinging them, SPY puts still open",
    "我个人选择持仓，SPY看跌期权继续拿着",
])
def test_author_holding_skips_whole_message(raw):
    assert parse_close(raw, OPEN_SYMS) is None


@pytest.mark.parametrize("raw,pct", [
    # 7/29 定的边界：未/此前 系的条件句是**真祈使句**，不能一起被抹掉
    ("若此前未减仓SPY，可在此处操作 @ 2.45", 33),
    # 真 trim 不受影响
    ("KC交易机器人：在2.15减仓三分之一SPY", 33),
    ("KC Trades Bot:trimmed 1/2 of my SPY here at 2.15", 50),
])
def test_real_close_instructions_still_execute(raw, pct):
    parsed = parse_close(raw, OPEN_SYMS)
    assert parsed is not None, raw
    assert parsed["pct"] == pct, raw


# ============================================================
# 3. "out 1/2" 分数路由
# ============================================================

@pytest.mark.parametrize("raw", ["$TSLA out 1/2", "$TSLA 出 1/2"])
def test_out_fraction_routes_and_parses(raw):
    assert detect_action(raw) == "CLOSE", raw
    parsed = parse_close(raw, OPEN_SYMS)
    assert parsed is not None, raw
    assert parsed["symbols"] == ["TSLA"]
    assert parsed["pct"] == 50, raw


@pytest.mark.parametrize("raw,pct", [
    ("$TSLA out 1/3", 33),
    ("$TSLA out 2/3", 67),
    ("$TSLA out 3/4", 75),
])
def test_out_fraction_other_denominators(raw, pct):
    parsed = parse_close(raw, OPEN_SYMS)
    assert parsed is not None and parsed["pct"] == pct, raw


@pytest.mark.parametrize("raw", [
    "grabbed TSLA 8/7 calls out 7/13 expiry looks better",
    "$TSLA 8/7 到期",
])
def test_expiry_dates_are_not_fractions(raw):
    """分子分母枚举成 _fraction_pct 的定义域，到期日不得被当分数路由成 CLOSE。"""
    from autotrade.parsing.close_parser import (
        _OUT_FRACTION_PATTERN, _ZH_OUT_FRACTION_PATTERN,
    )
    import re as _re
    assert not _re.search(_OUT_FRACTION_PATTERN, raw, _re.I), raw
    assert not _re.search(_ZH_OUT_FRACTION_PATTERN, raw), raw


def test_zh_out_fraction_left_boundary():
    """裸"出"只在紧跟合法分数时算动词，"退出/冲出" 不算。"""
    assert parse_close("$TSLA 退出 1/2 仓位讨论", OPEN_SYMS) is None


# ============================================================
# 4. day_trade → EOD 强平
# ============================================================

def test_day_trade_forces_eod_close():
    """8/3 AMZN：DTE=4 的 weekly，但信号写了 day trade → 必须收盘前强平。"""
    category, apply_sl, eod_force = categorize(
        date(2026, 8, 7), D, ["day_trade"],
    )
    assert (category, apply_sl) == ("weekly", True), "分类与止损口径不变"
    assert eod_force is True, "day_trade 不能再过夜"


@pytest.mark.parametrize("tags,expected", [
    ([], False),                      # 普通 weekly：行为逐字不变
    (["swing"], False),
    (["lotto"], False),               # 彩票仍然放飞
    (["scalp"], False),               # 只收 day_trade，不顺手扩到 scalp
])
def test_eod_force_matrix_otherwise_unchanged(tags, expected):
    _, _, eod_force = categorize(date(2026, 8, 7), D, tags)
    assert eod_force is expected, tags


def test_zero_dte_unchanged():
    assert categorize(D, D, [])[2] is True
    assert categorize(D, D, ["lotto"]) == ("0dte_lotto", False, True)


# ============================================================
# 5. ZH 孪生守卫（结构层，端到端）
# ============================================================
# 语义修复（第 2 节）挡的是 8/3 这一种措辞；本守卫不看措辞：
# EN 原文 60s 内已被判"非平仓指令" → 同频道同 symbol 的 ZH 机翻不执行。

CID = 424242


def _open_position(symbol: str, code_suffix: str) -> str:
    code = f"US.{symbol}{datetime.now().strftime('%H%M%S%f')}{code_suffix}"
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=100.0, side="PUT",
        expiry=date(2026, 8, 7), qty=2, fill_price=2.0,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut0803", msg_id=f"m{code_suffix}",
    )
    return code


def _reset_registries():
    dedup._close_fps.clear()
    heuristics._recent_close_skip.clear()
    heuristics._recent_exec.clear()


@pytest.mark.asyncio
async def test_zh_twin_blocked_after_en_twin_skipped():
    os.environ["DRY_RUN"] = "true"
    _reset_registries()
    code = _open_position("ZTWNA", "P100000")
    notifications, sells = [], []

    async def capture(msg):
        notifications.append(msg)

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order",
                      side_effect=lambda **kw: sells.append(kw) or {
                          "success": True, "order_id": "x", "code": code,
                          "qty": 1, "price": 1.9}):
        # EN 原文：没有动作动词 → parser 跳过，登记留痕
        await close_flow.handle_close_signal(
            "ZTWNA looking heavy into the close, watching for a flush",
            msg_id=1, channel_name="ut0803", channel_id=CID,
        )
        # ZH 机翻孪生：解析成 CLOSE 且带喊价（不靠"无价拒卖"兜底，
        # 必须证明是守卫本身拦下的）
        await close_flow.handle_close_signal(
            "ZTWNA 临近收盘走弱，关注是否减仓 @ 1.90",
            msg_id=2, channel_name="ut0803", channel_id=CID,
        )

    assert sells == [], "英文原文没说平仓，中文孪生不该下卖单"
    assert any("英文原文未判为平仓指令" in n for n in notifications), notifications
    assert positions_db.get(code)["qty_remaining"] == 2

    positions_db.record_close(code, 2, 2.0, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_en_close_after_zh_skip_still_executes():
    """方向不对称：EN 是源文本，ZH 先被跳过不能反过来堵住 EN。"""
    os.environ["DRY_RUN"] = "true"
    _reset_registries()
    code = _open_position("ZTWNB", "P100000")
    sells = []

    async def capture(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order",
                      side_effect=lambda **kw: sells.append(kw) or {
                          "success": True, "order_id": "x", "code": code,
                          "qty": 2, "price": 1.9}):
        await close_flow.handle_close_signal(
            "ZTWNB 临近收盘走弱，观察一下", msg_id=3,
            channel_name="ut0803", channel_id=CID,
        )
        await close_flow.handle_close_signal(
            "all out ZTWNB @ 1.90", msg_id=4,
            channel_name="ut0803", channel_id=CID,
        )

    assert sells, "EN 全平指令必须照常执行"

    pos = positions_db.get(code)
    if pos["status"] == "OPEN":
        positions_db.record_close(code, pos["qty_remaining"], 2.0,
                                  "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_zh_close_without_prior_en_skip_executes():
    """没有 EN 跳过留痕时，ZH 平仓信号行为逐字不变。"""
    os.environ["DRY_RUN"] = "true"
    _reset_registries()
    code = _open_position("ZTWNC", "P100000")
    sells = []

    async def capture(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order",
                      side_effect=lambda **kw: sells.append(kw) or {
                          "success": True, "order_id": "x", "code": code,
                          "qty": 1, "price": 1.9}):
        await close_flow.handle_close_signal(
            "ZTWNC 临近收盘走弱，关注是否减仓 @ 1.90", msg_id=5,
            channel_name="ut0803", channel_id=CID,
        )

    assert sells, "ZH 平仓信号本身不受影响"

    pos = positions_db.get(code)
    if pos["status"] == "OPEN":
        positions_db.record_close(code, pos["qty_remaining"], 2.0,
                                  "manual", note="ut cleanup")


def test_zh_skip_does_not_register_en_marker():
    """登记只认不含汉字的原文——ZH 版被跳过不构成"源文本说了不平"的证据。"""
    _reset_registries()
    heuristics._record_close_skip(CID, "ZTWND 临近收盘走弱", {"ZTWND"})
    assert heuristics._recent_close_skip == {}
    heuristics._record_close_skip(CID, "ZTWND looking heavy into the close", {"ZTWND"})
    assert (CID, "ZTWND") in heuristics._recent_close_skip
