"""CLOSE 信号解析

设计基于 6/14-6/18 真实样本（详见对话 review）：
- 20 条 close-ish 消息中 40% 是复盘/计划噪音，必须先过滤
- 70% 的可执行信号是 "trimmed/Selling X%"（部分平），不是全平
- 几乎所有 close 信号都不带 strike → symbol 级匹配
- "NOW" 在 close 信号里既可能是 ServiceNow 也可能是副词
  → 必须用 open_symbols 白名单消歧

返回结构：
    {
        "kind": "CLOSE" | "BULK_TRIM",
        "symbols": [...],   # BULK_TRIM 时空
        "pct": int,         # 卖出百分比 1-100
        "matched": str,     # debug 用
    }
    或 None（噪音/无法定位 symbol）

中文 fallback：
- 历史数据：63% 双语对子中文先到，但 100% 配对率（gap<60s 内英文必到）
- 实际链路：parse_close → 先 EN 路径 → 失败 fallback 到 ZH 路径
- 双发场景靠 listener 的 CLOSE fingerprint dedup（symbols+pct+kind）兜底
- ZH 路径只用 $SYMBOL + 白名单裸 ticker 两档
  → KC 翻译中文公司名（亚马逊/微软等）抓不到 → 静默 None → 1-3s 后 EN 版本兜底
  → 兜底失败时 log [zh_unrecognized] warning 便于事后排查

TODO（实测调整）：
- enrich 长段多 symbol 列表（"Trimmed the following:\n$APLD ...\n$CRWV ..."）
  当前 parser 会把所有 $XXX 都返回，需要进一步按行拆分各自的 pct
- "stop moved to entry" / "stops to BE" 是元信号，不是 close（应跳过，已在 RECAP）
- ACTION_DONE 里的 "took" 单独看可能误触（"took the trade"），暂依赖 RECAP_MARKERS 兜底
- 若 [zh_unrecognized] warning 频繁，再考虑数据驱动的中文名→ticker 自动学习
  （EN 版本成功时关联同时段 ZH 文本里的未知中文名）

风险 / 已知漏接（不修，列在这供日后参考）：
- **无 symbol 的 follow-up close**：例如 "BANG! Out half @ 8.05 💰"
  这种"承接上一条 trim 信号"的 close 没 ticker，要靠"最近交易"上下文判断。
  当前一律 return None。修这个等于引入"最近持仓"状态机：要决定时间窗、
  并发开仓如何选、多语言双发去重——容易引入更严重的"错平别的仓位"风险。
  当前判断：宁可丢这种 follow-up（前一条 trim 通常已经触发了），
  也不要 close 错仓位。
- **CLOSE 误平的代价 > OPEN 误触发**：风控对 OPEN 有 max_price/qty/熔断兜底，
  但 CLOSE 一旦匹配到 open_symbols 就直接挂卖单。改 close parser 前请
  把 symbols 必须 in open_symbols 这一硬约束保留住。
"""
import re
from typing import Optional

from autotrade.utils.logger import logger


# 复盘/计划标记 —— 出现任一即跳过
# 注意：必须先于 ACTION 检测，因为 "scaled some yesterday" 含 "scaled" 但不动手
RECAP_MARKERS = [
    "yesterday", "this morning", "earlier today",
    "the plan", "my plan", "here's my plan",
    "have an order", "will be ", "going to ",
    "out of 4", "out of 5",  # PnL 复盘 "2 losing out of 4"
    # 7/17 "Weekly recap, 7/13: $GOOGL 1,200%+ ..." 被当 BULK close 解析出
    # 7 个 symbol，扇出 3 条 runner-preserve TG。ZH 版"每周回顾"早就在
    # ZH_RECAP_MARKERS 里，EN 一直漏
    "weekly recap", "recap,", "recap:",
    # watchlist 帖本质是 recap——语料回放发现内文带 "lock in gains" 建议的
    # watchlist 会被 lock-in 动词路由成 CLOSE，这里统一拦掉
    "watchlist",
]

# 需要边界/上下文的 recap 标记，纯 substring 表达不了，与 RECAP_MARKERS 同权。
# 两者都命中即跳过（见 _has_recap_marker）。
RECAP_PATTERNS = [
    # [8/5 复盘] "Controlled selling so far - let's EMAs catch up"（本地 02:03）
    # 在有 RKLB 持仓时会被判成 CLOSE 2%（`2% in $RKLB` 的 2% 被 _extract_pct 抽走）。
    # 老写法是两条字面量 "(so far)" / "so far,"，原意"只拦作为分句收尾的 so far，
    # 不拦 'so far before FOMC'"是对的，但把收尾形态写死成了逗号和右括号 ——
    # 喊单员用的是破折号，差一个标点就漏。改成"so far 后面跟任意分句收尾符或行尾"，
    # 原意不变，覆盖 , - – — . : ; ) ! ? 和换行。
    # "so far before FOMC" 仍不拦：so far 后面是空格+单词，不在收尾符里。
    # 代价：像 "Trimmed 50% so far" 这种"已减完再报账"的句子现在也算 recap 被跳过。
    # 方向上是对的（那是报账不是指令），且符合模块顶部"宁漏平不误平"的取舍。
    re.compile(r"so far\s*(?:[,\-–—.:;)!?]|$)", re.M),

    # [8/5 复盘] "will trim SOFI calls closer to $19 stock price"（本地 23:55）
    # 是**未来条件意图**（等股价到 $19 再减），不是当下指令。但 STRONG_CLOSE_RE
    # 的裸 `trim` 判成 CLOSE，_extract_pct 找不到百分比 → 落到默认 33%，
    # 有 SOFI 持仓时会立刻减掉三分之一。
    # 老表里的 "will be " / "going to " 接得住 "will be trimming"，唯独接不住
    # 少了 be 的 "will trim"。这里补的就是这个形状。
    # 动词表与 ACTION_VERBS 的词根同源（trim/cut/sell/close/dump/scale/lock），
    # 加动词请两处一起看。
    # 已知代价（与既有的 "will be " 逐字同款，不是新引入的风险）：一句话里
    # 既有当下动作又有未来计划时整条被跳过，例如 "Out half here, will trim
    # the rest at $19" —— 宁可漏平也不误平，见模块顶部。
    re.compile(r"\b(?:will|'ll|’ll)\s+(?:be\s+)?(?:trim|cut|sell|close|dump|scale|lock)\w*"),

    # [8/28 实锤，当晚唯一动了钱的一条] KC 的战报
    # "got so wrapped up in SPY and AAPL I **didn't see** that UNG order filled
    # for 1.90 trim ..." 被判成 CLOSE，把 AAPL 330c 卖了一张。
    # "我没看见 / 没注意到" 是**明确的事后叙述**：作者在说自己当时不在场，
    # 不可能同时是一条要人跟单的指令。这是本表里语义最硬的一条 recap 信号。
    # 只收第一人称否定式，不收裸 see/notice —— "see if it holds 3.50 then trim"
    # 这类真指令不受影响。
    # 撇号必须同时收 ASCII ' 和 Unicode ’ —— Discord/iOS 键盘发出来的是后者。
    # 本条第一版只写了 ASCII，手敲测试通过、当晚原文（didn’t）照样漏过去。
    # 同一个坑：RECAP_PATTERNS 上面那条 will/'ll/’ll 早就把两种都列了。
    re.compile(r"\b(?:i\s+)?(?:did\s*n[o']?t|didn[’']t|did\s+not|never)\s+"
               r"(?:see|notice|catch|realize)\b", re.I),
]

# bulk action —— 不指定 symbol，对所有持仓批量 trim
BULK_MARKERS = [
    "all positions", "every position",
]

# 裸 "everything" 太弱，必须是**平仓动词的宾语**才算 bulk。
# [8/18 实测险情] "out the rest of AMZN to secure small green trade ✅ Price has on
# just about everything outside memory stocks has been slow and boring." ——
# 加了 _OUT_REST_PATTERN 之后这句终于路由成 CLOSE，而句尾闲聊里的 "everything"
# 让 _has_bulk_marker 为真 → BULK_TRIM pct=100 = **把全部持仓清光**。
# 原来它解析失败所以没炸；一修漏平就把这颗雷踩响了。
# 只认 <平仓动词> [副词/介词] everything 这一种形状，中间最多隔 12 个字符
# （"out of everything" / "closing everything" / "sold basically everything"）。
_BULK_EVERYTHING_RE = re.compile(
    r"\b(?:clos(?:e|ing|ed)|sell(?:ing)?|sold|trim(?:med|ming)?|dump(?:ed|ing)?"
    r"|cut(?:ting)?|out)\b[^.\n]{0,12}?\beverything\b",
    re.IGNORECASE,
)

# 当前动作（gerund / 完成时）—— 真要动手的信号
# 与 signal_parser 的 STRONG/WEAK_CLOSE_RE 保持同步，否则 detect_action 说 CLOSE
# 但这里 _has_action_verb 说没动词 → close_parser 返回 None（7/3 "all out TSLA" 案例）
#
# 多词 "out" 短语必须带词边界匹配——之前用 plain substring，
# "overall outlook" 跨词边界含 "all out"（over[all out]look），
# 把普通 trim 误升级成 100% 全平（实测 "Trimmed SPY ... overall outlook" 案例）。
ACTION_VERBS = [
    "trimming", "trimmed",
    # [9/9 实锤] 这两条的尾空格是拿来当词边界用的，于是**动词后面跟标点就整条漏掉**：
    # "TSLA 3.90 💵 new high of day trim, stop at entry now" 里的 `trim,` 不匹配
    # `"trim "`，_has_action_verb 返回 False，parse_close 返回 None ——
    # 而 detect_action 的 `trim(?:med|ming)?\b` 认得它，于是又是一次
    # "路由认得、解析不认得"（同族前案 7/3 "all out TSLA"）。
    # 当晚还连累了中文：ZH 孪生解析完全正确，却被 zh-twin guard 挡掉了 ——
    # 那道防护的前提是"EN 判为非平仓指令是可信的"，这次 EN 是误判。
    # 词边界现在交给 _BARE_ACTION_VERB_RE，下面两条保留只为记录意图（是它的子集）。
    "trim ",                  # 祈使式 "trim SPY runner at 3.10"（7/9 实测漏接）
    "cutting", "cut ",        # 边界见 _BARE_ACTION_VERB_RE（不许匹配 scout/circuit）
    "selling", "sold here",
    "closing", "closed",
    "dumping", "dumped",
    "scaling out",
    # [8/28 实锤漏平] "$ALAB - Scale out. Congrats to all!!!" —— 祈使式，
    # 词表只有 -ing 形，整条落到 OPEN 分支的 [parser] no signal。
    # 代价比一般漏平重：ALAB 是 category=lotto，按设计**不挂 TP、不挂 SL**
    # （tp_watcher 的 LADDER 里没有 lotto），喊单员的平仓信号是它唯一的
    # 主动出场路径。那条信号发出时 +300%，14 分钟后 +400%，当天到期。
    # 中文孪生是"扩展"（scale out 的机翻误译），词表接不住，只能靠 EN 这条。
    "scale out", "scaled out",
    "scaling down",           # 7/6 "Scaling down to 1/2 position sizing"
    "bang!", "bang -",        # KC 的情绪触发词，通常配 trim
    # enrich 止盈口头禅（7/17 "$XOM LOCK THEM ALL ON" / "lock them in!"）。
    # 两词 substring 避免误伤 blocked/clock；过去式 "locked in" 是 recap，
    # 与 detect_action 的 lock(?:ing)? 规则一致地排除
    "lock them", "lock it", "lock in", "lock these", "locking in",
]

