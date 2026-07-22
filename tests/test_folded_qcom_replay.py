"""回放 6/16 QCOM 信号，验证 expiry 修复到 6/18。

（原 scripts/test_qcom_replay.py 搁浅 assert 脚本，折叠为 pytest；期望值原样保留。）
"""
from datetime import date

from autotrade.parsing.signal_parser import parse_signal


def test_qcom_replay_expiry_moves_off_juneteenth():
    # 真实信号文本（enrich 6/16 ET 09:59）
    raw = "enrich:\n$QCOM - weekly $245 calls $2.25\n\n@everyone $alert"

    # ET 周一（msg_ts 模拟 listener 传入的值）
    sig = parse_signal(raw, msg_ts=date(2026, 6, 15))

    assert sig is not None, "应解析成功"
    assert sig.get("skip") is None, "不应是 skip"
    assert sig["symbol"] == "QCOM"
    assert sig["strike"] == 245.0
    assert sig["side"] == "CALL"
    assert sig["price"] == 2.25
    assert sig["expiry_date"] == date(2026, 6, 18), \
        f"expected 6/18 (Juneteenth 前移), got {sig['expiry_date']}"


def test_iren_replay_expiry_moves_off_juneteenth():
    # 再测 IREN（msg_ts 也是 6/15 周一 ET）
    raw2 = "enrich:\nPotential run into EOD - $IREN weekly $65 calls for $.93\n\n@everyone"
    sig2 = parse_signal(raw2, msg_ts=date(2026, 6, 15))
    assert sig2["symbol"] == "IREN"
    assert sig2["strike"] == 65.0
    assert sig2["expiry_date"] == date(2026, 6, 18)


def test_spy_weekly_non_holiday_friday_not_shifted():
    # 测一个非假日的周五，确保不被错误前移
    raw3 = "$SPY weekly $500 calls $1.50"
    sig3 = parse_signal(raw3, msg_ts=date(2026, 6, 22))  # 周一
    assert sig3["expiry_date"] == date(2026, 6, 26), "6/26 周五不是假日，应保留"
