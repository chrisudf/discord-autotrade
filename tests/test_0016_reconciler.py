"""0016: 定时对账 reconciler（report-only）测试

背景：本地 trades.db 与 broker 静默脱钩的实锤——7/2 OCC 自动行权
（lessons #14/#15）、7/25 夜 AVGO 415C 强平失败过期后本地仍挂 OPEN。
之前只有人工跑 ops/sync_positions 才能发现，夜里零可见性。

契约测试矩阵（WP-G）：
- diff 纯函数矩阵（同步/db_only/broker_only/qty 不一致/边界 qty<=0）
- interval=0 不起 task
- 有差异发 TG / 无差异沉默（mock broker + TG）
另加：report-only 不写 DB、DRY_RUN 跳过（不打 broker）、同一份漂移 TG
不重复轰炸（0002 runner-preserve 节流同哲学）、主循环吞 tick 异常继续跑、
trade.list_open_option_positions 的期权过滤与失败不静默。
"""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from autotrade.broker import trade
from autotrade.position import reconciler
from autotrade.storage import positions_db


@pytest.fixture(autouse=True)
def _reset_reconciler_state():
    # TG 节流签名是模块级状态——不清会让先跑的测试压掉后跑测试的告警
    # （与 conftest 清 runner-preserve 节流同一教训）
    reconciler._last_signature = None
    yield
    reconciler._last_signature = None


def _uniq_code(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _row(code: str, qty: int) -> dict:
    """diff_positions 只消费这两个键（纯函数矩阵不需要真 DB 行）。"""
    return {"option_code": code, "qty_remaining": qty}


def _open_pos(symbol: str, code: str, qty: int = 2, entry: float = 1.00):
    return positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=10.0, side="CALL",
        expiry=date(2026, 8, 21), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m1",
    )


# ============ diff_positions 纯函数矩阵 ============

def test_diff_both_empty():
    assert reconciler.diff_positions([], {}) == []


def test_diff_in_sync_is_silent():
    rows = [_row("US.A260821C010000", 2), _row("US.B260821P020000", 1)]
    broker = {"US.A260821C010000": 2, "US.B260821P020000": 1}
    assert reconciler.diff_positions(rows, broker) == []


def test_diff_db_only_flags_exercised_or_offline_close():
    """DB 有 / broker 无 = 疑似已行权或场外平仓（7/25 AVGO 的形状）。"""
    diffs = reconciler.diff_positions([_row("US.AVGO260724C415000", 1)], {})
    assert diffs == [{
        "kind": reconciler.KIND_DB_ONLY,
        "option_code": "US.AVGO260724C415000",
        "db_qty": 1, "broker_qty": 0,
    }]


def test_diff_broker_zero_qty_treated_as_absent():
    """broker 行 qty=0（挂过又清掉）与"没有这行"同义 → db_only。"""
    diffs = reconciler.diff_positions(
        [_row("US.A260821C010000", 2)], {"US.A260821C010000": 0})
    assert [d["kind"] for d in diffs] == [reconciler.KIND_DB_ONLY]


def test_diff_broker_only_flags_ghost():
    """broker 有 / DB 无 = 幽灵仓（bot 的 SL/TP/EOD 全不保护它）。"""
    diffs = reconciler.diff_positions([], {"US.SPY260731C745000": 3})
    assert diffs == [{
        "kind": reconciler.KIND_BROKER_ONLY,
        "option_code": "US.SPY260731C745000",
        "db_qty": 0, "broker_qty": 3,
    }]


def test_diff_qty_mismatch():
    """两边都有但张数不同 = 部分场外平仓 / 记账失败（SL 冻结链的残局）。"""
    diffs = reconciler.diff_positions(
        [_row("US.META260731C620000", 2)], {"US.META260731C620000": 1})
    assert diffs == [{
        "kind": reconciler.KIND_QTY_MISMATCH,
        "option_code": "US.META260731C620000",
        "db_qty": 2, "broker_qty": 1,
    }]


def test_diff_db_row_with_zero_remaining_skipped():
    """防御：OPEN 却 qty_remaining=0 的矛盾行不进对账（也不误报幽灵仓）。"""
    diffs = reconciler.diff_positions([_row("US.X260821C010000", 0)], {})
    assert diffs == []


