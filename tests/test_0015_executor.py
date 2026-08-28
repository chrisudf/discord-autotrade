"""[0015] close_flow Outcome 枚举 + SellExecutor 四路合一。

Part A —— close_flow TG 抑制矩阵**表征测试**（契约要求：先钉住现行为再动刀）。
矩阵覆盖 6/18 IWM（broker 拒单误报 no matching）、7/6 IBM（runner-preserve
落兜底文案）、7/25 AVGO（无价拒卖）等历次夜盘事故定下的 TG 语义：
  - 每个仓位级结果（成交/拒单/异常/runner/无价/strike 不匹配）都有专属 TG，
    绝不再落到 "no matching open positions" 兜底；
  - 指纹回滚仅在"零成交且出现 broker 失败"时发生（0005 的双语孪生天然重试），
    确定性跳过（runner/无价/strike）不回滚——孪生重试只会重复刷 TG。

Part B —— SellExecutor 单元与四路参数差异（重构后行为不变的证据）：
  - 四路 slip/remark/trigger_source 分档保留（KC5/SL8/TP5/EOD10）；
  - TP round() 与 manager ceil 的口径差异**原样保留**（契约点名）；
  - TP 先 mark_tp_hit 再记账；SL 记账失败冻结；锁内重读互斥。
"""
import asyncio
import time
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autotrade.listener import close_flow, dedup
from autotrade.position import eod_watcher, manager, sl_watcher, tp_watcher
from autotrade.position.sell_executor import (
    Outcome,
    SellPlan,
    SkipSell,
    execute_sell,
)
from autotrade.storage import positions_db


def _uniq_code(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open_pos(symbol: str, qty: int = 2, strike: float = 100.0,
              entry: float = 2.0, channel: str = "ut") -> str:
    code = _uniq_code(symbol[:3])
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side="CALL",
        expiry=date(2026, 7, 31), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name=channel, msg_id="m_0015",
    )
    return code


# ============================================================
# Part A: TG 抑制矩阵（表驱动）
# ============================================================
# 每行：场景名 / 持仓(qty,strike,channel) / 信号 / broker 行为 / quote mock /
#       期望卖单次数 / TG 必含 / TG 必不含 / 指纹是否保留 / 期望剩余张数
# sell 取值：ok=提交成功 / reject=success:False / raise=抛异常 / None=不该被调
_MATRIX = [
    # 正常成交：平仓成交 TG，指纹保留（孪生被 dup 拦）
    dict(name="sold", sym="MTXA", qty=2,
         signal="trimmed {sym} @ 2.45", sell="ok", quote=None,
         sells=1, tg_has=["平仓成交"],
         tg_not=["no matching", "CLOSE 未执行", "系统错误"],
         fp_kept=True, remaining=1),
    # broker 拒单：专属 TG + 指纹回滚（6/18 IWM：曾误报 no matching）
    dict(name="broker_reject", sym="MTXB", qty=2,
         signal="trimmed {sym} @ 2.45", sell="reject", quote=None,
         sells=1, tg_has=["Sell rejected"],
         tg_not=["no matching", "平仓成交"],
         fp_kept=False, remaining=2),
    # broker 异常：专属 TG + 指纹回滚
    dict(name="broker_error", sym="MTXC", qty=2,
         signal="trimmed {sym} @ 2.45", sell="raise", quote=None,
         sells=1, tg_has=["Sell order error"],
         tg_not=["no matching", "平仓成交"],
         fp_kept=False, remaining=2),
    # runner-preserve（7/6 IBM）：runner TG，不落兜底；确定性结果指纹保留
    dict(name="runner_preserve", sym="MTXD", qty=1,
         signal="trimmed {sym} @ 2.45", sell=None, quote=None,
         sells=0, tg_has=["runner", "策略 A"],
         tg_not=["no matching", "平仓成交"],
         fp_kept=True, remaining=1),
    # 无价格参照（7/25 AVGO 形态 + 实时报价也不可得）：拒卖 TG，指纹保留
    dict(name="no_price_ref", sym="MTXE", qty=2,
         signal="@everyone\nKC Trades Bot:Trimmed {sym} +20% 💰",
         sell=None, quote="none",
         sells=0, tg_has=["无价格参照", "实时报价"],
         tg_not=["no matching", "平仓成交"],
         fp_kept=True, remaining=2),
    # strike 不匹配（6/30 TSLA 420c vs 425c）：不匹配 TG，指纹保留
    dict(name="strike_mismatch", sym="MTXF", qty=1, strike=425.0,
         signal="trimmed {sym} 420c @ 15.35", sell=None, quote=None,
         sells=0, tg_has=["不匹配"],
         tg_not=["no matching", "平仓成交"],
         fp_kept=True, remaining=1),
    # BULK 跨频道全部静默跳过 → 唯一允许 "no matching" 兜底的路径
    dict(name="bulk_all_channel_mismatch", sym="MTXG", qty=2,
         channel="KC-期权-波段", signal_channel="enrich",
         signal=("Alright - here's what I'm doing: Closing all positions "
                 "- I am 99% cash @ 2.80"),
         sell=None, quote=None,
         sells=0, tg_has=["no matching"],
         tg_not=["平仓成交"],
         fp_kept=True, remaining=2),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _MATRIX, ids=[c["name"] for c in _MATRIX])
