"""parse-fail 启发式与双语孪生侦测（拆自 src/listener/discord_client.py）。

_looks_like_* 系列启发式控制 TG 告警噪音；twin 侦测（_recent_exec）负责
EN/ZH 翻译孪生的 parse-fail 报警抑制 + close 翻译孪生防护。
"""
from datetime import datetime, timedelta, timezone

from autotrade.position import manager as position_mgr
from autotrade.utils.logger import logger

# ============================================================
# 启发：判断"看起来像信号"，控制 TG 噪音
# ============================================================
# 背景：6/24 夜里 11 条 "Parse failed" TG 全是 KC 闲聊（"$RKLB - Boom."、
# "AMZN +50% who got paid?!" 之类），实际不是漏检信号，但每条都炸 TG。
# 改为：parse 失败时 → 只在文本"看起来真的想发信号"才 TG，否则 log.warn 收尾。

import re as _re

# 现成的"开仓"句法特征：含 TICKER + (Nc/p|calls/puts) + 价格-like 数字
# ticker 同时接受 $ 前缀和裸大写（parser 的 Pattern A 本身就是裸 ticker 语法，
# 只认 $ 会把 "TSLA 250c 7/11 @ 1.20 好像没接住" 这类真漏检静默掉）。
# 裸大写词（BANG/OK 等）会带来一点过报，但这只是 TG 告警闸门，宁多勿漏。
# 7/24 实锤:ZH 机翻 "买入META 620看涨期权" 里 ticker 紧贴汉字,\b 不触发
# (CJK 也是 \w)——裸 ticker 边界改用显式 alnum lookaround,
# 与 _twin_of_recent_exec / close_parser.BARE_SYM_PATTERN_ZH 同款做法。
_OPEN_TICKER_RE = _re.compile(
    r"\$[A-Z]{1,5}\b|(?<![A-Za-z0-9])[A-Z]{2,5}(?![A-Za-z0-9])"
)
# ZH 方向词不带 \b（汉字间无 word boundary）。parser 已归一化 看涨/看跌期权，
# 这里兜的是 parser 因**其他**原因失败的 ZH 信号——有方向词就该报
# "looks like signal"，而不是掉进 sized-entry 的"无 C/P 方向"（7/14 HOOD 误报）
_OPEN_SIDE_RE = _re.compile(
    r"\b\d+(?:\.\d+)?[cp]\b|\bcalls?\b|\bputs?\b|看[涨跌]期权", _re.I
)
# 价格写法：$X.XX / @X.XX / .98 fill / .98 filled
_OPEN_PRICE_RE = _re.compile(
    r"\$\.?\d+(?:\.\d+)?"
    r"|@\s*\$?\.?\d+(?:\.\d+)?"
    r"|\.?\d+(?:\.\d+)?\s*fill(?:ed)?"
    # 7/24 实锤：ZH 机翻把 "@ 4.80" 写成 "价格4.80"（META 620看涨期权,4天到期,
    # 价格4.80）——三件套里价格这件不认，ZH 独失败时连 looks-like-signal 的
    # 大声告警都没有。补 价格N / N美元 两种机翻形态；仍受三件套整体门控，
    # 不会放大闲聊误报。
    r"|价格\s*\$?\.?\d+(?:\.\d+)?"
    r"|\.?\d+(?:\.\d+)?\s*美元",
    _re.I,
)

# bot 名前缀会污染裸 ticker 启发式："KC Trades Bot:" 里的 "KC" 命中
# `\b[A-Z]{2,5}\b`，让每条带价格的无 symbol 跟单 trim 都触发 TG
# （7/8 一夜 ~10 条 "parser skipped" 噪音）。启发式判断前先剥掉。
# "BANG" 是 KC 的情绪叹词（7/9 "BANG! Out half 2.45" 还是触发了 TG），
# 全大写 4 位正好命中裸 ticker 形态，一并剥掉。
# 只影响启发式，不影响 parser 本体。
_BOT_NOISE_RE = _re.compile(
    r"@everyone|KC\s*Trades\s*Bot|KC\s*交易机器人|美股会员网机器人|enrich|丰富"
    r"|\bBANG\b",
    _re.IGNORECASE,
)


