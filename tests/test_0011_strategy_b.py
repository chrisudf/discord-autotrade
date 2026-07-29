"""[0011] 策略B runner 升级（ship-dark）回归。

背景：runner-preserve（策略A）对单张仓 + trim<100% 一律死拿——
6/30 SPY 748c / 7/1 MSFT 390c 提前平掉损失 ~$330+/合约是它的正面战绩，
但 AVGO 415c（7/23-24 夜）从 +50% 一路死拿到过期归零是它的反面教材。
策略B：KC 喊 trim 被 runner-preserve 拦下时，取实时报价算**我方**浮盈，
达标（>= STRATEGY_B_MIN_PNL_PCT，缺省 25）→ 该仓位全部剩余卖出
（单张仓不存在"卖 33%"，全出是唯一可行响应）；未达标/无新鲜报价 →
维持策略A死拿（节流 TG 附原因）。STRATEGY_B 缺省 false [ship-dark]。

分三层：
  1. policy.positions.strategy_b_decision 纯函数（阈值边界 / quote None /
     kc_pnl_pct 只进文案不进判断 / avg_entry 异常）
  2. close_flow 端到端：默认关 = 与今天**逐字**一致（契约硬要求）
  3. close_flow 端到端：开 + 达标全卖 / 未达标保留 / 无报价保留 /
     报价复用（不重复吃 snapshot 配额）/ 节流 key 不带 reason
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from autotrade.listener import close_flow, dedup
from autotrade.notify.messages import format_close_skipped
from autotrade.policy.positions import strategy_b_decision
from autotrade.storage import positions_db

# 7/25 夜形态：只报盈利不喊价（signal_price=None, signal_pnl_pct=+20, pct 缺省 33）
AVGO_TRIM_NOPRICE = "@everyone\nKC Trades Bot:Trimmed AVGO +20% 💰"
# 7/24 夜带价形态（signal_price=2.20 → 限价必须仍走 signal 优先级）
AVGO_TRIM_WITH_PRICE = "BANG! Trimmed AVGO @ 2.20"


# ============================================================
# 1. strategy_b_decision 纯函数
# ============================================================

def test_decision_sell_all_at_exact_threshold():
    """our_pnl == min_pnl_pct 恰好达标（>= 语义）→ SELL_ALL。"""
    decision, reason = strategy_b_decision(1.0, 1.25, None, 25)
    assert decision == "SELL_ALL"
    assert "25.0%" in reason or "+25" in reason


def test_decision_preserve_below_threshold():
    decision, reason = strategy_b_decision(1.0, 1.24, None, 25)
    assert decision == "PRESERVE"
    assert "浮盈" in reason and "阈值" in reason


def test_decision_quote_none_preserves():
    """无新鲜报价 → PRESERVE：与 CLOSE 无价拒卖同一"宁错过不错杀"底线，
    绝不按盲猜浮盈卖 runner。"""
    decision, reason = strategy_b_decision(1.0, None, 20.0, 25)
    assert decision == "PRESERVE"
    assert "无新鲜报价" in reason


def test_decision_kc_pnl_only_in_reason_not_in_judgement():
    """kc_pnl_pct 只进文案不进判断（显式推迟的口径对齐设计决策）：
    KC 自报 +999% 也拉不动未达标的我方浮盈。"""
    decision, reason = strategy_b_decision(1.0, 1.10, 999.0, 25)
    assert decision == "PRESERVE"
    assert "999" in reason  # 进文案
    # 反向：KC 没报盈亏也不妨碍我方达标全出
    decision2, reason2 = strategy_b_decision(1.0, 2.0, None, 25)
    assert decision2 == "SELL_ALL"
    assert "KC 未报盈亏" in reason2


def test_decision_bad_avg_entry_preserves():
    """avg_entry 异常（0/None）算不出浮盈 → PRESERVE 保守兜底。"""
    assert strategy_b_decision(0, 2.0, None, 25)[0] == "PRESERVE"
    assert strategy_b_decision(None, 2.0, None, 25)[0] == "PRESERVE"


def test_decision_negative_pnl_preserves():
    """浮亏 runner 照样死拿（策略B 只做"达标锁利"，不做止损——
    止损是 SL watcher 的地盘，边界不混）。"""
    decision, reason = strategy_b_decision(2.0, 1.0, -50.0, 25)
    assert decision == "PRESERVE"
    assert "-50.0%" in reason


# ============================================================
# 2/3. close_flow 端到端
# ============================================================

def _open_runner(symbol: str = "AVGO", qty: int = 1, entry: float = 1.65) -> str:
    code = f"US.{symbol}250731C415000"
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=415.0, side="CALL",
        expiry=date(2026, 7, 31), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_0011",
    )
    dedup._close_fps.clear()
    return code


@pytest.mark.asyncio
@pytest.mark.parametrize("env_val", [None, "false", "0"])
async def test_default_off_byte_equivalent_to_strategy_a(monkeypatch, env_val):
    """契约硬要求：默认关 = 行为与今天**逐字**一致。
    单张仓 + trim → 不取报价、不卖、TG 文案与 0011 之前完全相同
    （用 format_close_skipped 现算期望值做全等断言，不是子串匹配）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    if env_val is None:
        monkeypatch.delenv("STRATEGY_B", raising=False)
    else:
        monkeypatch.setenv("STRATEGY_B", env_val)
    code = _open_runner()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    quote_mock = MagicMock(return_value=2.20)
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order") as sell_mock, \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_NOPRICE, msg_id=110101)

    quote_mock.assert_not_called()  # 关 = 连报价都不碰（不吃 snapshot 配额）
    sell_mock.assert_not_called()
    expected = format_close_skipped(
        "runner-preserve：AVGO 415.0C 各剩 1 张，"
        "跳过 33% trim（策略 A，等 100% 全平信号；窗口期内不重复提醒）",
        AVGO_TRIM_NOPRICE,
    )
    assert notifications == [expected], (
        "默认关时 TG 必须与 0011 之前逐字一致", notifications,
    )
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN" and pos["qty_remaining"] == 1


