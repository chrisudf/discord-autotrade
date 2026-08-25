"""0014: ZH 机翻开仓模板 Pattern D —— 双语指纹对齐回归。

背景(7/24 夜):KC ZH 孪生 "买入META 620看涨期权,4天到期,价格4.80" 归一化后
变成 "META 620 calls ,4天到期,价格4.80"——A 系列要求 strike 紧贴 c/p("620c"),
B/C 系列要求 $ 前缀,三头全落空 → ZH 版解析失败。enrich 的 ZH 版实测常比 EN
早 ~2s 到达(7/14),ZH 不可解析意味着白等 EN;当晚 EN 版又恰好被 "into the
close" 误路由 CLOSE,双保险同时失效,一个完整可解析的开仓信号就此丢失。
7/28 夜同型再现:"SPY 745看涨期权 4DTE @ 3.15 日内交易"。

成功判据(契约 WP-E 硬要求):ZH 解析结果与 EN 孪生的
(symbol, strike, side, expiry_date) 完全一致——这四个字段正是 dedup 的
_signal_fingerprint 的全部成分,任何一个不一致,中英双发就拿到不同指纹,
5 分钟去重窗拦不住 → 同一信号双执行(双倍仓位)。
"""
from datetime import date

from autotrade.listener.dedup import _signal_fingerprint
from autotrade.parsing.signal_parser import detect_action, parse_signal

# ============================================================
# 语料(逐字):7/24 夜 META、7/28 夜 SPY,EN/ZH 孪生各一对
# ============================================================
MSG_TS_0724 = date(2026, 7, 23)
MSG_TS_0728 = date(2026, 7, 27)

META_EN = (
    "@everyone\nKC Trades Bot:META 620c 4DTE @ 4.80 "
    "little day trade into the close for fun"
)
META_ZH = (
    "@everyone\n:red_circle:KC交易机器人：买入META 620看涨期权，"
    "4天到期，价格4.80，收盘前做点日内交易玩玩。"
)

SPY_EN = (
    "@everyone\nKC Trades Bot:SPY 745c 4DTE @ 3.15 day trade, "
    "can add at PDL on SPY/SPX"
)
SPY_ZH = "@everyone\n:red_circle:KC交易机器人：SPY 745看涨期权 4DTE @ 3.15 日内交易"


def _fp_tuple(sig: dict) -> tuple:
    return (sig["symbol"], sig["strike"], sig["side"], sig["expiry_date"])


# ============================================================
# 1. 7/24 META ZH 模板:完整解析
# ============================================================

def test_meta_zh_template_parses():
    """当晚静默丢失的 ZH 孪生,现在必须完整解析(Pattern D)。"""
    sig = parse_signal(META_ZH, msg_ts=MSG_TS_0724)
    assert sig is not None and not sig.get("skip")
    assert sig["symbol"] == "META"
    assert sig["strike"] == 620.0
    assert sig["side"] == "CALL"
    assert sig["price"] == 4.8
    # "4天到期" 复用 NDTE 语义:7/23(周四) + 4 天 = 7/27(周一),与 0724 夜
    # EN 侧既有快照(test_overnight_0724)一致
    assert sig["expiry_date"] == date(2026, 7, 27)
    # "日内交易" 含 "日内" → 既有 zh_tag_map 覆盖,category 才不会错挂 SL/EOD
    assert "day_trade" in sig["tags"]


def test_meta_zh_routes_open_not_close():
    """"收盘前" 是时间状语,ZH 版不得被误路由 CLOSE(EN 版同案已在 0724 回归)。"""
    assert detect_action(META_ZH) != "CLOSE"


def test_meta_bilingual_fingerprint_parity():
    """契约硬要求:EN/ZH 孪生 (symbol,strike,side,expiry_date) 完全一致,
    否则 dedup 指纹不同 → 双语双执行。"""
    en = parse_signal(META_EN, msg_ts=MSG_TS_0724)
    zh = parse_signal(META_ZH, msg_ts=MSG_TS_0724)
    assert en is not None and not en.get("skip")
    assert zh is not None and not zh.get("skip")
    assert _fp_tuple(en) == _fp_tuple(zh)
    # 直接用生产 dedup 的指纹函数对齐,防止字段级相等但序列化口径漂移
    assert _signal_fingerprint(en) == _signal_fingerprint(zh)


# ============================================================
# 2. 7/28 SPY ZH 孪生(NDTE 直接出现在 ZH 模板里)
# ============================================================

def test_spy_zh_template_parses():
    sig = parse_signal(SPY_ZH, msg_ts=MSG_TS_0728)
    assert sig is not None and not sig.get("skip")
    assert sig["symbol"] == "SPY"
    assert sig["strike"] == 745.0
    assert sig["side"] == "CALL"
    assert sig["price"] == 3.15
    # 7/27(周一) + 4 天 = 7/31(周五),交易日不调整
    assert sig["expiry_date"] == date(2026, 7, 31)
    assert "day_trade" in sig["tags"]