def _strip_bot_noise(text: str) -> str:
    return _BOT_NOISE_RE.sub(" ", text or "")


def _looks_like_open_attempt(text: str) -> bool:
    """三件套都有 → 大概率是想发开仓信号但 parser 没接住。值得 TG。

    否则一律视为 KC 状态评论 / recap / 行情解说，silence 即可。
    """
    if not text:
        return False
    text = _strip_bot_noise(text)
    return bool(
        _OPEN_TICKER_RE.search(text)
        and _OPEN_SIDE_RE.search(text)
        and _OPEN_PRICE_RE.search(text)
    )


def _open_attempt_symbol(text: str) -> "str | None":
    """三件套命中时返回第一个 ticker，否则 None。

    [8/10 DELL] parser 主动 skip 的分支需要一个节流 key（中英孪生 + 编辑重发
    会把同一条消息送进来三四次）。判定逻辑与 _looks_like_open_attempt 完全同源，
    只是多回传一个 symbol —— 不新开一套启发式。
    """
    if not _looks_like_open_attempt(text):
        return None
    m = _OPEN_TICKER_RE.search(_strip_bot_noise(text))
    return m.group(0).lstrip("$") if m else None


# ============================================================
# 启发：enrich 风格"带仓位比例、无方向"的入场
# ============================================================
# 背景 7/13：enrich "$IBM weekly $310 $1.33 / 2% position"（中英×2 共 4 条）
# 全部静默漏掉——B2 pattern 和 _looks_like_open_attempt 都硬要求 calls/puts，
# 而 enrich 熟仓复入时会省掉方向（后续消息 "I'm in for now" 实锤是真入场）。
# 没方向不能自动下单（不猜方向），但必须 TG 提醒人工。
#
# 三件套控误报（enrich 的 levels/watchlist/持仓更新都发不出来）：
#   1. 恰好一个 $TICKER（watchlist 是一串 ticker；0 个不是信号）
#   2. "N% position/头寸/仓位" 仓位标记（enrich 入场信号的签名格式）
#   3. ≥2 个 $数字（strike + price；"Holding my 2% position" 这类纯状态没有）
_SIZED_ENTRY_TICKER_RE = _re.compile(r"\$([A-Z]{1,5})\b")
_SIZED_ENTRY_SIZE_RE = _re.compile(
    r"\d{1,2}(?:\.\d+)?\s*%\s*(?:position|头寸|仓位)", _re.I
)
# scalp 简写没有仓位比例但有 NDTE（7/15 "Scalp - $MSFT 0DTE $397.50 $.90"
# 中英双发全静默漏掉，后续 +200%）
_SIZED_ENTRY_DTE_RE = _re.compile(r"\b\d+\s*DTE\b", _re.I)
_SIZED_ENTRY_DOLLAR_NUM_RE = _re.compile(r"\$\s?\.?\d")
# 7/25 实锤：enrich 纯 scalp 形态 "$LLY $1215 scalp $1.36" / "$NVDA $212.50
# scalps off the 9EMA"（ZH 机翻 "头皮"）既无 N% position 也无 NDTE，双语
# 六连发全程静默。scalp 关键词 + 恰好一个 ticker + ≥1 个 $数字 也算疑似入场
# （NVDA 形态只有 strike 一个 $数字，≥2 的门槛接不住）。\bscalps?\b 的词边界
# 天然排除过去式 scalped（recap）。
_SIZED_ENTRY_SCALP_RE = _re.compile(r"\bscalps?\b|头皮")


def _looks_like_sized_entry(text: str) -> "str | None":
    """检测 enrich 式无方向入场（N% position / NDTE 简写 / scalp 形态）。
    返回命中的 symbol，未命中返回 None。"""
    if not text:
        return None
    text = _strip_bot_noise(text)
    tickers = {m.group(1) for m in _SIZED_ENTRY_TICKER_RE.finditer(text)}
    if len(tickers) != 1:
        return None
    dollar_nums = len(_SIZED_ENTRY_DOLLAR_NUM_RE.findall(text))
    # scalp 形态（7/25）：只要 1 个 $数字（NVDA 形态只有 strike）
    if _SIZED_ENTRY_SCALP_RE.search(text) and dollar_nums >= 1:
        return next(iter(tickers))
    if not (_SIZED_ENTRY_SIZE_RE.search(text) or _SIZED_ENTRY_DTE_RE.search(text)):
        return None
    if dollar_nums < 2:
        return None
    return tickers.pop()


