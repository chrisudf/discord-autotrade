"""9/11 复盘（US RTH 2026-09-10）。ashley 上线后的第一个完整交易夜。

7 单全部成交（ashley 6 + enrich 1，$3,636），9/10 那批 parser 修复在生产里
兑现。缺陷全在**交易之外**：

1. **reconciler 用一次读数平掉了一个活仓。** 01:47:39 报
   `drift db_only: US.AMZN260911C250000 db=2 broker=0` 并当场自动落账
   `CLOSE @ 0.00`；03:47 / 04:47 / 05:47 / 06:47 连续四轮又报同一个 code
   `broker_only` —— 仓位一直都在。那 2 张**当天到期**，落账之后掉出
   SL/TP/EOD 选仓，变成没有任何东西看管的幽灵仓；同时 fill_price=0 把一笔
   -100% 的假账写进了 position_events。三道既有闸门防的都是"整个查询坏掉"，
   这次是"一次良好响应里少了一行"。

2. **启动探测吃掉了开盘前那段不可压缩的时间。** OPRA 探测挂了 16m45s
   （23:11:13 → 23:27:59，以 OpenD KeepAliveFail 收场），期间 watcher 没起、
   Discord 没登录。源码注释写着"不阻塞启动，只 log"，但没有任何东西在兑现它。
   （当晚紧接着又睡了 19m48s，两段叠起来横跨 09:30 ET 开盘，首单落在开盘后
   18 分钟。睡眠那半是宿主行为，不在本文件射程内。）

3. **"will retry" 是句空话。** 00:30 `LITE OUT 40%` 撞上 00:28:38 那张还没
   确认的买单，deferred 分支写着"会重试"—— 而重试只对自带循环的 TP/SL
   watcher 成立，喊单员的平仓是一次性消息，那 40% 的离场就此丢失。
"""
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.broker.errors import is_deterministic_reject
from autotrade.position import reconciler
from autotrade.storage import positions_db

AMZN = "US.AMZN260911C250000"


@pytest.fixture(autouse=True)
def _reset():
    reconciler._last_signature = None
    reconciler._db_only_seen = set()
    yield
    reconciler._last_signature = None
    reconciler._db_only_seen = set()


def _uniq(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open(symbol: str, code: str, qty: int = 2):
    return positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=250.0, side="CALL",
        expiry=date(2026, 9, 11), qty=qty, fill_price=3.15,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ashley", msg_id="m1",
    )


# ============================================================
# 1. 闸门 4：一次读数不足以宣告一个仓位消失
# ============================================================

def test_split_by_confirmation_matrix():
    """纯函数：只有上一轮见过的才算确认。"""
    assert reconciler._split_by_confirmation({"A", "B"}, {"B"}) == ({"B"}, {"A"})
    assert reconciler._split_by_confirmation({"A"}, set()) == (set(), {"A"})
    assert reconciler._split_by_confirmation(set(), {"A"}) == (set(), set())


async def test_last_nights_amzn_is_not_closed_by_one_reading(monkeypatch):
    """契约翻转：当晚这个序列会在第一轮就落账，现在必须一张都不动。

    01:47 broker 说没有（但同一轮里别的仓位都在，所以闸门 2/3 都放行），
    03:47 broker 又说有。逐字重放这两轮。
    """
    monkeypatch.setenv("DRY_RUN", "false")
    amzn, alive = _uniq("AMZN"), _uniq("MSTR")
    _open("AMZN", amzn, qty=2)
    _open("MSTR", alive, qty=1)

    tg = AsyncMock(return_value=True)
    with patch.object(reconciler, "send_telegram", tg):
        # 01:47:39 —— 少了 AMZN 这一行，别的仓位都在
        with patch.object(reconciler, "list_open_option_positions",
                          return_value={alive: 1}):
            first = await reconciler._reconcile_tick()
        assert [d["kind"] for d in first] == [reconciler.KIND_DB_ONLY]
        assert positions_db.get(amzn)["status"] == "OPEN", "一次读数不许落账"

        # 03:47:42 —— 同一个 code 又回来了
        with patch.object(reconciler, "list_open_option_positions",
                          return_value={alive: 1, amzn: 2}):
            second = await reconciler._reconcile_tick()

    assert second == [], "两边一致 → 无漂移"
    pos = positions_db.get(amzn)
    assert pos["status"] == "OPEN" and pos["qty_remaining"] == 2
    assert all(e["trigger_source"] != "broker_sync"
               for e in positions_db.get_events(amzn)), "不许留下 broker_sync 假账"


async def test_two_consecutive_db_only_still_closes(monkeypatch):
    """反向：真的消失了（连续两轮都说没有）仍然照常落账。

    缺了这条，闸门 4 就等于把 0018 整个关掉 —— 8/13 夜 1918 次拒单的止血
    靠的就是自动落账。
    """
    monkeypatch.setenv("DRY_RUN", "false")
    gone, alive = _uniq("GONE"), _uniq("LIVE")
    _open("GONE", gone, qty=2)
    _open("LIVE", alive, qty=1)

    tg = AsyncMock(return_value=True)
    with patch.object(reconciler, "list_open_option_positions",
                      return_value={alive: 1}), \
         patch.object(reconciler, "send_telegram", tg):
        await reconciler._reconcile_tick()
        assert positions_db.get(gone)["status"] == "OPEN"
        await reconciler._reconcile_tick()

    assert positions_db.get(gone)["status"] == "CLOSED"
    assert positions_db.get(alive)["status"] == "OPEN"


