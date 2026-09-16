"""[9/16] swing 观测模式：取价、算阈值、**绝不下单**。

为什么要有它：`swing` 类目 `apply_sl=False`，压根不进 SL watcher，**连报价都
没取过**。于是「`WEEKLY_MAX_DTE=10` 该不该是 10」「swing 要不要 max_loss 硬底」
这两个问题从 8/22（-$976 两笔到期归零）问到现在都答不了 —— 不是没人想改，
是没有数据可回测。

9/15 夜的量化：10 个仓位 8 个裸奔，$4,810 / $5,272 = **91% 的在途资金无保护**，
而有保护那两个之所以有，只因为喊单员碰巧写了止损价（lesson #50）。

**为什么不直接加止损**：喊单员的 weekly 常常先跌后拉，止损可能是割在地板上，
也可能像 8/22 AMD 那样救命 —— 现在这两种分不清。直接加会永久失去对照。
所以先只观测，两三周后两条曲线都有了再用数据定。
"""
import os
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autotrade.position import sl_watcher
from autotrade.storage import positions_db


def _uniq(p):
    return f"US.{p}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open(code, category, apply_sl, entry=5.30, qty=1, manual_stop=None):
    positions_db.open_or_add(
        option_code=code, symbol=code[3:7], strike=380.0, side="CALL",
        expiry=date(2026, 9, 18), qty=qty, fill_price=entry,
        category=category, apply_sl=apply_sl, eod_force_close=False,
        tags=[], channel_name="ut", msg_id="m",
    )
    if manual_stop:
        positions_db.set_manual_stop(code, manual_stop)
    sl_watcher._triggered.discard(code)
    sl_watcher._observed_alerted.pop(code, None)
    return code


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SL_OBSERVE_SWING", "1")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    monkeypatch.setenv("LOTTO_STOP_LOSS_PCT", "0")   # lotto 硬底保持关
    yield


async def _tick(quotes: dict):
    sold, tg = [], AsyncMock(return_value=True)

    # place_sell_order 是**位置参数**调用；只收 **kw 的 lambda 会抛 TypeError
    # 而且被上层吞掉 —— 表现成"没卖"，而日志里明明写着 🛑 TRIGGER（第一版就这么
    # 假红过）。签名与 test_overnight_0909 / test_overnight_0910 里的 fake_sell 一致。
    def fake_sell(option_code, qty, limit_price, **kw):
        sold.append((option_code, qty, limit_price))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": "UT", "code": option_code}

    with patch.object(sl_watcher, "get_last_prices",
                      side_effect=lambda codes: {c: quotes.get(c) for c in codes}) as q, \
         patch.object(sl_watcher, "place_sell_order", side_effect=fake_sell), \
         patch.object(sl_watcher, "send_telegram", tg):
        await sl_watcher._sl_tick()
    return sold, tg, q


@pytest.mark.asyncio
async def test_swing_is_priced_but_never_sold():
    """核心契约：跌破假想阈值也**不下单**，只发一条 TG。"""
    code = _open(_uniq("OBS"), "swing", apply_sl=False, entry=5.30)
    sold, tg, q = await _tick({code: 1.00})          # 阈值 2.65，远远跌破
    assert code in q.call_args.args[0], "必须取到价 —— 没有价格序列就没有数据"
    assert not sold, "观测模式**绝不下单**"
    assert positions_db.get(code)["status"] == "OPEN"
    blob = " ".join(str(c) for c in tg.await_args_list)
    assert "未下单" in blob and "本会触发止损" in blob
    positions_db.record_close(code, 1, 1.0, "manual", note="ut")


@pytest.mark.asyncio
async def test_alert_is_once_per_day_not_every_tick():
    """5s 一个 tick，不节流就是一夜几千条（lesson #33）。"""
    code = _open(_uniq("ONCE"), "swing", apply_sl=False, entry=5.30)
    _, tg1, _ = await _tick({code: 1.00})
    _, tg2, _ = await _tick({code: 0.90})
    assert len([c for c in tg1.await_args_list if "本会触发止损" in str(c)]) == 1
    assert [c for c in tg2.await_args_list if "本会触发止损" in str(c)] == []
    positions_db.record_close(code, 1, 1.0, "manual", note="ut")


@pytest.mark.asyncio
async def test_above_threshold_is_recorded_but_silent():
    """没跌破就只记日志不发 TG —— 价格序列照样要采。"""
    code = _open(_uniq("HIGH"), "swing", apply_sl=False, entry=5.30)
    sold, tg, q = await _tick({code: 5.00})          # 高于阈值 2.65
    assert code in q.call_args.args[0]
    assert not sold
    assert [c for c in tg.await_args_list if "本会触发止损" in str(c)] == []
    positions_db.record_close(code, 1, 5.0, "manual", note="ut")


@pytest.mark.asyncio
async def test_real_stop_positions_still_actually_sell():
    """**反向不变量**：观测模式不许把真该止损的仓位也变成只看不动。"""
    code = _open(_uniq("REAL"), "weekly", apply_sl=True, entry=5.30)
    sold, _, _ = await _tick({code: 1.00})
    assert sold, "apply_sl=True 的仓位必须照常真卖"
    positions_db.record_close(code, 1, 1.0, "manual", note="ut")


@pytest.mark.asyncio
async def test_declared_stop_swing_still_sells_not_observed():
    """反向不变量：有声明止损的 swing 走的是**真**看护分支（#43），
    不许被观测分支截胡 —— 它已经是"喊单员明说了"的那一类。"""
    code = _open(_uniq("DECL"), "swing", apply_sl=False, entry=5.30,
                 manual_stop=4.00)
    sold, _, _ = await _tick({code: 3.00})
    assert sold, "声明止损的 swing 必须真卖（PR#9 的契约）"
    positions_db.record_close(code, 1, 3.0, "manual", note="ut")


@pytest.mark.asyncio
async def test_switch_off_reproduces_the_old_silence(monkeypatch):
    """开关关掉 = 逐字旧行为：连报价都不取（0013 的配额契约）。"""
    monkeypatch.setenv("SL_OBSERVE_SWING", "0")
    code = _open(_uniq("OFF"), "swing", apply_sl=False, entry=5.30)
    sold, tg, q = await _tick({code: 0.01})
    assert not sold
    assert not q.called, "关掉时不该为它打 snapshot（不占配额）"
    assert tg.await_args_list == []
    positions_db.record_close(code, 1, 0.01, "manual", note="ut")
