"""0013: lotto -80% 硬底测试

背景：AVGO 415C（7/23-24 夜）单张 lotto 从 +50% 一路拿到过期归零——
runner-preserve 挡掉 trim、KC 没发 100% close、lotto 又不吃全局 SL，
最终整仓归零。policy/positions.categorize 的 TODO（max_loss_pct -80% 硬底）
在 sl_watcher 侧兑现：category in ("lotto", "0dte_lotto") 且
LOTTO_STOP_LOSS_PCT>0 的仓位走与全局 SL 完全同一条代码，只是 pct 分档。

契约测试矩阵（WP-D）：
- lotto -85% 触发
- lotto -70% 不触发
- env=0 完全不选（连报价都不取）
- weekly 不受影响（照旧吃全局 STOP_LOSS_PCT）
另加：0dte_lotto 同样覆盖、冻结语义（落库失败不重复卖）与全局 SL 共享、
ship-dark 缺省（env 不设 = 关，护住 test_watchers 的既有断言）。
"""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autotrade.position import sl_watcher
from autotrade.storage import positions_db


def _uniq_code(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open_lotto(symbol: str, code: str, category: str = "lotto",
                qty: int = 1, entry: float = 1.00):
    """开一个 lotto 仓位（apply_sl=False——categorize 对 lotto 从不挂全局 SL）。"""
    assert category in ("lotto", "0dte_lotto")
    return positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=qty, fill_price=entry,
        category=category, apply_sl=False,
        eod_force_close=(category == "0dte_lotto"), tags=["lotto"],
        channel_name="ut", msg_id="m1",
    )


def _open_weekly(symbol: str, code: str, qty: int = 2, entry: float = 1.00):
    """开一个 weekly 仓位（apply_sl=True，吃全局 STOP_LOSS_PCT）。"""
    return positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=10.0, side="CALL",
        expiry=date(2026, 6, 25), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )


def _quotes_map(mapping: dict):
    """批量 get_last_prices mock：按 mapping 返回，未列出的 code 返回 None。

    （SL watcher 7/8 起整个 tick 只发一次批量取价——mock 也走批量接口，
    顺便让测试能断言"哪些 code 进了这一次批量请求"。）
    """
    def _f(codes):
        return {c: mapping.get(c) for c in codes}
    return _f


def _sell_ok(code: str, qty: int, price: float) -> dict:
    return {"success": True, "qty": qty, "price": price,
            "order_id": "LOTTO_ORD", "code": code}


@pytest.mark.asyncio
async def test_lotto_floor_triggers_at_minus_85(monkeypatch):
    """entry 1.00, last 0.15 → -85% ≤ -80% 硬底 → 残值回收全平。"""
    code = _uniq_code("LF1")
    _open_lotto("LFT1", code, qty=2, entry=1.00)
    sl_watcher._triggered.discard(code)

    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "80")
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_map({code: 0.15})), \
         patch("autotrade.position.sl_watcher.place_sell_order",
               return_value=_sell_ok(code, 2, 0.14)), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    pos = positions_db.get(code)
    assert pos["status"] == "CLOSED"
    assert pos["qty_remaining"] == 0
    # 卖出路径与全局 SL 同一条：trigger_source 就是 sl_polling
    sl_evt = [e for e in positions_db.get_events(code)
              if e["trigger_source"] == "sl_polling"]
    assert len(sl_evt) == 1
    assert sl_evt[0]["qty_delta"] == -2
    assert code not in sl_watcher._triggered  # 成功落库后释放（与全局 SL 同语义）


@pytest.mark.asyncio
async def test_lotto_floor_holds_at_minus_70(monkeypatch):
    """entry 1.00, last 0.30 → -70%，未到 -80% 硬底 → 放飞照旧，不卖。"""
    code = _uniq_code("LF2")
    _open_lotto("LFT2", code, qty=1, entry=1.00)
    sl_watcher._triggered.discard(code)

    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "80")
    sell_mock = MagicMock()
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_map({code: 0.30})), \
         patch("autotrade.position.sl_watcher.place_sell_order", sell_mock), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    sell_mock.assert_not_called()
    assert positions_db.get(code)["status"] == "OPEN"


@pytest.mark.asyncio
async def test_0dte_lotto_also_covered(monkeypatch):
    """0dte_lotto 同样吃硬底（EOD 强平之前的盘中残值回收）。"""
    code = _uniq_code("LF3")
    _open_lotto("LFT3", code, category="0dte_lotto", qty=1, entry=1.00)
    sl_watcher._triggered.discard(code)

    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "80")
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_map({code: 0.10})), \
         patch("autotrade.position.sl_watcher.place_sell_order",
               return_value=_sell_ok(code, 1, 0.09)), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    assert positions_db.get(code)["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_env_zero_lotto_not_selected_at_all(monkeypatch):
    """env=0 → lotto 仓位完全不进选仓：连批量报价都不发（不占 snapshot 配额）。"""
    code = _uniq_code("LF4")
    _open_lotto("LFT4", code, qty=1, entry=1.00)
    sl_watcher._triggered.discard(code)

    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "0")
    quotes_mock = MagicMock(side_effect=_quotes_map({code: 0.05}))  # -95% 也不理
    sell_mock = MagicMock()
    with patch("autotrade.position.sl_watcher.get_last_prices", quotes_mock), \
         patch("autotrade.position.sl_watcher.place_sell_order", sell_mock), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    quotes_mock.assert_not_called()  # watch 列表为空 → 整个 tick 早退
    sell_mock.assert_not_called()
    assert positions_db.get(code)["status"] == "OPEN"


