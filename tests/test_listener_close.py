"""listener close_flow.handle_close_signal（原 discord_client._handle_close_signal）端到端测试

补 6/18 IWM 事件暴露的两个 bug：
- broker 拒单后误报 "no matching open positions"（原 any_executed 没 set）
"""
import asyncio
import os
import time
from datetime import date, datetime
from unittest.mock import patch

import pytest

from autotrade.storage import positions_db
from autotrade.listener import close_flow, dedup, heuristics


def _uniq_code(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


@pytest.mark.asyncio
async def test_broker_reject_does_not_report_no_matching():
    """broker 拒单时 TG 应该收到 'Sell rejected'，不该再收到 'no matching open positions'。"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("BRJ")
    positions_db.open_or_add(
        option_code=code, symbol="REJX", strike=220.0, side="CALL",
        expiry=date(2026, 6, 30), qty=3, fill_price=2.0,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order",
                      return_value={"success": False,
                                    "message": "MOOMOO_ACC_ID 未配置",
                                    "order_id": None, "code": code,
                                    "qty": 1, "price": 1.9}):
        await close_flow.handle_close_signal(
            "KC Trades Bot:trimmed REJX @ 2.45", msg_id=9999,
        )

    text = "\n".join(notifications)
    assert "Sell rejected" in text or "rejected" in text.lower(), (
        "应有 broker 拒单告警", text,
    )
    assert "no matching" not in text.lower(), (
        "broker 拒单后不应再报 'no matching open positions'", text,
    )

    # 仓位仍然是 OPEN（broker 没成交）
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN"
    assert pos["qty_remaining"] == 3

    positions_db.record_close(code, 3, 2.0, "manual", note="ut cleanup")


# === 6/30 strike-aware close hint regression ===

@pytest.mark.asyncio
async def test_strike_hint_mismatch_skips_close():
    """KC 喊关 TSLA 420c，但我们持仓是 TSLA 425c → 跳过卖单，发 TG '不匹配' 提示"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("STK")
    # 持仓：TSLA 425c
    positions_db.open_or_add(
        option_code=code, symbol="TSLAA", strike=425.0, side="CALL",
        expiry=date(2026, 7, 1), qty=1, fill_price=1.67,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_strike_1",
    )
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    async def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True}

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        # KC 平 420c，不是 425c
        await close_flow.handle_close_signal(
            "trimmed TSLAA 420c @ 15.35", msg_id=88888,
        )

    text = "\n".join(notifications)
    # 应有"不匹配"告警，不应该卖
    assert "strike" in text.lower() and "不匹配" in text, (
        "应有 strike 不匹配的 TG 告警", text,
    )
    assert sell_called == [], "strike 不匹配时不应触发卖单"

    # 仓位仍 OPEN
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN"
    assert pos["qty_remaining"] == 1

    positions_db.record_close(code, 1, 1.67, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_strike_hint_match_executes_close():
    """KC 喊关 TSLA 425c，持仓恰好 TSLA 425c → 正常卖单"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("STM")
    positions_db.open_or_add(
        option_code=code, symbol="TSLAB", strike=425.0, side="CALL",
        expiry=date(2026, 7, 1), qty=1, fill_price=1.67,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_strike_2",
    )
    dedup._close_fps.clear()

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True, "order_id": "X", "code": code, "qty": 1, "price": 14.58}

    async def noop_notify(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop_notify), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "全部平仓 TSLAB 425c 持仓 @ 15.35", msg_id=77777,
        )

    # strike 匹配应该触发卖单
    assert len(sell_called) == 1, f"strike 匹配应触发卖单，实际: {sell_called}"

    # 收尾
    positions_db.record_close(code, 1, 14.58, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_no_strike_hint_keeps_legacy_symbol_only_behavior():
    """普通 trim 信号（无 strike）→ 按 symbol 关全部，保留旧行为。

    注：qty=2 而非 1，因为 v2 起 1 张 + trim<100% 会触发 runner-preserve 跳过
    （见 test_calc_qty_to_sell_single_contract_runner_preserve）。这里测的是
    strike-filter 的旧回落行为，跟 runner 规则解耦。
    """
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("LEG")
    positions_db.open_or_add(
        option_code=code, symbol="SPYZ", strike=748.0, side="CALL",
        expiry=date(2026, 7, 6), qty=2, fill_price=2.59,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_strike_3",
    )
    dedup._close_fps.clear()

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True, "order_id": "Y", "code": code, "qty": 1, "price": 2.71}

    async def noop_notify(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop_notify), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        # 无 strike 的 trim
        await close_flow.handle_close_signal(
            "trimmed SPYZ @ 2.85", msg_id=66666,
        )

    # 旧行为：symbol 匹配即关（qty=2 时 33% ceil = 1 张，卖单会挂）
    assert len(sell_called) == 1, "无 strike hint 应保持旧 symbol-only 行为"

    positions_db.record_close(code, 2, 2.71, "manual", note="ut cleanup")


# === 7/6 runner-preserve TG 文案回归 ===

@pytest.mark.asyncio
async def test_runner_preserve_reports_truthfully():
    """1 张持仓 + trim 信号 → 跳过卖单，TG 必须说 runner-preserve，
    不能落到 'no matching open positions' 兜底（7/6 IBM 两次实锤误导）。"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("RUN")
    positions_db.open_or_add(
        option_code=code, symbol="RUNX", strike=305.0, side="CALL",
        expiry=date(2026, 7, 10), qty=1, fill_price=2.11,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_run_1",
    )
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append((args, kwargs))
        return {"success": True}

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "trimmed RUNX @ 2.85", msg_id=55555,
        )

    assert sell_called == [], "runner-preserve 不应触发卖单"
    text = "\n".join(notifications)
    assert "runner" in text.lower(), ("TG 应说明 runner-preserve", text)
    assert "no matching" not in text.lower(), (
        "不应再报误导性的 'no matching open positions'", text,
    )
    # 仓位原样保留
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN"
    assert pos["qty_remaining"] == 1


