"""已实现盈亏账本（ops/pnl.py）。

这个模块的正确性没法靠"跑起来不报错"验证 —— 它输出的是钱的数字，错了也
一样会打印出来。所以口径逐条钉死，并且用**三份复盘里手算过的日结**做交叉
验证（那三个数字是在本工具存在之前、由人独立算出来的）。

写用例时抓出的真 bug：EXPIRE 按事件时间戳归日。expiry_sweep 跑在次日早晨
（LITE 1030C 9/11 到期，EXPIRE 事件 ts 是 9/12 09:11 ET），于是每一笔过期
损失都落到后一天 —— 到期日当天少记、次日凭空多出一笔。对 9/11 少算了 $460，
正是它与复盘手算 -$1,478 的全部差额。
"""
from autotrade.ops import pnl


def _pos(code, entry, qty_total=2, qty_rem=0, channel="ashley",
         category="weekly", expiry="2026-09-11"):
    return {"option_code": code, "avg_entry_price": entry,
            "qty_total": qty_total, "qty_remaining": qty_rem,
            "channel_name": channel, "category": category, "expiry": expiry}


def _evt(code, etype, dq, price, ts, trigger="kc_signal"):
    return {"option_code": code, "event_type": etype, "qty_delta": dq,
            "price": price, "ts": ts, "trigger_source": trigger}


# ============================================================
# 归日：UTC → ET，以及 EXPIRE 的特例
# ============================================================

def test_et_date_converts_from_utc_not_local():
    """本机是 AEST，直接切字符串会把 09:38 ET 的成交算到第二天。"""
    assert pnl.et_date("2026-09-09T13:38:17.998211Z") == "2026-09-09"
    assert pnl.et_date("2026-09-10T01:22:42Z") == "2026-09-09"   # ET 前一天 21:22


def test_expire_is_dated_by_contract_expiry_not_by_the_sweep():
    """契约：过期归日用合约到期日。

    LITE 1030C 9/11 到期，expiry_sweep 次日早晨才记事件 —— 按 ts 归日会把
    这 -$460 算到 9/12。第一版就是这么错的。
    """
    pos = {"US.LITE": _pos("US.LITE", 4.60, expiry="2026-09-11")}
    legs = pnl.exit_legs(
        [_evt("US.LITE", "EXPIRE", -1, None, "2026-09-12T13:11:18Z",
              "expiry_sweep")], pos)
    assert legs[0]["et_date"] == "2026-09-11"
    assert legs[0]["exit"] == 0.0, "EXPIRE 的 NULL 是真归零，不是缺数据"
    assert legs[0]["realized"] == -460.0


def test_normal_exits_are_dated_by_the_fill():
    pos = {"US.X": _pos("US.X", 3.20, expiry="2026-09-11")}
    legs = pnl.exit_legs(
        [_evt("US.X", "CLOSE", -1, 4.80, "2026-09-09T15:22:42Z")], pos)
    assert legs[0]["et_date"] == "2026-09-09"


# ============================================================
# 成本锚：avg_entry_price，不是 OPEN 事件的挂单限价
# ============================================================

def test_cost_anchors_on_filled_average_not_the_limit_price():
    """9/10 夜 MSTR：OPEN 记 2.86（限价），实成 2.78（FILL_ADJUST 回填）。

    拿 OPEN.price 算会系统性高估成本 —— 61 条 FILL_ADJUST 说明这是常态不是例外。
    """
    pos = {"US.M": _pos("US.M", 2.78)}
    events = [
        _evt("US.M", "OPEN", 2, 2.86, "2026-09-10T13:48:22Z"),        # 限价
        _evt("US.M", "FILL_ADJUST", 0, 2.78, "2026-09-10T13:48:37Z"),  # 实成
        _evt("US.M", "TRIM", -1, 2.90, "2026-09-10T13:57:32Z"),
    ]
    legs = pnl.exit_legs(events, pos)
    assert len(legs) == 1, "OPEN / FILL_ADJUST 不是卖出腿，不许进账本"
    assert legs[0]["entry"] == 2.78 and legs[0]["cost"] == 278.0
    assert legs[0]["realized"] == 12.0


# ============================================================
# 捏造价：reconciler 自动落账的 0 不是成交价
# ============================================================