async def test_a_vetoed_round_is_not_evidence(monkeypatch):
    """被前三道闸门否决的那一轮，读数本身就不可信 —— 不许当作"第一次观测"。

    否则一次坏读数（broker 返回空）加一次好读数就凑满两轮，闸门 4 形同虚设。
    """
    monkeypatch.setenv("DRY_RUN", "false")
    code, alive = _uniq("VETO"), _uniq("VLIV")
    _open("VETO", code, qty=2)
    _open("VLIV", alive, qty=1)

    tg = AsyncMock(return_value=True)
    with patch.object(reconciler, "send_telegram", tg):
        # 第一轮：broker 返回空 → 闸门 2 否决
        with patch.object(reconciler, "list_open_option_positions", return_value={}):
            await reconciler._reconcile_tick()
        assert reconciler._db_only_seen == set(), "被否决的轮次不留证据"

        # 第二轮：正常响应，但这才是第一次可信观测 → 仍然不落账
        with patch.object(reconciler, "list_open_option_positions",
                          return_value={alive: 1}):
            await reconciler._reconcile_tick()
    assert positions_db.get(code)["status"] == "OPEN"


async def test_auto_close_records_no_fill_price(monkeypatch):
    """落账事件不许带成交价：仓位是"消失"不是"卖了"。

    写 0 会被下游当成真成交价，一笔 -100% 的假账就此入库（9/10 夜 AMZN
    在 DB 里留下的正是 `CLOSE -2 @ 0.00`）。
    """
    monkeypatch.setenv("DRY_RUN", "false")
    code, alive = _uniq("NOPX"), _uniq("NLIV")
    _open("NOPX", code, qty=2)
    _open("NLIV", alive, qty=1)

    with patch.object(reconciler, "list_open_option_positions",
                      return_value={alive: 1}), \
         patch.object(reconciler, "send_telegram", AsyncMock(return_value=True)):
        await reconciler._reconcile_tick()
        await reconciler._reconcile_tick()

    evt = [e for e in positions_db.get_events(code)
           if e["trigger_source"] == "broker_sync"]
    assert evt and evt[0]["price"] is None, f"期望 NULL，实际 {evt[0]['price']!r}"


# ============================================================
# 2. deferred 的措辞：不许承诺一个不存在的重试
# ============================================================

def _deferred_msg(monkeypatch) -> str:
    # DRY_RUN=true 会在裸空检查**之前**短路返回 "DRY_RUN sell"，
    # 拿不到我们要断言的那段措辞（第一版就这么假绿过）。
    monkeypatch.setenv("DRY_RUN", "false")
    from autotrade.broker import inflight, trade
    with patch.object(inflight, "is_pending", return_value=True), \
         patch.object(trade, "_get_long_qty", return_value=0), \
         patch.object(trade, "_ensure_unlocked", return_value=None):
        r = trade.place_sell_order("US.LITE260911C1030000", 1, 4.84)
    assert not r["success"] and "naked-short deferred" in r["message"], r
    return r["message"]


def test_deferred_says_a_caller_close_will_not_be_retried(monkeypatch):
    """当晚这句话结尾是 "will retry."，而喊单员的平仓是一次性消息。"""
    msg = _deferred_msg(monkeypatch)
    assert "NOT be retried" in msg and "one-shot" in msg
    assert "will retry." not in msg


def test_deferred_is_still_transient_not_deterministic(monkeypatch):
    """不变量：改措辞不许把 deferred 推进确定性组（那样会熔断，
    正是 9/2 TSLA 那次 -$166 要避免的）。"""
    assert is_deterministic_reject(_deferred_msg(monkeypatch)) is False
    assert is_deterministic_reject(
        "naked-short refused: broker has only 0 long") is True


# ============================================================
# 3. 启动探测的超时：不许再吃掉开盘前那 16 分钟
# ============================================================

def test_a_hung_quote_probe_no_longer_blocks_startup():
    """契约翻转：当晚这个调用挂了 16m45s，启动被它整段卡住。

    断的是"多久返回"，不是"返回了什么" —— 前身在这里会一直等下去。
    """
    import time
    from autotrade.app import preflight as pf

    with patch.object(pf, "probe_quote_access",
                      side_effect=lambda: time.sleep(30)):
        t0 = time.monotonic()
        status, msg = pf._probe_quote_with_timeout(1)
        elapsed = time.monotonic() - t0

    assert elapsed < 5, f"超时没生效，等了 {elapsed:.1f}s"
    assert status == pf.QUOTE_ERROR and "继续启动" in msg


def test_a_fast_probe_is_passed_through_untouched():
    """反向不变量：正常返回时结果原样透传，行为与加超时之前逐字一致。"""
    from autotrade.app import preflight as pf

    with patch.object(pf, "probe_quote_access", return_value=("OK", "全都好")):
        assert pf._probe_quote_with_timeout(5) == ("OK", "全都好")