# ============================================================
# 双语孪生消息的 parse-fail 报警抑制
# ============================================================
# 背景 7/13：EN "MU 1050c July 15 @ 2.60" 执行成功后 ~1s，ZH 翻译版
# "MU 1050c 7月15日 @ 2.60" parse-fail 触发 "Parse failed (looks like signal)"
# 系统错误报警——每笔成功单后必跟一条假警报。
# 规则：同频道 + 窗口内刚**成功下单**过的 symbol 出现在 fail 文本里 → 只 log。
# 只认成功执行（风控拒/broker 拒不算），不同 symbol 的真漏检不受影响。
_TWIN_SUPPRESS_WINDOW = timedelta(seconds=60)
# (channel_id, symbol) → {"ts", "strike", "side", "price"}（开仓信号快照，
# 供 parse-fail 报警抑制 + close 翻译孪生防护共用）
_recent_exec: dict[tuple[int, str], dict] = {}


def _record_recent_exec(cid: int, signal: dict):
    now = datetime.now(timezone.utc)
    # 顺手清掉过期条目，dict 不增长
    for key in [k for k, e in _recent_exec.items() if now - e["ts"] > _TWIN_SUPPRESS_WINDOW]:
        _recent_exec.pop(key, None)
    _recent_exec[(cid, signal["symbol"])] = {
        "ts": now,
        "strike": signal.get("strike"),
        "side": signal.get("side"),
        "price": signal.get("price"),
    }


def _twin_of_recent_exec(text: str, cid: int) -> "str | None":
    """fail 文本是否像"刚执行过的信号"的翻译孪生。返回命中 symbol 或 None。"""
    now = datetime.now(timezone.utc)
    for (c, sym), entry in _recent_exec.items():
        if c != cid or now - entry["ts"] > _TWIN_SUPPRESS_WINDOW:
            continue
        # ZH 文本里汉字紧贴 ticker，\b 不触发——用 lookaround（同 BARE_SYM_PATTERN_ZH）
        if _re.search(rf"(?<![A-Za-z0-9]){_re.escape(sym)}(?![A-Za-z0-9])", text):
            return sym
    return None


def _close_is_open_twin(cid: "int | None", symbol: str, parsed: dict) -> "str | None":
    """CLOSE 信号是否疑似"刚成功开仓的 OPEN 消息"的翻译孪生。返回原因或 None。

    7/15 实测：SPY 开仓 2s 后，ZH 孪生把 "smaller size" 机翻成"小规模减仓"，
    close parser 完整解析出 SPY 760c pct=33 @3.00（== 开仓价），一路走到卖出
    计算，只靠 runner-preserve（恰好持 1 张）才没把刚开的仓原价卖掉。

    判定：同频道 + 窗口内刚成功开仓过该 symbol，且满足其一——
      a. close 喊价 ≈ 开仓信号价（±1%；同一条消息的翻译价格必然相同）
      b. close 的 strike+side hint == 刚开的合约
    真砍仓通常在几分钟后且价格/pnl 已变化；60s 内"同价平仓"只有机翻场景。
    误杀时有 TG 提示，用户可手动补平。
    """
    if cid is None:
        return None
    entry = _recent_exec.get((cid, symbol))
    if not entry:
        return None
    age = (datetime.now(timezone.utc) - entry["ts"]).total_seconds()
    if age > _TWIN_SUPPRESS_WINDOW.total_seconds():
        return None
    close_price = parsed.get("signal_price")
    open_price = entry.get("price")
    if close_price is not None and open_price and abs(close_price - open_price) <= open_price * 0.01:
        return (
            f"{symbol} {age:.0f}s 前刚开仓 @ {open_price}，close 喊价相同"
        )
    if (parsed.get("hint_strike") is not None
            and parsed.get("hint_strike") == entry.get("strike")
            and parsed.get("hint_side") == entry.get("side")):
        return (
            f"{symbol} {age:.0f}s 前刚开仓 "
            f"{entry.get('strike')}{(entry.get('side') or '?')[0]}，close 指向同一合约"
        )
    return None