def test_broker_sync_zero_is_flagged_and_kept_out_of_totals():
    """9/10 夜 AMZN 250C 被一次读数误平，库里留下 CLOSE @ 0.00。

    把它当成交价算进去 = 让一次读数错误永久污染业绩统计（虚记 -$630）。
    闸门 4 只防复发、不追溯，所以这条排除要长期留着。
    """
    pos = {"US.A": _pos("US.A", 3.15), "US.B": _pos("US.B", 1.00)}
    legs = pnl.exit_legs([
        _evt("US.A", "CLOSE", -2, 0.0, "2026-09-10T15:47:39Z", "broker_sync"),
        _evt("US.B", "CLOSE", -1, 1.50, "2026-09-10T15:00:00Z"),
    ], pos)
    fab = [l for l in legs if l["fabricated"]]
    assert len(fab) == 1 and fab[0]["option_code"] == "US.A"
    # 汇总里不许出现它
    assert sum(r["realized"] for r in pnl.group_sum(legs, "channel")) == 50.0


def test_a_genuine_zero_exit_is_not_flagged():
    """反向护栏：真的卖在 0.00（不是 broker_sync）不该被当成捏造价。

    到期日彩票确实会成交在 0.01/0.00 —— 那是真实结局，必须计入。
    """
    pos = {"US.Z": _pos("US.Z", 1.00)}
    legs = pnl.exit_legs(
        [_evt("US.Z", "CLOSE", -1, 0.01, "2026-09-11T19:50:00Z", "eod_force")],
        pos)
    assert legs[0]["fabricated"] is False
    assert legs[0]["realized"] == -99.0


# ============================================================
# 复盘手算值交叉验证（本工具存在之前由人独立算出）
# ============================================================

def test_reproduces_the_hand_computed_0909_figure():
    """9/10 复盘：「昨晚落袋的两条 CLOSE 腿 = +$326」。"""
    pos = {"US.DELL": _pos("US.DELL", 3.20, channel="enrich"),
           "US.TSLA": _pos("US.TSLA", 3.35, channel="KC-期权-波段")}
    legs = pnl.exit_legs([
        _evt("US.DELL", "CLOSE", -1, 4.80, "2026-09-09T15:22:42Z", "sl_polling"),
        _evt("US.TSLA", "CLOSE", -1, 5.01, "2026-09-09T13:38:17Z", "sl_polling"),
    ], pos)
    assert sum(l["realized"] for l in legs) == 326.0


def test_per_channel_split_is_not_double_counted():
    """三个频道各算各的，合计等于总和 —— 这是"哪个频道在赚钱"的唯一依据。"""
    pos = {"US.A": _pos("US.A", 1.00, channel="ashley"),
           "US.E": _pos("US.E", 1.00, channel="enrich"),
           "US.K": _pos("US.K", 1.00, channel="KC-期权-波段")}
    legs = pnl.exit_legs([
        _evt("US.A", "CLOSE", -1, 2.00, "2026-09-11T15:00:00Z"),
        _evt("US.E", "CLOSE", -1, 0.50, "2026-09-11T15:00:00Z"),
        _evt("US.K", "CLOSE", -2, 1.50, "2026-09-11T15:00:00Z"),
    ], pos)
    rows = {r["channel"]: r["realized"] for r in pnl.group_sum(legs, "channel")}
    assert rows == {"ashley": 100.0, "enrich": -50.0, "KC-期权-波段": 100.0}
    assert sum(rows.values()) == sum(l["realized"] for l in legs)


# ============================================================
# 在途：既不是赚也不是亏
# ============================================================

def test_open_exposure_is_reported_separately_not_as_pnl():
    pos = {"US.OPEN": _pos("US.OPEN", 5.30, qty_rem=1, expiry="2026-09-18"),
           "US.DONE": _pos("US.DONE", 1.00, qty_rem=0)}
    ex = pnl.open_exposure(pos)
    assert [e["option_code"] for e in ex] == ["US.OPEN"]
    assert ex[0]["cost"] == 530.0
    # 没有任何卖出腿 → 已实现为 0，而不是把在途成本记成亏损
    assert pnl.exit_legs([], pos) == []
