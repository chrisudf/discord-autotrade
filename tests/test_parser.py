from datetime import date
import pytest
from autotrade.parsing.signal_parser import parse_signal

# 固定 msg_ts，避免测试随今天日期飘
FIXED_TODAY = date(2026, 6, 17)  # 周三，非假日


def test_basic_call():
    r = parse_signal("Lotto $IREN 0DTE $60 calls $.68", msg_ts=FIXED_TODAY)
    assert r["symbol"] == "IREN"
    assert r["side"] == "CALL"
    assert r["strike"] == 60.0
    assert r["price"] == 0.68
    # NDTE 被 _finalize_signal 覆盖成 M/D（与 expiry_date 对齐）
    assert r["expiry"] == "6/17"
    assert r["expiry_date"] == date(2026, 6, 17)


def test_put():
    r = parse_signal("$TSLA 1DTE $250 puts $1.20", msg_ts=FIXED_TODAY)
    assert r["symbol"] == "TSLA"
    assert r["side"] == "PUT"
    assert r["strike"] == 250.0
    assert r["price"] == 1.20
    # 6/18 周四，是交易日，不被假日调整
    assert r["expiry"] == "6/18"


def test_invalid():
    assert parse_signal("hello world") is None


def test_scalp_puts_with_modifier():
    """`$SPY $746 scalp puts $.40` —— strike 和 puts 之间有 'scalp' 修饰词

    旧 pattern `\\s*(calls?|puts?)` 太严，scalp 卡住返回 None。
    现在允许中间最多 3 个修饰词。
    """
    r = parse_signal("$SPY $746 scalp puts can work here $.40 - in and out",
                     msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "SPY"
    assert r["side"] == "PUT"
    assert r["strike"] == 746.0
    assert r["price"] == 0.40


def test_weekly_modifier():
    """`$AAOI weekly $220 calls for $2.05`"""
    r = parse_signal("$AAOI weekly $220 calls for $2.05", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "AAOI"
    assert r["strike"] == 220.0
    assert r["price"] == 2.05


def test_detect_action_zh_close():
    """中文 close 关键字应被识别为 CLOSE，不能漏到 OPEN parser"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("KC交易机器人：减仓特斯拉 3.00") == "CLOSE"
    assert detect_action("$AAOI 全部卖出") == "CLOSE"
    assert detect_action("平仓微软420看涨 @ 7.00") == "CLOSE"
    # 纯 OPEN 信号不能误判
    assert detect_action("TSLA 415c June 26 @ 2.70") == "OPEN"
    assert detect_action("$AAOI weekly $220 calls $2.05") == "OPEN"


def test_detect_action_en_gerund_and_phrases():
    """7/3 复盘发现：'closing' 等 gerund 和 'all out'/'out half' 等短语应识别为 CLOSE

    历史漏检 case（重放）：
    - 7/3 03:20 `closing the MSFT 390c runner here at 5.00` → 之前误路由到 OPEN
    - 6/30 02:51 `all out TSLA 420c runner @ 15.35` → 同样
    - 6/29 `Out half MSFT @ 2.90` → 同样
    """
    from autotrade.parsing.signal_parser import detect_action
    # gerund 形式
    assert detect_action("closing the MSFT 390c runner here at 5.00") == "CLOSE"
    assert detect_action("scaling out MSFT here") == "CLOSE"
    # 多词 phrase
    assert detect_action("all out TSLA 420c runner @ 15.35") == "CLOSE"
    assert detect_action("out half MSFT @ 2.90") == "CLOSE"
    assert detect_action("out full on TSLA 420c") == "CLOSE"
    assert detect_action("out majority SPY here @ 3.00") == "CLOSE"


def test_detect_action_open_not_falsely_matched():
    """反向：真 OPEN 信号不能因为新加的关键字被误判为 CLOSE"""
    from autotrade.parsing.signal_parser import detect_action
    # 边缘：含 "close" 字符串但显然是开仓文本
    assert detect_action("MSFT 390c 7/6 small @ 2.30") == "OPEN"
    assert detect_action("Adding $APLD 50c weeklies @ .98") == "OPEN"
    assert detect_action("$SPY $748 calls @ $2.40 close to breakout") == "OPEN"
    # ^ [7/24 patch] 原钉死为 CLOSE(允许假阳,注释称"KC 从不用这种表达")——
    #   7/24 "META ... into the close for fun" 实锤该假设在类级别上不成立,
    #   一个可解析的开仓信号被误路由丢失。STRONG_CLOSE_RE 现对裸名词 close
    #   加边界(the/at/into/before/near/after 前置、"close to" 后置都不算动词),
    #   此文本按其本义路由 OPEN;真动词用法见 test_overnight_0724。


def test_tag_day_trade_variants():
    """'small day trade' / 'daytrade' / 'day-trade' 都应该 tag day_trade"""
    r = parse_signal("TSLA 415c June 26 @ 2.70 small day trade",
                     msg_ts=FIXED_TODAY)
    assert "day_trade" in r["tags"]

    r = parse_signal("NOW 115c June 26 small fun day trade here @ 2.00",
                     msg_ts=FIXED_TODAY)
    assert "day_trade" in r["tags"]

    # daytrade（无空格）
    r = parse_signal("$SPY $746 daytrade puts $.40", msg_ts=FIXED_TODAY)
    assert "day_trade" in r["tags"]


def test_tag_no_day_trade_when_swing():
    """普通 swing 信号不该被打 day_trade tag"""
    r = parse_signal("MRVL 400c 7/2 @ 6.95 smaller size swing for now",
                     msg_ts=FIXED_TODAY)
    assert "day_trade" not in r["tags"]
    assert "swing" in r["tags"]


# === Pattern C: 简写格式（6/22 APLD 漏接修复）===

def test_apld_shorthand_weeklies():
    """6/22 凌晨 enrich 频道 APLD 漏接，原始文本：
    'Adding $APLD 50c weeklies here @role_1362783378704699603 +alert .98 fill'
    """
    r = parse_signal(
        "Adding $APLD 50c weeklies here @role_1362783378704699603 +alert .98 fill",
        msg_ts=FIXED_TODAY,
    )
    assert r is not None, "APLD 简写信号应该被识别"
    assert r["symbol"] == "APLD"
    assert r["side"] == "CALL"
    assert r["strike"] == 50.0
    assert r["price"] == 0.98


def test_shorthand_put_mmdd():
    """简写 + 显式日期：$SYMBOL Nc/p MM/DD ... .XX fill"""
    r = parse_signal("$SNOW 215p 7/2 .85 fill", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "SNOW"
    assert r["side"] == "PUT"
    assert r["strike"] == 215.0
    assert r["price"] == 0.85
    assert r["expiry"] == "7/2"


def test_shorthand_at_price():
    """@$X.XX 价格写法"""
    r = parse_signal("Adding $NVDA 800c weeklies @ $1.20", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "NVDA"
    assert r["strike"] == 800.0
    assert r["price"] == 1.20


def test_shorthand_at_price_no_dollar():
    """@.98 不带 $"""
    r = parse_signal("$IREN 60c weeklies @ .68", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "IREN"
    assert r["price"] == 0.68


def test_shorthand_does_not_match_role_mention():
    """@role_数字 不应该被当成价格"""
    # 仅有 @role 没有真价格 → 不匹配
    r = parse_signal("Adding $APLD 50c weeklies @role_1362783378704699603 alert",
                     msg_ts=FIXED_TODAY)
    assert r is None, "没有合法价格写法应该返回 None"


def test_shorthand_requires_dollar_prefix():
    """裸 SYMBOL Nc/p 没有 $ 前缀不应该被 Pattern C 抓（避免假阳）。

    `APLD 50c` 在 A 路径需要 MM/DD，C 路径要求 $ 前缀，两个都不命中 → None
    """
    r = parse_signal("APLD 50c weeklies .98 fill", msg_ts=FIXED_TODAY)
    assert r is None


# === 6/23 APLD "holding up well" skip 误伤回归 ===
# 修改 SKIP_KEYWORDS 后：精确短语 skip status 消息，"holding up well" 不再误伤

def test_apld_with_holding_up_well_NOT_skipped():
    """6/23 漏接原文：'$APLD weekly $50 calls $.66 ... holding up well'

    'holding up well' 是描述价格走势，不该 skip。
    """
    text = ("enrich:\nUsing some $IBM gains - lotto sized (1%)\n\n"
            "$APLD weekly $50 calls $.66\n\n"
            "Risky business - holding up well though\n\n"
            "@everyone $alert")
    r = parse_signal(text, msg_ts=FIXED_TODAY)
    assert r is not None and r.get("symbol") == "APLD", (
        f"APLD 信号应该被解析，实际: {r}"
    )
    assert r["strike"] == 50.0
    assert r["price"] == 0.66
    assert r["side"] == "CALL"


def test_holding_status_phrases_still_skipped():
    """status 短语仍然正确 skip，不变成"无效信号下单"风险源"""
    cases = [
        "I'm holding into tomorrow",
        "still holding my SNOW calls",
        "currently holding 3 contracts",
        "keep holding the position",
        "holding my other half overnight",
        "持仓 +30%",
    ]
    for c in cases:
        r = parse_signal(c, msg_ts=FIXED_TODAY)
        assert r is None or (isinstance(r, dict) and r.get("skip") == "holding_or_remaining"), (
            f"应 skip 但没 skip: {c} → {r}"
        )


def test_holding_up_well_with_no_signal_returns_none():
    """没有信号语法的 status 消息：之前 skip，现在 parse fail → None。
    不会下错单（无 SYMBOL+strike+price 三件套）。
    """
    r = parse_signal("Stock is holding up well today, no setups yet", msg_ts=FIXED_TODAY)
    assert r is None  # 没法 parse 出 OPEN 信号


def test_invalid_calendar_date_returns_none():
    """6/31 不存在 → 按解析失败处理（返回 None），绝不能抛 ValueError 炸掉链路。"""
    assert parse_signal("$TSLA 250c 6/31 @ 1.20", msg_ts=FIXED_TODAY) is None


def test_feb30_returns_none():
    assert parse_signal("SPY 600c 2/30 @ .55", msg_ts=FIXED_TODAY) is None


def test_feb29_non_leap_skips_to_valid_year():
    """2/29 在 2026/2027/2025 都无效 → 三个候选年全跳过 → None（不炸）。"""
    assert parse_signal("$NVDA 150c 2/29 @ 2.00", msg_ts=FIXED_TODAY) is None


# === detect_action 强/弱关键词回归 ===

def test_detect_action_closing_bell_is_open():
    """'closing bell' 是时间状语——带买入动词的消息必须路由 OPEN。
    回归：旧版 'closing' 无条件命中 → 反向卖出。"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("Buying $QQQ 560c into the closing bell @ 1.35") == "OPEN"


def test_detect_action_going_all_out_is_open():
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("Going all out on $NVDA 200c here @ 3.50") == "OPEN"


def test_detect_action_closing_runner_still_close():
    """7/3 案例：无开仓动词的 'closing ...' 仍然路由 CLOSE。"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("closing the MSFT 390c runner here at 5.00") == "CLOSE"


def test_detect_action_all_out_still_close():
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("all out TSLA @ 8.05") == "CLOSE"


def test_detect_action_selling_without_open_verb_is_close():
    """回归：'Selling $MSFT 390c @ 5.20' 旧版走 OPEN parser → Pattern C 误买入。"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("Selling $MSFT 390c here @ 5.20") == "CLOSE"


# === Pattern C 收紧回归 ===

def test_pattern_c_ignores_target_price_commentary():
    """回归：裸 '$5' 目标价曾被 Pattern C 当 entry → 评论变买单。"""
    assert parse_signal(
        "Chart update: $MSFT 390c looking great, target $5", msg_ts=FIXED_TODAY
    ) is None


def test_pattern_c_half_size_not_expiry():
    """回归：'1/2 size' 曾被当成 1 月 2 日 → 跨年推到下一年。"""
    r = parse_signal("Adding $APLD 50c weeklies 1/2 size @ .98", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["price"] == 0.98
    # weekly 默认下一个周五 6/19，Juneteenth 假日前移到 6/18
    assert r["expiry_date"] == date(2026, 6, 18)


def test_pattern_c_fill_style_still_works():
    r = parse_signal("$APLD 50p 7/2 .85 fill", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["side"] == "PUT"
    assert r["price"] == 0.85
    assert r["expiry_date"] == date(2026, 7, 2)


# === holding 状态贴回归 ===

def test_holding_dollar_ticker_skipped():
    r = parse_signal("Holding $APLD 50c weeklies @ .98 into CPI", msg_ts=FIXED_TODAY)
    assert r == {"skip": "holding_or_remaining"}


def test_holding_bare_ticker_skipped():
    r = parse_signal("Holding TSLA 420c 7/11 from 2.50 now @ 4.20", msg_ts=FIXED_TODAY)
    assert r == {"skip": "holding_or_remaining"}


def test_holding_up_well_not_skipped():
    """6/23 回归方向不变：'holding up well' 是走势评论，真信号仍要解析。"""
    r = parse_signal("$APLD weekly $50 calls $.66 - holding up well", msg_ts=FIXED_TODAY)
    assert r is not None
    assert r["symbol"] == "APLD"


# === 7/8 复盘回归 ===

def test_lowercase_word_not_ticker():
    """'taking AAPL again but 300p 7/17 @ 1.50' —— 小写 'but' 不是 ticker BUT。

    IGNORECASE 让 [A-Z]{1,5} 实际也吃小写，垃圾单 US.BUT... 曾真实提交
    （被预校验 'Unknown stock' 拦下）。无 gap-tolerant 关联时信号仍为 None
    → 走 listener 的 looks-like-signal TG 告警人工接住；绝不能出 BUT 单。
    """
    r = parse_signal("taking AAPL again but 300p 7/17 @ 1.50 for a small swing",
                     msg_ts=FIXED_TODAY)
    assert r is None


def test_detect_action_trimming_gerund():
    """'Start trimming. Down to 1/2.' —— trimming 必须进 close 路由
    （\\btrim\\b 匹配不到 gerund，7/8 实测漏路由）。"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("$DELL - Congrats all. Start trimming. Down to 1/2.") == "CLOSE"


def test_detect_action_stop_at_entry_not_open_intent():
    """'out half 3.22 stop at entry' —— 'at entry' 是止损备注不是开仓意图，
    不能把弱 close 路由压掉（7/8 实测）。"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("out half 3.22 💰 stop at entry") == "CLOSE"


def test_detect_action_re_enter_not_open_intent():
    """'will look to re-enter' —— re-enter 是未来意图，\\benter\\b 在连字符处
    误命中导致 'all out apple' 没进 close 路由（7/7 实测）。"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action(
        "all out apple to secure green trade 🙏🏼 will look to re-enter again "
        "for a put swing again"
    ) == "CLOSE"


def test_detect_action_zh_chuqing():
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("全部出清苹果仓位，确保交易盈利") == "CLOSE"


# ============ ZH 方向词归一化（7/14 复盘：enrich ZH 版先到但解析不了） ============

ZH_TODAY = date(2026, 7, 14)  # 周二，非假日


def test_zh_call_word_enrich_hood():
    """7/14 实测原文：ZH 版比 EN 版早 ~2s，之前差一个方向词全 pattern 落空。"""
    r = parse_signal(
        "enrich:\n$HOOD - 7/24 $125 看涨期权 $1.50\n\n2% 头寸 \n\n@everyone $alert",
        msg_ts=ZH_TODAY,
    )
    assert r is not None and not r.get("skip")
    assert r["symbol"] == "HOOD"
    assert r["side"] == "CALL"
    assert r["strike"] == 125.0
    assert r["price"] == 1.50
    assert r["expiry_date"] == date(2026, 7, 24)


def test_zh_call_word_enrich_googl():
    """7/14 实测原文：strike 带小数 + 价格 $.80 简写 + 方向词后接汉字。"""
    r = parse_signal(
        "enrich:\n$GOOGL 7/15 $362.50 看涨期权 - 逐步增加到 $.80\n\n@everyone $alert",
        msg_ts=ZH_TODAY,
    )
    assert r is not None and not r.get("skip")
    assert r["symbol"] == "GOOGL"
    assert r["side"] == "CALL"
    assert r["strike"] == 362.5
    assert r["price"] == 0.80
    assert r["expiry_date"] == date(2026, 7, 15)


def test_zh_put_word():
    r = parse_signal("$SPY 7/17 $740 看跌期权 $2.10", msg_ts=ZH_TODAY)
    assert r is not None and not r.get("skip")
    assert r["side"] == "PUT"
    assert r["strike"] == 740.0


def test_zh_side_word_attached_no_space():
    """"$362.50看涨期权" 紧贴写法——归一化补了空格，pattern 仍应命中。"""
    r = parse_signal("$GOOGL 7/15 $362.50看涨期权 $.80", msg_ts=ZH_TODAY)
    assert r is not None and not r.get("skip")
    assert r["side"] == "CALL"


def test_zh_bare_kanzhang_not_converted():
    """裸"看涨"（无"期权"后缀）是行情评论用词，不得触发方向归一化。"""
    r = parse_signal("我看涨大盘，$SPY 目标 $750", msg_ts=ZH_TODAY)
    assert r is None or r.get("skip")


# ============ detect_action：否定式开仓词 + all out 提级（7/15） ============

def test_detect_action_all_out_with_negated_add():
    """7/15 实测："not adding" 的 adding 曾一票否决 WEAK 'all out' → 误判 OPEN，
    KC -11% 离场我们没跟。现在 all out 是 STRONG，且否定式不算开仓意图。"""
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("@everyone\nKC Trades Bot:all out SPY -11% not adding") == "CLOSE"


def test_detect_action_going_all_out_still_open():
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("I'm going all out tomorrow, loading calls") == "OPEN"


def test_detect_action_weak_close_with_negated_buy():
    from autotrade.parsing.signal_parser import detect_action
    # weak "selling" + 否定式 "not buying" → 否定形不该否决 close
    assert detect_action("selling some here, not buying more") == "CLOSE"
    # 真开仓意图仍然否决 weak close："selling puts to buy calls"
    assert detect_action("selling my house and buying TSLA calls") == "OPEN"


def test_zh_holding_keywords_skip():
    """7/15："只持有我的 $HOOD 7/24 $125 看涨期权" 归一化后带全三件套，
    曾触发 looks-like-signal 误报；EN 孪生 "Only holding my" 正确 skip。"""
    r = parse_signal(
        "enrich:\n只持有我的 $HOOD 7/24 $125 看涨期权 - 喜欢这个日线图。\n\n"
        "1.5% 的仓位。真是一天。\n\n@everyone $alert",
        msg_ts=date(2026, 7, 15),
    )
    assert r is not None and r.get("skip") == "holding_or_remaining"


# ============ A3: 裸 ticker + NDTE（7/17 漏掉 KC +200% 主力单） ============

def test_bare_ticker_ndte():
    """"SPY 745p 8DTE @ 2.66"——A 系列只认 M/D、B 系列要 $ 前缀，曾两头落空。"""
    r = parse_signal(
        "@everyone\nKC Trades Bot:SPY 745p 8DTE @ 2.66 potential swing, "
        "can add to these later",
        msg_ts=date(2026, 7, 16),
    )
    assert r is not None and not r.get("skip")
    assert r["symbol"] == "SPY"
    assert r["side"] == "PUT"
    assert r["strike"] == 745.0
    assert r["price"] == 2.66
    assert r["expiry_date"] == date(2026, 7, 24)  # 7/16(周四) + 8 天


def test_bare_ticker_ndte_lowercase_word_not_ticker():
    # "but 15p 3DTE" 之类：小写单词不是 ticker（与 A1/A2 同规则）
    r = parse_signal("nothing here but 15p 3DTE @ 1.00", msg_ts=date(2026, 7, 16))
    assert r is None or r.get("skip")


# ============ ZH tags（7/17 ARM ZH 先执行但 tags=[] → category 错） ============

def test_zh_tags_extracted():
    r = parse_signal(
        "enrich:\n彩票头皮 - $ARM $272.50 看涨期权 $1.30\n\n@everyone $alert",
        msg_ts=date(2026, 7, 17),
    )
    assert r is not None and not r.get("skip")
    assert "lotto" in r["tags"] and "scalp" in r["tags"]


def test_zh_tag_swing():
    r = parse_signal(
        "@everyone\nKC交易机器人：SPY 745p 8DTE @ 2.66 潜在波段",
        msg_ts=date(2026, 7, 16),
    )
    assert r is not None and not r.get("skip")
    assert "swing" in r["tags"]


# ============ lock-in 止盈口头禅（7/17 XOM/ARM 双漏） ============

def test_detect_action_lock_in_variants():
    from autotrade.parsing.signal_parser import detect_action
    assert detect_action("enrich:\n$XOM LOCK THEM ALL ON\n\n@everyone $alert") == "CLOSE"
    assert detect_action("$ARM - Cheers, lock them in!") == "CLOSE"
    assert detect_action("丰富：\n$XOM 全部锁定\n\n@everyone $警报") == "CLOSE"


def test_detect_action_locked_past_tense_is_recap():
    from autotrade.parsing.signal_parser import detect_action
    # 过去式 "locked in 200%" 是 PnL 复盘，不是平仓动作
    assert detect_action("locked in 200% on my runners today, what a day") == "OPEN"