# ============================================================
# 双语孪生：EN 已判"不是平仓指令"时，别让 ZH 机翻版把仓位卖了
# ============================================================
# 背景 8/3（当晚唯一一笔误平）：
#   16:06:25 EN "SPY puts near entry price at the close around 1.72, if you
#            don't want to swing you can close until 4:15pm EST. I personally
#            am swinging them" → close_parser 返 None，正确跳过；
#   16:06:29 ZH 机翻 "…若不想持仓过夜，可在美东时间下午4:15前平仓…"
#            → 解析成 CLOSE 100% @1.72，卖在 1.63（入场 1.93）。
# 频道里的 ZH 永远是 EN 的机器翻译，不是独立信号源：**源文本读不出平仓指令时，
# 译文里的平仓动词就是翻译噪音**。close_parser 已按语义修了这一条（若不想…/
# 我个人选择持仓），这里是不依赖具体措辞的第二道防线——下一个机翻怪句同样接得住。
#
# 方向是刻意不对称的：只挡"EN 先跳过 → ZH 要执行"。
# 反过来（ZH 先跳过 → EN 执行）不挡：EN 是源文本，它说平就是平。
# 已知边界：本频道 63% 的对子是 ZH 先到，那种顺序本守卫不生效（EN 会正常执行，
# 也正是我们想要的）；它专治 8/3 这种 EN 先到并被判 None 的顺序。
_CJK_RE = _re.compile(r"[一-鿿]")
# (channel_id, symbol) → 该 symbol 在 EN 文本里被 close_parser 判 None 的时刻
_recent_close_skip: dict[tuple[int, str], datetime] = {}


def _record_close_skip(cid: "int | None", text: str, open_symbols) -> None:
    """close_parser 对一条 **EN** 消息返回 None 时登记，供 ZH 孪生守卫查。

    只登记不含汉字的文本：ZH 版自己被跳过不构成"源文本说了不平"的证据。
    """
    if cid is None or not text or _CJK_RE.search(text):
        return
    now = datetime.now(timezone.utc)
    for key in [k for k, ts in _recent_close_skip.items()
                if now - ts > _TWIN_SUPPRESS_WINDOW]:
        _recent_close_skip.pop(key, None)
    for sym in open_symbols or ():
        # ticker 在 EN 文本里用 \b 就够，但与 _twin_of_recent_exec 保持同一种
        # lookaround 写法，免得两处边界规则日后各走各的
        if _re.search(rf"(?<![A-Za-z0-9]){_re.escape(sym)}(?![A-Za-z0-9])", text):
            _recent_close_skip[(cid, sym)] = now


def _close_is_zh_twin_of_skipped_en(
    cid: "int | None", symbol: str, parsed: dict,
) -> "str | None":
    """ZH 解析出的 CLOSE 是否是"刚被判定为非指令的 EN 消息"的机翻孪生。"""
    if cid is None or parsed.get("lang") != "zh":
        return None
    ts = _recent_close_skip.get((cid, symbol))
    if ts is None:
        return None
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > _TWIN_SUPPRESS_WINDOW.total_seconds():
        return None
    return (
        f"{symbol} 的英文原文 {age:.0f}s 前已判定为非平仓指令，"
        f"中文孪生（pct={parsed.get('pct')}）不执行"
    )


# ============================================================
# 启发：疑似加仓（add-on）信号检测
# ============================================================
# 背景 7/6：KC "small add SPY @ 1.86" ×4（EN×2 + ZH×2）全部 parse-fail 静默丢弃。
# 无 strike/side 的 add-on 没法自动执行（需要"关联已有仓位"上下文，错配风险
# 同 follow-up close，见 close_parser 顶部注释），但至少要提醒人工——
# 裸 ticker 不满足 _looks_like_open_attempt 的 $TICKER 要求，之前连 TG 都没有。
_ADDON_KEYWORD_RE = _re.compile(r"\badd(?:ing|ed)?\b", _re.I)
_ZH_ADDON_KEYWORDS = ("加仓", "补仓")
# KC 的 add-on 惯例带 @price；没喊价的 add 评论不值得吵醒人
_ADDON_PRICE_RE = _re.compile(r"@\s*\$?\s*\.?\d+(?:\.\d+)?")