# === 7/6 add-on 检测 ===

def _open_addon_pos(symbol: str) -> str:
    code = _uniq_code("ADO")
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=742.0, side="PUT",
        expiry=date(2026, 7, 13), qty=1, fill_price=2.57,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_addon",
    )
    return code


def test_addon_detected_en_bare_ticker():
    """7/6 00:31 'small add SPY @ 1.86' —— 裸 ticker + 持仓白名单 → 命中"""
    _open_addon_pos("ADX1")
    got = heuristics._looks_like_addon_attempt(
        "KC Trades Bot:small add ADX1 @ 1.86, my stop is right above this zone near 750"
    )
    assert got == "ADX1"


def test_addon_detected_zh():
    """ZH 版 '小加仓SPY @ 1.86'（汉字-字母边界无 \\b，用 lookaround）"""
    _open_addon_pos("ADX2")
    got = heuristics._looks_like_addon_attempt(
        "KC Trades Bot: 小加仓ADX2 @ 1.86, 止损位于750附近区域上方。"
    )
    assert got == "ADX2"


def test_addon_not_held_symbol_returns_none():
    """add + @price 但 symbol 不在持仓白名单 → None（新开仓评论不提醒）"""
    got = heuristics._looks_like_addon_attempt(
        "small add ZZQQ @ 2.10 looks good here"
    )
    assert got is None


def test_addon_requires_price():
    """持仓 symbol + add 但没喊价 → None（'can add more later' 类评论）"""
    _open_addon_pos("ADX3")
    got = heuristics._looks_like_addon_attempt(
        "might add more ADX3 if we reclaim the level"
    )
    assert got is None


def test_addon_requires_add_keyword():
    """持仓 symbol + @price 但无 add 词 → None（普通评论）"""
    _open_addon_pos("ADX4")
    got = heuristics._looks_like_addon_attempt(
        "ADX4 holding the line @ 1.86 nicely"
    )
    assert got is None