@pytest.mark.asyncio
async def test_on_meets_threshold_sells_all(monkeypatch):
    """开 + 达标：entry 1.65 / quote 2.20 → +33.3% >= 25 → 全出。
    限价 = 2.20 × (1-SELL_SLIP)，报价只取一次（决策与挂单复用同一参照，
    不重复吃 snapshot 配额）；remark/TG 按 100% 口径如实呈报。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STRATEGY_B", "true")
    monkeypatch.delenv("STRATEGY_B_MIN_PNL_PCT", raising=False)  # 验证缺省 25
    monkeypatch.delenv("CLOSE_QUOTE_FALLBACK", raising=False)
    code = _open_runner(entry=1.65)

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append(kwargs)
        return {"success": True, "order_id": "SB1", "code": code,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    quote_mock = MagicMock(return_value=2.20)
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_NOPRICE, msg_id=110102)

    quote_mock.assert_called_once_with(code)  # 决策+限价共用一次取价
    assert len(sell_called) == 1, "达标应全出"
    assert sell_called[0]["qty"] == 1  # 全部剩余（runner 仓 remaining=1）
    assert sell_called[0]["limit_price"] == round(2.20 * 0.95, 2)
    assert sell_called[0]["remark"] == "kc_close_100pct"  # pct 语义=100 只对该仓位
    text = "\n".join(notifications)
    assert "平仓成交" in text and "100%" in text
    assert "runner" not in text.lower()
    pos = positions_db.get(code)
    assert pos["qty_remaining"] == 0


@pytest.mark.asyncio
async def test_on_below_threshold_preserves_with_reason(monkeypatch):
    """开 + 未达标：entry 1.65 / quote 1.80 → +9.1% < 25 → 保留，
    TG 走 runner-preserve 合并通道且文案附 reason（浮盈数字 + KC 自报值）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STRATEGY_B", "true")
    code = _open_runner(entry=1.65)

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    quote_mock = MagicMock(return_value=1.80)
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order") as sell_mock, \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_NOPRICE, msg_id=110103)

    quote_mock.assert_called_once_with(code)
    sell_mock.assert_not_called()
    text = "\n".join(notifications)
    # TG 走 MarkdownV2 escape（- . + 均带反斜杠），断言按转义后形态
    assert "runner\\-preserve" in text
    assert "策略 B" in text
    assert "浮盈" in text and "阈值" in text, ("文案必须附 reason", text)
    assert "9\\.1%" in text   # 我方口径的实时浮盈
    assert "自报 \\+20%" in text  # KC 自报 +20% 进文案供参考
    assert "no matching" not in text.lower()
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN" and pos["qty_remaining"] == 1