def _looks_like_addon_attempt(text: str) -> "str | None":
    """检测"疑似加仓已持仓标的"的简写信号。返回命中的 symbol，未命中返回 None。

    三件套（缺一不可，控制误报）：
      1. add 关键词（EN add/adding/added；ZH 加仓/补仓）
      2. @price 写法
      3. 文本中出现**我们已持仓**的 symbol（裸写或 $ 前缀都认）——
         白名单消歧是关键：add-on 的语义就是加已有仓位，
         白名单外的 ticker + add 多半是新开仓评论/闲聊
    """
    if not text:
        return None
    text = _strip_bot_noise(text)
    has_kw = bool(_ADDON_KEYWORD_RE.search(text)) or any(
        k in text for k in _ZH_ADDON_KEYWORDS
    )
    if not has_kw or not _ADDON_PRICE_RE.search(text):
        return None
    try:
        open_symbols = position_mgr.get_open_symbols()
    except Exception as e:
        logger.error(f"addon-check get_open_symbols failed: {e}")
        return None
    for sym in open_symbols:
        # 汉字-字母边界 \b 不触发（"小加仓SPY"），用显式 alnum lookaround
        # （同 close_parser.BARE_SYM_PATTERN_ZH 的做法）
        if _re.search(rf"(?<![A-Za-z0-9]){_re.escape(sym)}(?![A-Za-z0-9])", text):
            return sym
    return None


# 常见的中文公司名 → 大致映射到 ticker 的兜底（仅用作"这段文本里含 ticker 提及"判断，
# 不参与下单）。命中即认为 close skipped TG 有价值。
_ZH_TICKER_HINTS = ("亚马逊", "微软", "特斯拉", "苹果", "英伟达", "谷歌", "脸书", "网飞")


def _looks_like_close_attempt(text: str, lone_day_trade: bool = False) -> bool:
    """close_parser 返回 None 但看着像漏接的平仓指令 → 值得 TG。

    两条路径都要求**价格 hint**（$X / @X / d.dd）：

    1. 文本里有 ticker —— 原有判据，捕获 ZH 公司名映射失败那类真漏检。
    2. `lone_day_trade`：文本里没有 ticker，**但当前全库只有一个 day_trade 活仓**
       —— 无歧义，喊单员省略 ticker 就是在说那一个。

    第 2 条是 8/19～8/20 连吃三晚的缺口。原来只有第 1 条，"没 ticker 的
    follow-up 一律 silence"，结果：

      8/19 00:35  "small safety trim @ 2.60 to de-risk after the 2.20 add"
      8/20 00:34  "small trim @ 2.60"
      8/20 00:37  "BANG! Out half 2.80 💰"
      8/20 00:39  "BANG! Out majority @ 3.05 🚀"

    四条全是对当时唯一那个 day_trade 仓位（SPY）说的，全部静默丢弃、
    一条 TG 都没发 —— 8/20 那晚 KC 从 2.60 一路减到 3.45，我们一动没动，
    第二张一直拿到 EOD 的 1.71。

    **只提醒不下单**：放宽的是告警面，不是自动下单面。误平的代价远大于漏平
    （见 close_parser 顶部注释），要不要跟由人决定。
    """
    if not text:
        return False
    text = _strip_bot_noise(text)
    # 价格-like：$X / @X / 任何 d.dd（不用 \b 边界，因为中文+数字无 word boundary）
    has_price_hint = bool(_re.search(r"\$\.?\d|@\s*\.?\d|\d+\.\d{1,2}", text))
    if not has_price_hint:
        return False
    has_ticker = bool(_OPEN_TICKER_RE.search(text)) or any(t in text for t in _ZH_TICKER_HINTS)
    return has_ticker or lone_day_trade
