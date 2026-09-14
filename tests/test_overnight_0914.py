"""9/14：EOD 无报价的升级路径（周五 9/12 复盘 §5③ 的落地）。

原提案是三选一，核完账之后否了一条、换了一条：

- **① 到期日放宽限价（`last` 取不到时按旧价打折卖）—— 否决。** 它只在没有
  报价时触发，也就是恰好无法分辨这张值 $0.01 还是 $19.02 的时候（MU 260812C900000
  当时就卖在 19.02）。赔率是反的：上行几美元，下行几百美元，而且正是 7/25
  那次自残卖事故的形状。
- **② 换 bid 兜底 —— 降级成"先把证据记下来"。** 查 `quote.py` 才发现
  `get_sell_ref_price` 和 `get_last_prices` **共用同一个 `_snapshot`**
  （docstring：「不开第二条 snapshot 路径」），差别只有"先读 bid_price"一个字段。
  而历史上每一次"先无报价、后来拿到价"的成交价都是 **$0.01**（8/22 ASTS、
  8/21 MSFT、9/12 HOOD），一分钱的成交救不回任何东西 —— LITE 那 $460、AMD 那
  $540 是方向做错亏的，不是 EOD 拒卖亏的。要不要真上 bid 兜底，得先有数据说
  "bid 存在而 last 不存在"这种情况到底有没有、值多少，而那个数据**从来没被记过**：
  `[eod] no quote for {code}` 这行日志什么都不带。所以先上 `describe_quote`。
- **③ 降噪 + ④ 分清故障模式 —— 做。** 9/12 周五 80 条告警、9/5 那晚 90 条，
  而 9/5 那 90 条**一条都没送出去**（101 次 ConnectError）。更要命的是两种
  完全相反的故障发的是同一句"请手动平仓"：
    9/12  4 次连接错误 / 4 次成功强平  → 通路好的，是这张合约没市场（翻身睡）
    9/5 107 次连接错误 / 0 次成功强平  → 通路挂了（爬起来看 OpenD）
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from autotrade.position import eod_watcher
from autotrade.storage import positions_db

ET_TZ = ZoneInfo("America/New_York")


def _trading_now_et(hour=15, minute=51):
    now = datetime.now(ET_TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
    while now.weekday() >= 5:
        now -= timedelta(days=1)
    return now


def _open(code, expiry, entry=1.00, qty=2):
    positions_db.open_or_add(
        option_code=code, symbol=code[3:7], strike=100.0, side="CALL",
        expiry=expiry, qty=qty, fill_price=entry, category="0dte",
        apply_sl=False, eod_force_close=True, tags=[],
        channel_name="ut", msg_id="ut",
    )
    eod_watcher._skip_until.pop(code, None)
    eod_watcher._alerted_until.pop(code, None)
    return code


@pytest.fixture(autouse=True)
def _reset():
    eod_watcher._expiry_alert_stage.clear()
    eod_watcher._tick_noquote.clear()
    eod_watcher._tick_priced = 0
    yield
    eod_watcher._expiry_alert_stage.clear()


def _u(p):
    return f"US.{p}{datetime.now().strftime('%H%M%S%f')}C001000"


async def _run(now_et, quotes: dict, probe=None):
    """跑一轮 tick。quotes: {code: last|None}；probe: describe_quote 的返回。"""
    probe = probe or {"transport_ok": True, "row": True, "bid": None,
                      "last": None, "age_sec": 1.0, "detail": "bid=None last=None"}
    tg = AsyncMock(return_value=True)
    with patch.object(eod_watcher, "get_last_price", side_effect=lambda c: quotes.get(c)), \
         patch.object(eod_watcher, "describe_quote", return_value=probe), \
         patch.object(eod_watcher, "place_sell_order",
                      return_value={"success": True, "order_id": "U", "qty": 1, "price": 0.5}), \
         patch.object(eod_watcher, "send_telegram", tg), \
         patch.object(eod_watcher, "_is_eod_window", return_value=True), \
         patch.object(eod_watcher, "sweep_expired_and_notify", new_callable=AsyncMock):
        await eod_watcher._eod_tick(now_et)
    return [str(c) for c in tg.await_args_list if "无报价" in str(c)]


# ============================================================
# ③ 降噪：一轮一条，不是一个仓位一条
# ============================================================

@pytest.mark.asyncio
async def test_many_no_quote_positions_produce_one_alert():
    """周五那晚 80 条的直接来源：每个仓位每个 tick 各发各的。"""
    now = _trading_now_et()
    codes = [_open(_u(f"NQ{i}"), now.date()) for i in range(3)]
    calls = await _run(now, {c: None for c in codes})
    assert len(calls) == 1, f"3 个无报价仓位只该发 1 条，实际 {len(calls)}"
    # TG 走 Markdown 转义（US.X → US\.X），按去转义后比
    flat = calls[0].replace("\\", "")
    for c in codes:
        assert c in flat, "汇总条目里每个 code 都要点名"
        positions_db.record_close(c, 2, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_all_positions_unpriced_says_unknown_not_healthy():
    """本轮零个成功样本时，不许拿"另有 0 个取到价"当"通路是好的"的证据。

    这是写用例时抓出来的真 bug：第一版文案无条件说"本轮另有 N 个仓位正常
    取到价，说明通路是好的"，N=0 时它在自证一个自己没有证据的结论。
    """
    now = _trading_now_et()
    code = _open(_u("UNK"), now.date())
    calls = await _run(now, {code: None})
    assert "通路是好的" not in calls[0]
    assert "待确认" in calls[0]
    positions_db.record_close(code, 2, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_second_tick_is_silent_until_the_final_call():
    """首条之后不再每 tick 刷屏。"""
    now = _trading_now_et()
    code = _open(_u("SIL"), now.date())
    q = {code: None}
    assert len(await _run(now, q)) == 1
    assert await _run(now + timedelta(seconds=30), q) == []
    assert await _run(now + timedelta(seconds=60), q) == []
    positions_db.record_close(code, 2, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_final_call_fires_before_the_bell():
    """收盘前最后一次必须响 —— 到期日过了这个点就是归零。

    这是 8/22 AMD -$540 那条规则（"到期日不能只喊一声"）在新节流下的落点：
    不再每 tick 喊，但 deadline 前一定还有一条。
    """
    now = _trading_now_et()
    code = _open(_u("FIN"), now.date())
    q = {code: None}
    assert len(await _run(now, q)) == 1                       # 15:51 首条
    assert await _run(now + timedelta(seconds=30), q) == []   # 中间安静
    final = await _run(_trading_now_et(15, 58), q)            # 收盘前
    assert len(final) == 1 and "最后一次" in final[0]
    positions_db.record_close(code, 2, 0.01, "manual", note="ut")


# ============================================================
# ④ 两种故障模式必须说不同的话
# ============================================================

@pytest.mark.asyncio
async def test_other_positions_priced_means_the_contract_is_dead_not_the_feed():
    """9/12 周五的形状：BE 05:50:02 拿到 0.64，LITE 05:50:04 无报价。"""
    now = _trading_now_et()
    dead, alive = _open(_u("DEAD"), now.date()), _open(_u("ALIV"), now.date())
    calls = await _run(now, {dead: None, alive: 0.64})
    assert len(calls) == 1
    assert "通路是好的" in calls[0] and "没有市场" in calls[0]
    assert "OpenD" not in calls[0], "通路没问题时不该让人去查 OpenD"
    positions_db.record_close(dead, 2, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_nothing_priced_plus_transport_failure_means_the_feed_is_down():
    """9/5 的形状：107 次连接错误、全盘 0 次成功强平。"""
    now = _trading_now_et()
    code = _open(_u("FEED"), now.date())
    calls = await _run(now, {code: None},
                       probe={"transport_ok": False, "row": False, "bid": None,
                              "last": None, "age_sec": None,
                              "detail": "snapshot 失败: Disconnect"})
    assert len(calls) == 1
    assert "通路挂了" in calls[0] and "OpenD" in calls[0]
    assert "不需要你做任何事" not in calls[0], "通路挂了的时候不许说没事"
    positions_db.record_close(code, 2, 0.01, "manual", note="ut")


@pytest.mark.asyncio
async def test_transport_failure_alone_is_not_enough_when_others_priced():
    """反向护栏：只要本轮有仓位取到价，就不许判成"通路挂了" ——
    一次探测失败可能只是那一个 code 的瞬时抖动。"""
    now = _trading_now_et()
    dead, alive = _open(_u("MIXD"), now.date()), _open(_u("MIXA"), now.date())
    calls = await _run(now, {dead: None, alive: 1.20},
                       probe={"transport_ok": False, "row": False, "bid": None,
                              "last": None, "age_sec": None, "detail": "抖了一下"})
    assert "通路挂了" not in calls[0]
    positions_db.record_close(dead, 2, 0.01, "manual", note="ut")


# ============================================================
# 不变量：改的只是"说什么"，不是"做什么"
# ============================================================

@pytest.mark.asyncio
async def test_still_refuses_to_sell_without_a_quote():
    """① 被否决的那条 —— 无报价**永远不许**挂 entry-fallback 卖单。"""
    now = _trading_now_et()
    code = _open(_u("NOSELL"), now.date(), entry=5.40)
    sold = []
    with patch.object(eod_watcher, "get_last_price", return_value=None), \
         patch.object(eod_watcher, "describe_quote",
                      return_value={"transport_ok": True, "row": True, "bid": None,
                                    "last": None, "age_sec": 1.0, "detail": "x"}), \
         patch.object(eod_watcher, "place_sell_order",
                      side_effect=lambda **kw: sold.append(kw)), \
         patch.object(eod_watcher, "send_telegram", new_callable=AsyncMock), \
         patch.object(eod_watcher, "_is_eod_window", return_value=True), \
         patch.object(eod_watcher, "sweep_expired_and_notify", new_callable=AsyncMock):
        await eod_watcher._eod_tick(now)
    assert not sold, "entry × 0.9 自残卖是 7/25 事故本体，永远不许回来"
    assert positions_db.get(code)["status"] == "OPEN"
    positions_db.record_close(code, 2, 0.01, "manual", note="ut")


def test_describe_quote_separates_the_two_modes():
    """诊断函数本身：通路问题 vs 合约没市场，必须分得开。"""
    from autotrade.broker import quote as q
    with patch.object(q, "_is_dry_run", return_value=False), \
         patch.object(q, "_snapshot", return_value=(-1, "Disconnect")):
        r = q.describe_quote("US.X260911C1000")
    assert r["transport_ok"] is False and "snapshot 失败" in r["detail"]

    class _Row(dict):
        pass
    import pandas as pd
    df = pd.DataFrame([{"code": "US.X260911C1000", "bid_price": 0.0,
                        "last_price": 0.0, "update_time": "2026-09-11 15:50:00"}])
    with patch.object(q, "_is_dry_run", return_value=False), \
         patch.object(q, "_snapshot", return_value=(q.RET_OK, df)):
        r = q.describe_quote("US.X260911C1000")
    assert r["transport_ok"] is True and "没有市场" in r["detail"]


@pytest.mark.asyncio
async def test_alert_is_not_dressed_up_as_a_system_error():
    """「你不用做任何事」不许挂在 ❌ 系统错误 底下 —— 那是 lesson #33 的
    训练方式：把没有动作的告警和有动作的告警做成同一个样子。

    顺带钉住截断：format_error 截在 500 字符，而这条消息的价值恰好在逐条
    探测结果，几张合约就会把它挤掉。
    """
    now = _trading_now_et()
    codes = [_open(_u(f"FMT{i}"), now.date()) for i in range(3)]
    calls = await _run(now, {c: None for c in codes},
                       probe={"transport_ok": True, "row": True, "bid": 0.0,
                              "last": 0.0, "age_sec": 2.0,
                              "detail": "bid=0.0 last=0.0 age=2.0s " + "细节" * 40})
    assert "系统错误" not in calls[0]
    # 三条探测各带 ~80 字的 detail，500 字符会切掉后两条
    for c in codes:
        assert c in calls[0].replace("\\", ""), "逐条探测不许被截断掉"
        positions_db.record_close(c, 2, 0.01, "manual", note="ut")
