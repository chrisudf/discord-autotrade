"""Signal parser - supports KC Trades / generic Discord format

Rules (2026-06):
1. English-only
2. No multi-leg
3. No price = no trade
4. No price range
5. expiry 相对消息时间戳计算（回测正确性）
6. expiry 落假日/周末 → 自动前移到最近交易日（如 6/19 Juneteenth → 6/18）
7. expiry 显示字符串与 expiry_date 保持一致（避免 TG/DB 显示 6/19 但实际下 6/18）
8. (2026-07) ZH 机翻开仓模板走 Pattern D（A/B/C 全落空后兜底，五要素严格匹配，
   详见 _try_pattern_d）——规则 1 "English-only" 自 ZH 方向词归一化起已放宽
"""
import re
from datetime import date, timedelta
from autotrade.parsing.holidays import adjust_to_trading_day, is_trading_day
# out N% 的模式只有一份，定义在 close_parser（"out" 短语词表的所在地），
# 这里 import 复用：路由与解析必须同进同退。close_parser 只依赖 re/logger，
# 不反向 import 本模块，无循环风险。
from autotrade.parsing.close_parser import _OUT_PCT_PATTERN
from autotrade.utils.logger import logger


# ===== 跨年容错 =====
def smart_expiry(mm: int, dd: int, today: date = None) -> date:
    """跨年取最近的有效日期。

    无效的 月/日 组合（6/31、2/30，或非闰年的 2/29）逐年跳过；
    三个候选年都无效时抛 ValueError —— parse_signal 会捕获并按
    解析失败处理，而不是让异常炸掉整条消息链路。
    """
    today = today or date.today()
    candidates = []
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            candidates.append(date(year, mm, dd))
        except ValueError:
            continue
    if not candidates:
        raise ValueError(f"invalid month/day combination: {mm}/{dd}")
    future = [d for d in candidates if d >= today - timedelta(days=2)]
    if not future:
        return candidates[0]
    return min(future, key=lambda d: abs((d - today).days))


def _next_friday(today: date) -> date:
    """本周或下周五（如果今天就是周五，返回今天）。"""
    days_to_friday = (4 - today.weekday()) % 7
    return today + timedelta(days=days_to_friday)


def _third_friday(year: int, month: int) -> date:
    """该月第三个周五 —— 美股月度期权（含 LEAPS）的标准到期日。

    [7/29] KC "added SOFI 20c Jan 2027 leaps @ 1.22" 只给月+年不给日，
    月度合约的到期日按规则就是第三个周五（2027-01 → 01-15）。
    调用方仍要过 _adjust_expiry：第三个周五撞假日（如 Good Friday）时
    整体前挪到周四，与 weekly 路径同一套回退。
    """
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)  # 当月第一个周五
    return d + timedelta(days=14)


# === 假日调整封装 ===
def _adjust_expiry(d: date, context: str = "") -> date:
    """把 expiry 调整到最近的交易日（向前回退）。

    适用场景：weekly 算到 6/19 Juneteenth → 实际为 6/18 周四。

    Args:
        d: 候选 expiry
        context: 日志上下文（如 "weekly" / "MM/DD"），仅 log 用
    """
    if is_trading_day(d):
        return d
    adjusted = adjust_to_trading_day(d, direction="backward")
    logger.info(
        f"[parser] expiry {d} → {adjusted} (holiday/weekend adjusted, ctx={context})"
    )
    return adjusted


# === 同步 expiry 显示字符串（避免 holiday adjust 后显示与实际不符）===
def _finalize_signal(sig: dict) -> dict:
    """在 return 之前调用：把 expiry 字符串统一改成 M/D，
    与 expiry_date 实际日期对齐。
    NDTE / weekly 原始字符串会被覆盖——实际下单日比相对表达更有用，
    TG/DB 也不会出现"显示 6/19 实际下 6/18"的错位。
    """
    if sig.get("expiry_date"):
        d = sig["expiry_date"]
        sig["expiry"] = f"{d.month}/{d.day}"
    return sig


# === 英文月份映射 ===
MONTH_NAME_TO_NUM = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

MONTH_NAMES_RE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)


# ===== Pre-filter =====
# 注意：bare "holding" 之前会误伤 "holding up well" 这种描述价格走势的状态语，
# 导致 6/23 APLD weekly $50 calls $.66 真信号被 skip。
# 改为精确短语清单，只 skip 明确"我在持有/已持有"语境。
# 风险：若 KC 出现 "Started holding AMZN 255c @ 2.25" 这种边缘写法，
# 会被当真信号下单。实测样本里没出现，等真碰到再加。
SKIP_KEYWORDS = [
    "remaining", "into tomorrow", "持仓",
    # ZH 持有系（7/15："只持有我的 $HOOD 7/24 $125 看涨期权" 归一化后
    # 触发 looks-like-signal 误报，EN 孪生 "Only holding my" 正确 skip）。
    # 只收带前缀的形态——裸"持有"太宽，会误伤"买入 X 打算持有到 9 月"这类真开仓
    "只持有", "仅持有", "继续持有", "暂时持有",
    # 第一人称主语 + holding
    "i'm holding", "im holding", "i am holding",
    # 状语 + holding
    "still holding", "currently holding", "just holding", "keep holding",
    # holding + 明确的所有物/介词
    "holding my", "holding the", "holding all", "holding our",
    "holding into", "holding overnight", "holding tight",
]