# "out N%"（7/25 加：enrich "$LLY - Out 25% more."）的**唯一**来源。
# signal_parser.WEAK_CLOSE_RE 的路由判定 import 这个常量——两处必须逐字一致，
# 否则会出现"路由成 CLOSE 但 parse_close 返 None"（或反过来）的漏单裂缝，
# 各写一份字面量迟早漂移。
#
# 前缀排除的是行情解说里的 "<动词> out N%"（不是减仓动作）：
#   "IV crush knocked/knocking out 25% of the premium"  ← 权利金被打掉
#   "shaking out 20% of weak hands"                     ← 洗盘
# Python re 的 lookbehind 必须定长，所以每个词形单独写一条。刻意**不**排除
# 通用的 "-ing + out"：真实减仓句式里 "scaling out 50%" / "taking out 30%"
# 正是这个形状，一刀切会把真 trim 一起砍掉。
_OUT_PCT_PATTERN = (
    r"(?<!knock\s)(?<!knocks\s)(?<!knocked\s)(?<!knocking\s)"
    r"(?<!shake\s)(?<!shakes\s)(?<!shaken\s)(?<!shook\s)(?<!shaking\s)"
    r"\bout\s+\d{1,3}\s*%"
)

# 裸 "out <TICKER>" —— 8/3 AMZN 事故的直接成因。KC "out AMZN -15%" 是当晚
# 唯一一句清仓喊话，EN 侧 detect_action 判成 OPEN（STRONG 只认 "out of"，
# WEAK 只认 out half/full/majority 和 out N%），ZH 孪生"减持亚马逊 -15%"
# 落到 trim 默认 33 → runner-preserve 跳过。双语冗余同时降级 = 仓位过夜，
# 隔天 KC 已经走人我们还在场内。与 _OUT_PCT_PATTERN 同为 signal_parser
# 路由与本模块解析的**唯一**来源（两处 import 同一常量，见下方 WEAK_CLOSE_RE）。
#
# 三道安全边界（CLOSE 误平代价 > OPEN 误触发，见模块顶部）：
#   1. ticker 必须**原文全大写** —— 用 (?-i:) 局部关掉 IGNORECASE。不这样做
#      "out of the money" 里的 of/the 都是合法 [A-Z]{2,5}，每条行情解说都变平仓。
#      代价：判定必须喂原文而非 text_lower（见 _has_action_verb 的 text 参数）；
#   2. 停用词表挡掉大写语境里仍会出现的介词/黑话（OUT OF/ITM/EOD/EST…）；
#   3. 抽出来的裸 ticker 最终仍要过 open_symbols 白名单（_extract_symbols）——
#      本 pattern 只回答"这句话是不是平仓动作"，不负责选仓位。
_OUT_BARE_SYM_STOPWORDS = (
    "OF|THE|ALL|AT|ON|IN|TO|FOR|AND|BUT|NOW|HERE|THERE|SOON|TODAY|EARLY|LATE|"
    "FLAT|HALF|FULL|MOST|BE|EOD|ITM|ATM|OTM|IV|PM|AM|ET|EST|EDT|DTE|LOL"
)
_OUT_BARE_SYM_PATTERN = (
    r"\bout\s+(?:\$[A-Z]{1,5}\b"
    r"|(?!(?:" + _OUT_BARE_SYM_STOPWORDS + r")\b)[A-Z]{2,5}\b)"
)

# "out 1/2" —— 8/3 enrich "$TSLA out 1/2" 漏路由。FRACTION_OUT_PATTERN 早就认得
# 这个形状，但它在 pct 抽取阶段才跑，而路由/动词判定只认 out half/full/majority
# → 根本走不到那一步（分数管道形同虚设）。
# 分子分母**显式枚举**成 num<den 且 den∈[2,5]，与 _fraction_pct 的定义域逐条对齐：
# 这样 "out 7/13" / "out 8/7" 这类到期日不会被当分数路由成 CLOSE（落到定义域外
# 会让 pct 掉回 33 默认值 = 凭空 trim）。
_OUT_FRACTION_PATTERN = (
    r"\bout\s+(?:1\s*/\s*[2-5]|2\s*/\s*[3-5]|3\s*/\s*[45]|4\s*/\s*5)\b"
)

# "chop(ping) in half" —— 8/10 enrich 收盘前 3 分钟
# "$DELL - gross price action into the end of the day - I will hold a 1% lotto
# position - chopping in half"（ZH 孪生"…削减一半"）双语双漏：detect_action
# 压根没路由成 CLOSE（日志实证：两条都走了 OPEN 侧的 Parse failed）。
# 当晚零损失（无 DELL 持仓），但与"放开倒序分隔符"那条修复是**耦合**的——
# 改完 parser 后 DELL 那族信号能自动买入，平仓动词再接不住就是"买得进卖不出"，
# 比原来的漏单更糟（该标的当日收盘 -52%）。
#
# **必须带 "in half"**：裸 chop/chopping 在盘面黑话里是"横盘震荡"
# （"market is chopping around"、"choppy price action"），单独入表等于每条
# 行情吐槽都变平仓指令。同理不收 "chopped"（过去式多为 recap）。
_CHOP_HALF_PATTERN = (
    r"\bchop(?:ping|s)?\s+(?:it\s+|them\s+|these\s+|the\s+position\s+)?in\s+half\b"
)

# "out the rest" —— 8/18 AMZN + 8/19 SPY 连续两晚同一个死法，双语双漏。
# EN 侧的成因很绕：_OUT_BARE_SYM_PATTERN 认得 "out <TICKER>"，但 out 后面紧跟的是
# `the`，而 `THE` 就在 _OUT_BARE_SYM_STOPWORDS 里（那张表挡的是 "out of the money"
# 这类行情解说）→ 整条不匹配，也没有别的 ACTION_VERBS 命中。
# **不放宽 THE 停用词**（那会让 "out of the money" 变平仓指令），而是单开一条
# 显式短语。语义是全平（"out the rest" = 清掉剩下的），所以同时进
# _OUT_FULL_CLOSE_RE —— 8/19 那次就是因为 pct 掉回 33 又被 runner-preserve
# 吃掉，2 张 SPY 从 3.45 一路拿到 EOD 的 1.71。
_OUT_REST_PATTERN = r"\bout\s+(?:the\s+)?rest\b"

# "took another off at 1.98" —— 8/18 AMZN 同一晚的另一条（ZH 孪生降级成 trim 33%）。
# 也收 "took off 10% more"（8/21 UBER 实测）。
# **必须带 another 或紧跟的百分比/分数**：裸 "took off" 是行情黑话
# （"the stock took off" = 起飞），单独入表等于每条上涨评论都变平仓指令。
_TOOK_OFF_PATTERN = (
    r"\b(?:took|taking|take)\s+"
    r"(?:another\b|(?:\d{1,3}\s*%|\d{1,2}\s*/\s*\d{1,2})\s+)"
    r"[^.\n]{0,20}?\boff\b"
    r"|\b(?:took|taking|take)\s+off\s+(?:\d{1,3}\s*%|\d{1,2}\s*/\s*\d{1,2})"
)

# "runners only SPY @ 3.28" —— KC 的固定离场句式（8/20 一晚出现 3 次，全漏）。
# 语义是"我已经减到只剩 runner 了"，**不是**"我还拿着"。ZH 机翻成"仅持仓X @ 3.28"
# 更糟：`持仓` 在 signal_parser.SKIP_KEYWORDS 里，直接被判成 holding 语境跳过。
#
# 两侧都**要求跟着 @价格**：带价格的是离场播报，不带的是持仓陈述
# （对照 8/20 01:56 "1 runner left for 490 in the money" / "490行权价剩余1个持仓"
# —— 无 @价格，判成 holding 是对的，那两条当晚也确实该跳过）。
# pct 不显式给出 → 落默认 33，最后一张仍由 runner-preserve 兜住。
_RUNNERS_ONLY_PATTERN = r"\brunners?\s+only\b[^.\n]{0,24}?@\s*\.?\d"
_ZH_RUNNERS_ONLY_PATTERN = r"仅持仓[^。！？\n]{0,24}?[@＠]\s*\.?\d"

# "Down to 1/2." —— 9/1 enrich 对 COIN 连喊两次减仓，双语双漏（"$COIN - Down to 1/2."
# / "$COIN - 降至 1/2。"）。成因与 8/3 的 "out 1/2" 一模一样、只是换了个介词：
# FRACTION_DOWN_TO_PATTERN（"剩 X/Y → 卖 1-X/Y"）7/6 起就在 pct 抽取阶段等着，
# 而**动词判定与路由都不认这个形状** → 走不到那一步，整条落 [parser] no signal。
#
# 分子分母的枚举与 _OUT_FRACTION_PATTERN 逐字同源（num<den 且 den∈[2,5]，
# 对齐 _fraction_pct 的定义域）：拦住 "down to 7/13" 这类到期日被当分数。
# **必须跟分数**：裸 "down to" 是行情叙述（"down to 2.50 support"、
# "down to runners" 是 KC 黑话且无比例），单独入表就是每条回调评论都变平仓。
_DOWN_TO_FRACTION_PATTERN = (
    r"\bdown\s+to\s+(?:1\s*/\s*[2-5]|2\s*/\s*[3-5]|3\s*/\s*[45]|4\s*/\s*5)\b"
)
_ZH_DOWN_TO_FRACTION_PATTERN = (
    r"(?:降至|降到|减至|减到)\s*(?:1\s*/\s*[2-5]|2\s*/\s*[3-5]|3\s*/\s*[45]|4\s*/\s*5)"
)

# "Start securing some." —— 9/1 同一晚同一个标的的另一条（ZH "开始确保一些。"）。
# 无数字 → pct 落默认 33。
#
# **绝不收裸 secure/securing**，这是本条最要紧的边界，而且同一晚就有现成反例：
#   00:59 "If you are green in this trade - make sure it stays that way."
#         ZH "确保它保持这样" —— 纯鼓励，收裸词当场误平；
#   8/18  "out the rest of AMZN to secure small green trade" —— 目的状语；
#   7/23  "stop at entry now to secure green trade" —— 移动止损子句，
#         lessons #? 里明写着"EN 孪生本来就无 EN 动词，安全"，收裸词就把
#         那条既有的安全结论推翻了。
# 只认**减仓动作的宾语**（some / a few / half / part / profits），
# 而上面三条反例的宾语都是 "green trade" / "it"。
_SECURE_SOME_PATTERN = (
    r"\bsecur(?:e|ing)\s+(?:some|a\s+few|half|part|profits?)\b"
)
_ZH_SECURE_SOME_PATTERN = r"确保(?:一些|一部分|部分|一点|少量)"

# "out" 短语统一走词边界 regex（勿放回 ACTION_VERBS/FULL_CLOSE_VERBS 的
# substring 匹配——见上方 "overall outlook" 案例）
_OUT_PHRASE_RE = re.compile(
    r"\ball\s+out\b|\bout\s+(?:half|full|majority)\b"
    r"|" + _OUT_PCT_PATTERN
    + r"|" + _OUT_FRACTION_PATTERN
    + r"|(?-i:" + _OUT_BARE_SYM_PATTERN + r")"
    + r"|" + _CHOP_HALF_PATTERN
    + r"|" + _OUT_REST_PATTERN,
    re.IGNORECASE,
)
# 裸 "out AMZN" 无 %/分数时是**全平**语义（"out X" = 清掉 X），
# 与 "all out" / "out full" 同级；带了 %/分数的走 _extract_pct 前面的分支，
# 到不了这里。
_OUT_FULL_CLOSE_RE = re.compile(
    r"\ball\s+out\b|\bout\s+full\b"
    r"|(?-i:" + _OUT_BARE_SYM_PATTERN + r")"
    + r"|" + _OUT_REST_PATTERN,
    re.IGNORECASE,
)

# 全平动词（pct 缺省 → 100）
FULL_CLOSE_VERBS = ["closed", "cutting", "cut ", "dumped", "dumping"]

