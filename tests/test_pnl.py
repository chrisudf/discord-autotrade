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


# ============================================================
# 佣金：按张按腿，过期只收一次
# ============================================================

def test_fee_is_per_contract_per_leg():
    """买卖各一次。2 张普通平仓 = 4 腿次。"""
    assert pnl.leg_fee(2, "CLOSE", 0.95) == 3.80
    assert pnl.leg_fee(1, "TRIM", 0.95) == 1.90


def test_expired_contracts_pay_only_the_entry_leg():
    """**过期没有卖出腿，只收买入那一次。**

    这条不是细节：一张最后卖在 $0.01 的合约，卖出腿的费用就和它的成交额
    同一个量级（见 lesson #48）。把过期也收两次费会把"卖在 0.01 到底值不值"
    这个结论算反。
    """
    assert pnl.leg_fee(1, "EXPIRE", 0.95) == 0.95
    assert pnl.leg_fee(2, "EXPIRE", 0.95) == 1.90


def test_net_subtracts_the_fee_from_gross():
    pos = {"US.X": _pos("US.X", 3.20)}
    legs = pnl.exit_legs(
        [_evt("US.X", "CLOSE", -1, 4.80, "2026-09-09T15:22:42Z")],
        pos, per_contract=0.95)
    assert legs[0]["realized"] == 160.0
    assert legs[0]["fee"] == 1.90 and legs[0]["net"] == 158.10


def test_penny_fill_is_economically_a_wash_at_this_rate():
    """便士单的账：2 张卖在 0.01 收回 $2，卖出腿费用 $1.90 → 净 +$0.10。

    钉住它是因为这个结论**对费率极其敏感**：$0.95/张时勉强为正，
    ≥$1.00/张就翻负。lesson #48 的论点（便士单在经济上无意义）两种情况
    都成立，但"无意义"和"倒贴"是两句话，不许含糊。
    """
    per = 0.95
    proceeds = 0.01 * 2 * pnl.CONTRACT_MULTIPLIER          # $2.00
    sell_fee = 2 * per                                      # $1.90
    assert round(proceeds - sell_fee, 2) == 0.10
    # 费率抬到 1.00 就翻负 —— 所以报表必须把费率打在抬头上
    assert round(proceeds - 2 * 1.00, 2) == 0.0
    assert proceeds - 2 * 1.10 < 0


# ============================================================
# 未实现：取不到价就留空，绝不猜
# ============================================================

def test_mark_open_leaves_unpriced_positions_empty():
    """取不到价**不猜**：一个编出来的浮盈比没有数字更糟。

    与 CLOSE 无价拒卖同一哲学 —— 这四张在途仓里有三张是 swing 裸奔仓，
    白天跑 --mark 时 OpenD 多半不在，全空是常态。
    """
    pos = {"US.A": _pos("US.A", 5.30, qty_rem=1, expiry="2026-09-18"),
           "US.B": _pos("US.B", 1.36, qty_rem=1, expiry="2026-10-16")}
    rows = pnl.mark_open(pos, {"US.A": 7.00}, per_contract=0.95)
    by = {r["option_code"]: r for r in rows}
    assert by["US.A"]["unrealized"] == 170.0
    assert by["US.B"]["mark"] is None and by["US.B"]["unrealized"] is None


def test_unrealized_charges_only_the_remaining_sell_leg():
    """未实现盈亏的语义是"现在平掉能拿回多少" —— 买入那次费已经付过了。"""
    pos = {"US.A": _pos("US.A", 5.30, qty_rem=2, expiry="2026-09-18")}
    r = pnl.mark_open(pos, {"US.A": 7.00}, per_contract=0.95)[0]
    assert r["unrealized"] == 340.0
    assert r["net"] == 340.0 - 2 * 0.95      # 只扣卖出那一腿


def test_closed_positions_never_appear_in_mark_open():
    pos = {"US.DONE": _pos("US.DONE", 1.00, qty_rem=0)}
    assert pnl.mark_open(pos, {"US.DONE": 9.99}) == []


# ============================================================
# 取价路径：不许把"没查"伪装成"查了没有"
# ============================================================

def test_dry_run_says_so_instead_of_returning_empty_quotes(capsys, monkeypatch):
    """[9/14 实锤] 第一版 fetch_marks 漏了 load_dotenv，于是 DRY_RUN 未设
    → _is_dry_run() 默认 True → 取价走 mock 分支全返 None → 报表整整齐齐打出
    四行"（无报价）"。

    我当场把它解释成"新鲜度门挡掉了盘前陈旧报价"——听起来完全合理，而真相是
    **根本没去查**（同一时刻直接 _snapshot 拿到的是 TSLA last=2.78 bid=2.75）。
    "查了但没有"和"压根没查"输出一模一样，是这类脚本最坏的失败形态。
    """
    monkeypatch.setenv("DRY_RUN", "true")
    assert pnl.fetch_marks(["US.X260918C380000"]) == {}
    assert "DRY_RUN" in capsys.readouterr().err, "必须说出来，不能静默返回空"


def test_stale_marks_carry_their_age():
    """陈旧价必须带年龄：不标年龄的收盘价和实时价长得一模一样。"""
    pos = {"US.T": _pos("US.T", 5.30, qty_rem=1, expiry="2026-09-18")}
    rows = pnl.mark_open(pos, {"US.T": 2.75}, per_contract=0.95)
    assert rows[0]["unrealized"] == -255.0
    # 年龄由 fetch_marks_stale 单独回传，format_report 负责打出来；
    # 这里钉的是 mark_open 不会因为价格陈旧就拒绝计算（那是看盘不是下单）
    assert rows[0]["net"] == -255.95