def test_spy_bilingual_fingerprint_parity():
    en = parse_signal(SPY_EN, msg_ts=MSG_TS_0728)
    zh = parse_signal(SPY_ZH, msg_ts=MSG_TS_0728)
    assert en is not None and not en.get("skip")
    assert zh is not None and not zh.get("skip")
    assert _fp_tuple(en) == _fp_tuple(zh)
    assert _signal_fingerprint(en) == _signal_fingerprint(zh)


def test_contract_normalized_form_verbatim():
    """契约引用的归一化后形态逐字可解析(ASCII 逗号变体,
    证明 Pattern D 不依赖 "看涨期权" 归一化是否已经跑过)。"""
    sig = parse_signal("META 620 calls ,4天到期,价格4.80", msg_ts=MSG_TS_0724)
    assert sig is not None and not sig.get("skip")
    assert _fp_tuple(sig) == ("META", 620.0, "CALL", date(2026, 7, 27))
    assert sig["price"] == 4.8


# ============================================================
# 3. "N天到期" 与 NDTE 必须同一条 expiry 路径(含假日回退)
# ============================================================

def test_zh_days_to_expiry_weekend_adjust_parity():
    """7/23(周四) + 2 天 = 7/25(周六) → 假日回退到 7/24(周五)。
    EN 2DTE 与 ZH "2天到期" 必须落到同一天——否则孪生指纹分叉。"""
    en = parse_signal("META 620c 2DTE @ 4.80", msg_ts=MSG_TS_0724)
    zh = parse_signal("买入META 620看涨期权，2天到期，价格4.80", msg_ts=MSG_TS_0724)
    assert en is not None and not en.get("skip")
    assert zh is not None and not zh.get("skip")
    assert zh["expiry_date"] == date(2026, 7, 24)
    assert _fp_tuple(en) == _fp_tuple(zh)
    assert _signal_fingerprint(en) == _signal_fingerprint(zh)


def test_zh_puts_variant():
    """"看跌期权" → PUT,方向不能吃错(吃错方向比漏单更糟)。"""
    sig = parse_signal("买入SPY 740看跌期权，4天到期，价格2.10", msg_ts=MSG_TS_0728)
    assert sig is not None and not sig.get("skip")
    assert _fp_tuple(sig) == ("SPY", 740.0, "PUT", date(2026, 7, 31))
    assert sig["price"] == 2.1


# ============================================================
# 4. 保守边界:模板五要素缺一不命中(契约铁律 2,宁错过不错杀)
# ============================================================

def test_missing_price_no_match():
    """缺价:不半猜下单,走解析失败路径(None → listener 大声告警)。"""
    assert parse_signal("买入META 620看涨期权，4天到期", msg_ts=MSG_TS_0724) is None


def test_missing_expiry_no_match():
    assert parse_signal("买入META 620看涨期权，价格4.80", msg_ts=MSG_TS_0724) is None


def test_missing_side_no_match():
    assert parse_signal("买入META 620，4天到期，价格4.80", msg_ts=MSG_TS_0724) is None


def test_missing_strike_no_match():
    assert parse_signal("买入META 看涨期权，4天到期，价格4.80", msg_ts=MSG_TS_0724) is None


def test_lowercase_word_not_ticker():
    """同 A 系列 7/8 教训:IGNORECASE 下小写单词不是 ticker。"""
    assert parse_signal("buy 620 calls 4DTE @ 3.15", msg_ts=MSG_TS_0728) is None


def test_zh_month_day_form_out_of_scope():
    """"7月15日" 型 ZH 日期无实测语料,契约明确不扩——落到告警而非猜单。"""
    assert parse_signal(
        "买入MU 1050看涨期权，7月15日到期，价格2.60", msg_ts=date(2026, 7, 10)
    ) is None


# ============================================================
# 7/28 对抗评审:Pattern D 两个 should-fix(限定价格 + 跨 ticker 拼单)
# ============================================================

def test_pattern_d_qualified_price_not_order():
    """目标/止损/目前/当前/现 价格 是评论,不是喊单——不下单
    (对抗评审实锤:旧版把"目标价格6.00"当 entry 买入)。"""
    for text in (
        "SPY 745看涨期权 4DTE，目标价格6.00",
        "SPY 745看涨期权 4DTE 止损价格2.40",
        "SPY 745看涨期权 4DTE 目前价格6.00，拿住",
        "SPY 745看涨期权 4DTE 当前价格6.00",
        "SPY 745看涨期权 4DTE 现价6.00",
    ):
        s = parse_signal(text, msg_ts=date(2026, 7, 27))
        assert s is None or s.get("skip"), text


def test_pattern_d_bare_price_still_orders():
    """裸"价格N"(入场语义)不受限定词排除影响,仍要命中。"""
    s = parse_signal("买入META 620看涨期权，4天到期，价格4.80", msg_ts=date(2026, 7, 24))
    assert s is not None and not s.get("skip")
    assert s["price"] == 4.8