# 提取百分比："25%" / "20 %"
# 排除 `-15%` `+30%` 这类 PnL 标注（前面有符号/数字 → 不是 trim 比例）
# 排除 "1% position" / "99% cash" / "2% 的仓位/头寸" 这类**仓位大小标注**
# （7/8 实测：enrich "Closing all positions ... this is a 1% position -
# I am 99% cash" 被读成 trim 1%，BULK 遍历全部持仓刷了 8 连 TG）
#
# [8/10 DELL] 仓位标注和名词之间会夹一个修饰词："I will hold a 1% **lotto**
# position - chopping in half"。老 lookahead 只认紧邻的 position，于是 1% 被当成
# trim 比例 → EN 侧 pct=1，而 ZH 孪生"削减一半"走"一半"→50，**同一条消息双语
# 解析出两个比例**（谁先到就按谁执行，另一条被指纹 dedup 吞掉）。
# 放宽成"可夹一个**仓位形容词**"，用白名单而不是通配 \w+：通配会把
# "trimming 50% of position" 的 "of" 也当修饰词，把真 trim 比例排除掉，
# pct 悄悄掉回默认 33（比原缺陷更贵）。只收描述仓位大小/性质的词。
_PCT_SIZE_ADJ = r"(?:lotto|starter|core|runner|swing|scalp|day|initial|small|full|tiny)"
_PCT_SIZE_NOUN = r"(?:position|pos\b|sizing|cash)"
PCT_PATTERN = re.compile(
    r"(?<![-+\d.])(\d{1,3})\s*%"
    r"(?!\s*(?:(?:" + _PCT_SIZE_ADJ + r"\s+)?" + _PCT_SIZE_NOUN
    + r"|的?\s*仓位|的?\s*头寸|的?\s*现金))",
    re.IGNORECASE,
)

# 提取 $SYMBOL（强信号）
DOLLAR_SYM_PATTERN = re.compile(r"\$([A-Z]{1,5})\b")

# 提取裸 SYMBOL（弱信号，需 open_symbols 白名单验证）
BARE_SYM_PATTERN = re.compile(r"\b([A-Z]{2,5})\b")

# Chinese 友好版：用 lookahead/lookbehind 在字母/数字边界判定，
# 这样 "减仓IWM" 也能正确抓 IWM（Python re 把中文当 \w，\b 在汉字-字母处不触发）
BARE_SYM_PATTERN_ZH = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5})(?![A-Za-z0-9])")

# === "还拿着"语境排除（7/10 near-miss）===
# "+100% on SPY closed out now and just have runners on the NVDA call swings"
# ——"closed out" 说的是 SPY，NVDA 是**继续持有**的对象，却被抽成 close 目标
# （雪上加霜：SPY 已平出白名单，NVDA 成了唯一命中 → 差点 100% 误平，
# 靠"无价格参照拒卖"才躲过）。出现在这些短语里的 symbol 不进 close 目标。
# 注意方向性："runners on SPY" 是持有（排除）；"SPY runner" 是被 trim 的
# 对象（"trimmed 1 SPY runner @ 4.00"），不受影响。
_HOLD_CONTEXT_TEMPLATES = (
    r"runners?\s+on\s+(?:the\s+)?\$?{sym}\b",
    r"hold(?:ing)?\s+(?:the\s+)?\$?{sym}\b",
    r"keep(?:ing)?\s+(?:the\s+)?\$?{sym}\b",
    # "All cash now besides $HOOD 1.5% position"（7/15）——besides/except
    # 后面的 symbol 是**留着**的，close 目标是"其他所有"，不是它
    r"(?:besides|except(?:\s+for)?)\s+(?:the\s+|my\s+)?\$?{sym}\b",
)
_ZH_HOLD_CONTEXT_TEMPLATES = (
    r"保留[^\n，。]{{0,8}}{sym}",
    r"持有[^\n，。]{{0,8}}{sym}",
    r"留着?[^\n，。]{{0,6}}{sym}",
    # "除了 $HOOD 1.5% 的头寸外，现在全是现金"（7/15）
    r"除了?[^\n，。]{{0,8}}{sym}",
)


def _in_hold_context(sym: str, text: str, is_zh: bool = False) -> bool:
    """symbol 是否只出现在"继续持有"语境里（runners on X / 保留X）。

    ZH 路径同时检查该 ticker 的中文名（"仅保留英伟达" 也要能排除 NVDA）。
    """
    templates = _ZH_HOLD_CONTEXT_TEMPLATES if is_zh else _HOLD_CONTEXT_TEMPLATES
    names = [sym]
    if is_zh:
        names += [n for n, t in ZH_NAME_TO_TICKER.items() if t == sym]
    for cand in names:
        for t in templates:
            if re.search(t.format(sym=re.escape(cand)), text, re.IGNORECASE):
                return True
    return False

# "N% LEFT" / "down to N% runners" / "runners only" → 卖 (100-N)%
LEFT_PATTERN = re.compile(r"(\d{1,3})\s*%\s*(?:left|remaining)", re.I)

# === 分数仓位表达（7/6 夜实测 KC 高频用法）===
# 两种语义方向，容易搞反：
#   "scaling out 1/3" / "selling 1/3" / "trimmed 1/2"       → 卖出 X/Y
#   "down to 1/3 (of my position)" / "scaling down to 1/2"  → 剩 X/Y，卖 (1 - X/Y)
# 日期防误伤（"sold my 7/13 puts"）：_fraction_pct 只认分母 2-5 且分子<分母。
FRACTION_DOWN_TO_PATTERN = re.compile(
    r"\bdown\s+to\s+(\d{1,2})\s*/\s*(\d{1,2})", re.I,
)
FRACTION_OUT_PATTERN = re.compile(
    r"\b(?:out|trim(?:med|ming)?|sell(?:ing)?|sold|scaling\s+out)\s+"
    r"(\d{1,2})\s*/\s*(\d{1,2})",
    re.I,
)
# ZH："缩减至 1/2" / "减持到 1/3" / "剩下 1/4" → 剩余语义
ZH_FRACTION_TO_PATTERN = re.compile(r"(?:至|到|剩下?|降至)\s*(\d{1,2})\s*/\s*(\d{1,2})")
# ZH："减持1/3" / "卖出1/3" → 卖出语义（动词后紧跟分数）
ZH_FRACTION_OUT_PATTERN = re.compile(
    r"(?:减持|减仓|卖出|卖了|砍掉|砍仓|抛出|抛了)\s*了?\s*(\d{1,2})\s*/\s*(\d{1,2})"
)


def _fraction_pct(num: int, den: int) -> "int | None":
    """X/Y → 百分比整数。分母 2-5 且分子<分母才当分数，否则视为日期（7/13）返 None。"""
    if den < 2 or den > 5 or num < 1 or num >= den:
        return None
    return round(num * 100 / den)

# KC 喊出的卖出价 —— 用来挂卖单限价，避免按 entry × 0.95 倒挂
# 场景：
#   `trimmed MSFT 420c @ 7.00`     ← @ + 价
#   `trimmed IWM @ 2.45`           ← @ + 价
#   `BANG! Trimmed another IWM here @ 2.80` ← @ + 价
#   `trimmed TSLA 3.00`            ← 无 @，裸 X.XX
# 排除：strike ("$420")、pct ("25%")、PnL ("-15%")、合约后缀 (420c)
PRICE_AT_PATTERN = re.compile(r"@\s*\$?(\.?\d+(?:\.\d+)?)")
# 裸 X.XX：必须带小数点，且前面不是 $ 或数字，后面不是 c/p/%/ 数字
PRICE_BARE_PATTERN = re.compile(
    r"(?<![\$\d.])(\d+\.\d{1,2})(?![cp%\d])",
    re.IGNORECASE,
)


def _extract_signal_price(scope: str) -> "float | None":
    """从 action 句 scope 抽取 KC 喊的卖出价。

    返回 None → caller fallback 到 entry-based 算法
    """
    # 两种形态都要过叙述价闸：命中就继续找下一个候选，而不是直接放弃——
    # "SPY 跌至1.50后，减仓 @ 2.48" 里第一个是叙述、第二个才是喊价。
    for pattern in (PRICE_AT_PATTERN, PRICE_BARE_PATTERN):
        for m in pattern.finditer(scope):
            if _price_is_narrative(scope, m.start(1)):
                continue
            try:
                v = float(m.group(1))
                if 0.01 <= v <= 100:  # 期权合理价区间
                    return v
            except ValueError:
                continue
    return None


# === KC 喊的 PnL (% 标注) ===
# 场景：
#   "closed NOW small day trade -15%"      → -15
#   "Closed IREN 60c for +30%"             → +30
#   "closed NOW at entry"                  → 0   （持平退出）
#   "trimmed @ break even"                 → 0
# 不同于 trim 比例 ("Selling 25%")：PnL 必须带正负号 / 或 "at entry" 之类显式标记。
# "stop(s) at entry" 是移止损备注不是持平退出（7/8 "trimmed AAPL +20% stop at
# entry" 被误标 pnl=0），用 lookbehind 排除。
PNL_SIGNED_PATTERN = re.compile(r"([+\-])\s*(\d{1,3}(?:\.\d+)?)\s*%")
AT_ENTRY_PATTERN = re.compile(
    r"(?<!stop\s)(?<!stops\s)\bat\s+(?:entry|breakeven|break\s*even|be)\b", re.I
)
# 中文版："在进场位 / 在入场位 / 保本 / 平本"
ZH_AT_ENTRY_PATTERN = re.compile(r"在\s*(?:进场|入场)位|保本|平本")


def _extract_signal_pnl(scope: str, is_zh: bool = False) -> "float | None":
    """从 action 句 scope 抽 KC 报告的 PnL（%）。

    返回 None → 没找到（不代表 PnL 为零，而是不知道）
    返回 0    → 显式 "at entry" / "保本"
    返回 -15  → "-15%"
    """
    # 显式持平
    if AT_ENTRY_PATTERN.search(scope):
        return 0.0
    if is_zh and ZH_AT_ENTRY_PATTERN.search(scope):
        return 0.0

    # 带符号 N%
    m = PNL_SIGNED_PATTERN.search(scope)
    if m:
        sign = -1.0 if m.group(1) == "-" else 1.0
        try:
            v = float(m.group(2))
            if 0 <= v <= 1000:  # 合理 PnL 范围（lotto +500% 也可能）
                return sign * v
        except ValueError:
            pass
    return None


# ============================================================
# 中文 fallback
# ============================================================
#
# 不维护中文公司名→ticker 映射（亚马逊/微软等）。
# 这类信号靠 1-3s 后到达的 EN 版本兜底；本路径主要处理：
#   - $SYMBOL（enrich 中文版常保留 $）
#   - 裸 ticker（IWM/SPY/QQQ 等不翻译的）+ 白名单消歧
# ZH 抓到 action 动词但没 symbol 时 → log [zh_unrecognized] 便于事后追踪。