@pytest.mark.asyncio
async def test_on_no_quote_preserves(monkeypatch):
    """开 + 报价拿不到（无 bid/last 或 stale）→ 保留：拿不到可靠参照就
    退回策略A死拿，绝不盲卖（与 0010 拒卖同一底线哲学）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STRATEGY_B", "true")
    code = _open_runner()

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    quote_mock = MagicMock(return_value=None)
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order") as sell_mock, \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_NOPRICE, msg_id=110104)

    quote_mock.assert_called_once_with(code)
    sell_mock.assert_not_called()
    text = "\n".join(notifications)
    assert "runner\\-preserve" in text  # MarkdownV2 escape 后形态
    assert "无新鲜报价" in text, ("无报价保留必须在文案里说明原因", text)
    pos = positions_db.get(code)
    assert pos["status"] == "OPEN" and pos["qty_remaining"] == 1


@pytest.mark.asyncio
async def test_on_sell_all_with_signal_price_keeps_signal_priority(monkeypatch):
    """带价 trim（7/24 形态 @2.20）触发 SELL_ALL 时，限价仍走 signal 优先级
    （calc_sell_limit 的优先级排序不因策略B改变）；报价只为决策取一次。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STRATEGY_B", "true")
    code = _open_runner(entry=1.65)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append(kwargs)
        return {"success": True, "order_id": "SB2", "code": code,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    async def noop(msg):
        pass

    quote_mock = MagicMock(return_value=2.50)  # +51.5% → SELL_ALL
    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_WITH_PRICE, msg_id=110105)

    quote_mock.assert_called_once_with(code)  # 只有决策那一次
    assert len(sell_called) == 1
    assert sell_called[0]["limit_price"] == round(2.20 * 0.95, 2)  # ref=signal


@pytest.mark.asyncio
async def test_min_pnl_env_respected(monkeypatch):
    """STRATEGY_B_MIN_PNL_PCT=50 时 +33% 不达标 → 保留（阈值 per-call 重读）。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STRATEGY_B", "true")
    monkeypatch.setenv("STRATEGY_B_MIN_PNL_PCT", "50")
    code = _open_runner(entry=1.65)

    async def noop(msg):
        pass

    quote_mock = MagicMock(return_value=2.20)  # +33.3% < 50
    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order") as sell_mock, \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_NOPRICE, msg_id=110106)

    sell_mock.assert_not_called()
    pos = positions_db.get(code)
    assert pos["qty_remaining"] == 1


@pytest.mark.asyncio
async def test_preserve_tg_throttle_key_excludes_reason(monkeypatch):
    """7/23 节流回归：策略B 的 reason 含实时浮盈数字（每次报价都不同），
    节流 key 必须仍是裸 pos_label——否则连环 trim 一夜退回 6 连发。
    两次不同报价的未达标 trim，窗口期内只发一条 runner-preserve TG。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STRATEGY_B", "true")
    code = _open_runner(entry=1.65)

    notifications = []

    async def capture(msg):
        notifications.append(msg)

    quote_values = iter([1.80, 1.85])  # 两次浮盈不同 → reason 不同
    quote_mock = MagicMock(side_effect=lambda _c: next(quote_values))
    with patch.object(close_flow, "_safe_notify", side_effect=capture), \
         patch.object(close_flow, "place_sell_order") as sell_mock, \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_NOPRICE, msg_id=110107)
        dedup._close_fps.clear()  # 只想测 TG 节流，绕开 CLOSE 指纹 60s 窗口
        await close_flow.handle_close_signal(AVGO_TRIM_NOPRICE, msg_id=110108)

    sell_mock.assert_not_called()
    preserve_msgs = [n for n in notifications if "runner\\-preserve" in n]
    assert len(preserve_msgs) == 1, (
        "窗口期内同仓位只发一条 runner-preserve TG（key 不带 reason）",
        notifications,
    )
    pos = positions_db.get(code)
    assert pos["qty_remaining"] == 1


@pytest.mark.asyncio
async def test_multi_qty_position_untouched_by_strategy_b(monkeypatch):
    """开着策略B 但 remaining>1 → 走正常 ceil trim，策略B 完全不介入
    （不取报价）：策略B 只在 runner-preserve 拦下的那一个路口存在。"""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STRATEGY_B", "true")
    code = _open_runner(qty=3, entry=1.65)

    sell_called = []

    def fake_sell(*args, **kwargs):
        sell_called.append(kwargs)
        return {"success": True, "order_id": "SB3", "code": code,
                "qty": kwargs["qty"], "price": kwargs["limit_price"]}

    async def noop(msg):
        pass

    quote_mock = MagicMock(return_value=9.99)
    with patch.object(close_flow, "_safe_notify", side_effect=noop), \
         patch.object(close_flow, "place_sell_order", side_effect=fake_sell), \
         patch.object(close_flow, "get_sell_ref_price", quote_mock):
        await close_flow.handle_close_signal(AVGO_TRIM_WITH_PRICE, msg_id=110109)

    quote_mock.assert_not_called()
    assert len(sell_called) == 1
    assert sell_called[0]["qty"] == 1  # ceil(3 × 33%) = 1
    assert sell_called[0]["remark"] == "kc_close_33pct"  # 正常路径 pct 口径不变
    pos = positions_db.get(code)
    assert pos["qty_remaining"] == 2