@pytest.mark.asyncio
async def test_env_zero_weekly_still_protected(monkeypatch):
    """env=0 只关 lotto 档：同一 tick 里 weekly 照旧吃全局 SL，
    且批量报价请求里只有 weekly 的 code（lotto 完全不选）。"""
    lotto_code = _uniq_code("LF5A")
    weekly_code = _uniq_code("LF5B")
    _open_lotto("LFT5A", lotto_code, qty=1, entry=1.00)
    _open_weekly("LFT5B", weekly_code, qty=2, entry=1.00)
    sl_watcher._triggered.discard(lotto_code)
    sl_watcher._triggered.discard(weekly_code)

    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "0")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    quotes_mock = MagicMock(
        side_effect=_quotes_map({lotto_code: 0.05, weekly_code: 0.40}))
    with patch("autotrade.position.sl_watcher.get_last_prices", quotes_mock), \
         patch("autotrade.position.sl_watcher.place_sell_order",
               return_value=_sell_ok(weekly_code, 2, 0.37)), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    (requested_codes,), _ = quotes_mock.call_args
    assert weekly_code in requested_codes
    assert lotto_code not in requested_codes
    assert positions_db.get(weekly_code)["status"] == "CLOSED"  # -60% > 50% → 触发
    assert positions_db.get(lotto_code)["status"] == "OPEN"     # -95% 也放飞


@pytest.mark.asyncio
async def test_weekly_and_lotto_thresholds_are_tiered(monkeypatch):
    """分档验证：同样 -60%，weekly（全局 50%）触发、lotto（硬底 80%）不触发。
    weekly 行为与 0013 之前逐字一致——硬底档不改全局档任何语义。"""
    lotto_code = _uniq_code("LF6A")
    weekly_code = _uniq_code("LF6B")
    _open_lotto("LFT6A", lotto_code, qty=1, entry=1.00)
    _open_weekly("LFT6B", weekly_code, qty=2, entry=1.00)
    sl_watcher._triggered.discard(lotto_code)
    sl_watcher._triggered.discard(weekly_code)

    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "80")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_map({lotto_code: 0.40, weekly_code: 0.40})), \
         patch("autotrade.position.sl_watcher.place_sell_order",
               return_value=_sell_ok(weekly_code, 2, 0.37)), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    assert positions_db.get(weekly_code)["status"] == "CLOSED"
    assert positions_db.get(lotto_code)["status"] == "OPEN"


@pytest.mark.asyncio
async def test_env_unset_defaults_dark(monkeypatch):
    """变量不设 = 关（[ship-dark] 契约偏差，见 sl_watcher._cfg 注释）：
    保持 test_watchers.py::test_sl_skips_apply_sl_false 的既有断言
    （lotto -95% 也不触发）在裸环境下依然成立；生产 .env 从模板拿到 80。"""
    code = _uniq_code("LF7")
    _open_lotto("LFT7", code, qty=1, entry=1.00)
    sl_watcher._triggered.discard(code)

    monkeypatch.delenv("LOTTO_STOP_LOSS_PCT", raising=False)
    quotes_mock = MagicMock(side_effect=_quotes_map({code: 0.05}))
    sell_mock = MagicMock()
    with patch("autotrade.position.sl_watcher.get_last_prices", quotes_mock), \
         patch("autotrade.position.sl_watcher.place_sell_order", sell_mock), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    quotes_mock.assert_not_called()
    sell_mock.assert_not_called()
    assert positions_db.get(code)["status"] == "OPEN"


@pytest.mark.asyncio
async def test_lotto_floor_shares_freeze_semantics(monkeypatch):
    """冻结语义与全局 SL 完全共享：lotto 硬底卖出成功但落库失败 →
    code 留在 _triggered，下一轮绝不重复挂卖单（同一条 _trigger_sl 代码）。"""
    code = _uniq_code("LF8")
    _open_lotto("LFT8", code, qty=1, entry=1.00)
    sl_watcher._triggered.discard(code)

    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "80")
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_map({code: 0.10})), \
         patch("autotrade.position.sl_watcher.place_sell_order",
               return_value=_sell_ok(code, 1, 0.09)) as sell_mock, \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock), \
         patch.object(sl_watcher.position_mgr, "on_close_filled",
                      side_effect=RuntimeError("db down")):
        await sl_watcher._sl_tick()
        assert code in sl_watcher._triggered
        await sl_watcher._sl_tick()
        assert sell_mock.call_count == 1

    # 清理进程级 set，避免污染其他测试
    sl_watcher._triggered.discard(code)