# 复盘/计划 —— 出现即跳
# 注意区分 "卖出了一些" (recap) vs "又减仓了一笔" (announcement)：
# 前者必带"时"/"在 N% 时"/"昨天"等过去时间锚；后者是当下宣告
ZH_RECAP_MARKERS = [
    "昨天", "昨日", "今早", "今天早些", "早些时候",
    "我的计划", "的计划是", "计划：",
    "打算", "准备", "即将", "将要", "将把",  # 未来意图
    "时卖出", "时减仓", "时清", "时砍", "时抛",  # "在 40% 时卖出"
    # [8/5 复盘] "到目前为止控制性卖出 - 让我们的EMA追赶上来"（本地 02:03）
    # 是 EN "Controlled selling so far - " 的孪生。EN 侧这次靠 RECAP_PATTERNS
    # 的 so-far 规则拦下了，ZH 侧不补就是**白修** —— 双语各发一条，ZH 常先到
    # （历史 63%），拦住一边另一边照样把 RKLB 平掉 2%。
    # 用 "目前为止" 而非 "到目前为止"，顺带覆盖"截至目前为止"。
    "目前为止",
    # 注：EN 侧同批补的 "will trim" 未来意图，ZH 孪生
    # "将在股价接近19美元时减仓SOFI看涨期权" 已被上面的 "时减仓" 拦住
    # （实测 ZH skip (recap)），所以这里不重复加 "将减仓" 一类裸词 ——
    # 没有语料证据的裸词正是本表下方 "不加裸缩减" 那条注释要避免的。
    "了一些",  # "卖出了一些" 多为复盘；与 "了一笔" / "了一份" 区分
    "每周回顾", "观察列表", "观察名单",  # 周报/watchlist（与 EN 侧对齐）

    # [8/14 SPCX] 昨晚离误平最近的一次。原文：
    #   ZH  "临收盘SPCX跌得漂亮，早间利润减仓后仍持有看跌期权✅" → **判成 CLOSE 33%**
    #   EN  "nice drop on SPCX into end of day, still in the puts after the
    #        profit trims this morning ✅"                        → 正确 no signal
    # 三个因素叠出来的：`减仓` 在 ZH_ACTION_VERBS；`早间` 不在本表（有 今早 /
    # 今天早些 / 早些时候，唯独缺它）；整句用逗号不用句号，_zh_action_sentences
    # 切不开，SPCX 是拉丁 ticker 所以 symbol 照样抽得到。
    # **只因为 SPCX 当时剩 1 张、撞上 runner-preserve 才没卖出去** —— 剩 2 张
    # 这条明说"仍持有"的复盘就会直接减掉 1 张。
    # `早间` 补进过去时间锚那一组（与 今早 同族）。
    "早间",
    # "仍持有 / 仍在持有" 是比时间锚更硬的证据：它明说仓位还在，不可能是平仓指令。
    # EN 侧对应的是 signal_parser.SKIP_KEYWORDS 里的 "still holding"（那边是
    # 开仓路径的守卫），close 路径的 ZH 侧一直没有等价物。
    # 同批实测的良性样本：03:19:38 "$ASTS 仍在持有" 当时落到 no signal，
    # 属于侥幸而不是被拦住。
    # 仍收带前缀的形态、不加裸"持有"——理由同 signal_parser.SKIP_KEYWORDS 的
    # 注释：裸"持有"会误伤"买入 X 打算持有到 9 月"这类真信号。
    "仍持有", "仍在持有", "还持有", "还在持有",
]

# bulk action
ZH_BULK_MARKERS = ["所有持仓", "全部持仓", "全部仓位"]

# 当前动作动词
# 7/6 加 减持 / 缩减至|缩减到（KC ZH 翻译的 "scaling out/down" 惯用词）。
# 不加裸 "缩减"——"缩减购债" 类宏观评论会误触。
# "减持" 理论上也可能出现在 "巴菲特减持苹果" 类新闻转述里，但风险与既有
# 的 卖出/砍掉 相同（channel 只有 KC bot 发言 + symbol 白名单双保险），接受。
# 7/8 加 出清（"全部出清苹果仓位"）。
ZH_ACTION_VERBS = [
    "减仓", "平仓", "清仓", "全平", "清空",
    "卖出", "卖了",
    "砍掉", "砍仓",
    "抛出", "抛了",
    "止盈",
    "减持", "缩减至", "缩减到",
    "出清",
    "减半",   # "减半仓于2.45"（7/9 实测；"减仓" 不是它的连续子串，接不住）
    # EN "chopping in half" 的 ZH 机翻（8/10 DELL，见 _CHOP_HALF_PATTERN）。
    # 只收带"半"的组合，不收裸"削减"——机翻里"削减利率/削减开支"太常见，
    # 而 ZH 路径一旦命中动词就直接对白名单里的持仓挂卖单。
    # pct 由既有的 "一半"→50 分支给出，不新开管道。
    "削减一半", "削减半",
    # [8/19 SPY 实锤丢单] "平掉剩余SPY仓位，这里-9.5%" 整句一个动词都没命中 ——
    # 表里有 平仓/清仓/全平/清空，唯独没有 平掉 —— 连 detect_action 都没路由成
    # CLOSE，直接落 [parser] no signal。EN 孪生 "out the rest of SPY" 同时漏
    # （见 _OUT_REST_PATTERN），2 张仓位一路留到 EOD。
    # 只进 ACTION 不进 FULL_CLOSE：**"平掉一半" 必须仍然是 50%**。
    "平掉",
    # "出半"（7/23 NBIS）不进本表：裸子串会命中"冲出半年新高"类评论，
    # 由 ZH_OUT_HALF_RE 带边界匹配后归一化成"减半"（见 _parse_close_zh 入口）
    "锁定",   # "$XOM 全部锁定"（7/17，enrich 止盈口头禅的 ZH 版）
]

# 全平动词 / 短语（pct 缺省 → 100）
ZH_FULL_CLOSE_VERBS = [
    "平仓", "清仓", "全平", "清空", "全部卖出", "全部抛", "出清",
    # [8/18 AMZN / 8/19 SPY] "平掉剩余…仓位" 的"剩余"语义 = 清掉剩下的全部。
    # 8/18 那条靠句尾的"锁定"侥幸进了 CLOSE 路径，但 pct 掉回默认 33，
    # 被 runner-preserve 吃掉；8/19 那条连路径都没进。
    # 逐条列出带"剩"的完整短语，**不收裸"剩余"**——"减仓后剩余 2 张"是陈述不是指令。
    "平掉剩余", "平掉剩下", "剩余仓位", "剩下的仓位", "剩余的仓位",
]

# 移动止损子句 —— 不是卖出指令，动词判定前整句抹掉。
# 7/23 实测："AVGO 触及下一目标 +37% ✅ 止损设在现价以锁定盈利交易"
# （EN 孪生 "stop at entry now to secure green trade" 本来就无 EN 动词，安全；
# ZH 版的"锁定"落在止损子句里 → 被 ZH_ACTION_VERBS 误判为 CLOSE pct=33。
# 当晚靠 runner-preserve + 无喊价拒卖两道防线才没误卖）。
# EN 侧对应防护见 AT_ENTRY_PATTERN 的 stop-lookbehind（7/8）。
# 抹除范围：从"止损"起、沿"止损子句内合法字符"（汉字/数字/./%/+/-）走到头。
# 白名单式终止（第二轮对抗评审改法）：em-dash——、省略号…、emoji、零宽空格这类
# 分隔符无法在否定字符类里穷举，反过来枚举"子句里会出现什么"更稳——
# "止损设在现价以锁定盈利交易"（纯汉字）、"止损位2.0"、"止损-10%" 都整段被抹，
# 而 "止损打掉——全部卖出"、"止损上移✅全部卖出"、"止损上移到$NVDA成本线"
# 在 —/✅/$ 处停下，真卖出动词和 $SYMBOL 都活下来。
# 边界规则（两轮对抗评审实锤，每条都对应一个真实误伤）：
#   1. (?<!防) 左边界——"为防止损失扩大 卖出" 里的 止损 是"防止+损失"；
#   2. 不吃前缀 把/将——否则 recap 标记"将把"被拆掉，"我将把止损上移…若跌破
#      就全部卖出" 这类计划类消息从 recap-skip 变成真卖单（recap 检查也已
#      移到掩码之前，见 _parse_close_zh）；
#   3. 真砍仓的动作动词在止损子句之外（"减仓AVGO，止损移到保本"），不受影响。
# 已知残留：全连写"止损全部卖出"（纯汉字无分隔）仍会整段被抹——正则层面无法与
# "止损设在现价"区分，宁可漏卖（有 EN 孪生兜底）不可误卖。
ZH_SL_ADJUST_CLAUSE_RE = re.compile(r"(?<!防)止损[一-鿿0-9.．%％+\-]*")

# 建议句（"若想…就…"）—— 是选项不是指令，动词判定前整句抹掉。
# 7/29 实测最危险的一条：
#   "SPY看涨期权跌至1.50后回到入场价，若想止盈离场而非持有至FOMC，现在正是时机"
# ZH 把它解析成 CLOSE 33%，还把叙述价 1.50 当卖出参照（当时实际约 2.5）。
# EN 孪生 "if you want to exit for green" 当晚返回 None 纯属侥幸——KC 写的是
# 小写 "spy"，裸 ticker 抽取要求大写才没命中；下次他写 "SPY" 就漏过去了。
#
# 为什么必须在"是不是指令"这一层挡，而不是只修价格：
# 0010 落地后 CLOSE_QUOTE_FALLBACK 默认开，"信号无喊价" 不再等于拒卖
# （改为按实时 bid 卖）。旧的"无价→拒卖"安全网没了，建议句会真成交。
#
# 边界收得很紧，只认 想（want）系：
#   "若想止盈离场"        → 建议，抹掉 ✅
#   "若此前未减仓，可在此处操作" → 祈使句（KC 让你现在做），**不抹**
# 后者同样带"若"，但动作不受"想"支配——一刀切按"若"抹会漏掉真指令。
ZH_SUGGESTION_CLAUSE_RE = re.compile(
    r"(?:若想|如果想|若你想|如果你想|想要的话)[^，,。！？\n]*"
)

# EN 对应形态："if you want/wish/'d like to <verb>"。
# 同样只认 want 系；"if you missed the trim earlier, trim here @ 2.54" 这类
# 条件-祈使句不在内（那是真指令）。
EN_SUGGESTION_CLAUSE_RE = re.compile(
    r"\bif\s+(?:you|u)\s*(?:'d|\bwould\b|\bwant\b|\bwish\b|\bcare\b)[^,.\n]*",
    re.IGNORECASE,
)

# === 条件-建议句的**否定**形态（8/3 SPY，当晚唯一一笔误平）===
#   EN "SPY puts near entry price at the close around 1.72, if you don't want to
#       swing you can close until 4:15pm EST. I personally am swinging them"
#   ZH "…若不想持仓过夜，可在美东时间下午4:15前平仓。我个人选择持仓…"
# ZH 版解析成 CLOSE 100% @1.72（1.72 本身还是"接近入场价"的行情描述），
# 按 1.63 挂单卖出——而作者本人明说在持仓过夜。
#
# 与 ZH_SUGGESTION_CLAUSE_RE（若想 系）的区别只在**抹除范围**：
#   "若想 X"        动作在从句里，抹到逗号就够；
#   "若不想 X，可 Y" 动作在**主句 Y** 里，必须抹到句末才盖得住"平仓"。
# 扩到句末只对"不想"系生效——"若此前未减仓，可在此处操作" 是真祈使句（KC 让你
# 现在做），仍然不抹，与 7/29 定的边界一致。
ZH_OPTIONAL_CLAUSE_RE = re.compile(r"(?:若|如果)(?:你)?不想[^。！？\n]*")
EN_OPTIONAL_CLAUSE_RE = re.compile(
    r"\bif\s+(?:you|u)\s+(?:do\s*n['’]?t|don['’]?t|dont)\s+want\b[^.!?\n]*",
    re.IGNORECASE,
)

# 时间状语从句 "在我<平仓动词>后…" —— 是**时间参照**不是当前动作，动词判定前整句抹掉。
# [8/20 实锤误平] enrich "这是我现在的情况。在我大部分减仓后，我会交易 $PLTR $TSLA
# 和一些 $LLY 的涨幅股。" —— EN 孪生 "I'll be swinging $PLTR $TSLA & a few $LLY
# runners after I scale out most." 是将来时 + after 从句，EN 正确判 no signal；
# ZH 侧 `减仓` 命中 ZH_ACTION_VERBS，而 ZH_RECAP_MARKERS 里没有任何时间从句标记
# → 判成当前动作，真的卖了 1 张 PLTR 180C @0.90（入场 1.09）。
#
# 边界收得很紧，三个条件同时成立才抹：
#   1. 以 `在` 开头（"减仓后止损移到保本" 这类不带 `在` 的不受影响）；
#   2. 从句里出现平仓动词；
#   3. 以 `后` 收尾，且整段不跨 ，。！？换行。
# 反例（**不该抹**）："在2.20加仓后，于2.60进行小幅安全减仓" —— 从句动词是
# 加仓不在表里，真正的 减仓 在从句之外，照常执行。
# EN 侧的同款时间状语从句守卫 —— "after I scale out most" 是时间参照不是当前动作。
#
# [8/28] 这条是补 `scale out` 到动词表**逼出来**的：8/20 那次 EN 判 no signal
# 靠的不是任何防护，而是 `scale out`（祈使/原形）压根不在词表里 —— 注释里
# 写着 "EN 正确判 no signal"，实际是**巧合正确**。把祈使式补进词表的同一秒，
# 那条 8/20 的原文就变成了真卖单（回归测试当场变红，见
# test_overnight_0818_0824::test_zh_after_clause_is_not_an_instruction）。
#
# 这正是 lesson #24 的形状：ZH 侧 8/20 补了 ZH_AFTER_CLAUSE_RE，EN 侧没补，
# 因为当时"EN 没问题"。防护的缺失被一个偶然的词表空缺掩盖了 8 天。
#
# 边界与 ZH 版对齐：after/once 引导 + 从句里出现平仓动词 + 不跨句子标点。
EN_AFTER_CLAUSE_RE = re.compile(
    r"\b(?:after|once)\s+(?:i|we|he|she|they)\s+"
    r"(?:[a-z]+\s+){0,2}?"
    r"(?:scal(?:e|ing|ed)\s+out|trim\w*|cut\w*|sell\w*|sold|clos\w*|dump\w*)"
    r"[^.!?\n]*",
    re.IGNORECASE,
)