async def test_close_flow_tg_suppression_matrix(monkeypatch, case):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("CLOSE_QUOTE_FALLBACK", raising=False)
    monkeypatch.delenv("STRATEGY_B", raising=False)
    code = _open_pos(case["sym"], qty=case["qty"],
                     strike=case.get("strike", 100.0),
                     channel=case.get("channel", "ut"))
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_calls = []

    def fake_sell(*args, **kwargs):
        sell_calls.append(kwargs)
        if case["sell"] == "raise":
            raise RuntimeError("simulated broker down")
        if case["sell"] == "reject":
            return {"success": False, "message": "simulated reject",
                    "order_id": None, "code": code,
                    "qty": kwargs["qty"], "price": kwargs["limit_price"]}
        return {"success": True, "order_id": "MTX1", "code": code,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    quote_mock = MagicMock(
        return_value=None if case["quote"] == "none" else 2.00)

    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(
            case["signal"].format(sym=case["sym"]), msg_id=150001,
            channel_name=case.get("signal_channel"),
        )

    text = "\n".join(notifications)
    assert len(sell_calls) == case["sells"], (case["name"], sell_calls)
    for frag in case["tg_has"]:
        assert frag.lower() in text.lower(), (case["name"], "缺", frag, text)
    for frag in case["tg_not"]:
        assert frag.lower() not in text.lower(), (case["name"], "不该有", frag, text)
    # 指纹回滚矩阵：只有"零成交且有 broker 失败"回滚
    assert (len(dedup._close_fps) == 1) is case["fp_kept"], (
        case["name"], dedup._close_fps)
    pos = positions_db.get(code)
    assert pos["qty_remaining"] == case["remaining"], case["name"]


@pytest.mark.asyncio
async def test_partial_success_with_broker_failure_keeps_fp(monkeypatch):
    """混合结局：一个 symbol 成交 + 另一个 broker 拒单 → 有成交就不回滚指纹
    （孪生重试会把已成交的仓位再 trim 一次——0008 复盘的 META 场景反例）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    code_ok = _open_pos("MXOK", qty=2)
    code_rej = _open_pos("MXRJ", qty=2)
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    def fake_sell(*args, **kwargs):
        if kwargs["option_code"] == code_rej:
            return {"success": False, "message": "sim reject",
                    "order_id": None, "code": code_rej,
                    "qty": kwargs["qty"], "price": kwargs["limit_price"]}
        return {"success": True, "order_id": "MX1", "code": kwargs["option_code"],
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    # [8/28] 多标的 + 单一喊价 → parser 丢弃喊价（价格无法归属，见
    # close_parser._drop_unattributable_price），落到 quote fallback。
    # 本用例要测的是"混合结局下的指纹去留"，不是定价 —— 给一个报价让它
    # 照常走完卖出流程，测试意图不变。
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "get_sell_ref_price", return_value=7.0), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "Trimmed $MXOK and $MXRJ here @ 7.00", msg_id=150002,
        )

    text = "\n".join(notifications)
    assert "平仓成交" in text and "Sell rejected" in text
    assert "no matching" not in text.lower()
    assert len(dedup._close_fps) == 1, "有成交 → 指纹保留，孪生必须被 dup 拦"


@pytest.mark.asyncio
async def test_runner_plus_broker_failure_rolls_back_fp(monkeypatch):
    """runner-preserve + broker 拒单混合：runner 不算成交 →
    零成交 + broker 失败 → 回滚指纹让孪生重试。"""
    monkeypatch.setenv("DRY_RUN", "true")
    _open_pos("MXRN", qty=1)   # runner-preserve
    code_rej = _open_pos("MXRB", qty=2)
    dedup._close_fps.clear()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    def fake_sell(*args, **kwargs):
        return {"success": False, "message": "sim reject",
                "order_id": None, "code": code_rej,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    # [8/28] 多标的 + 单一喊价 → parser 丢弃喊价（价格无法归属，见
    # close_parser._drop_unattributable_price），落到 quote fallback。
    # 本用例要测的是"混合结局下的指纹去留"，不是定价 —— 给一个报价让它
    # 照常走完卖出流程，测试意图不变。
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "get_sell_ref_price", return_value=7.0), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell):
        await close_flow.handle_close_signal(
            "Trimmed $MXRN and $MXRB here @ 7.00", msg_id=150003,
        )

    text = "\n".join(notifications)
    assert "runner" in text.lower() and "Sell rejected" in text
    assert len(dedup._close_fps) == 0, "零成交 + broker 失败 → 必须回滚指纹"


# ============================================================
# Part B: SellExecutor 四路参数差异 + 执行骨架单元
# ============================================================

def _pos_dict(code: str) -> dict:
    return positions_db.get(code)


@pytest.mark.asyncio
async def test_four_paths_slip_remark_trigger_source_preserved(monkeypatch):
    """契约点名的四路有意差异逐项验证（合并执行器后一个都不能被"顺手统一"）：
      slip：KC 5% / SL 8% / TP 5% / EOD 10%（参照价同为 2.00 → 限价四档分明）
      remark：kc_close_33pct / sl_polling / tp_t1 / eod_force
      trigger_source（记账事件）：kc_signal / sl_polling / tp_polling / eod
    """
    monkeypatch.setenv("DRY_RUN", "true")

    # --- SL: last=2.00, slip 8% → 1.84 ---
    code_sl = _open_pos("EXSL", qty=2, entry=5.0)
    sl_watcher._triggered.discard(code_sl)
    sl_calls = []

    def sl_sell(**kw):
        sl_calls.append(kw)
        return {"success": True, "order_id": "XSL", "code": code_sl,
                "qty": kw["qty"], "price": kw["limit_price"]}

    with patch("autotrade.position.sl_watcher.place_sell_order", side_effect=sl_sell), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._trigger_sl(_pos_dict(code_sl), 2.00, 2.50, 0.08)
    assert sl_calls[0]["limit_price"] == round(2.00 * 0.92, 2)  # 1.84
    assert sl_calls[0]["remark"] == "sl_polling"
    assert sl_calls[0]["qty"] == 2  # SL 全平剩余

    # --- TP: last=2.00, slip 5% → 1.90 ---
    code_tp = _open_pos("EXTP", qty=2, entry=1.0)
    tp_watcher._triggered_this_tick.clear()
    tp_calls = []

    def tp_sell(**kw):
        tp_calls.append(kw)
        return {"success": True, "order_id": "XTP", "code": code_tp,
                "qty": kw["qty"], "price": kw["limit_price"]}

    with patch("autotrade.position.tp_watcher.place_sell_order", side_effect=tp_sell), \
         patch("autotrade.position.tp_watcher.send_telegram", new_callable=AsyncMock):
        await tp_watcher._trigger_tp(_pos_dict(code_tp), 2.00, 0.50, 50, 1, 0.05)
    assert tp_calls[0]["limit_price"] == round(2.00 * 0.95, 2)  # 1.90
    assert tp_calls[0]["remark"] == "tp_t1"
    assert tp_calls[0]["qty"] == 1  # round(2×50%) = 1

    # --- EOD: last=2.00, slip 10% → 1.80（锁内取价） ---
    code_eod = _open_pos("EXEO", qty=2, entry=1.0)
    eod_watcher._skip_until.pop(code_eod, None)
    eod_calls = []

    def eod_sell(**kw):
        eod_calls.append(kw)
        return {"success": True, "order_id": "XEO", "code": code_eod,
                "qty": kw["qty"], "price": kw["limit_price"]}

    with patch("autotrade.position.eod_watcher.get_last_price", return_value=2.00), \
         patch("autotrade.position.eod_watcher.place_sell_order", side_effect=eod_sell), \
         patch("autotrade.position.eod_watcher.send_telegram", new_callable=AsyncMock):
        await eod_watcher._force_close(_pos_dict(code_eod), 0.10, time.time())
    assert eod_calls[0]["limit_price"] == round(2.00 * 0.90, 2)  # 1.80
    assert eod_calls[0]["remark"] == "eod_force"
    assert eod_calls[0]["qty"] == 2  # EOD 全平剩余

    # --- KC: signal_price=2.00, SELL_SLIP 5% → 1.90 ---
    code_kc = _open_pos("EXKC", qty=3, entry=1.0)
    dedup._close_fps.clear()
    kc_calls = []

    def kc_sell(**kw):
        kc_calls.append(kw)
        return {"success": True, "order_id": "XKC", "code": code_kc,
                "qty": kw["qty"], "price": kw["limit_price"]}

    async def noop(msg):
        pass

    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=kc_sell):
        await close_flow.handle_close_signal("trimmed EXKC @ 2.00", msg_id=150010)
    assert kc_calls[0]["limit_price"] == round(2.00 * 0.95, 2)  # 1.90
    assert kc_calls[0]["remark"] == "kc_close_33pct"
    assert kc_calls[0]["qty"] == 1  # ceil(3×33%) = 1

    # trigger_source 记账口径四路各归各（qty_delta<0 只看卖出事件——
    # open_or_add 的建仓事件 trigger_source 也是 kc_signal，要滤掉）
    for code, src in ((code_sl, "sl_polling"), (code_tp, "tp_polling"),
                      (code_eod, "eod"), (code_kc, "kc_signal")):
        evts = [e for e in positions_db.get_events(code)
                if e["trigger_source"] == src and e["qty_delta"] < 0]
        assert len(evts) == 1, (code, src)


@pytest.mark.asyncio
async def test_tp_round_vs_kc_ceil_divergence_preserved(monkeypatch):
    """契约点名**原样保留**的口径差异：同样"卖剩余的 50%"，
    TP 用 round()（banker's：round(2.5)=2，少卖让 runner 多跑），
    KC 路径的 manager.calc_qty_to_sell 用 ceil()（跟足信号比例 → 3）。
    合并执行器时若被"顺手统一"，TP 阶梯的仓位曲线会整体偏移。"""
    monkeypatch.setenv("DRY_RUN", "true")
    code = _open_pos("EXRC", qty=5, entry=1.0)
    tp_watcher._triggered_this_tick.clear()

    tp_calls = []

    def tp_sell(**kw):
        tp_calls.append(kw)
        return {"success": True, "order_id": "XRC", "code": code,
                "qty": kw["qty"], "price": kw["limit_price"]}

    with patch("autotrade.position.tp_watcher.place_sell_order", side_effect=tp_sell), \
         patch("autotrade.position.tp_watcher.send_telegram", new_callable=AsyncMock):
        await tp_watcher._trigger_tp(_pos_dict(code), 2.00, 0.50, 50, 1, 0.05)

    assert tp_calls[0]["qty"] == 2, "TP round(5×50%)=2（banker's rounding）"
    # 同参数走 KC 的 ceil 口径是 3——差异是策略语义不是笔误
    assert manager.calc_qty_to_sell({"qty_remaining": 5}, 50) == 3


@pytest.mark.asyncio
async def test_tp_marks_tier_before_record_and_freezes_tier_on_db_failure(monkeypatch):
    """TP 顺序契约：先 mark_tp_hit 再 on_close_filled。记账失败时档位已持久化，
    下一次触发同档被 tp_hits 位掩码拦住——绝不对同档重复挂卖单。"""
    monkeypatch.setenv("DRY_RUN", "true")
    code = _open_pos("EXTM", qty=2, entry=1.0)
    tp_watcher._triggered_this_tick.clear()

    order = []
    real_mark = positions_db.mark_tp_hit

    def spy_mark(c, bit):
        order.append("mark_tp_hit")
        return real_mark(c, bit)

    def failing_record(**kw):
        order.append("on_close_filled")
        raise RuntimeError("db down")

    sell_mock = MagicMock(return_value={"success": True, "order_id": "XTM",
                                        "code": code, "qty": 1, "price": 1.9})
    with patch("autotrade.position.tp_watcher.place_sell_order", sell_mock), \
         patch("autotrade.position.tp_watcher.send_telegram", new_callable=AsyncMock), \
         patch.object(tp_watcher.positions_db, "mark_tp_hit", side_effect=spy_mark), \
         patch.object(tp_watcher.position_mgr, "on_close_filled",
                      side_effect=failing_record):
        await tp_watcher._trigger_tp(_pos_dict(code), 2.00, 0.50, 50, 1, 0.05)
        assert order == ["mark_tp_hit", "on_close_filled"], "必须先持久化档位再记账"
        # 记账失败：DB 仓位仍 OPEN，但 T1 档位已置位 → 重触发被拦
        tp_watcher._triggered_this_tick.clear()
        await tp_watcher._trigger_tp(_pos_dict(code), 2.00, 0.50, 50, 1, 0.05)
        assert sell_mock.call_count == 1, "同档绝不重复挂卖单"

    assert positions_db.get(code)["tp_hits"] & 1
    positions_db.record_close(code, 2, 2.0, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_executor_outcome_values_direct(monkeypatch):
    """execute_sell 骨架直测：SOLD / BROKER_FAILED(异常/拒单) /
    SKIPPED_SILENT(锁内重读已平) / SkipSell 透传。注入全部用测试桩，
    证明骨架自身不 import broker/transport（注入点契约）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    code = _open_pos("EXDR", qty=2, entry=1.0)
    pos = positions_db.get(code)

    notified, confirmed = [], []

    async def notify(msg):
        notified.append(msg)

    async def plan(fresh):
        return SellPlan(qty=fresh["qty_remaining"], limit=1.5,
                        remark="ut", note="ut", notify_pct=100)

    def sell_ok(**kw):
        return {"success": True, "order_id": "XDR", "code": code,
                "qty": kw["qty"], "price": kw["limit_price"]}

    # SOLD：记账走真 position_mgr → DB CLOSED；fill_confirm/通知注入被调
    outcome, result = await execute_sell(
        pos, trigger_source="manual", notify_trigger="manual",
        plan_fn=plan, place_sell_order=sell_ok, notify=notify,
        fill_confirm=lambda oid, q: confirmed.append((oid, q)),
    )
    assert outcome is Outcome.SOLD and result["order_id"] == "XDR"
    assert confirmed == [("XDR", 2)]
    assert "平仓成交" in notified[0]
    assert positions_db.get(code)["status"] == "CLOSED"

    # SKIPPED_SILENT：锁内重读发现已平，plan 根本不执行
    plan_called = []

    async def plan_spy(fresh):
        plan_called.append(1)
        return SkipSell(Outcome.SKIPPED_SILENT)

    outcome, _ = await execute_sell(
        pos, trigger_source="manual", notify_trigger="manual",
        plan_fn=plan_spy, place_sell_order=sell_ok, notify=notify,
        fill_confirm=lambda oid, q: None,
    )
    assert outcome is Outcome.SKIPPED_SILENT and plan_called == []

    # BROKER_FAILED（拒单 / 异常）+ 钩子收到 plan 与 result
    code2 = _open_pos("EXDS", qty=1, entry=1.0)
    pos2 = positions_db.get(code2)
    hook_seen = []

    async def rejected(result, plan_):
        hook_seen.append(("reject", plan_.qty, result.get("message")))

    async def errored(e, plan_):
        hook_seen.append(("error", plan_.qty, str(e)))

    outcome, result = await execute_sell(
        pos2, trigger_source="manual", notify_trigger="manual",
        plan_fn=plan,
        place_sell_order=lambda **kw: {"success": False, "message": "nope"},
        notify=notify, fill_confirm=lambda oid, q: None,
        on_sell_rejected=rejected,
    )
    assert outcome is Outcome.BROKER_FAILED and result == {"success": False, "message": "nope"}

    def raising(**kw):
        raise RuntimeError("boom")

    outcome, result = await execute_sell(
        pos2, trigger_source="manual", notify_trigger="manual",
        plan_fn=plan, place_sell_order=raising,
        notify=notify, fill_confirm=lambda oid, q: None,
        on_sell_error=errored,
    )
    assert outcome is Outcome.BROKER_FAILED and result is None
    assert hook_seen == [("reject", 1, "nope"), ("error", 1, "boom")]
    # 两次失败都没动仓位
    assert positions_db.get(code2)["status"] == "OPEN"

    # SkipSell 透传：RUNNER_PRESERVED 原样带回给调用方
    async def preserve(fresh):
        return SkipSell(Outcome.RUNNER_PRESERVED)

    outcome, _ = await execute_sell(
        pos2, trigger_source="manual", notify_trigger="manual",
        plan_fn=preserve, place_sell_order=sell_ok,
        notify=notify, fill_confirm=lambda oid, q: None,
    )
    assert outcome is Outcome.RUNNER_PRESERVED
    positions_db.record_close(code2, 1, 1.0, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_sl_vs_tp_concurrent_single_sell(monkeypatch):
    """SL 与 TP 经同一执行骨架并发触发同一仓位 → 锁内重读互斥，只有一条卖单
    （与既有 SL/EOD 并发测试同款语义，证明合并后锁没有被绕开）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    code = _open_pos("EXLK", qty=1, entry=1.0)
    sl_watcher._triggered.discard(code)
    tp_watcher._triggered_this_tick.clear()

    calls = []

    def slow_sell(option_code, qty, limit_price, remark):
        time.sleep(0.05)  # 模拟 broker RTT，制造并发窗口
        calls.append((option_code, qty, remark))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": f"LK{len(calls)}", "code": option_code}

    pos = positions_db.get(code)
    with patch("autotrade.position.sl_watcher.place_sell_order", side_effect=slow_sell), \
         patch("autotrade.position.tp_watcher.place_sell_order", side_effect=slow_sell), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock), \
         patch("autotrade.position.tp_watcher.send_telegram", new_callable=AsyncMock):
        await asyncio.gather(
            sl_watcher._trigger_sl(pos, 0.40, 0.50, 0.08),
            tp_watcher._trigger_tp(dict(pos), 1.60, 0.50, 50, 1, 0.05),
        )

    assert len(calls) == 1, f"并发触发必须只有一条卖单，实际: {calls}"
    assert positions_db.get(code)["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_sl_freeze_tg_keeps_order_id(monkeypatch):
    """SL 记账失败冻结告警必须带 order id（对账线索）——钩子签名改造
    （on_record_failure 带 result）后文案信息量不缩水。"""
    monkeypatch.setenv("DRY_RUN", "true")
    code = _open_pos("EXFZ", qty=1, entry=1.0)
    sl_watcher._triggered.discard(code)

    sell_ok = {"success": True, "qty": 1, "price": 0.37,
               "order_id": "SL_ORD_FZ", "code": code}
    with patch("autotrade.position.sl_watcher.place_sell_order", return_value=sell_ok), \
         patch("autotrade.position.sl_watcher.send_telegram",
               new_callable=AsyncMock) as tg, \
         patch.object(sl_watcher.position_mgr, "on_close_filled",
                      side_effect=RuntimeError("db down")):
        await sl_watcher._trigger_sl(positions_db.get(code), 0.40, 0.50, 0.08)

    freeze = [str(c) for c in tg.await_args_list if "冻结" in str(c)]
    assert len(freeze) == 1
    # TG 走 MarkdownV2 escape（下划线带反斜杠），剥掉转义再断言
    assert "SL_ORD_FZ" in freeze[0].replace("\\", ""), "冻结 TG 必须带 order id"
    assert code in sl_watcher._triggered  # 冻结语义保留
    sl_watcher._triggered.discard(code)
    positions_db.record_close(code, 1, 1.0, "manual", note="ut cleanup")
