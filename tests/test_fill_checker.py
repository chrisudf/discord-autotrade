"""fill_checker 单元测试

覆盖：
- 买单成交 → dealt_avg 回填 avg_entry_price（含加仓时按腿重算、成交价越界拒绝回填）
- 卖单超时未成交 → TG 告警（这是'提交≠成交'缺口的核心保护）
- 终态失败（撤单）→ 立即告警
"""
import os
from datetime import date, datetime
from unittest.mock import patch, AsyncMock

import pytest

from autotrade.storage import positions_db
from autotrade.position import fill_checker


def _uniq_code(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open(code: str, qty: int = 2, entry: float = 1.10) -> dict:
    return positions_db.open_or_add(
        option_code=code, symbol="FILLT", strike=10.0, side="CALL",
        expiry=date(2026, 7, 10), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )


def _fast_cfg():
    os.environ["FILL_POLL_SEC"] = "0.01"
    os.environ["FILL_TIMEOUT_SEC"] = "0.03"


@pytest.mark.asyncio
async def test_buy_fill_backfills_avg_entry():
    """FILLED_ALL + dealt_avg 与 limit 不同 → avg_entry 回填 + FILL_ADJUST 事件"""
    _fast_cfg()
    code = _uniq_code("FB1")
    _open(code, qty=2, entry=1.10)  # limit 价入账

    with patch.object(fill_checker, "query_order_status",
                      return_value={"success": True, "status": "FILLED_ALL",
                                    "filled_qty": 2, "filled_avg_price": 1.04,
                                    "message": "ok"}), \
         patch.object(fill_checker, "send_telegram", new_callable=AsyncMock) as tg:
        await fill_checker.confirm_buy_fill("ORD1", code, qty=2, limit_price=1.10)

    pos = positions_db.get(code)
    assert pos["avg_entry_price"] == pytest.approx(1.04)
    events = positions_db.get_events(code)
    assert any(e["event_type"] == "FILL_ADJUST" for e in events)
    tg.assert_not_called()  # 正常成交零噪音

    positions_db.record_close(code, 2, 1.04, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_buy_fill_reprices_only_its_own_leg_after_addon():
    """确认期间发生了加仓 → 只修正本笔那条腿，别的腿原样保留。

    [8/18 SPCX] 这条用例原本钉的是"保守跳过回填"——那正是当晚的缺陷：
    加仓路径**必然**走到跳过分支，limit 2.33 被当成成交价进了整仓均价，
    8/19 平仓时账面 -5.5% 而 KC 报 +40%。现在改为按腿重算。
    """
    _fast_cfg()
    code = _uniq_code("FB2")
    _open(code, qty=2, entry=1.10)
    _open(code, qty=1, entry=1.30)  # add-on，qty_total=3

    with patch.object(fill_checker, "query_order_status",
                      return_value={"success": True, "status": "FILLED_ALL",
                                    "filled_qty": 2, "filled_avg_price": 1.04,
                                    "message": "ok"}), \
         patch.object(fill_checker, "send_telegram", new_callable=AsyncMock):
        await fill_checker.confirm_buy_fill("ORD2", code, qty=2, limit_price=1.10)

    pos = positions_db.get(code)
    # 那 2 张从 1.10 改记成 1.04，1 张 @1.30 的腿不动
    assert pos["avg_entry_price"] == pytest.approx((2 * 1.04 + 1 * 1.30) / 3)

    positions_db.record_close(code, 3, 1.2, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_buy_fill_rejects_absurd_dealt_price():
    """[8/20 TSLA] 限价 2.97、broker 回报成交 0.13（-95.6%）→ 拒绝回填 + 告警。"""
    _fast_cfg()
    code = _uniq_code("FB3")
    _open(code, qty=2, entry=2.97)

    tg = AsyncMock()
    with patch.object(fill_checker, "query_order_status",
                      return_value={"success": True, "status": "FILLED_ALL",
                                    "filled_qty": 2, "filled_avg_price": 0.13,
                                    "message": "ok"}), \
         patch.object(fill_checker, "send_telegram", tg):
        await fill_checker.confirm_buy_fill("ORD3", code, qty=2, limit_price=2.97)

    pos = positions_db.get(code)
    assert pos["avg_entry_price"] == pytest.approx(2.97), "成本基准不许被垃圾值打穿"
    assert tg.await_count == 1, "拒绝回填必须告警，不能静默"

    positions_db.record_close(code, 2, 1.0, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_buy_fill_accepts_a_genuinely_good_fill():
    """反向护栏：8/21 UBER 限价 0.50 实际吃到 0.37（-26%）必须照常回填。"""
    _fast_cfg()
    code = _uniq_code("FB4")
    _open(code, qty=2, entry=0.50)

    with patch.object(fill_checker, "query_order_status",
                      return_value={"success": True, "status": "FILLED_ALL",
                                    "filled_qty": 2, "filled_avg_price": 0.37,
                                    "message": "ok"}), \
         patch.object(fill_checker, "send_telegram", new_callable=AsyncMock):
        await fill_checker.confirm_buy_fill("ORD4", code, qty=2, limit_price=0.50)

    assert positions_db.get(code)["avg_entry_price"] == pytest.approx(0.37)

    positions_db.record_close(code, 2, 0.6, "manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_sell_timeout_alerts():
    """卖单一直 SUBMITTED（限价没人接）→ 超时 TG 告警"""
    _fast_cfg()
    code = _uniq_code("FS1")

    with patch.object(fill_checker, "query_order_status",
                      return_value={"success": True, "status": "SUBMITTED",
                                    "filled_qty": 0, "filled_avg_price": 0.0,
                                    "message": "ok"}), \
         patch.object(fill_checker, "send_telegram", new_callable=AsyncMock) as tg:
        await fill_checker.confirm_sell_fill("ORD3", code, qty=2, trigger="sl_polling")

    tg.assert_called_once()
    # format_error 会做 MarkdownV2 转义（US.X → US\.X），先剥掉再断言
    alert = tg.call_args[0][0].replace("\\", "")
    assert code in alert
    assert "sl_polling" in alert


@pytest.mark.asyncio
async def test_buy_dead_order_alerts_immediately():
    """买单被撤（CANCELLED_ALL）→ 不等超时立即告警"""
    _fast_cfg()
    code = _uniq_code("FD1")

    with patch.object(fill_checker, "query_order_status",
                      return_value={"success": True, "status": "CANCELLED_ALL",
                                    "filled_qty": 0, "filled_avg_price": 0.0,
                                    "message": "ok"}), \
         patch.object(fill_checker, "send_telegram", new_callable=AsyncMock) as tg:
        await fill_checker.confirm_buy_fill("ORD4", code, qty=1, limit_price=0.98)

    tg.assert_called_once()
    assert "CANCELLED_ALL" in tg.call_args[0][0].replace("\\", "")


@pytest.mark.asyncio
async def test_empty_order_id_is_noop():
    """order_id 为空（异常路径）→ 直接返回，不轮询不告警"""
    with patch.object(fill_checker, "query_order_status") as q, \
         patch.object(fill_checker, "send_telegram", new_callable=AsyncMock) as tg:
        await fill_checker.confirm_buy_fill("", "US.X", qty=1, limit_price=1.0)
        await fill_checker.confirm_sell_fill("", "US.X", qty=1, trigger="eod")
    q.assert_not_called()
    tg.assert_not_called()