ZH_AFTER_CLAUSE_RE = re.compile(
    r"在[^，,。！？\n]{0,12}?(?:减仓|减持|平仓|清仓|卖出|止盈|出清|平掉)[^，,。！？\n]{0,6}?后"
)

# === 作者自述"我不平，我拿着" ===
# 同一条 8/3 SPY 消息的第二道防线：作者明确说自己在持有，这条消息就不是
# 给跟单方的平仓指令，整条跳过。
# 收得很窄（第一人称 + personally/个人 + 持有动词三件齐全），避免吞掉
# "减仓一半，剩下的我继续持有" 这类**真 trim**消息的尾巴——那种句子没有
# "我个人选择"这种整体表态。
AUTHOR_HOLD_MARKERS = [
    "i personally am swinging", "i personally am holding",
    "i am personally swinging", "i am personally holding",
    "i'm personally swinging", "i’m personally swinging",
    "i'm personally holding", "i’m personally holding",
    "personally i'm swinging", "personally i’m swinging",
]
ZH_AUTHOR_HOLD_MARKERS = [
    "我个人选择持仓", "我个人选择持有", "我个人选择拿着",
    "我个人继续持有", "我个人继续持仓", "我个人持有",
    "我本人选择持仓", "我本人选择持有",
]

# 叙述价：价格前紧邻"跌至/回到/涨到"这类**行情叙述**动词时，那个数字是在
# 描述走势，不是喊卖价。7/29 "跌至1.50后回到入场价" 实锤（见上）。
# 与建议句防护是两层独立防线：建议句挡"要不要卖"，本表挡"按什么价卖"——
# 只要有一条真指令句里夹了叙述价（"减仓 SPY，之前跌至1.50"），前者就拦不住。
# 收词很克制——只收"纯粹在描述走势"的动词。以下几个**特意排除**，每个都对应
# 一次真实误伤或明确的反例：
#   触及 —— KC 用它宣布可执行位，不是叙述：
#           "#GOOGL 正在抛售！350 安全减仓区域已触及 7.00" 的 7.00 就是喊价
#           （lessons:zh_googl_sharp_prefix，加进来当场变红）；
#   最高/最低、low of/high of —— 无语料支撑，不臆测；
#   down to/up to —— "down to runners" 是 KC 高频黑话，且 "trim down to 2.50"
#           里的数字是目标不是叙述，歧义太大。
_NARRATIVE_PRICE_MARKERS = (
    "跌至", "跌到", "跌破", "涨至", "涨到", "回到", "回落至", "回落到",
    "反弹至", "反弹到",
    "dipped to", "dip to", "fell to", "dropped to", "back to",
    "rallied to", "ran to",
)
_NARRATIVE_LOOKBACK = 12


def _price_is_narrative(scope: str, price_start: int) -> bool:
    """价格 token 前 _NARRATIVE_LOOKBACK 字符内是否有行情叙述动词。"""
    window = scope[max(0, price_start - _NARRATIVE_LOOKBACK):price_start].lower()
    return any(mk in window for mk in _NARRATIVE_PRICE_MARKERS)

# "出半"（EN "out half" 的 ZH 孪生，7/23 NBIS 漏路由）——两侧都要边界：
# 右边界：裸"出半"后不能跟汉字（"冲出半年新高"类评论，7/23 对抗评审实锤）；
# 左边界：前面不能是汉字——"走出半V型反转 / 冲出半年" 的 出半 是"走出/冲出"
# 的一部分，ASCII 跟随（半V/半M 形态黑话）右边界拦不住，靠左边界拦；
# "出半仓" 变体：仓 后允许任意接续（"出半仓于2.45" 对齐 7/9 "减半仓于2.45"）。
# 命中后归一化成既有动词"减半"（pct=50 语义现成，无需新管道）。
ZH_OUT_HALF_RE = re.compile(r"(?<![一-鿿])出半(?:仓|(?![一-鿿]))")

# "出 1/2"（EN "out 1/2" 的 ZH 孪生，8/3 enrich $TSLA 双语双漏）。
# 裸"出"同样不进 ZH_ACTION_VERBS（理由见 ZH_OUT_HALF_RE），但"出 + 合法分数"
# 这个形状足够窄，可以带左边界单独收：左边界拦"退出1/2""冲出1/2"，
# 分子分母枚举（与 _OUT_FRACTION_PATTERN / _fraction_pct 同定义域）拦到期日。
# 命中后归一化成既有动词"卖出"，pct 交给现成的 ZH_FRACTION_OUT_PATTERN。
# pattern 串单独导出：signal_parser.STRONG_CLOSE_RE import 同一份做路由判定
# （与 _OUT_PCT_PATTERN 同规矩——两处各写一份字面量迟早漂移成漏单裂缝）。
_ZH_OUT_FRACTION_PATTERN = (
    r"(?<![一-鿿])出(\s*(?:1\s*/\s*[2-5]|2\s*/\s*[3-5]|3\s*/\s*[45]|4\s*/\s*5))"
)
ZH_OUT_FRACTION_RE = re.compile(_ZH_OUT_FRACTION_PATTERN)

# "剩下 30%" / "剩 N%" → 卖 (100-N)%
ZH_LEFT_PATTERN = re.compile(r"剩\s*下?\s*(\d{1,3})\s*%")


# === 公司名 → ticker 最小映射（白名单门控）===
# 7/7-7/8 实测：KC 平仓爱写公司名不写 ticker（"all out apple" / "减仓苹果"），
# EN/ZH 都抽不出 symbol → 平仓信号静默丢失（AAPL 305p 僵尸仓的直接成因）。
# 只映射高频大票，且**必须命中 open_symbols 白名单才生效**——
# 没持仓时这些词只是行情闲聊，映射了反而会误平。
EN_NAME_TO_TICKER = {
    "apple": "AAPL", "tesla": "TSLA", "amazon": "AMZN", "microsoft": "MSFT",
    "nvidia": "NVDA", "google": "GOOGL", "meta": "META", "netflix": "NFLX",
}
ZH_NAME_TO_TICKER = {
    "苹果": "AAPL", "特斯拉": "TSLA", "亚马逊": "AMZN", "微软": "MSFT",
    "英伟达": "NVDA", "谷歌": "GOOGL", "脸书": "META", "网飞": "NFLX",
}


# === BULK 例外抽取 ===
# "Closing all positions outside of the $IBM $310 lotto"（7/8 实测）——
# BULK_TRIM 不能连人家明确保留的仓位一起卖。
# EN: outside of / except (for) / other than / besides + $TICKER
# ZH: "$IBM ... 以外" / "除了 $IBM"
EXCLUDE_EN_PATTERN = re.compile(
    r"(?:outside of|except(?:\s+for)?|other than|besides)\s+(?:the\s+)?\$?([A-Z]{1,5})\b",
    re.IGNORECASE,
)
EXCLUDE_ZH_PATTERN = re.compile(
    r"\$?([A-Z]{1,5})[^\n，。]{0,15}?以外"
    r"|除了?\s*\$?([A-Z]{1,5})"
)


def _extract_exclude_symbols(text: str) -> list[str]:
    """抽 BULK 例外 symbol。EN pattern 带 IGNORECASE，要求命中的词原文全大写
    （否则 "outside of the money" 会捕到 "money"）。"""
    out, seen = [], set()
    for pat in (EXCLUDE_EN_PATTERN, EXCLUDE_ZH_PATTERN):
        for m in pat.finditer(text):
            s = next((g for g in m.groups() if g), None)
            if not s or not s.isupper():
                continue
            if s not in seen:
                out.append(s)
                seen.add(s)
    return out


def _has_recap_marker(text_lower: str) -> bool:
    return (
        any(m in text_lower for m in RECAP_MARKERS)
        or any(p.search(text_lower) for p in RECAP_PATTERNS)
    )


def _has_bulk_marker(text_lower: str) -> bool:
    return (
        any(m in text_lower for m in BULK_MARKERS)
        or bool(_BULK_EVERYTHING_RE.search(text_lower))
    )


# took-off / runners-only 不属于 "out" 家族，单开一个 RE（两条都自带边界条件：
# took 必须跟 another 或百分比，runners only 必须跟 @价格，见各自常量注释）
_EXTRA_ACTION_RE = re.compile(
    _TOOK_OFF_PATTERN + r"|" + _RUNNERS_ONLY_PATTERN
    # [9/1 COIN] down-to 分数 / securing 宾语，两条都自带边界（见各自常量注释）
    + r"|" + _DOWN_TO_FRACTION_PATTERN
    + r"|" + _SECURE_SOME_PATTERN,
    re.IGNORECASE,
)


# 裸动词的词边界。\b 而不是尾空格：标点 / 换行 / 句末后面的 `trim` `cut` 同样算动作。
# "scout" / "circuit" 不会命中（\b 要求 c 前面是非词字符）。
_BARE_ACTION_VERB_RE = re.compile(r"\b(?:trim|cut)\b", re.IGNORECASE)


def _has_action_verb(text_lower: str, text: str = "") -> bool:
    """text 传**原文**时才能命中裸 "out <TICKER>"（大写敏感，见 _OUT_BARE_SYM_PATTERN）。

    默认空串保持旧调用方（只有 text_lower）的行为逐字不变。
    """
    return (
        any(v in text_lower for v in ACTION_VERBS)
        or bool(_BARE_ACTION_VERB_RE.search(text_lower))
        or bool(_OUT_PHRASE_RE.search(text_lower))
        or bool(text and _OUT_PHRASE_RE.search(text))
        or bool(_EXTRA_ACTION_RE.search(text_lower))
    )


def _has_full_close_verb(text_lower: str, text: str = "") -> bool:
    """同 _has_action_verb：裸 "out <TICKER>" 要靠原文判定大小写。"""
    return (
        any(v in text_lower for v in FULL_CLOSE_VERBS)
        or bool(_OUT_FULL_CLOSE_RE.search(text_lower))
        or bool(text and _OUT_FULL_CLOSE_RE.search(text))
    )


# 分句 scope 用：单词动词加 \b 边界；"out" 短语与 _OUT_PHRASE_RE 同边界规则
# （裸 out <TICKER> / out 分数同样入表，否则 "out AMZN -15%, bored…" 分不出
# action 句，signal_price / strike hint 都退化到全文扫描）
_ACTION_RE = re.compile(
    r"\b(?:trimming|trimmed|cutting|selling|closing|closed|dumping|dumped)\b"
    r"|\btrim\b|\bcut\b|\bsold\s+here\b|\bscaling\s+(?:out|down)\b|bang!|\bbang\s+-"
    r"|\ball\s+out\b|\bout\s+(?:half|full|majority)\b"
    r"|" + _OUT_FRACTION_PATTERN
    + r"|(?-i:" + _OUT_BARE_SYM_PATTERN + r")"
    + r"|" + _CHOP_HALF_PATTERN
    + r"|" + _OUT_REST_PATTERN
    + r"|" + _TOOK_OFF_PATTERN
    + r"|" + _RUNNERS_ONLY_PATTERN
    + r"|" + _DOWN_TO_FRACTION_PATTERN
    + r"|" + _SECURE_SOME_PATTERN,
    re.IGNORECASE,
)