# === CLOSE 指纹登记时机回归（review 0005）===

@pytest.mark.asyncio
async def test_failed_close_allows_bilingual_retry():
    """卖单失败时不登记指纹 → 1-3s 后到达的另一语言版本可以正常重试。

    回归：之前查即登记，ZH 版先到但 broker 拒单后，EN 版被当 dup 拦掉，
    天然的重试机会丢失。
    """
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("RTY")
    positions_db.open_or_add(
        option_code=code, symbol="RTYX", strike=100.0, side="CALL",
        expiry=date(2026, 7, 10), qty=2, fill_price=2.0,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_rty_1",
    )
    dedup._close_fps.clear()

    async def noop_notify(msg):
        pass

    calls = []

    def failing_then_ok_sell(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"success": False, "message": "transient broker error",
                    "order_id": None, "code": code, "qty": 1, "price": 1.9}
        return {"success": True, "order_id": "RTY_OK", "code": code,
                "qty": 1, "price": 1.9}

    with patch.object(close_flow, "_safe_notify", side_effect=noop_notify), \
         patch.object(close_flow, "place_sell_order", side_effect=failing_then_ok_sell):
        # 第一发（模拟 ZH 先到）：broker 拒单 → 不应登记指纹
        await close_flow.handle_close_signal("减仓 RTYX @ 2.45", msg_id=1111)
        # 第二发（模拟 EN 双发）：应被当作重试执行，而不是 dup 拦掉
        await close_flow.handle_close_signal("trimmed RTYX @ 2.45", msg_id=1112)

    assert len(calls) == 2, f"第二发应重试卖出，实际 broker 调用: {len(calls)}"

    # 第二发成功后指纹已登记 → 第三发同信号应被 dup 拦掉
    with patch.object(close_flow, "_safe_notify", side_effect=noop_notify), \
         patch.object(close_flow, "place_sell_order", side_effect=failing_then_ok_sell):
        await close_flow.handle_close_signal("trimmed RTYX @ 2.45", msg_id=1113)
    assert len(calls) == 2, "成功后同指纹信号应被 dup 拦截"

    positions_db.record_close(code, 2, 2.0, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_deterministic_skip_registers_fp_no_twin_spam():
    """确定性跳过（runner-preserve）也登记指纹 → 双语孪生不会重复刷 TG。

    这是对 0005 "只在成功后登记" 的本地精修：runner-preserve / 无价格参照
    这类结果是确定性的，孪生重试只会重复告警，应照常去重。
    """
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("DSK")
    positions_db.open_or_add(
        option_code=code, symbol="DSKX", strike=100.0, side="CALL",
        expiry=date(2026, 7, 10), qty=1, fill_price=2.0,  # qty=1 → runner-preserve
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_dsk_1",
    )
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order") as sell_mock:
        await close_flow.handle_close_signal("trimmed DSKX @ 2.45", msg_id=2221)
        await close_flow.handle_close_signal("减仓 DSKX @ 2.45", msg_id=2222)

    sell_mock.assert_not_called()
    runner_alerts = [n for n in notifications if "runner" in n.lower()]
    assert len(runner_alerts) == 1, (
        f"孪生版本应被指纹去重，只发一条 runner-preserve TG，实际 {len(runner_alerts)}",
        notifications,
    )

    positions_db.record_close(code, 1, 2.0, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_multi_symbol_close_hint_only_scopes_first_symbol():
    """'Trimmed TSLA 420c and MSFT here' —— strike hint 来自 TSLA，
    不能拿去过滤 MSFT 的持仓（回归：MSFT 平仓被静默跳过）。"""
    os.environ["DRY_RUN"] = "true"
    code_a = _uniq_code("MSA")
    code_b = _uniq_code("MSB")
    positions_db.open_or_add(
        option_code=code_a, symbol="TSLAM", strike=420.0, side="CALL",
        expiry=date(2026, 7, 10), qty=2, fill_price=2.0,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="ms1",
    )
    positions_db.open_or_add(
        option_code=code_b, symbol="MSFTM", strike=390.0, side="CALL",
        expiry=date(2026, 7, 10), qty=2, fill_price=2.5,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="ms2",
    )
    dedup._close_fps.clear()

    sold_codes = []

    def fake_sell(*args, **kwargs):
        sold_codes.append(kwargs["option_code"])
        return {"success": True, "order_id": "MS", "code": kwargs["option_code"],
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    async def noop_notify(msg):
        pass

    # [8/28] 多标的 + 单一喊价：@7.00 归不到具体合约（"7.00" 是 TSLA 420c 的价，
    # 拿去给 MSFT 定限价就是 8/28 那笔 UNG→AAPL 串台的同一形状），parser 丢弃
    # 喊价并置 price_unattributable，close_flow 落到 quote fallback。
    # **平仓意图仍然执行** —— 不可归属的是价格不是意图，本用例的契约不变。
    with patch.object(close_flow, "_safe_notify", side_effect=noop_notify), \
         patch.object(close_flow, "get_sell_ref_price", return_value=7.0), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "Trimmed $TSLAM 420c and $MSFTM here @ 7.00", msg_id=55555,
        )

    assert code_a in sold_codes, "hint 匹配的 TSLA 420c 应该卖"
    assert code_b in sold_codes, "MSFT 不该被 TSLA 的 strike hint 过滤掉"

    positions_db.record_close(code_a, 2, 7.0, "manual", note="ut cleanup")
    positions_db.record_close(code_b, 2, 7.0, "manual", note="ut cleanup")


# === 7/8 复盘回归：fp 原子性 / BULK 例外 ===

@pytest.mark.asyncio
async def test_close_fp_atomic_blocks_inflight_twin():
    """双发在第一条还没处理完时到达（实测 0.9s < handler 耗时 1.2s）
    → 第二条必须被 dup 拦住。

    7/7-7/8 连续两夜实锤：0005 把登记挪到 handler 末尾后，第二条 EN 穿过
    dup 检查双重处理（qty≥2 时 trim 执行两次）。现在查即登记（原子）。
    """
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("ATW")
    positions_db.open_or_add(
        option_code=code, symbol="ATWX", strike=100.0, side="CALL",
        expiry=date(2026, 7, 17), qty=4, fill_price=2.0,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_atw",
    )
    dedup._close_fps.clear()

    calls = []

    def slow_sell(*args, **kwargs):
        time.sleep(0.05)  # 模拟 broker RTT，制造双发竞态窗口
        calls.append(kwargs)
        return {"success": True, "order_id": f"ATW{len(calls)}",
                "code": code, "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    async def noop(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=slow_sell):
        await asyncio.gather(
            close_flow.handle_close_signal("trimmed ATWX @ 2.45", msg_id=71),
            close_flow.handle_close_signal("trimmed ATWX @ 2.45", msg_id=72),
        )

    assert len(calls) == 1, f"孪生并发必须只执行一次 trim，实际: {calls}"

    positions_db.record_close(code, 4 - calls[0]["qty"], 2.45, "manual",
                              note="ut cleanup")


@pytest.mark.asyncio
async def test_bulk_close_excludes_kept_symbol():
    """7/8 'Closing all positions outside of the $IBM lotto'：
    IBM 不卖，其余按 100% 全平。"""
    os.environ["DRY_RUN"] = "true"
    code_keep = _uniq_code("BKE")
    code_sell = _uniq_code("BKS")
    positions_db.open_or_add(
        option_code=code_keep, symbol="IBM", strike=310.0, side="CALL",
        expiry=date(2026, 7, 10), qty=2, fill_price=2.27,
        category="lotto", apply_sl=False, eod_force_close=False, tags=["lotto"],
        channel_name="ut", msg_id="m_bke",
    )
    positions_db.open_or_add(
        option_code=code_sell, symbol="DELL", strike=465.0, side="CALL",
        expiry=date(2026, 7, 10), qty=2, fill_price=2.65,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_bks",
    )
    dedup._close_fps.clear()

    sold = []

    def fake_sell(*args, **kwargs):
        sold.append(kwargs["option_code"])
        return {"success": True, "order_id": "BK", "code": kwargs["option_code"],
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    async def noop(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "Alright - here's what I'm doing: Closing all positions outside of "
            "the $IBM $310 lotto - this is a 1% position - I am 99% cash @ 2.80",
            msg_id=73,
        )

    assert code_sell in sold, "DELL 应被全平"
    assert code_keep not in sold, "IBM 在例外表里，不能卖"
    assert positions_db.get(code_sell)["status"] == "CLOSED"  # pct=100 全平
    assert positions_db.get(code_keep)["status"] == "OPEN"

    positions_db.record_close(code_keep, 2, 2.27, "manual", note="ut cleanup")


def test_bang_not_bare_ticker():
    """7/9 'BANG! Out half 2.45' —— BANG 是叹词不是 ticker，无 symbol 的
    跟单 trim 不该触发 looks-like-close TG。"""
    assert heuristics._looks_like_close_attempt(
        "@everyone\nKC Trades Bot:BANG! Out half 2.45, stop at 2.10 ✅"
    ) is False
    # 带真 ticker 的仍然要告警
    assert heuristics._looks_like_close_attempt(
        "@everyone\nKC Trades Bot:BANG! Out half SPY 2.45"
    ) is True


# === 7/10 事故回归：频道来源约束 / strike-hint 端到端 ===

@pytest.mark.asyncio
async def test_cross_channel_close_skipped():
    """enrich 的 close 信号不能平 KC 来源的仓位（7/10 near-miss）。"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("XCH")
    positions_db.open_or_add(
        option_code=code, symbol="XCHX", strike=230.0, side="CALL",
        expiry=date(2026, 8, 21), qty=2, fill_price=2.93,
        category="swing", apply_sl=False, eod_force_close=False, tags=[],
        channel_name="KC-期权-波段", msg_id="m_xch",
    )
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append(kwargs)
        return {"success": True, "order_id": "XC", "code": code,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    # enrich 的信号 → 不卖 + 专属 TG
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "$XCHX - I'm practically all out @ 3.60", msg_id=81,
            channel_name="enrich",
        )
    assert sell_called == [], "跨频道信号不能触发卖单"
    text = "\n".join(notifications)
    assert "频道不匹配" in text, ("应有频道不匹配 TG", text)
    assert positions_db.get(code)["status"] == "OPEN"

    # 同频道信号 → 正常平仓
    dedup._close_fps.clear()
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "$XCHX - I'm practically all out @ 3.60", msg_id=82,
            channel_name="KC-期权-波段",
        )
    assert len(sell_called) == 1, "同频道信号应正常平仓"


@pytest.mark.asyncio
async def test_wrong_side_close_blocked_end_to_end():
    """7/10 事故端到端复现：KC 平 755 call，我们持 730 put →
    strike/side hint 现在能从全文抽到 → 不匹配 → 拒绝卖出。"""
    os.environ["DRY_RUN"] = "true"
    code = _uniq_code("WSC")
    positions_db.open_or_add(
        option_code=code, symbol="WSCX", strike=730.0, side="PUT",
        expiry=date(2026, 7, 17), qty=1, fill_price=2.93,
        category="swing", apply_sl=False, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_wsc",
    )
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append(kwargs)
        return {"success": True}

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "KC Trades Bot:WSCX 755c IN THE MONEY! Closed @ 4.40 💰", msg_id=83,
        )

    assert sell_called == [], "错向（call hint vs put 持仓）不能卖"
    assert "不匹配" in "\n".join(notifications)
    assert positions_db.get(code)["status"] == "OPEN"