def test_diff_mixed_matrix_deterministic_order():
    """混合场景：db 序在前（db_only/qty_mismatch），broker_only 按 code 排序在后。"""
    rows = [_row("US.GONE260821C010000", 2), _row("US.HALF260821C020000", 2)]
    broker = {"US.HALF260821C020000": 1,
              "US.GHOSTB260821C030000": 1, "US.GHOSTA260821C040000": 1}
    diffs = reconciler.diff_positions(rows, broker)
    assert [(d["kind"], d["option_code"]) for d in diffs] == [
        (reconciler.KIND_DB_ONLY, "US.GONE260821C010000"),
        (reconciler.KIND_QTY_MISMATCH, "US.HALF260821C020000"),
        (reconciler.KIND_BROKER_ONLY, "US.GHOSTA260821C040000"),
        (reconciler.KIND_BROKER_ONLY, "US.GHOSTB260821C030000"),
    ]


# ============ tick：有差异发 TG / 无差异沉默 / 不写 DB ============

async def test_tick_diff_sends_tg_and_never_writes_db(monkeypatch):
    """有漂移 → TG 一条（纯文本）；report-only：DB 分毫不动。"""
    monkeypatch.setenv("DRY_RUN", "false")
    code = _uniq_code("RC1")
    _open_pos("RC1", code, qty=2)

    tg = AsyncMock(return_value=True)
    with patch.object(reconciler, "list_open_option_positions", return_value={}), \
         patch.object(reconciler, "send_telegram", tg):
        diffs = await reconciler._reconcile_tick()

    assert [d["kind"] for d in diffs] == [reconciler.KIND_DB_ONLY]
    tg.assert_awaited_once()
    text = tg.await_args.args[0]
    assert code in text
    assert "行权" in text          # 类别文案：疑似已行权/场外平仓
    assert "report-only" in text
    assert tg.await_args.kwargs.get("parse_mode") is None  # 纯文本，免转义事故

    pos = positions_db.get(code)   # v1 只做可见性：不 record_close、不改 status
    assert pos["status"] == "OPEN"
    assert pos["qty_remaining"] == 2
    assert all(e["trigger_source"] == "kc_signal"
               for e in positions_db.get_events(code))  # 没有 broker_sync 事件


async def test_tick_no_diff_is_silent(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    code = _uniq_code("RC2")
    _open_pos("RC2", code, qty=2)

    tg = AsyncMock()
    with patch.object(reconciler, "list_open_option_positions",
                      return_value={code: 2}), \
         patch.object(reconciler, "send_telegram", tg):
        diffs = await reconciler._reconcile_tick()

    assert diffs == []
    tg.assert_not_awaited()


async def test_tick_dry_run_skips_broker_entirely(monkeypatch):
    """DRY_RUN 的 mock 仓位永远不会出现在 broker——对账必然满屏假漂移，
    整轮跳过：不打 broker、不发 TG。"""
    monkeypatch.setenv("DRY_RUN", "true")
    code = _uniq_code("RC3")
    _open_pos("RC3", code, qty=1)

    broker_mock = MagicMock(return_value={})
    tg = AsyncMock()
    with patch.object(reconciler, "list_open_option_positions", broker_mock), \
         patch.object(reconciler, "send_telegram", tg):
        diffs = await reconciler._reconcile_tick()

    assert diffs == []
    broker_mock.assert_not_called()
    tg.assert_not_awaited()


async def test_tick_same_drift_not_rebroadcast_new_drift_realerts(monkeypatch):
    """同一份漂移每轮重发 TG = 7/23 runner-preserve 6 连发的重演（0002 教训）。
    内容不变只发一次；漂移集合变化重发；清零后同样漂移再现 → 重新告警。"""
    monkeypatch.setenv("DRY_RUN", "false")
    code_a = _uniq_code("RC4A")
    _open_pos("RC4A", code_a, qty=2)

    tg = AsyncMock(return_value=True)
    with patch.object(reconciler, "list_open_option_positions",
                      return_value={}) as broker_mock, \
         patch.object(reconciler, "send_telegram", tg):
        await reconciler._reconcile_tick()
        assert tg.await_count == 1
        await reconciler._reconcile_tick()          # 同一份漂移
        assert tg.await_count == 1                  # TG 不重发
        assert broker_mock.call_count == 2          # 但对账每轮照跑

        code_b = _uniq_code("RC4B")                 # 漂移集合变化
        _open_pos("RC4B", code_b, qty=1)
        await reconciler._reconcile_tick()
        assert tg.await_count == 2

        broker_mock.return_value = {code_a: 2, code_b: 1}   # 全部对齐
        assert await reconciler._reconcile_tick() == []

        broker_mock.return_value = {code_b: 1}      # code_a 再次漂移（新事件）
        await reconciler._reconcile_tick()
        assert tg.await_count == 3


# ============ 接线：interval=0 不起 / 强引用 task ============

async def test_interval_zero_no_task(monkeypatch):
    monkeypatch.setenv("RECONCILE_INTERVAL_MIN", "0")
    tasks: set = set()
    assert reconciler.start_reconciler(tasks) is None
    assert tasks == set()


async def test_interval_unset_defaults_off(monkeypatch):
    """变量不设 = 关（缺省 "0"）：行为与 0016 之前逐字一致。"""
    monkeypatch.delenv("RECONCILE_INTERVAL_MIN", raising=False)
    tasks: set = set()
    assert reconciler.start_reconciler(tasks) is None
    assert tasks == set()


async def test_interval_positive_starts_strong_ref_task(monkeypatch):
    """env>0 → 建 task 并挂进强引用 set（防 GC 静默回收，[refactor-change] c），
    结束后 done_callback 自动 discard——与 alive_heartbeat 同款接线。"""
    monkeypatch.setenv("RECONCILE_INTERVAL_MIN", "30")
    monkeypatch.setenv("DRY_RUN", "true")   # 首轮 tick 走 DRY_RUN skip，不打 broker
    tasks: set = set()
    task = reconciler.start_reconciler(tasks)
    assert task is not None
    assert task in tasks
    assert task.get_name() == "reconciler"

    await asyncio.sleep(0.05)               # 让首轮 tick 跑过（skip 分支）
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)                  # done_callback 在下一轮 loop 执行
    assert task not in tasks