def _action_sentences(text: str) -> str:
    """返回包含动作动词的句子拼接。

    用 . ! ? 分句，但避免在小数点处切（`@ 2.45` 必须保持完整）。
    场景：'closed the NOW small day trade -15%. Just hanging out now after 3/3 on AMZN MSFT swings.'
    第一句有 closed 抓 NOW；第二句是复盘，不该抓 AMZN/MSFT。
    若没有任何句子命中（整段一句话），fallback 用整段 —— 不漏召回。
    """
    # 分隔符：句末 .!? 后跟空白（且 .!? 前后不是数字 → 排除小数点）
    sentences = re.split(r"(?<!\d)[.!?](?!\d)\s+|(?<=[.!?])(?=\s)", text)
    hit = [s for s in sentences if _ACTION_RE.search(s)]
    return " ".join(hit) if hit else text


def _extract_strike_hint(scope: str, symbols: list, full_text: str = "") -> tuple:
    """从 close 文本里抽 strike + side hint。

    场景背景：6/30 KC 发 "all out TSLA 420c @ 15.35"，但我们持仓是 TSLA 425c
    （我们抄的 enrich 信号）。旧 parser 只看 symbol → 抽到 TSLA → 关掉我们 425c。
    这次因为 420c/425c 同方向同到期日同 ITM，价差很小，意外赚了大钱。
    下次未必有这种运气：KC 平 TSLA put 时我们的 TSLA call 也会被错平。

    策略：只在文本里**显式给出 strike** 时返回 hint。无 strike → None，
    保持旧"symbol-only"语义不变（不破坏没 strike 的 trim 消息行为）。

    ⚠️ 必须 scope 没找到时**回退全文**（7/10 事故）：
    "SPY 755c IN THE MONEY! Closed @ 4.40" 分句后 "SPY 755c" 在感叹句、
    动作在下一句 → 只扫 scope 时 hint 丢失 → symbol-only 匹配把我们的
    SPY **put** 当成 KC 的 755 **call** 平掉了。pattern 本身 symbol 锚定
    （要求 "SYM 数字c/p" 紧邻），全文回退的误报风险很低。

    支持的写法（symbol 在前，strike+side 紧邻）：
      "TSLA 420c", "SPY 748c", "AMZN 255 calls", "MSFT 420put"
      ZH: "TSLA 420c" (KC ZH 翻译里 strike 通常保留 Latin)

    Args:
        scope: 含 close action 的句子片段（优先搜索——动作句里的 hint 最可信）
        symbols: 已抽出的 symbols 列表（用于"靠近"判断）
        full_text: 原始全文，scope 未命中时回退

    Returns:
        (strike: float, side: "CALL"|"PUT") 或 (None, None)
    """
    if not symbols:
        return (None, None)
    # 第一个 symbol 是主对象
    sym = symbols[0]
    # 匹配 "SYM 数字 c/p" 或 "SYM 数字 call(s)/put(s)"，最多隔 3 个空白字符
    pat = re.compile(
        rf"\b{re.escape(sym)}\s+(\d+(?:\.\d+)?)\s*(c\b|p\b|calls?|puts?)",
        re.IGNORECASE,
    )
    m = pat.search(scope) or (pat.search(full_text) if full_text else None)
    if not m:
        return (None, None)
    strike = float(m.group(1))
    side_raw = m.group(2).lower()
    side = "CALL" if side_raw.startswith("c") else "PUT"
    return (strike, side)


def _extract_symbols(text: str, open_symbols: set[str]) -> list[str]:
    """抽 symbol。优先 $SYMBOL；只有完全没有 $ 标记时才 fallback 到裸 SYMBOL。

    设计原因：enrich 长消息里"$HOOD - NOW ITM"，NOW 在持仓白名单也会被误匹配。
    既然作者用了 $ 标注，就只信 $ —— 这是 KC/enrich 的明确约定。
    KC Bot 风格（"trimmed AMZN"）不带 $，才需要白名单兜底。

    扫描范围限制在含 action 动词的句子内，避免抓到下一句 commentary 里的 ticker。
    """
    # 两遍扫描：先 action 句 scope，没抓到再 fallback 全文
    # 场景：'$HOOD - Nobody let these go red. Selling 25% here.' —— $HOOD 在第一句，
    # 动作在第二句，分句后 action scope 没 $HOOD，需要 fallback。
    # 每层出结果前都过 hold-context 过滤（"runners on the NVDA" 的 NVDA 不是
    # close 目标）；过滤后为空则继续下一层/下一个 scope。
    def _keep(cands: list) -> list:
        return [s for s in cands if not _in_hold_context(s, text)]

    for scope in (_action_sentences(text), text):
        found = []
        seen = set()
        for m in DOLLAR_SYM_PATTERN.finditer(scope):
            s = m.group(1)
            if s not in seen:
                found.append(s)
                seen.add(s)
        kept = _keep(found)
        if kept:
            return kept
        for m in BARE_SYM_PATTERN.finditer(scope):
            s = m.group(1)
            if s in seen:
                continue
            if s in open_symbols:
                found.append(s)
                seen.add(s)
        kept = _keep(found)
        if kept:
            return kept
        # 3. 公司名兜底（"all out apple"）——只认已持仓的映射，见 EN_NAME_TO_TICKER 注释
        scope_lower = scope.lower()
        for name, tick in EN_NAME_TO_TICKER.items():
            if tick in open_symbols and tick not in seen \
                    and re.search(rf"\b{name}\b", scope_lower):
                found.append(tick)
                seen.add(tick)
        kept = _keep(found)
        if kept:
            return kept
    return []


def _extract_pct(text: str, text_lower: str) -> int:
    """提取卖出百分比。

    规则（优先级从上到下）：
    - "30% LEFT" / "30% remaining" → 卖 70%
    - "down to 1/3" → 剩 1/3，卖 67%（剩余语义，先查——
      "Scaling out more. Down to 1/3" 两个方向的词都在，剩余语义才是对的）
    - "scaling out 1/3" / "sold 1/2" → 卖 33% / 50%
    - "Selling 25%" → 卖 25%
    - 无数字：FULL_CLOSE_VERBS → 100%，否则 33%（trim 默认）

    扫描范围限制在 action 句内，避免抓到 commentary 里的 PnL %。
    PCT_PATTERN 已经排除了 -N% / +N%（PnL 标注）。

    TODO: KC "trimmed @ price" 没有 %，默认 33% 是经验值，实测后调
    """
    scope = _action_sentences(text)

    left_m = LEFT_PATTERN.search(scope)
    if left_m:
        n = int(left_m.group(1))
        return max(1, min(100, 100 - n))

    # 分数：先 action 句 scope，没有再全文兜底。
    # KC 惯用两句式 "Scaling out more. Down to 1/3 of my position."——
    # 分数落在 action 句外，只扫 scope 会漏（7/6 实测误判成默认 33）。
    # 全文兜底安全性：recap 已在上层整条过滤；symbols / signal_price
    # 仍然严格限 scope（那两个扫全文才有错平/错价风险，pct 没有）。
    for search_space in (scope, text):
        m = FRACTION_DOWN_TO_PATTERN.search(search_space)
        if m:
            frac = _fraction_pct(int(m.group(1)), int(m.group(2)))
            if frac is not None:
                return max(1, min(100, 100 - frac))
        m = FRACTION_OUT_PATTERN.search(search_space)
        if m:
            frac = _fraction_pct(int(m.group(1)), int(m.group(2)))
            if frac is not None:
                return max(1, min(100, frac))

    pct_m = PCT_PATTERN.search(scope)
    if pct_m:
        n = int(pct_m.group(1))
        return max(1, min(100, n))

    # 显式份额短语：比 33% 默认值语义更强，但弱于明确的数字 %
    # "out half" = 卖一半；"out majority/most" = 卖大部分（75% 经验值，实测调整）
    # "chopping in half" 同为 50%（8/10 DELL，见 _CHOP_HALF_PATTERN）——不写这条
    # 就会掉回默认 33，和 ZH 孪生"削减一半"（"一半"→50）指纹不一致
    if re.search(r"\bout\s+half\b", scope, re.I) or re.search(_CHOP_HALF_PATTERN, scope, re.I):
        return 50
    if re.search(r"\bout\s+(?:majority|most)\b", scope, re.I):
        return 75

    return 100 if _has_full_close_verb(text_lower, text) else 33


# 中继机器人的发言人标签 —— 解析前整段剥掉。
#
# [8/28 起] KC 频道的消息多了一层中继前缀，格式从
#     "@everyone\nKC Trades Bot:trimmed AAPL 330c ..."
# 变成
#     "核心期权交易综合BOT: KC Trades Bot:trimmed AAPL 330c ..."
#     "核心期权交易综合BOT: :red_circle:KC交易机器人：减仓340c @ 4.30 🚀"
#
# 后果（8/29 实测）：ZH 侧的 symbol 抽取是**自由文本 + 持仓白名单**，
# `综合BOT` 里的 BOT、`KC交易机器人` 里的 KC 都被当成 ticker 抽了出来：
#
#     [close_parser] ZH close intent, symbols=['BOT', 'KC'] 不在当前持仓 —— 跳过
#
# 那一晚零损失（BOT / KC 都不在持仓白名单，被正确挡掉），但两处代价是真的：
#   1. 它污染 `[zh_unrecognized]` 的计数口径 —— 那个 warning 的**唯一用途**
#      就是攒样本判断要不要做中文公司名映射（ROADMAP P1 #13），
#      混进"发言人标签"这种噪音，样本就没法用了；
#   2. 哪天持仓里出现同名票（BOT 是真实存在的 ticker），就是一次误平。
#
# **为什么只在 close 路径剥、不在 open 路径剥**：两条路径对前缀的暴露面不同。
# open 侧走的是结构化模板（锚在 `$TICKER` + 行权价 + calls/puts 上），
# 实测同一条消息加不加前缀都解析成同一个信号（回归见
# test_overnight_0829::test_relay_prefix_does_not_affect_open_path）。
# 只有 close 侧的自由文本抽取会被标签污染。lesson #24 要求的是"检查另一侧"，
# 不是"无条件两边都改" —— 这里检查过了，结论是 open 侧不需要。
#
# 边界收得紧，避免把正文吃掉：
#   - 只从**开头**剥，且最多剥 3 段（观察到最多两层：中继 + 喊单机器人）；
#   - 每段必须短（≤ 24 字符）且含 BOT / bot / 机器人 —— 正文里的
#     "trimmed AAPL 330c" 不含这些词，剥不到；
#   - 顺带吃掉两段之间的 :emoji_shortcode:（`:red_circle:`）。
#   - 允许标签前面先有一行 `@everyone`（Discord 提及，纯噪音）：**老格式**
#     `@everyone\nKC Trades Bot:...` 同样会漏 `KC` 进 symbols（8/28 日志实测
#     `symbols=['KC'] 不在当前持仓`），只是没人注意 —— 一起洗掉。
_RELAY_LABEL_RE = re.compile(
    r"^\s*(?:@everyone\s*)?"               # 可选的 @everyone 提及
    r"(?::[a-z_]{2,20}:\s*)?"              # 可选的 :emoji_shortcode:
    r"[^\n:：]{0,24}?(?:BOT|Bot|bot|机器人)\s*[:：]\s*",
)


def strip_relay_prefix(text: str) -> str:
    """剥掉开头的中继/喊单机器人标签。剥不动就原样返回。"""
    if not text:
        return text
    for _ in range(3):
        new = _RELAY_LABEL_RE.sub("", text, count=1)
        if new == text:
            break
        text = new
    return text