# "Holding <TICKER>" / "Holding $<TICKER>" —— 上面的短语清单接不住
# 直接跟 ticker 的写法（"Holding TSLA 420c ... now @ 4.20" 是状态贴，
# 不 skip 会被 Pattern A/C 当新买单）。ticker 要求全大写 2-5 位，
# 所以 "holding up well" (up 小写) 不受影响。
_HOLDING_TICKER_RE = re.compile(r"\b[Hh]olding\s+\$?[A-Z]{2,5}\b")

PRICE_RANGE_PATTERN = re.compile(
    r"\$\.?\d+(?:\.\d+)?\s*(?:-|to|~)\s*\$?\.?\d+(?:\.\d+)?",
    re.IGNORECASE,
)

CHINESE_MARKERS = ["美股会员网rich", "美股会员网机器人"]


def _strip_chinese(text: str) -> str:
    for marker in CHINESE_MARKERS:
        if marker not in text:
            continue

        idx = text.find(f"\n\n{marker}")
        if idx > 0:
            return text[:idx].strip()

        idx = text.find(f"\n{marker}")
        if idx > 0:
            return text[:idx].strip()

        first = text.find(marker)
        second = text.find(marker, first + len(marker))
        if second > 0:
            segment = text[first:second]
            segment = re.sub(rf"^{re.escape(marker)}\s*:?\s*", "", segment)
            return segment.strip()

        if text.lstrip().startswith(marker):
            cleaned = re.sub(rf"^\s*{re.escape(marker)}\s*:?\s*", "", text)
            return cleaned.strip()

        return text[:first].strip()

    return text


def _has_price_range(text: str) -> bool:
    return bool(PRICE_RANGE_PATTERN.search(text))


def _has_skip_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in SKIP_KEYWORDS)


# ===== 主解析 =====
def parse_signal(text: str, msg_ts: date = None):
    """Parse single signal.

    Args:
        text: 消息内容
        msg_ts: 消息发送日期（默认 today，回测时传消息时间戳）

    Returns:
        - dict: 成功解析的信号
        - dict {"skip": "..."}: 主动 skip（非错误，listener 不应警告）
        - None: 真·解析失败（应警告 + TG）
    """
    if not text or len(text.strip()) < 5:
        return None

    today = msg_ts or date.today()

    text = _strip_chinese(text)
    if not text or len(text.strip()) < 5:
        return None

    # ZH 方向词 → EN，让 ZH 版信号走既有 pattern（7/14 实测：enrich 的 ZH 版
    # 比 EN 版早 ~2s 到达，"$HOOD - 7/24 $125 看涨期权 $1.50" 差一个方向词
    # 全 pattern 落空，白等 EN 版。归一化后 ZH 先解析先下单，EN 孪生被
    # 指纹 dedup 自然吸收。只认完整词"看涨期权/看跌期权"——裸"看涨/看跌"
    # 在行情评论里太常见（"我看涨大盘"），不碰。
    text = text.replace("看涨期权", " calls ").replace("看跌期权", " puts ")

    if _has_skip_keyword(text) or _HOLDING_TICKER_RE.search(text):
        logger.info(f"[parser] skip (holding/remaining): {text[:60]}")
        return {"skip": "holding_or_remaining"}

    if _has_price_range(text):
        logger.info(f"[parser] skip (price range): {text[:60]}")
        return {"skip": "price_range"}

    try:
        # Pattern D 在 A/B/C 全落空后才尝试（ZH 机翻模板兜底，不影响既有优先级）
        sig = (
            _try_pattern_a(text, today)
            or _try_pattern_b(text, today)
            or _try_pattern_c(text, today)
            or _try_pattern_d(text, today)
        )
    except ValueError as e:
        # smart_expiry 对 6/31 这类无效日期抛 ValueError → 按解析失败处理，
        # 走 None 路径（listener 会 TG 报警），不让异常传出去
        logger.warning(f"[parser] invalid date in signal: {e} | {text[:80]}")
        return None

    if sig is None:
        logger.warning(f"[parser] no signal: {text[:80]}")
        return None

    if sig.get("price") is None:
        logger.warning(f"[parser] skip (no price): {sig['matched']}")
        return {"skip": "no_price"}

    return _finalize_signal(sig)