async def test_interval_zero_at_runtime_pauses_ticks(monkeypatch):
    """运行中把 RECONCILE_INTERVAL_MIN 改成 0 → 真的停，而不是加速到每分钟一轮。

    PR#2 review：老写法只把 sleep clamp 到 max(_,1)，_reconcile_tick() 照跑——
    "0=关"实际变成每 60s 打一次 broker，与文档和热重读语义都相反。
    """
    monkeypatch.setenv("RECONCILE_INTERVAL_MIN", "0")
    monkeypatch.setenv("DRY_RUN", "false")
    probe = MagicMock(return_value={})
    with patch.object(reconciler, "list_open_option_positions", probe), \
         patch.object(reconciler, "send_telegram", AsyncMock()):
        task = asyncio.create_task(reconciler.run_reconciler())
        await asyncio.sleep(0.2)
        assert not task.done()          # task 存活（改回正数能自动恢复）
        probe.assert_not_called()       # 关键：一次 broker 都没打
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_interval_restored_resumes_ticks(monkeypatch):
    """0 只是暂停不是终止：改回正数后自动恢复对账。"""
    monkeypatch.setenv("RECONCILE_INTERVAL_MIN", "0")
    monkeypatch.setenv("DRY_RUN", "false")
    probe = MagicMock(return_value={})
    with patch.object(reconciler, "list_open_option_positions", probe), \
         patch.object(reconciler, "send_telegram", AsyncMock()), \
         patch.object(reconciler, "_DISABLED_POLL_SEC", 0.01):
        task = asyncio.create_task(reconciler.run_reconciler())
        await asyncio.sleep(0.05)
        probe.assert_not_called()
        monkeypatch.setenv("RECONCILE_INTERVAL_MIN", "1")
        await asyncio.sleep(0.2)
        assert probe.call_count >= 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_run_loop_survives_broker_failure(monkeypatch):
    """OpenD 半夜抖一下不能杀死对账循环：tick 异常被 catch、下一轮再试，
    且失败本身不发 TG（连接类故障每轮告警就是新的噪音源）。"""
    monkeypatch.setenv("RECONCILE_INTERVAL_MIN", "1")
    monkeypatch.setenv("DRY_RUN", "false")
    boom = MagicMock(side_effect=RuntimeError("OpenD down"))
    tg = AsyncMock()
    with patch.object(reconciler, "list_open_option_positions", boom), \
         patch.object(reconciler, "send_telegram", tg):
        task = asyncio.create_task(reconciler.run_reconciler())
        await asyncio.sleep(0.2)            # to_thread 往返 + 异常被 catch
        assert not task.done()              # 循环活着（进入 sleep 等下一轮）
        assert boom.call_count == 1
        tg.assert_not_awaited()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ============ broker 查询 helper（trade.list_open_option_positions） ============