def test_pattern_d_no_cross_ticker_stitch():
    """要素窗口不得跨越第二个 ticker / 子句 / 句读拼出混合单
    (对抗评审实锤:"META 620...，SPY 4DTE @ 3.15" 拼成 META 的 SPY-DTE 单)。"""
    for text in (
        "META 620看涨期权，SPY 4DTE @ 3.15 更好",
        "META 620看涨期权表现不错，SPY那张4天到期，价格4.80",
        "另外那张QQQ的4天到期，价格4.80",
    ):
        s = parse_signal(text, msg_ts=date(2026, 7, 27))
        assert s is None or s.get("skip"), text


# ============================================================
# [7/29 对抗测试] 限定价防护:`@` 分支原本零防护
# ============================================================
def test_pattern_d_qualified_at_price_not_order():
    """`目标/止损 @ 价` 是评论,不是喊单——不下单。

    实锤(7/29 对抗测试):`价格` 分支有 lookbehind (?<![标损前的现]),
    但 **`@` 分支一个防护都没有** → "AMD 210看涨期权 4天到期 目标 @ 4.50"
    按 4.50 下单(真实 entry 3.00,溢价 50%),且 4.50 低于
    MAX_PRICE_PER_CONTRACT 熔断线,风控接不住。

    更要命的是它是**单边**错单:按 docstring,ZH 孪生常比 EN 早 ~2s 到,
    ZH 误解析先执行、EN 版压根不匹配 → 没有孪生来纠正。
    """
    for text in (
        "AMD 210看涨期权 4天到期 目标 @ 4.50",
        "AMD 210看涨期权 4天到期 止损 @ 2.10",
        "META 620看涨期权 4天到期 目标价 @ 6.00",
        "META 620看涨期权 4天到期 现价 @ 6.00",
        "META 620看涨期权 4天到期 当前 @ 6.00",
        "META 620看涨期权 4天到期 止盈 @ 8.00",
    ):
        s = parse_signal(text, msg_ts=date(2026, 7, 27))
        assert s is None or s.get("skip"), text


def test_pattern_d_en_qualified_price_not_order():
    """EN 限定词同样不得当 entry。

    这条钉的是**设计**而非巧合:改前 EN "target @ 6.00" 被拒纯粹是
    _GAP 字母墙的副作用(否定字符类 + IGNORECASE 连小写一起排除),
    一旦有人"照注释修正" _GAP 成只挡大写,这个洞立刻打开(实测验证过)。
    现在 _price_qualified 是第二道独立防线。
    """
    for text in (
        "META 620 calls 4DTE target @ 6.00",
        "META 620 calls 4DTE stop loss @ 2.40",
        "META 620 calls 4DTE current @ 6.00",
    ):
        s = parse_signal(text, msg_ts=date(2026, 7, 27))
        assert s is None or s.get("skip"), text


def test_pattern_d_qualified_price_blocklist_extended():
    """`价格` 分支的限定词表补全:旧 lookbehind 只看紧邻一个字,
    "预期价格6.00" 这类漏网(实测旧版当 entry 下单)。"""
    for text in (
        "META 620看涨期权,4天到期,预期价格6.00",
        "META 620看涨期权,4天到期,目标价格6.00",
    ):
        s = parse_signal(text, msg_ts=date(2026, 7, 27))
        assert s is None or s.get("skip"), text


def test_pattern_d_now_at_price_still_orders():
    """"now @ 价" 是合法开仓,不能被限定词表误伤——
    _PRICE_QUALIFIERS 特意不含 "now"。"""
    s = parse_signal(
        "buying NOW META 620 calls 4DTE @ 3.15", msg_ts=date(2026, 7, 27),
    )
    assert s is not None and not s.get("skip")
    assert s["price"] == 3.15


def test_gap_rewrite_is_behavior_preserving():
    """_GAP 由 [^A-Z...]+IGNORECASE 改写成显式 [^A-Za-z...] 必须逐字等价。

    改写目的是消灭"注释说挡大写、实际挡所有字母"的陷阱,**不是**放宽召回:
    要素间夹任何英文单词依旧不命中(没有语料要求支持,按 0012 的规矩
    先加语料行再改 parser)。
    """
    import re as _re
    from autotrade.parsing.signal_parser import _GAP
    old = _re.compile(r"^(?:[^A-Z\n。！？；.!?]|[A-Z](?![A-Z]))*?$", _re.IGNORECASE)
    new = _re.compile(r"^" + _GAP + r"$", _re.IGNORECASE)
    for probe in ("，", " 4天到期 ", "目标 @ ", "in ", "X ", "AB", "ab", "。"):
        assert bool(old.match(probe)) == bool(new.match(probe)), probe
    # 夹英文单词仍不命中(召回未放宽)
    assert parse_signal("META 620 calls in 4DTE @ 3.15", msg_ts=date(2026, 7, 27)) is None
