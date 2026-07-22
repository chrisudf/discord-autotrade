"""验证 4 条规则在样本上的表现

（原 scripts/test_parser_rules.py 打印式回放脚本，折叠为 pytest parametrize；
SAMPLES 样本与期望 symbol 原样保留。原脚本 parse_signal 不传 msg_ts，保持一致。）
"""
import pytest

from autotrade.parsing.signal_parser import parse_signal

SAMPLES = [
    # (label, text, expected_symbol_or_None)
    ("KC AMZN swing", "@everyone\nKC Trades Bot:AMZN 260c 7/17 @ 2.64 swing", "AMZN"),
    ("KC SPIR IPO lotto", "@everyone\nKC Trades Bot:Space X IPO lotto, SPIR 20c 6/18 @ 1.90", "SPIR"),
    ("KC MSFT 多腿 → 只取第一", "@everyone\nKC Trades Bot:Dumpster Diver trade setup\n\nMSFT 440c 7/17 @ 3.05 swing, I like the 420c 7/17 @ 6.05 if you want a higher delta", "MSFT"),
    ("KC SMH put", "@everyone\nKC Trades Bot:SMH 530p 6/18 lotto swing @ 8.20\n\nRemember, PPI data tomorrow", "SMH"),
    ("KC HIMS small swing", "@everyone\nKC Trades Bot:small swing/lotto size\n\nHIMS 35c 7/17 @ 1.46", "HIMS"),
    ("KC AMZN put", "@everyone\nKC Trades Bot:AMZN 240p 6/18 @ 3.40", "AMZN"),

    ("enrich IREN 0DTE", "enrich:\nLotto $IREN 0DTE $60 calls $.68\n\n@everyone $alert", "IREN"),
    ("enrich TSLA puts", "enrich:\nScalp - lotto\n\n$TSLA 0DTE $405 PUTS $1.17\n\n@everyone $alert", "TSLA"),
    ("enrich SPY hedge", "enrich:\n$SPY 0DTE $752 puts for $.55\n\nLotto size - nice hedge into EOD\n\n@everyone $alert", "SPY"),
    ("enrich HIMS super lotto", "enrich:\nSuper lotto - $HIMS 0DTE $26.50 calls $.15\n\nPlease go light\n\n@everyone $alert", "HIMS"),

    # 重复播报：两条都应该解析到（去重靠 dedup deque，不在 parser 层）
    ("enrich MRVL @.95", "美股会员网rich:\n$MRVL - lotto - 1% \n\n0DTE $185 calls $.95\n\n@everyone $alert", "MRVL"),
    ("enrich MRVL @1.10", "美股会员网rich:\n$MRVL - lotto - 1%\n\n0DTE $185 calls - $1.10 fill\n\n@everyone $alert", "MRVL"),

    # 规则 3: 持仓汇报 → None
    ("持仓汇报", "美股会员网rich:\nInto tomorrow, this is what I'm holding:\n\n$ARM $232.50 1DTE calls\n$APLD remaining 5/22 $50 calls\n$OSCR remaining 6/18 $25 calls", None),

    # 规则 4: 价格区间 → None
    ("价格区间 NBIS", "美股会员网rich:\n$NBIS - Lotto - 1% - scalp\n\n0DTE $152.50 calls - try to fill close to $1.30 - $1.70\n\n@everyone $alert\n\n美股会员网rich:\n$NBIS - 彩票", None),

    # 规则 1: 中文段被切掉，英文段正常解析
    ("中英双语 TSLA fill", "美股会员网rich:\nLotto - size for 0 \n\n$TSLA 0dte $340 puts $.45 fill\n\n@everyone $alert\n\n美股会员网rich:\n彩票 - 尺寸为 0\n\n$TSLA 0dte $340 看跌期权 $.45 成交", "TSLA"),
    ("中英双语 TSLA heavy", "美股会员网rich:\nLOTTO DO NOT SIZE HEAVY \n\n$TSLA 0DTE $342.50 puts $.70 \n\n@everyone $alert\n\n美股会员网rich:\n彩票不要重仓", "TSLA"),

    # === [新增] 英文月份 - Pattern A2 ===
    ("KC NOW June 26", "@everyone\nKC Trades Bot:NOW 115c June 26 @ 2.00", "NOW"),
    ("KC IWM June 22", "@everyone\nKC Trades Bot:IWM 293p June 22 @ 2.23", "IWM"),
    ("KC AMZN January 15", "@everyone\nKC Trades Bot:AMZN 200c January 15 @ 3.50", "AMZN"),
    ("KC AAPL Jun 26th", "@everyone\nKC Trades Bot:AAPL 240c Jun 26th @ 4.20", "AAPL"),
    ("KC TSLA Sept 19", "@everyone\nKC Trades Bot:TSLA 350p Sept 19 @ 4.50", "TSLA"),

    # === [新增] 英文月份 - Pattern B0.5 ===
    # 已知缺口：老 repo 原样复现 "invalid date in signal: too many values to
    # unpack (expected 6)" → None（脚本原输出 21/22 passed）。按契约不修
    # known-weird 行为，这条标 xfail 锁定缺口，期望 symbol 不变。
    pytest.param(
        "enrich NVDA June 20",
        "enrich:\n$NVDA June 20 $180 calls $2.50\n\n@everyone $alert",
        "NVDA",
        marks=pytest.mark.xfail(
            reason="Pattern B0.5 月份日期解析缺口（老 repo 同样失败，21/22）",
            strict=False,
        ),
        id="enrich NVDA June 20",
    ),
]


def _is_skip(r):
    return isinstance(r, dict) and r.get("skip")


def _is_signal(r):
    return isinstance(r, dict) and not r.get("skip")


@pytest.mark.parametrize(
    "label,text,expected", SAMPLES,
    ids=[s[0] if isinstance(s, tuple) else s.id for s in SAMPLES],
)
def test_parser_rule_sample(label, text, expected):
    result = parse_signal(text)

    # 兼容新增 intentional skip：视为 None（无可下单信号）
    if _is_signal(result):
        actual = result["symbol"]
    else:
        actual = None

    assert actual == expected, (
        f"{label}: expected={expected!s} actual={actual!s} raw={result!r}"
    )