def test_list_open_option_positions_filters(monkeypatch):
    """期权 code 收、正股/qty<=0 排除——与 ops/sync_positions 同一判据。"""
    df = pd.DataFrame([
        {"code": "US.AVGO260724C415000", "qty": 2},   # 期权 ✓
        {"code": "US.NVDA", "qty": 100},              # 正股（行权换来的孤儿）✗
        {"code": "US.SPY260731C745000", "qty": 0},    # qty 0 ✗
        {"code": "US.META260731P600000", "qty": 1},   # put 也是期权 ✓
    ])
    fake_ctx = MagicMock()
    fake_ctx.position_list_query.return_value = (trade.RET_OK, df)
    monkeypatch.setattr(trade, "_get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(trade, "_ensure_account", lambda: 123)

    out = trade.list_open_option_positions()
    assert out == {"US.AVGO260724C415000": 2, "US.META260731P600000": 1}
    # 全量查询：不带 code 过滤
    assert "code" not in fake_ctx.position_list_query.call_args.kwargs


def test_list_open_option_positions_failure_raises(monkeypatch):
    """查询失败必须抛，不许静默当空仓——否则对账会把所有 DB 仓位误报为
    漂移（与 _get_long_qty"查询失败不当 qty=0"同一教训）。"""
    fake_ctx = MagicMock()
    fake_ctx.position_list_query.return_value = (object(), "connection lost")
    monkeypatch.setattr(trade, "_get_ctx", lambda: fake_ctx)
    monkeypatch.setattr(trade, "_ensure_account", lambda: 123)

    with pytest.raises(RuntimeError, match="position_list_query failed"):
        trade.list_open_option_positions()


# ============================================================
# [7/29] 期权代码判定：strike < $100 曾被判成正股
# ============================================================
# moomoo 的 strike×1000 不补零，旧判据 `[CP]\d{6,}$` 要求 strike 段 ≥6 位，
# 于是所有 strike < $100 的期权漏判。危险链条：reconciler 拿不到这类仓 →
# 误报 db_only「疑似已行权」→ 指引跑 ops/sync_positions → 那边同一 bug →
# record_close 把活仓错标 CLOSED → 掉出 SL/TP/EOD 选仓，裸放。
_OPTION_CODES = [
    "US.SOFI270115C20000",   # $20   —— 实测出现在生产模拟盘账户里
    "US.NIO260731C5500",     # $5.5  —— 4 位 strike 段
    "US.F260731C12000",      # $12
    "US.INTC260731C35000",   # $35
    "US.AMD260731C99000",    # $99   —— 边界：旧判据的分水岭
    "US.AMD260731C100000",   # $100  —— 边界：旧判据从这里开始才对
    "US.NOW260731C115000",   # $115  —— 实测持仓
    "US.SPY260731C745000",   # $745  —— 实测持仓
    "US.SPY260731P745000",   # put 侧同理
]
_STOCK_CODES = [
    "US.XOM", "US.HOOD", "US.IBM", "US.GOOGL", "US.TSLA", "US.BRK.B",
]


def test_option_code_predicate_covers_sub_100_strikes():
    from autotrade.broker.trade import _looks_like_option_code
    for code in _OPTION_CODES:
        assert _looks_like_option_code(code), f"期权被漏判: {code}"
    for code in _STOCK_CODES:
        assert not _looks_like_option_code(code), f"正股被误判成期权: {code}"


def test_sync_positions_predicate_agrees_with_broker():
    """两处判据必须逐条一致 —— 只修一处等于留着另一条路踩雷，
    而 sync_positions 那条**会写库**。"""
    from autotrade.broker.trade import _looks_like_option_code
    from autotrade.ops.sync_positions import _looks_like_option
    for code in _OPTION_CODES + _STOCK_CODES:
        assert _looks_like_option(code) == _looks_like_option_code(code), code


def test_sub_100_strike_not_reported_as_phantom_drift():
    """端到端：DB 与 broker 都持有一张 $20 期权 → 不该报任何漂移。
    旧判据下 broker 侧拿不到它 → 误报 db_only（那正是错误落账的起点）。"""
    db_rows = [{"option_code": "US.SOFI270115C20000", "qty_remaining": 1}]
    broker_rows = {"US.SOFI270115C20000": 1}
    assert reconciler.diff_positions(db_rows, broker_rows) == []
