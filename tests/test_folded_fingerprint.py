"""验证 fingerprint 去重逻辑

（原 scripts/test_fingerprint.py 搁浅 assert 脚本，折叠为 pytest；
7 个 case 的期望值原样保留，reset() 改为 autouse fixture。）
"""
from datetime import datetime, timedelta, timezone

import pytest

from autotrade.listener import dedup as dc
from autotrade.listener.dedup import _is_duplicate_signal, _signal_fps


@pytest.fixture(autouse=True)
def _reset():
    _signal_fps.clear()
    yield
    _signal_fps.clear()


def test_case1_nne_double_send():
    # Case 1: NNE 双发（间隔 8s 实测场景）
    sig = {"symbol": "NNE", "side": "CALL", "strike": 31.0, "expiry_date": "2026-05-15"}
    r1 = _is_duplicate_signal(sig)
    r2 = _is_duplicate_signal(sig)
    assert r1[0] is False, f"1st should be False, got {r1}"
    assert r2[0] is True,  f"2nd should be True, got {r2}"


def test_case2_mrvl_price_correction_same_fingerprint():
    # Case 2: MRVL 价格修正 $1.10 → $.95（同指纹）
    sig1 = {"symbol": "MRVL", "side": "CALL", "strike": 185.0, "expiry_date": "2026-05-15", "price": 1.10}
    sig2 = {"symbol": "MRVL", "side": "CALL", "strike": 185.0, "expiry_date": "2026-05-15", "price": 0.95}
    r1 = _is_duplicate_signal(sig1)
    r2 = _is_duplicate_signal(sig2)
    assert r1[0] is False
    assert r2[0] is True, "价格不同但 fingerprint 同，应被拦"


def test_case3_different_strike_not_blocked():
    # Case 3: 不同 strike 不拦
    sig1 = {"symbol": "TSLA", "side": "CALL", "strike": 460.0, "expiry_date": "2026-05-15"}
    sig2 = {"symbol": "TSLA", "side": "CALL", "strike": 465.0, "expiry_date": "2026-05-15"}
    r1 = _is_duplicate_signal(sig1)
    r2 = _is_duplicate_signal(sig2)
    assert r1[0] is False
    assert r2[0] is False, "不同 strike 不应拦"


def test_case4_call_vs_put_not_blocked():
    # Case 4: CALL vs PUT 不拦
    sig1 = {"symbol": "SPY", "side": "CALL", "strike": 700.0, "expiry_date": "2026-06-15"}
    sig2 = {"symbol": "SPY", "side": "PUT",  "strike": 700.0, "expiry_date": "2026-06-15"}
    r1 = _is_duplicate_signal(sig1)
    r2 = _is_duplicate_signal(sig2)
    assert r1[0] is False
    assert r2[0] is False, "CALL 和 PUT 不应互相拦"


def test_case5_different_expiry_not_blocked():
    # Case 5: 不同 expiry 不拦（同 strike 不同周）
    sig1 = {"symbol": "AAPL", "side": "CALL", "strike": 200.0, "expiry_date": "2026-06-19"}
    sig2 = {"symbol": "AAPL", "side": "CALL", "strike": 200.0, "expiry_date": "2026-06-26"}
    r1 = _is_duplicate_signal(sig1)
    r2 = _is_duplicate_signal(sig2)
    assert r1[0] is False
    assert r2[0] is False


def test_case6_window_expiry_releases():
    # Case 6: 窗口过期后不拦（手动改时间戳）
    sig = {"symbol": "QQQ", "side": "PUT", "strike": 500.0, "expiry_date": "2026-06-19"}
    r1 = _is_duplicate_signal(sig)
    assert r1[0] is False

    # 手动把刚记录的时间戳改成 6 分钟前
    # （原脚本用 naive datetime.now()，写于 dedup 切 timezone.utc 之前，
    #   如今对老库直接 TypeError——注入时间戳按结构现状用 aware UTC，期望值不变）
    fp_key = list(dc._signal_fps.keys())[0]
    dc._signal_fps[fp_key] = datetime.now(timezone.utc) - timedelta(minutes=6)

    r2 = _is_duplicate_signal(sig)
    assert r2[0] is False, "过 5min 窗口后应放行"


def test_case7_cross_channel_same_signal_blocked():
    # Case 7: 跨频道同信号（验证 fingerprint 不含 channel）
    # 模拟 KC 主频道 + enrich 翻译版同时发 MRVL
    sig_kc      = {"symbol": "MRVL", "side": "CALL", "strike": 190.0, "expiry_date": "2026-05-15"}
    sig_enrich  = {"symbol": "MRVL", "side": "CALL", "strike": 190.0, "expiry_date": "2026-05-15"}
    r1 = _is_duplicate_signal(sig_kc)
    r2 = _is_duplicate_signal(sig_enrich)
    assert r1[0] is False
    assert r2[0] is True, "跨频道转发也应拦"
