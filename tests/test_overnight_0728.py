"""7/27-7/28(周一)第四夜复盘回归。

当晚定性:四夜"16-20min 节拍器掉线"的真相是 **macOS 系统睡眠**(dark-wake):
  - 睡眠期间消息全丢(回补锚记在"醒来检测到断线"那刻,睡眠段落在窗口外);
  - watcher 全程停摆(SPY 745C 的 SL 保护名义在、实际每 16min 才醒几十秒);
  - monotonic 不走 → churn 计数失真(01:55 报"5/30min"、09:09 报"32/30min"
    都是累计值——这条反常正是定位睡眠的关键证据)。
另:script 晚启动 50 分钟,开盘后 09:30-10:22 ET 的信号无任何找回机制。

对应修复:alive 心跳睡眠检测 + 回补锚回拨、启动回补(STARTUP_BACKFILL_MIN)、
OPEN 信号年龄闸门(陈旧开仓只告警不追高)、churn 记账改挂钟。
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import autotrade.app.connection as conn
from autotrade.listener import dedup
from autotrade.listener import open_flow

SPY_RAW = "@everyone\nKC Trades Bot:SPY 745c 4DTE @ 3.15 day trade, can add at PDL on SPY/SPX"


# ============================================================
# 1. OPEN 信号年龄闸门
# ============================================================

def _wire_open_flow_stubs(monkeypatch, placed, alerts):
    async def fake_notify(msg):
        alerts.append(msg)

    dedup._signal_fps.clear()
    dedup._stale_open_alerted.clear()   # 年龄闸门告警节流(同 symbol 5min)
    monkeypatch.setattr(open_flow, "_safe_notify", fake_notify)
    monkeypatch.setattr(open_flow, "notify_bg", lambda msg: None)
    monkeypatch.setattr(
        open_flow, "check_order",
        lambda **kw: SimpleNamespace(passed=True, reason="", detail=""),
    )
    monkeypatch.setattr(
        open_flow, "place_order",
        lambda signal, qty=None: (
            placed.append(signal["symbol"]),
            {"success": True, "order_id": "T1", "code": "US.T", "qty": 1, "price": 3.31},
        )[1],
    )
    monkeypatch.setattr(open_flow, "log_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow, "record_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow.position_mgr, "on_order_filled", lambda **kw: None)
    monkeypatch.setattr(open_flow.fill_checker, "spawn", lambda coro: coro.close())


async def test_stale_open_signal_alerts_instead_of_ordering(monkeypatch):
    """回补重放的 17 分钟前 OPEN:不下单,发"错过的开仓信号"告警。"""
    dedup._signal_fps.clear()
    placed, alerts = [], []
    _wire_open_flow_stubs(monkeypatch, placed, alerts)

    message = SimpleNamespace(
        id=728001,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=17),
    )
    cfg = SimpleNamespace(name="KC-期权-波段", default_qty=1, max_price=1000.0)
    await open_flow.process_open(
        message, SPY_RAW, cfg, 1, datetime.now(timezone.utc), date(2026, 7, 27)
    )
    assert placed == []
    assert any("错过的开仓信号" in a for a in alerts)


async def test_fresh_open_signal_still_orders(monkeypatch):
    dedup._signal_fps.clear()
    placed, alerts = [], []
    _wire_open_flow_stubs(monkeypatch, placed, alerts)

    message = SimpleNamespace(
        id=728002,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=2),
    )
    cfg = SimpleNamespace(name="KC-期权-波段", default_qty=1, max_price=1000.0)
    await open_flow.process_open(
        message, SPY_RAW, cfg, 1, datetime.now(timezone.utc), date(2026, 7, 27)
    )
    assert placed == ["SPY"]
    assert not any("错过的开仓信号" in a for a in alerts)


async def test_no_created_at_treated_as_realtime(monkeypatch):
    """FakeMessage/无 created_at 的调用方视为实时——闸门不拦(兼容旧口径)。"""
    dedup._signal_fps.clear()
    placed, alerts = [], []
    _wire_open_flow_stubs(monkeypatch, placed, alerts)
    message = SimpleNamespace(id=728003)
    cfg = SimpleNamespace(name="KC-期权-波段", default_qty=1, max_price=1000.0)
    await open_flow.process_open(
        message, SPY_RAW, cfg, 1, datetime.now(timezone.utc), date(2026, 7, 27)
    )
    assert placed == ["SPY"]


# ============================================================
# 2. churn 记账用挂钟(睡眠压缩 monotonic 的失真修复)
# ============================================================

async def test_churn_counts_wall_clock_window(monkeypatch):
    """睡眠场景:monotonic 每次只走 5s(睡眠不计时),挂钟每次走 17min。
    30 分钟挂钟窗口里最多容纳 2 个事件——老代码(monotonic 记账)会把
    整夜累计出来(7/27 夜实测报 32)。"""
    mono = [1000.0]
    wall = [1_000_000.0]
    monkeypatch.setattr(
        conn, "_time",
        SimpleNamespace(monotonic=lambda: mono[0], time=lambda: wall[0]),
    )
    monkeypatch.setattr(conn, "_safe_notify", AsyncMock())
    monkeypatch.setattr(conn, "_churn_disconnects", [])
    monkeypatch.setattr(conn, "_churn_notified_at", 0.0)
    monkeypatch.setattr(conn, "_recent_disconnects", [])
    monkeypatch.setattr(conn, "_disconnect_in_progress", False)
    monkeypatch.setattr(conn, "_last_disconnect_wall", None)

    for _ in range(12):  # 整夜 12 次醒来
        mono[0] += 5.0        # monotonic 睡眠中不走,每次只推进 5s
        wall[0] += 17 * 60.0  # 挂钟每次推进 17 分钟
        conn._disconnect_in_progress = False  # 每次都当成新断线(绕过防抖)
        await conn.on_disconnect()

    # 挂钟 30min 窗口(17min 间隔)最多存 2 条——绝不该是 12
    assert len(conn._churn_disconnects) <= 2


# ============================================================
# 3. alive 心跳:睡眠检测 → 回补锚回拨 + TG 节流
# ============================================================

async def test_alive_gap_pulls_backfill_anchor_and_alerts_once(monkeypatch):
    tg = AsyncMock()
    monkeypatch.setattr(conn, "_safe_notify", tg)
    monkeypatch.setattr(conn, "_last_disconnect_wall", None)
    monkeypatch.setattr(conn, "_sleep_alerted_wall", None)
    monkeypatch.setattr(conn, "client", None)  # 未就绪 → 不立即回补,只回拨锚

    now = datetime.now(timezone.utc)
    prev = now - timedelta(minutes=20)
    await conn._on_alive_gap(prev, now)
    assert conn._last_disconnect_wall == prev  # 锚回拨到跳变前最后一拍
    assert tg.await_count == 1

    # 冷却窗口内的第二次 gap:锚取更早者,TG 不重发
    earlier = now - timedelta(minutes=40)
    await conn._on_alive_gap(earlier, now + timedelta(minutes=5))
    assert conn._last_disconnect_wall == earlier
    assert tg.await_count == 1


async def test_alive_gap_backfills_immediately_when_client_ready(monkeypatch):
    monkeypatch.setattr(conn, "_safe_notify", AsyncMock())
    monkeypatch.setattr(conn, "_sleep_alerted_wall", None)
    monkeypatch.setattr(conn, "_last_disconnect_wall", None)
    backfill = AsyncMock()
    monkeypatch.setattr(conn, "_backfill_missed", backfill)
    monkeypatch.setattr(conn, "client", SimpleNamespace(is_ready=lambda: True))

    now = datetime.now(timezone.utc)
    await conn._on_alive_gap(now - timedelta(minutes=16), now)
    backfill.assert_awaited_once()


# ============================================================
# 4. 启动回补(晚启动 50 分钟场景)
# ============================================================

async def test_startup_backfill_sets_anchor_and_replays(monkeypatch):
    backfill = AsyncMock()
    monkeypatch.setattr(conn, "_backfill_missed", backfill)
    monkeypatch.setattr(conn, "_last_disconnect_wall", None)

    await conn.startup_backfill(50)
    backfill.assert_awaited_once()
    anchor = conn._last_disconnect_wall
    assert anchor is not None
    age_min = (datetime.now(timezone.utc) - anchor).total_seconds() / 60
    assert 49 <= age_min <= 51


async def test_startup_backfill_disabled_by_default(monkeypatch):
    backfill = AsyncMock()
    monkeypatch.setattr(conn, "_backfill_missed", backfill)
    monkeypatch.setattr(conn, "_last_disconnect_wall", None)
    await conn.startup_backfill(0)
    backfill.assert_not_awaited()
    assert conn._last_disconnect_wall is None


# ============================================================
# 5. [7/28 二次复盘] 0008 自身的三个洞
# ============================================================

class _FakeMsg:
    def __init__(self, mid):
        self.id = mid


class _FakeChan:
    """按时间正序持有消息;如实模拟 discord.py 的 oldest_first/limit 语义。"""

    def __init__(self, msgs, raises=False):
        self._msgs = msgs
        self._raises = raises

    async def history(self, limit=50, after=None, oldest_first=True):
        if self._raises:
            raise ConnectionError("gateway is dead (刚睡醒)")
        msgs = self._msgs if oldest_first else list(reversed(self._msgs))
        for m in msgs[:limit]:
            yield m


def _wire_backfill(monkeypatch, chan, replayed, *, db_ids=()):
    async def fake_handle(m):
        replayed.append(m.id)

    monkeypatch.setattr(conn, "handle_message", fake_handle)
    monkeypatch.setattr(conn.registry, "enabled_channel_ids", lambda: [1])
    monkeypatch.setattr(conn, "client", SimpleNamespace(get_channel=lambda cid: chan))
    monkeypatch.setattr(conn, "processed_msg_ids_since", lambda since: set(db_ids))


async def test_backfill_keeps_anchor_when_fetch_fails(monkeypatch):
    """抓取失败不能吞掉回补锚点。

    0008 原版一进 _backfill_missed 就把锚置 None,而"刚睡醒"正是 gateway
    最可能还没活过来的时刻(_on_alive_gap 立即回补 → history 全抛异常 →
    异常被逐频道 except 吞掉 → 函数正常返回,锚没了)。随后重登录的
    on_ready 回补拿到 since=None 直接 return —— 整段睡眠期静默丢失,
    恰好是 0008 想治的病。
    """
    replayed = []
    _wire_backfill(monkeypatch, _FakeChan([], raises=True), replayed)
    anchor = datetime.now(timezone.utc) - timedelta(minutes=20)
    monkeypatch.setattr(conn, "_last_disconnect_wall", anchor)

    await conn._backfill_missed()

    assert replayed == []
    assert conn._last_disconnect_wall == anchor   # 锚点必须留着等重试


async def test_backfill_consumes_anchor_on_success(monkeypatch):
    replayed = []
    _wire_backfill(monkeypatch, _FakeChan([_FakeMsg(1), _FakeMsg(2)]), replayed)
    monkeypatch.setattr(
        conn, "_last_disconnect_wall", datetime.now(timezone.utc) - timedelta(minutes=5)
    )

    await conn._backfill_missed()

    assert replayed == [1, 2]                 # 时间正序重放
    assert conn._last_disconnect_wall is None  # 成功才消费


async def test_backfill_skips_messages_already_processed_last_run(monkeypatch):
    """跨进程幂等:重启后 _seen 是空的,不查库就会把上次运行**已执行过**的
    trim 再跑一遍(CLOSE 没有年龄闸门保护)。raw_signals 当水位线。"""
    replayed = []
    _wire_backfill(
        monkeypatch, _FakeChan([_FakeMsg(11), _FakeMsg(12), _FakeMsg(13)]),
        replayed, db_ids=("11", "12"),
    )
    monkeypatch.setattr(
        conn, "_last_disconnect_wall", datetime.now(timezone.utc) - timedelta(minutes=50)
    )

    await conn._backfill_missed()

    assert replayed == [13]   # 11/12 上次运行已处理


async def test_backfill_truncation_drops_oldest_not_newest(monkeypatch):
    """窗口超过条数上限时,丢的必须是最老的。

    oldest_first=True + limit 的组合正好拿反(从锚点往后取最老的 N 条),
    活跃频道的 60 分钟启动回补会把最近、最该跟的几条丢掉。
    """
    replayed = []
    msgs = [_FakeMsg(i) for i in range(1, 11)]   # 1..10,10 最新
    _wire_backfill(monkeypatch, _FakeChan(msgs), replayed)
    monkeypatch.setenv("BACKFILL_HISTORY_LIMIT", "3")
    monkeypatch.setattr(
        conn, "_last_disconnect_wall", datetime.now(timezone.utc) - timedelta(minutes=50)
    )

    await conn._backfill_missed()

    assert replayed == [8, 9, 10]              # 保最新三条,仍按时间正序
    assert conn._last_disconnect_wall is not None  # 截断=不完整,锚留着


async def test_stale_open_does_not_mute_live_resend(monkeypatch):
    """陈旧重放不能污染指纹表。

    _is_duplicate_signal 是"查即登记";年龄闸门若排在它后面,一条 17 分钟前
    的重放会先把指纹登记上,KC 随后**实时**重喊同一张(实测 8s 重发)就会被
    当成孪生静默丢掉——没下单、没告警、没日志之外的痕迹。
    """
    placed, alerts = [], []
    _wire_open_flow_stubs(monkeypatch, placed, alerts)
    cfg = SimpleNamespace(name="KC-期权-波段", default_qty=1, max_price=1000.0)

    stale = SimpleNamespace(
        id=728101, created_at=datetime.now(timezone.utc) - timedelta(minutes=17))
    await open_flow.process_open(
        stale, SPY_RAW, cfg, 1, datetime.now(timezone.utc), date(2026, 7, 27))
    assert placed == []
    assert any("错过的开仓信号" in a for a in alerts)

    # 紧接着 KC 实时重喊同一张 —— 必须照常下单
    live = SimpleNamespace(
        id=728102, created_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    await open_flow.process_open(
        live, SPY_RAW, cfg, 1, datetime.now(timezone.utc), date(2026, 7, 27))
    assert placed == ["SPY"]


async def test_stale_open_bilingual_twins_alert_once(monkeypatch):
    """双语孪生(不同 msg_id、同信号)的超龄告警只发一条。"""
    placed, alerts = [], []
    _wire_open_flow_stubs(monkeypatch, placed, alerts)
    cfg = SimpleNamespace(name="KC-期权-波段", default_qty=1, max_price=1000.0)
    for mid in (728201, 728202):
        msg = SimpleNamespace(
            id=mid, created_at=datetime.now(timezone.utc) - timedelta(minutes=17))
        await open_flow.process_open(
            msg, SPY_RAW, cfg, 1, datetime.now(timezone.utc), date(2026, 7, 27))
    assert placed == []
    assert len([a for a in alerts if "错过的开仓信号" in a]) == 1


async def test_backfill_keeps_anchor_moved_by_a_second_sleep(monkeypatch):
    """回补途中又睡了一觉(_on_alive_gap 把锚回拨得更早)——不能顺手清掉。"""
    replayed = []
    earlier = datetime.now(timezone.utc) - timedelta(minutes=90)
    _wire_backfill(monkeypatch, _FakeChan([_FakeMsg(21)]), replayed)
    monkeypatch.setattr(
        conn, "_last_disconnect_wall", datetime.now(timezone.utc) - timedelta(minutes=5)
    )

    # 重放某条消息时 _on_alive_gap 触发,把锚回拨到 90 分钟前
    orig_handle = conn.handle_message

    async def handle_then_sleep_again(m):
        await orig_handle(m)
        conn._last_disconnect_wall = earlier

    monkeypatch.setattr(conn, "handle_message", handle_then_sleep_again)
    await conn._backfill_missed()

    assert replayed == [21]
    assert conn._last_disconnect_wall == earlier   # 新锚必须活着