def _drop_unattributable_price(symbols: list, signal_price, lang: str, text: str):
    """多标的 + 单一喊价 → 丢弃喊价，返回 (price, unattributable?)。

    [8/28 实锤，唯一真正动了钱的一条] KC 的战报
    "got so wrapped up in SPY and AAPL I didn't see that UNG order filled for
    1.90 trim as it approached the 10.75 level" 抽出 symbols=['AAPL','UNG']、
    price=1.9 —— 1.90 是 **UNG** 的成交价，却被拿去给 **AAPL 330c** 定限价：
    1.9 × 0.95 = 1.80 挂单卖出，而同一时段喊单员报的 AAPL 330c 是 5.80-6.00。

    喊价天然是**单个合约**的属性。一句话里出现两个标的时，没有任何可靠依据
    把它归给其中之一 —— 归错的代价是限价差出数倍。所以这里不猜：丢掉喊价，
    让 close_flow 落到既有的 quote fallback（逐合约取实时报价定限价，7/25
    AVGO 那条路径），并让调用方发一条 TG 说明。

    **不拦执行**是刻意的：`Trimmed $TSLA 420c and $MSFT here @ 7.00` 这类
    多标的平仓是既有契约（test_multi_symbol_close_hint_only_scopes_first_symbol），
    不可归属的是**价格**不是**意图**。降级到实时报价既止住串台，又不丢平仓动作。
    """
    if signal_price is None or len(symbols) <= 1:
        return signal_price, False
    logger.warning(
        f"[close_parser] {lang.upper()} 多标的 {symbols} + 单一喊价 "
        f"{signal_price} —— 价格无法归属到具体合约，丢弃喊价改用实时报价: {text[:80]}"
    )
    return None, True


def _normalize_en(text: str) -> str:
    """EN 侧的从句剥离链。

    **`_parse_close_en` 与 `parse_symbolless_close` 共用同一份** —— 两边各写
    一遍就是一条误平裂缝（同 signal_parser 与本模块 pattern 共享的理由）。
    每一条的实锤见对应 RE 的定义处注释。
    """
    # 建议句（7/29 SPY "if you want to exit"）。与 ZH 侧同一层：
    # EN 当晚返回 None 靠的是 KC 写了小写 "spy"，不是防护。
    text = EN_SUGGESTION_CLAUSE_RE.sub(" ", text)
    text = EN_OPTIONAL_CLAUSE_RE.sub(" ", text)   # 否定条件句抹到句末（8/3 SPY）
    text = EN_AFTER_CLAUSE_RE.sub(" ", text)      # 时间状语从句（8/20 PLTR EN 侧）
    return text


def _normalize_zh(text: str) -> str:
    """ZH 侧的归一化 + 从句剥离链。共用理由同 `_normalize_en`。"""
    text = ZH_OUT_HALF_RE.sub("减半", text)          # "出半(仓)" → 减半（7/23 NBIS）
    text = ZH_OUT_FRACTION_RE.sub(r"卖出\1", text)   # "出 1/2" → 卖出 1/2（8/3 TSLA）
    text = ZH_SL_ADJUST_CLAUSE_RE.sub(" ", text)     # 移动止损备注（7/23 AVGO）
    text = ZH_SUGGESTION_CLAUSE_RE.sub(" ", text)    # 建议句（7/29 SPY）
    text = ZH_OPTIONAL_CLAUSE_RE.sub(" ", text)      # 否定条件句（8/3 SPY）
    text = ZH_AFTER_CLAUSE_RE.sub(" ", text)         # 时间状语从句（8/20 PLTR）
    return text


def _parse_close_en(text: str, open_symbols: set[str]) -> Optional[dict]:
    """英文路径（原 parse_close 逻辑）。"""
    text_lower = text.lower()
    if _has_recap_marker(text_lower):
        logger.info(f"[close_parser] EN skip (recap): {text[:80]}")
        return None
    if any(m in text_lower for m in AUTHOR_HOLD_MARKERS):
        logger.info(f"[close_parser] EN skip (author holding): {text[:80]}")
        return None
    text = _normalize_en(text)
    text_lower = text.lower()
    if not _has_action_verb(text_lower, text):
        return None

    scope_en = _action_sentences(text)
    signal_price = _extract_signal_price(scope_en)
    signal_pnl_pct = _extract_signal_pnl(scope_en, is_zh=False)

    if _has_bulk_marker(text_lower):
        pct = _extract_pct(text, text_lower)
        if pct == 33:
            # 无显式比例："closing/closed all positions" 是全清语义 → 100；
            # "trimming all positions" 保持 bulk 默认 50
            pct = 100 if re.search(r"\bclos(?:e|ing|ed)\b", text_lower) else 50
        exclude = _extract_exclude_symbols(text)
        logger.info(f"[close_parser] EN BULK_TRIM pct={pct} exclude={exclude}")
        return {"kind": "BULK_TRIM", "symbols": [], "pct": pct, "hint_strike": None, "hint_side": None,
                "exclude_symbols": exclude,
                "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
                "matched": text[:120], "lang": "en"}

    symbols = _extract_symbols(text, open_symbols)
    if not symbols:
        return None
    pct = _extract_pct(text, text_lower)
    hint_strike, hint_side = _extract_strike_hint(scope_en, symbols, full_text=text)
    signal_price, price_unattributable = _drop_unattributable_price(
        symbols, signal_price, "en", text)
    logger.info(
        f"[close_parser] EN CLOSE symbols={symbols} pct={pct} "
        f"strike={hint_strike} side={hint_side} "
        f"price={signal_price} pnl={signal_pnl_pct} text={text[:80]}"
    )
    return {"kind": "CLOSE", "symbols": symbols, "pct": pct,
            "hint_strike": hint_strike, "hint_side": hint_side,
            "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
            "price_unattributable": price_unattributable,
            "matched": text[:120], "lang": "en"}


# ---- 中文 helpers ----

def _has_zh_recap(text: str) -> bool:
    return any(m in text for m in ZH_RECAP_MARKERS)


# "仅持仓X @ 3.28" —— KC "runners only" 的机翻，语义是**已经减到只剩 runner**。
# 必须跟 @价格才算动作（不带价格的 "剩余1个持仓" 是持仓陈述，见常量注释）。
_ZH_RUNNERS_ONLY_RE = re.compile(_ZH_RUNNERS_ONLY_PATTERN)

# [9/1 COIN] ZH 侧的 down-to 分数 / securing 宾语。**不进 ZH_ACTION_VERBS**：
# 那张表是裸子串匹配，"降至"/"确保" 裸词是行情叙述与鼓励语（同一晚就有
# "确保它保持这样"），必须带边界条件，与 _ZH_RUNNERS_ONLY_RE 同款处理。
_ZH_EXTRA_ACTION_RE = re.compile(
    _ZH_DOWN_TO_FRACTION_PATTERN + r"|" + _ZH_SECURE_SOME_PATTERN
)


def _has_zh_action(text: str) -> bool:
    return (
        any(v in text for v in ZH_ACTION_VERBS)
        or bool(_ZH_RUNNERS_ONLY_RE.search(text))
        or bool(_ZH_EXTRA_ACTION_RE.search(text))
    )


def _has_zh_bulk(text: str) -> bool:
    return any(m in text for m in ZH_BULK_MARKERS)


def _has_zh_full_close(text: str) -> bool:
    return any(v in text for v in ZH_FULL_CLOSE_VERBS)


def _zh_action_sentences(text: str) -> str:
    """按中文句号 / 英文句号切，留含动作动词的句子。

    `.` 在数字之间不切（如 '@ 2.45'）。中文 `。！？` 总是切。
    """
    sents = re.split(r"[。！？]|(?<!\d)[.!?](?!\d)", text)
    hit = [s for s in sents
           if any(v in s for v in ZH_ACTION_VERBS) or _ZH_EXTRA_ACTION_RE.search(s)]
    return " ".join(hit) if hit else text


class _AnySymbols(frozenset):
    """诊断专用的"白名单全通过"哨兵。

    只给 [zh_unrecognized] 的日志文案用：区分"整段没抽到 ticker"和
    "抽到了但不在当前持仓"。**不参与任何下单路径** —— 传它进来意味着
    跳过白名单消歧，那正是 open_symbols 存在的理由（"NOW" 既是副词
    也是 ServiceNow，见模块顶部）。
    """

    def __contains__(self, item):  # noqa: D105
        return True


_ANY_SYMBOLS = _AnySymbols()


def _extract_zh_symbols(text: str, open_symbols: set[str]) -> list[str]:
    """中文版 symbol 抽取：$SYMBOL → 裸 ticker 白名单。

    两遍扫描：先在 action 句 scope 找，没找到再 fallback 全文（同 EN 路径）。
    用 BARE_SYM_PATTERN_ZH 避免汉字-字母边界 \b 失效。

    不处理中文公司名（亚马逊→AMZN 之类）—— 这类信号靠 EN 版本兜底。
    """
    def _keep(cands: list) -> list:
        return [s for s in cands if not _in_hold_context(s, text, is_zh=True)]

    for scope in (_zh_action_sentences(text), text):
        found = []
        seen = set()
        # 1. $SYMBOL（enrich 中文版常保留）
        for m in DOLLAR_SYM_PATTERN.finditer(scope):
            s = m.group(1)
            if s not in seen:
                found.append(s); seen.add(s)
        kept = _keep(found)
        if kept:
            return kept
        # 2. 裸 ticker + 白名单消歧（IWM/SPY/QQQ 等不被翻译的）
        for m in BARE_SYM_PATTERN_ZH.finditer(scope):
            s = m.group(1)
            if s in seen:
                continue
            if s in open_symbols:
                found.append(s); seen.add(s)
        kept = _keep(found)
        if kept:
            return kept
        # 3. 中文公司名兜底（"减仓苹果"）——只认已持仓的映射
        for name, tick in ZH_NAME_TO_TICKER.items():
            if tick in open_symbols and tick not in seen and name in scope:
                found.append(tick); seen.add(tick)
        kept = _keep(found)
        if kept:
            return kept
    return []


def _extract_zh_pct(text: str) -> int:
    """中文版百分比抽取。优先级同 EN：剩余% → 一半 → 剩余分数 → 卖出分数 → N%。"""
    scope = _zh_action_sentences(text)

    left_m = ZH_LEFT_PATTERN.search(scope)
    if left_m:
        n = int(left_m.group(1))
        return max(1, min(100, 100 - n))

    # 中文数词分数："减仓一半" / "减半仓" / "卖出半仓" → 50%
    # （"出半" 已在入口归一化成"减半"；"半仓" 只在动作动词已确认的 scope 里
    # 评估，"卖出半仓" 因 出半 左边界让位给 卖出 动词后靠它拿到 50，
    # 与 EN 孪生 "sold half"=50 指纹对齐）。
    # 7/10 实测："减仓一半" 落到默认 33，和 EN 孪生 "out half"=50 指纹
    # 不匹配 → dedup 失效多发一条 TG。
    if "一半" in scope or "减半" in scope or "半仓" in scope:
        return 50

    # 分数：先 scope 后全文兜底（同 EN 版 _extract_pct 的两句式问题）
    for search_space in (scope, text):
        m = ZH_FRACTION_TO_PATTERN.search(search_space)
        if m:
            frac = _fraction_pct(int(m.group(1)), int(m.group(2)))
            if frac is not None:
                return max(1, min(100, 100 - frac))
        m = ZH_FRACTION_OUT_PATTERN.search(search_space)
        if m:
            frac = _fraction_pct(int(m.group(1)), int(m.group(2)))
            if frac is not None:
                return max(1, min(100, frac))

    pct_m = PCT_PATTERN.search(scope)
    if pct_m:
        return max(1, min(100, int(pct_m.group(1))))

    return 100 if _has_zh_full_close(text) else 33