def _try_pattern_a(text: str, today: date):
    """Pattern A: SYMBOL STRIKEc/p {MM/DD | Month DD} @ PRICE"""

    # === A2L: 月份 + 四位年份（LEAPS 形态，无 day）===
    # [7/29 实锤丢单] "added SOFI 20c Jan 2027 leaps @ 1.22"：下面 A2 的
    # (\d{1,2}) 没有右边界，把年份 "2027" 截成 day=20 → 2027-01-20 →
    # US.SOFI270120C20000 被 OPRA 拒（Unknown stock），整单丢失。
    # 月度/LEAPS 到期日 = 第三个周五 = 2027-01-15（账户里真实持有的正是它）。
    # 必须排在 A2 前面，且 A2 的 day 已加 (?!\d) 右边界双保险。
    pattern_a2_leaps = re.compile(
        rf"\b([A-Z]{{1,5}})\s+"
        rf"(\d+(?:\.\d+)?)([cp])\s+"
        rf"({MONTH_NAMES_RE})\s+(20\d{{2}})\b"
        rf"(?:[^@\n]*?@\s*\$?(\d+(?:\.\d+)?))?",
        re.IGNORECASE,
    )
    for m in pattern_a2_leaps.finditer(text):
        symbol, strike, cp, month_name, yyyy, price = m.groups()
        if not symbol.isupper():
            continue
        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue
        if price is None:
            filled = re.search(r"filled?\s*@\s*\$?(\d+(?:\.\d+)?)", text, re.I)
            if filled:
                price = filled.group(1)
        if price is None:
            continue
        mm = MONTH_NAME_TO_NUM[month_name.lower()]
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if cp.lower() == "c" else "PUT",
            "strike": float(strike),
            "expiry": f"{mm}/{int(yyyy)}",
            "expiry_date": _adjust_expiry(
                _third_friday(int(yyyy), mm), context="A2L Month YYYY (LEAPS)"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # === A2: 英文月份在先（优先级更高，避免 A1 误吃）===
    # day 的 (?!\d) 右边界：没有它，"Jan 2027" 会被截成 day=20（见上方 A2L）。
    pattern_a2 = re.compile(
        rf"\b([A-Z]{{1,5}})\s+"
        rf"(\d+(?:\.\d+)?)([cp])\s+"
        rf"({MONTH_NAMES_RE})\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?"
        rf"(?:[^@\n]*?@\s*\$?(\d+(?:\.\d+)?))?",
        re.IGNORECASE,
    )
    for m in pattern_a2.finditer(text):
        symbol, strike, cp, month_name, dd, price = m.groups()
        # 7/8 实测：regex 带 IGNORECASE，[A-Z] 实际也吃小写——
        # "taking AAPL again but 300p 7/17" 里的 "but" 被当成 ticker BUT。
        # 停用词表永远列不全，要求 symbol 在原文中就是全大写（真 ticker 惯例）
        if not symbol.isupper():
            continue
        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue
        if price is None:
            filled = re.search(r"filled?\s*@\s*\$?(\d+(?:\.\d+)?)", text, re.I)
            if filled:
                price = filled.group(1)
        if price is None:
            continue
        mm = MONTH_NAME_TO_NUM[month_name.lower()]
        dd_int = int(dd)
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if cp.lower() == "c" else "PUT",
            "strike": float(strike),
            "expiry": f"{mm}/{dd_int}",
            "expiry_date": _adjust_expiry(
                smart_expiry(mm, dd_int, today=today), context="A2 Month DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # A1: 数字日期 MM/DD
    pattern = re.compile(
        r"\b([A-Z]{1,5})\s+"
        r"(\d+(?:\.\d+)?)([cp])\s+"
        r"(\d{1,2})/(\d{1,2})"
        r"(?:[^@\n]*?@\s*\$?(\d+(?:\.\d+)?))?",
        re.IGNORECASE,
    )

    for m in pattern.finditer(text):
        symbol, strike, cp, mm, dd, price = m.groups()

        # 同 A2：小写单词不是 ticker（"but 300p 7/17" 案例，7/8）
        if not symbol.isupper():
            continue
        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue

        if price is None:
            filled = re.search(r"filled?\s*@\s*\$?(\d+(?:\.\d+)?)", text, re.I)
            if filled:
                price = filled.group(1)

        if price is None:
            continue

        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if cp.lower() == "c" else "PUT",
            "strike": float(strike),
            "expiry": f"{int(mm)}/{int(dd)}",
            "expiry_date": _adjust_expiry(
                smart_expiry(int(mm), int(dd), today=today), context="A1 MM/DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # A3: SYMBOL STRIKEc/p NDTE [@ PRICE]
    # 7/17 实锤："SPY 745p 8DTE @ 2.66" 中英双发全漏（A 系列只认 M/D 日期，
    # B 系列要 $ 前缀，裸 ticker + NDTE 两头落空），正是 KC 当晚 +200% 的主力单
    pattern_a3 = re.compile(
        r"\b([A-Z]{1,5})\s+"
        r"(\d+(?:\.\d+)?)([cp])\s+"
        r"(\d+)\s*DTE\b"
        r"(?:[^@\n]*?@\s*\$?(\d+(?:\.\d+)?))?",
        re.IGNORECASE,
    )
    for m in pattern_a3.finditer(text):
        symbol, strike, cp, dte, price = m.groups()
        # 同 A1/A2：小写单词不是 ticker（"but 300p ..." 案例）
        if not symbol.isupper():
            continue
        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue
        if price is None:
            filled = re.search(r"filled?\s*@\s*\$?(\d+(?:\.\d+)?)", text, re.I)
            if filled:
                price = filled.group(1)
        if price is None:
            continue
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if cp.lower() == "c" else "PUT",
            "strike": float(strike),
            "expiry": f"{dte}DTE",
            "expiry_date": _adjust_expiry(
                today + timedelta(days=int(dte)), context="A3 NDTE"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    return None


def _try_pattern_b(text: str, today: date):
    """Pattern B: 多种 $SYMBOL 形态。

    优先级：
    B0:    含明确 MM/DD（最准确）
    B0.5:  含英文月份 (June 26 / Jan 15)
    B1:    含 NDTE
    B2:    weekly 无日期 → 默认本周五
    B3:    $STRIKE 在 calls 前的倒序写法
    """

    # ----- B0: 含 MM/DD -----
    p_mmdd = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?(\d{1,2})/(\d{1,2})"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(?:[a-z0-9]+\s+){0,3}?(calls?|puts?)"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_mmdd.search(text)
    if m:
        symbol, mm, dd, strike, side, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{int(mm)}/{int(dd)}",
            "expiry_date": _adjust_expiry(
                smart_expiry(int(mm), int(dd), today=today), context="B0 MM/DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B0.5: $SYMBOL ... Month DD ... $STRIKE calls $PRICE -----
    p_month_name = re.compile(
        rf"\$([A-Z]{{1,5}})\b"
        rf"[^\$\n]*?({MONTH_NAMES_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
        rf"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(?:[a-z0-9]+\s+){0,3}?(calls?|puts?)"
        rf"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_month_name.search(text)
    if m:
        symbol, month_name, dd, strike, side, price = m.groups()
        mm = MONTH_NAME_TO_NUM[month_name.lower()]
        dd_int = int(dd)
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{mm}/{dd_int}",
            "expiry_date": _adjust_expiry(
                smart_expiry(mm, dd_int, today=today), context="B0.5 Month DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B1: $SYMBOL ... NDTE ... $STRIKE calls/puts ... $PRICE -----
    p_dte_first = re.compile(
        r"\$([A-Z]{1,5})\b"
        r".*?(\d+)DTE"
        r".*?\$(\d+(?:\.\d+)?)\s*(?:[a-z0-9]+\s+){0,3}?(calls?|puts?)"
        r".*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE | re.DOTALL,
    )
    m = p_dte_first.search(text)
    if m:
        symbol, dte, strike, side, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{dte}DTE",
            "expiry_date": _adjust_expiry(
                today + timedelta(days=int(dte)), context="B1 NDTE"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B1b: $SYMBOL $STRIKE calls NDTE $PRICE -----
    p_dte_mid = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(?:[a-z0-9]+\s+){0,3}?(calls?|puts?)"
        r"[^\$\n]*?(\d+)DTE"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_dte_mid.search(text)
    if m:
        symbol, strike, side, dte, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{dte}DTE",
            "expiry_date": _adjust_expiry(
                today + timedelta(days=int(dte)), context="B1b NDTE"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B2: $SYMBOL [weekly] $STRIKE calls/puts $PRICE （无日期） -----
    p_weekly = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*(?:[a-z0-9]+\s+){0,3}?(calls?|puts?)"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_weekly.search(text)
    if m:
        symbol, strike, side, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": "weekly",
            "expiry_date": _adjust_expiry(
                _next_friday(today), context="B2 weekly"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    # ----- B3: $SYMBOL $STRIKE weekly calls MM/DD $PRICE -----
    p_alt = re.compile(
        r"\$([A-Z]{1,5})\b"
        r"[^\$\n]*?\$(\d+(?:\.\d+)?)\s*"
        r"\s*(?:[a-z0-9]+\s+){0,3}?(calls?|puts?)"
        r"[^\$\n]*?(\d{1,2})/(\d{1,2})"
        r"[^\$\n]*?\$(\.?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = p_alt.search(text)
    if m:
        symbol, strike, side, mm, dd, price = m.groups()
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side.lower().startswith("call") else "PUT",
            "strike": float(strike),
            "expiry": f"{int(mm)}/{int(dd)}",
            "expiry_date": _adjust_expiry(
                smart_expiry(int(mm), int(dd), today=today), context="B3 MM/DD"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }

    return None


def _try_pattern_c(text: str, today: date):
    """Pattern C: 简写 `$SYMBOL <STRIKE><c|p> [weeklies|MM/DD] ... <PRICE>`

    覆盖 6/22 漏接的 KC 风格简写，例如：
        "Adding $APLD 50c weeklies here @role_1362xxx +alert .98 fill"
        "$APLD 50p 7/2 .85 fill"

    与 A/B 的关键差异：
      - strike 用单字符 `c`/`p` 而非 `calls/puts`（A 是无 $，B 要求 `calls/puts`）
      - expiry 缺省时默认 next Friday（信号文本通常含 weeklies 字样但非强制）
      - 价格容忍多种写法：`.98 fill` / `@.98` / `$0.98` / `@$ 0.98`

    为避免误伤：
      - symbol 必须有 `$` 前缀
      - 必须能找到至少一种合法价格写法（不接受裸数字 .98 没有上下文）
    """
    # 锚点：$SYMBOL N(c|p)，c/p 后要么是 word boundary，要么紧跟空白/标点
    anchor = re.search(
        r"\$([A-Z]{1,5})\s+(\d+(?:\.\d+)?)([cp])(?=\b|\s|$)",
        text, re.IGNORECASE,
    )
    if anchor is None:
        return None
    symbol, strike, cp = anchor.groups()

    # anchor 之后的窗口（限制范围避免跨段误匹配）
    after = text[anchor.end(): anchor.end() + 200]
    # Discord 角色提及 @role_数字 会被价格扫描误伤，先剔
    after_clean = re.sub(r"@role_\d+", " ", after)

    # 找 expiry
    # M/D 后面跟 size/position 类词的是仓位描述不是日期（"1/2 size" 曾被
    # 解析成 1 月 2 日 → smart_expiry 跨年推到下一年年初）；月份范围也要校验
    expiry_str = "weekly"
    mmdd = re.search(
        r"\b(\d{1,2})/(\d{1,2})\b(?!\s*(?:size|sized|position|pos\b|risk))",
        after_clean[:80],
    )
    if mmdd and not (1 <= int(mmdd.group(1)) <= 12 and 1 <= int(mmdd.group(2)) <= 31):
        mmdd = None
    dte = re.search(r"\b(\d+)\s*dte\b", after_clean[:80], re.I)
    if mmdd:
        mm, dd = int(mmdd.group(1)), int(mmdd.group(2))
        expiry_str = f"{mm}/{dd}"
        expiry_date = _adjust_expiry(smart_expiry(mm, dd, today=today), context="C MM/DD")
    elif dte:
        n = int(dte.group(1))
        expiry_str = f"{n}DTE"
        expiry_date = _adjust_expiry(today + timedelta(days=n), context="C NDTE")
    else:
        expiry_date = _adjust_expiry(_next_friday(today), context="C weekly")

    # 找价格：只接受带明确"成交"语义的写法（@ 前缀 / fill 后缀）。
    # 不再接受裸 "$N"——那是把评论/目标价当 entry 的主要来源
    # （"$MSFT 390c looking great, target $5" 曾被解析成 @5 买入）。
    price = None
    for pattern in (
        r"@\s*\$\s*(\.?\d+(?:\.\d+)?)",         # @$.98 / @$ 0.98
        r"@\s*(\.?\d+(?:\.\d+)?)\b",            # @.98 / @0.98
        r"fill(?:ed)?\s*@\s*\$?\s*(\.?\d+(?:\.\d+)?)",  # filled @ .98
        r"(\.?\d+(?:\.\d+)?)\s*fill(?:ed)?\b",  # .98 fill (用户实际信号)
    ):
        m = re.search(pattern, after_clean, re.I)
        if m:
            try:
                price = float(m.group(1))
                break
            except ValueError:
                continue
    if price is None:
        return None

    return {
        "raw": text,
        "matched": anchor.group(0).strip(),
        "symbol": symbol.upper(),
        "side": "CALL" if cp.lower() == "c" else "PUT",
        "strike": float(strike),
        "expiry": expiry_str,
        "expiry_date": expiry_date,
        "price": price,
        "tags": _extract_tags(text),
    }


# Pattern D: ZH 机翻开仓模板（编译一次，模块级；分组次序见 _try_pattern_d）
# 形态：SYM + strike + calls/puts + (NDTE | N天到期) + (@价 | 价格N)
#   - symbol 允许紧贴 CJK（"买入META"——ZH 原文动词和 ticker 之间没有空格，
#     \b 在 \w(CJK 也是 \w) 之间不成立，只能用 lookbehind 排除英文字母/$）
#   - side 是完整词 calls/puts：ZH 归一化("看涨期权"→" calls ")的产物，
#     正是 A 系列(要求 strike 紧贴单字符 c/p)接不住的原因
#   - 要素间窗口 _GAP：容纳全角逗号/空格等机翻标点，但**不允许跨越**句读
#     （。！？；.!?）、换行、或**连续 2+ 个英文字母**（第二个 ticker，以及
#     "target"/"stop loss" 这类英文限定词都会被它挡住）。
#     [7/28 对抗评审] 旧版 [^\n]{0,30} 会把 "META 620看涨期权，SPY 4DTE @ 3.15"
#     拼成 META+SPY 的 4DTE @3.15 混合单——30 字符轻松跨过一整个 ZH 子句
#     和第二个标的。禁止字符类把窗口锁死在"同一子句、同一 ticker 内"。
#     [7/29 复核] 原写法是 `[^A-Z...]` + re.IGNORECASE，实际语义 **不是**
#     注释当时写的"≥2 连续大写字母"：Python 里否定字符类叠加 IGNORECASE 会
#     把小写一并排除（re.match(r'[^A-Z]','a',re.I) is None），所以它挡的是
#     任何 2+ 连续英文字母。这里改写成 [^A-Za-z...] 显式表达同一行为
#     （**逐字等价，不改召回**），免得后人"照注释修正"成只挡大写——那会
#     让 "META 620 calls 4DTE target @ 6.00" 当 entry 下单（实测验证过）。
#     英文限定词的防线现在有两层：_GAP 的字母墙 + 下面的 _price_qualified。
#   - 价格必须是**入场价**语义：见 _PRICE_QUALIFIERS / _price_qualified。
#     "目标价格6.00"/"止损价格2.40"/"目前价格6.00" 是评论不是喊单。
_GAP = r"(?:[^A-Za-z\n。！？；.!?]|[A-Za-z](?![A-Za-z]))*?"  # 连续 2+ 英文字母不允许
_PATTERN_D = re.compile(
    r"(?<![A-Za-z$])([A-Z]{1,5})\s+"                        # symbol（禁 $ 前缀：$ 形态归 B/C 管）
    r"(\d+(?:\.\d+)?)\s+"                                   # strike
    r"(calls?|puts?)\b"                                     # side（完整词）
    r"" + _GAP + r"(?:(\d{1,3})\s*DTE\b|(\d{1,3})\s*天到期)"  # NDTE | N天到期
    r"" + _GAP + r"(?:@\s*\$?\s*(\.?\d+(?:\.\d+)?)"         # @3.15 / @ $3.15
    r"|(?<![标损前的现])价格\s*[:：]?\s*\$?\s*(\.?\d+(?:\.\d+)?))",  # 价格4.80（紧邻限定词末字 标/损/前/的/现 → 目标|止损|目前|当前|…的|现 价格，视为评论不下单）
    re.IGNORECASE,
)

# 价格限定词：出现在价格 token **之前**就说明那个数字不是入场价，是评论。
# [7/29 对抗测试] `价格` 分支原本只有负向 lookbehind (?<![标损前的现])，
# **`@` 分支一个防护都没有** —— 实测 "AMD 210看涨期权 4天到期 目标 @ 4.50"
# 会按 4.50 下单（真实 entry 3.00，溢价 50%），且 4.50 低于
# MAX_PRICE_PER_CONTRACT 熔断线，风控接不住。EN 版 "target @ 6.00" 当时能
# 被拒纯属 _GAP 字母墙的副作用（见 _GAP 注释），不是有意设计——ZH 侧因为
# 限定词是 CJK 直接穿墙而过。而按 docstring，ZH 孪生常比 EN 早 ~2s 到，
# ZH 误解析先执行、EN 版压根不匹配 → **没有孪生来纠正**，这是单边错单。
#
# 只收"这个数字明确不是入场价"的词，宁可漏挡也不误伤真喊单：
# 特意**不含** "now"——"buying now @ 3.15" 是合法开仓。
_PRICE_QUALIFIERS = (
    "目标", "止损", "止盈", "目前", "当前", "现价", "预期",
    "target", "stop", "current",
)
# 回看窗口：够装下 "stop loss @ "(12) 即可；再长会摸到 DTE 要素区徒增误伤。
_QUALIFIER_LOOKBACK = 16


def _price_qualified(text: str, price_start: int, match_start: int) -> bool:
    """价格 token 前 _QUALIFIER_LOOKBACK 字符内是否有限定词（→ 评论，不下单）。

    只往回看到本次匹配的起点，不跨出 m.group(0)——否则同一条消息里前面
    某个仓位的"止损"会误伤后面真正的开仓喊价。
    """
    window = text[max(match_start, price_start - _QUALIFIER_LOOKBACK):price_start]
    return any(q in window.lower() for q in _PRICE_QUALIFIERS)


def _try_pattern_d(text: str, today: date):
    """Pattern D: ZH 机翻开仓模板 `SYM STRIKE calls/puts (NDTE|N天到期) (@价|价格N)`

    两夜实锤（逐字语料在 tests/test_0014_zh_open.py）：
      1. 7/24 夜："买入META 620看涨期权，4天到期，价格4.80" —— 归一化后
         "META 620 calls ，4天到期，价格4.80"：A 系列要求 strike 紧贴 c/p
         （"620c"），B/C 系列要求 $ 前缀，三头全落空 → 当晚 ZH 孪生解析失败，
         全靠 EN 版被路由修复接住。enrich 的 ZH 版实测常早 EN ~2s 到达（7/14），
         ZH 不可解析 = 白等 EN；EN 同时失手（当晚 "into the close" 误路由）
         就是整单丢失。
      2. 7/28 夜："SPY 745看涨期权 4DTE @ 3.15 日内交易" 同型再现。

    语义复用：
      - "N天到期" 等价 NDTE —— expiry_date 与 A3 同一条路径
        （today + N 天，再 _adjust_expiry 假日回退），保证 ZH/EN 孪生
        指纹 (symbol,side,strike,expiry_date) 完全一致，dedup 才拦得住双发。
      - "日内交易" 的 day_trade tag 由 _extract_tags 的 zh_tag_map（"日内"）
        既有词表覆盖，无需另补。

    保守边界（契约铁律 2：宁错过不错杀）：
      - 模板五要素缺一不命中，不做宽松匹配——缺价/缺期的残句宁可落到
        looks-like-signal 大声告警，也不半猜下单。
      - "7月15日" 这类 ZH 日期形态不在本模板内（无实测语料，不扩）。
    """
    for m in _PATTERN_D.finditer(text):
        symbol, strike, side_word, dte_en, dte_zh, price_at, price_zh = m.groups()
        # 同 A 系列：IGNORECASE 下 [A-Z] 也吃小写，要求原文全大写才算 ticker
        # （"buy 620 calls 4DTE @ 3.15" 里的 "buy" 不是 ticker）
        if not symbol.isupper():
            continue
        if symbol.upper() in {"I", "A", "THE", "AT", "ON", "IS", "DTE", "IPO"}:
            continue
        # 限定价防护：目标/止损/现价 等前缀说明这个数字不是入场价。
        # finditer + continue（而非 return None）：同一条消息里"目标 @ 6.00"
        # 之后若还跟着真正的喊价，仍有机会被后一个 match 接住。
        price_group = 6 if price_at is not None else 7
        if _price_qualified(text, m.start(price_group), m.start()):
            logger.info(
                f"[parser] Pattern D 跳过限定价（目标/止损/现价类，非入场价）: "
                f"{m.group(0).strip()[:60]}"
            )
            continue
        n = int(dte_en if dte_en is not None else dte_zh)
        price = price_at if price_at is not None else price_zh
        return {
            "raw": text,
            "matched": m.group(0).strip(),
            "symbol": symbol.upper(),
            "side": "CALL" if side_word.lower().startswith("call") else "PUT",
            "strike": float(strike),
            # 显示字符串统一记 NDTE（_finalize_signal 会覆盖成实际 M/D，
            # 与 A3 行为一致）
            "expiry": f"{n}DTE",
            "expiry_date": _adjust_expiry(
                today + timedelta(days=n), context="D NDTE/天到期"
            ),
            "price": float(price),
            "tags": _extract_tags(text),
        }
    return None


def _extract_tags(text: str) -> list:
    """从 KC 信号文本抽 tag，给后续 category/分析用。

    KC 风格变种统一：
      "day trade" / "day-trade" / "daytrade" → day_trade
      "small day trade" / "small fun day trade" 都命中
    其它（lotto / swing / scalp / fafo）保留原样。
    """
    tags = []
    lower = text.lower()
    for kw in ["lotto", "swing", "scalp", "fafo"]:
        if kw in lower:
            tags.append(kw)
    # day trade 三种写法
    if any(p in lower for p in ("day trade", "day-trade", "daytrade")):
        tags.append("day_trade")
    # ZH 对应词——ZH 归一化后 enrich 的 ZH 版常常先到先执行（7/17 "彩票头皮 -
    # $ARM ..." ZH 先下单，tags=[] → category 记成 0dte 而非 0dte_lotto；
    # 对周内 lotto 更要命：会被错挂 SL）。tags 决定 category/SL/EOD，必须双语。
    zh_tag_map = {"彩票": "lotto", "波段": "swing", "头皮": "scalp", "日内": "day_trade"}
    for zh, tag in zh_tag_map.items():
        if zh in text and tag not in tags:
            tags.append(tag)
    return tags


# ===== Action detection =====
# 必须双语都覆盖。否则 ZH close 信号会被路由到 OPEN parser，浪费一次解析失败
# + 错过中文先到的场景。
#
# 双层结构（7/6 review 修复，替代旧的单一 CLOSE_KEYWORDS）：
# - 强关键词：几乎只出现在平仓语境（closed/sold/trimmed/平仓...），命中即 CLOSE
# - 弱关键词：在开仓语境同样常见，只有文本里**没有**明确开仓动词时才按 CLOSE 路由
#   实测踩坑（弱词直接进 CLOSE_KEYWORDS 的后果）：
#     "Buying $QQQ 560c into the closing bell @ 1.35" → 'closing' 误路由 CLOSE
#       → close_parser 抽到 QQQ+strike hint → **反向卖出**
#     "Going all out on $NVDA 200c here @ 3.50"      → 'all out' 同上
#   'selling' 旧版被排除在外（怕误伤开仓评论），代价是
#     "Selling $MSFT 390c here @ 5.20" 走 OPEN parser → Pattern C 把它当**买入**。
#     现在作为弱关键词：无开仓动词 → CLOSE；有 → OPEN。
#
# 7/6 复盘补充（并入双层结构）：
#   - "Scaling down to 1/2 position sizing" → scaling down 放弱词层
#     （"adding..., scaling down size" 类开仓语境靠 OPEN_INTENT 豁免）
#   - ZH "减持1/3" / "缩减至 1/2" → 减持 / 缩减至|缩减到 放强词层
#     （不加裸 "缩减"：会误伤 "缩减购债" 类宏观评论）
# 7/8 复盘补充：
#   - trim(?:med|ming)?：\btrim\b 匹配不到 "trimming"（"Start trimming. Down
#     to 1/2" 没进 close 路径），close_parser 的 ACTION_VERBS 一直认 trimming，
#     双层词表不同步的实锤
#   - 出清：ZH "全部出清苹果仓位" 漏路由
#   - OPEN_INTENT 两次实测误伤（都是把弱 close 压掉）：
#       "will look to re-enter" → \benter\b 在连字符处成立命中 "re-enter"
#       "out half 3.22 stop at entry" → \bentry\b 命中 "stop at entry"
#     "at/to entry" 和 "re-enter" 是 KC 高频的止损/复盘用语，不是开仓动作
STRONG_CLOSE_RE = re.compile(
    # 7/24 实锤：`closed?` 同时命中裸名词 close——"META 620c 4DTE @ 4.80 little
    # day trade **into the close** for fun" 被误路由 CLOSE，一个完全可解析的开仓
    # 信号丢失（只发了条误导性的 "CLOSE 未执行" TG）。裸 close 只在**不是**
    # 名词/副词用法时才算平仓动词：前面不能是 the/at/into/before/near/after
    # （"into the close"、"at close"），后面不能接 to（"close to 620" 是邻近副词）。
    # "close TSLA here" / "want to close half" 不受影响；closed/closing 语义不变。
    r"\b(closed"
    r"|(?<!the\s)(?<!at\s)(?<!into\s)(?<!before\s)(?<!near\s)(?<!after\s)close(?!\s+to\b)"
    r"|sold|exit|stopped|trim(?:med|ming)?|out of|scaling\s+out)\b"
    # "all out" 从 WEAK 提级（7/15："all out SPY -11% not adding" 里的
    # "adding" 命中 OPEN_INTENT 把 WEAK close 一票否决 → 误判 OPEN，
    # 靠 ZH 孪生"全部平仓"才兜住检测）。"going all out" 是开仓情绪，排除。
    r"|(?<!going\s)\ball\s+out\b"
    # enrich 的止盈口头禅（7/17 "$XOM LOCK THEM ALL ON" / "$ARM lock them in!"
    # 双双漏掉，两个都是我们的持仓）。只认现在时/祈使——过去式 "locked in 200%"
    # 是 recap，不匹配。
    r"|\block(?:ing)?\s+(?:them\s+|these\s+|it\s+|profits?\s+)?(?:all\s+)?(?:in|on)\b"
    r"|减仓|平仓|清仓|卖出|卖了|砍仓|砍掉|抛出|止盈|全平|清空|减持|缩减至|缩减到|出清"
    # 7/23 实测：enrich ZH 孪生 "$NBIS - 出半"（EN "Out half"）没进 CLOSE 路由，
    # 落到 OPEN 解析失败。EN 侧 WEAK_CLOSE_RE 一直认 "out half"，双语不对称。
    # 两侧边界与 close_parser.ZH_OUT_HALF_RE 保持一致（对抗评审两轮实锤）：
    # 右边界拦"冲出半年新高"，左边界拦"走出半V型反转"（ASCII 跟随右边界拦不住），
    # "出半仓" 变体后面允许任意接续（"出半仓于2.45"）。
    r"|(?<![一-鿿])出半(?:仓|(?![一-鿿]))"
    r"|锁定",
    re.I,
)
WEAK_CLOSE_RE = re.compile(
    r"\bclosing\b(?!\s+bell)"          # 'closing bell' 是时间状语不是动作
    r"|\bout\s+(?:half|full|majority)\b"
    # 7/25 实测:enrich "$LLY - Out 25% more. Down to runners." 双语双发全漏
    # (裸 out 不在词表)。out N% 的模式只有一份、定义在 close_parser
    # (_OUT_PCT_PATTERN,含 knocked-out 解说的 lookbehind),这里 import 复用——
    # 路由与解析同进同退,两边写法漂移就是漏单裂缝。
    r"|" + _OUT_PCT_PATTERN
    + r"|\bselling\b"
    r"|\bscaling\s+down\b",
    re.I,
)
OPEN_INTENT_RE = re.compile(
    r"\b(buy(?:ing)?|bought|add(?:ing|ed)?|grab(?:bed|bing)?|"
    r"load(?:ing|ed)?|bto|(?<!re-)enter(?:ed|ing)?|"
    r"(?<!at\s)(?<!to\s)entry|in at)\b",
    re.I,
)
# 否定式的开仓词（"not adding" / "won't buy"）是**放弃**开仓，不该否决
# close 判定。正则不懂否定，先把这类短语抹掉再查 OPEN_INTENT。
# 撇号兼容 ASCII ' 和弯引号 '（KC 消息里两种都出现过）。
_NEGATED_OPEN_INTENT_RE = re.compile(
    r"\b(?:not|no|never|stop(?:ped)?|won['’]?t|wouldn['’]?t|don['’]?t|didn['’]?t)\s+"
    r"(?:be\s+)?(?:add(?:ing|ed)?|buy(?:ing)?|enter(?:ing)?|load(?:ing)?)\b",
    re.I,
)


def detect_action(text: str) -> str:
    if STRONG_CLOSE_RE.search(text):
        return "CLOSE"
    text_wo_neg = _NEGATED_OPEN_INTENT_RE.sub(" ", text)
    if WEAK_CLOSE_RE.search(text_wo_neg) and not OPEN_INTENT_RE.search(text_wo_neg):
        return "CLOSE"
    return "OPEN"