def _parse_close_zh(text: str, open_symbols: set[str]) -> Optional[dict]:
    """中文 fallback 路径。"""
    # recap 判定必须在任何改写**之前**跑原文：掩码会拆掉"将把"这类
    # 未来意图标记（第二轮对抗评审实锤——"我将把止损上移…若跌破就全部卖出"
    # 原本 recap-skip，改写后变成真卖单）
    if _has_zh_recap(text):
        logger.info(f"[close_parser] ZH skip (recap): {text[:80]}")
        return None
    # 作者自述持有 —— 同 recap，必须在任何改写之前跑原文（8/3 SPY）
    if any(m in text for m in ZH_AUTHOR_HOLD_MARKERS):
        logger.info(f"[close_parser] ZH skip (author holding): {text[:80]}")
        return None
    text = _normalize_zh(text)
    if not _has_zh_action(text):
        return None

    scope_zh = _zh_action_sentences(text)
    signal_price = _extract_signal_price(scope_zh)
    signal_pnl_pct = _extract_signal_pnl(scope_zh, is_zh=True)

    if _has_zh_bulk(text):
        pct = _extract_zh_pct(text)
        if pct == 33:
            pct = 50
        exclude = _extract_exclude_symbols(text)
        logger.info(f"[close_parser] ZH BULK_TRIM pct={pct} exclude={exclude}")
        return {"kind": "BULK_TRIM", "symbols": [], "pct": pct, "hint_strike": None, "hint_side": None,
                "exclude_symbols": exclude,
                "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
                "matched": text[:120], "lang": "zh"}

    symbols = _extract_zh_symbols(text, open_symbols)
    if not symbols:
        # 区分两种"抽不到" —— 文案写错会把下一次复盘引向错误的方向。
        # [8/19 实测] 当晚 4 次 [zh_unrecognized] 里有 3 次原文的 ticker 是
        # 明晃晃的拉丁字母（SPCX / META / 仅持仓SPY），真实原因是**白名单过滤后
        # 为空**（SPCX 5 秒前刚被全平、我们没有 META 持仓），行为完全正确，
        # 却被记成"中文公司名抓不到"。
        seen_any = _extract_zh_symbols(text, _ANY_SYMBOLS)
        if seen_any:
            logger.info(
                f"[close_parser] ZH close intent, symbols={seen_any} 不在当前持仓 "
                f"({sorted(open_symbols)}) —— 无仓可平，跳过: {text[:80]}"
            )
        else:
            logger.warning(
                f"[close_parser] [zh_unrecognized] ZH close intent but symbol "
                f"not extractable (likely Chinese company name): {text[:120]}"
            )
        return None
    pct = _extract_zh_pct(text)
    hint_strike, hint_side = _extract_strike_hint(scope_zh, symbols, full_text=text)
    signal_price, price_unattributable = _drop_unattributable_price(
        symbols, signal_price, "zh", text)
    logger.info(
        f"[close_parser] ZH CLOSE symbols={symbols} pct={pct} "
        f"strike={hint_strike} side={hint_side} "
        f"price={signal_price} pnl={signal_pnl_pct} text={text[:80]}"
    )
    return {"kind": "CLOSE", "symbols": symbols, "pct": pct,
            "hint_strike": hint_strike, "hint_side": hint_side,
            "signal_price": signal_price, "signal_pnl_pct": signal_pnl_pct,
            "price_unattributable": price_unattributable,
            "matched": text[:120], "lang": "zh"}


def parse_close(text: str, open_symbols: set[str]) -> Optional[dict]:
    """CLOSE 信号解析（顶层 dispatcher）。

    流程：
      EN 路径 → 命中返回
      ZH 路径 → 命中返回
      都没命中 → None

    返回 dict 多了 'lang' 字段标识哪个路径命中，方便日志和回归。
    """
    if not text or len(text.strip()) < 3:
        return None

    # 先剥中继机器人标签（见 _RELAY_LABEL_RE 注释，8/28 起 KC 频道的新格式）。
    # 放在 dispatcher 而不是各语言分支：两条分支都吃同一份干净文本，
    # 将来加第三条路径也自动继承。
    text = strip_relay_prefix(text)

    return _parse_close_en(text, open_symbols) or _parse_close_zh(text, open_symbols)


# $ 后面跟 1-5 个字母 = 喊单员在标注标的，不管大小写。
# 只用于 parse_symbolless_close 的"这句话有没有写标的"判定，不参与抽取
# （抽取仍走 _extract_symbols 的大写口径，那是白名单消歧的前提）。
_DOLLAR_TICKER_ANYCASE_RE = re.compile(r"\$[A-Za-z]{1,5}\b")


def parse_symbolless_close(text: str) -> Optional[dict]:
    """**没写标的**的平仓跟进指令 —— 只做抽取，不决定平谁。

    [9/2 实锤 -$166] KC 的习惯是开仓带 ticker、后续减仓不带。TSLA 345P
    开仓 90 秒后第一条减仓就没有 ticker，双语双漏：

        23:40:03  out half 2.82        → [CLOSE] parser skipped
        23:40:05  2.82减半仓            → [zh_unrecognized]

    `parse_close` 对这两条返回 None 是**对的** —— 它手里只有一句话，
    没有任何依据决定平哪个仓。缺的判据（哪个频道、哪个仓刚开、喊价对不对得上）
    全在 close_flow 那一层，所以本函数只回答"这是不是一条无标的平仓指令、
    比例和喊价是多少"，绑定留给调用方（见 close_flow._bind_symbolless_close）。

    **刻意不复用 parse_close 的返回值**：那个 None 契约被 48 条用例断言着，
    是本仓库最贵的一条防线（误平的代价远高于漏平，见模块顶部）。本函数是
    additive 的第二个入口，parse_close 的行为一个字节都没动。

    五道闸门，缺一返回 None：
      1. recap / 作者自述持有 —— 双语两套 marker 都在**原文**上跑（同主路径，
         掩码会拆掉"将把"这类未来意图标记）
      2. 有当前动作动词（EN 或 ZH 任一）
      3. **不是** bulk —— "close all positions" 有 BULK_TRIM 走，不归这里
      4. **全文抓不到任何 ticker**（连白名单外的也不许有）。抓到了但不在持仓，
         那是"无仓可平"，必须继续返回 None —— 拿一句提到 META 的话去平
         TSLA 是这条改动最该防的事故
      5. 有喊价 —— 喊价是 close_flow 验证绑定对不对的唯一凭据（2.82 落在
         TSLA 345P 的报价区间里，这条约束比任何词表都硬）
    """
    if not text or len(text.strip()) < 3:
        return None
    text = strip_relay_prefix(text)

    # 1. recap / 持有自述：双语都拦，在任何改写之前跑原文
    if _has_recap_marker(text.lower()) or _has_zh_recap(text):
        return None
    if (any(m in text.lower() for m in AUTHOR_HOLD_MARKERS)
            or any(m in text for m in ZH_AUTHOR_HOLD_MARKERS)):
        return None

    en_text = _normalize_en(text)
    zh_text = _normalize_zh(text)
    en_lower = en_text.lower()

    # 2. 当前动作动词
    is_en = _has_action_verb(en_lower, en_text)
    is_zh = _has_zh_action(zh_text)
    if not (is_en or is_zh):
        return None

    # 3. bulk 有自己的路（BULK_TRIM 会遍历全部持仓，语义与"绑定到一个仓"相反）
    if _has_bulk_marker(en_lower) or _has_zh_bulk(zh_text):
        return None

    # 4. 全文不许出现任何 ticker（_ANY_SYMBOLS = 跳过白名单，见其 docstring）。
    #
    # [PR#7 review] _extract_symbols 的 $SYMBOL / 裸 ticker 两条正则**都只认大写**，
    # 于是 "out half $spy 2.82" 会被当成无标的指令绑到当日唯一新开仓上 ——
    # 而喊单员写小写是真实存在的（7/29 SPY 那次 EN 侧返回 None 靠的正是
    # KC 写了小写 "spy"，见 _parse_close_en 里那句注释）。绑错标的是这条
    # 改动最该防的事故，所以这里单独补一道大小写无关的 $ticker 闸门。
    if _DOLLAR_TICKER_ANYCASE_RE.search(text):
        return None
    if _extract_symbols(en_text, _ANY_SYMBOLS) or _extract_zh_symbols(zh_text, _ANY_SYMBOLS):
        return None

    # 5. 喊价：绑定的凭据
    lang = "en" if is_en else "zh"
    scope = _action_sentences(en_text) if is_en else _zh_action_sentences(zh_text)
    signal_price = _extract_signal_price(scope)
    if signal_price is None:
        return None

    pct = _extract_pct(en_text, en_lower) if is_en else _extract_zh_pct(zh_text)
    logger.info(
        f"[close_parser] {lang.upper()} SYMBOLLESS close pct={pct} "
        f"price={signal_price} text={text[:80]}"
    )
    return {"kind": "SYMBOLLESS", "symbols": [], "pct": pct,
            "hint_strike": None, "hint_side": None,
            "signal_price": signal_price, "signal_pnl_pct": None,
            "matched": text[:120], "lang": lang}


# ============================================================
# 喊单员声明的止损（"stop at entry now" / "止损设置为保本"）
# ============================================================
# [9/9] 一周内第三次：9/2 `stop is at 2.35 now`、9/9 `stop at entry now`、
# 9/9 `止损设置为保本`。三次的减仓那半都执行了，止损那半一次都没接住 ——
# ZH_SL_ADJUST_CLAUSE_RE 的作用是把这类子句**抹掉**（免得误判成平仓指令），
# 抹完就没有下文了。DELL 剩的那张因此还挂在 entry×0.5，而喊单员说的是保本。
#
# 这里只做抽取，**存不存、抬不抬由 close_flow / sl_watcher 决定**（同
# parse_symbolless_close 的分工）。刻意不进 detect_action 的路由表：
# 纯止损消息（不含平仓动作）不该因此被路由成 CLOSE —— 本函数只在
# 已经判定为 CLOSE 的消息上跑，加零新路由风险。

# 绝对价："stop is at 2.35" / "stop at 2.35" / "raising stop to 3.00"
# `stops?` 的 \b 天然排除 "stopped"（那是平仓动词，不是设止损）。
_EN_STOP_ABS_RE = re.compile(
    r"\bstops?\b[^.\n]{0,24}?\b(?:at|to|is)\b\s*\$?(\d+(?:\.\d+)?)", re.IGNORECASE)
# 保本档："stop at entry" / "stop set to break-even" / "stop to BE"
_EN_STOP_BE_RE = re.compile(
    r"\bstops?\b[^.\n]{0,24}?\b(?:entry|break[-\s]?even|breakeven)\b", re.IGNORECASE)

# ZH 孪生。"止损"后面跟设/移/调/现/在，再跟保本/入场/成本 或一个数字。
_ZH_STOP_ABS_RE = re.compile(r"止损[^。\n]{0,12}?(\d+(?:\.\d+)?)")
_ZH_STOP_BE_RE = re.compile(r"止损[^。\n]{0,12}?(?:保本|入场|成本|开仓价)")

# 语义哨兵：调用方拿到它就知道"锚在我们自己的成本上"，而不是一个绝对价。
STOP_AT_BREAKEVEN = "breakeven"


def parse_stop_adjust(text: str) -> "dict | None":
    """抽取喊单员声明的止损。返回 {"stop": float | STOP_AT_BREAKEVEN, "lang": str} 或 None。

    保本档**优先于**绝对价：`stop at entry now to secure green trade` 里没有
    数字，但 `trimmed 2.95, stop is at 2.35` 有 —— 先判保本能避免把句子里
    别处的价格（成交价、目标价）误当成止损。
    """
    if not text:
        return None
    text = strip_relay_prefix(text)

    if _EN_STOP_BE_RE.search(text):
        return {"stop": STOP_AT_BREAKEVEN, "lang": "en"}
    if _ZH_STOP_BE_RE.search(text):
        return {"stop": STOP_AT_BREAKEVEN, "lang": "zh"}

    for rx, lang in ((_EN_STOP_ABS_RE, "en"), (_ZH_STOP_ABS_RE, "zh")):
        m = rx.search(text)
        if m:
            try:
                price = float(m.group(1))
            except ValueError:
                continue
            if price > 0:
                return {"stop": price, "lang": lang}
    return None
