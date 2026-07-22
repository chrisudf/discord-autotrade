"""完整重登录后的历史回补（7/20 复盘）。

on_ready（完整 re-IDENTIFY）不重放 gateway 事件，掉线窗口内的信号 on_message
收不到。_backfill_missed 拉频道历史重喂 handle_message，靠 handle_message
顶部的 _seen(msg_id) 去重保证幂等（重放不会重复下单）。

[refactor-change 测试侧] asyncio.get_event_loop().run_until_complete(...)
现代化为 asyncio.run(...)（py3.12 起 get_event_loop 在无运行 loop 时弃用）。
"""
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DRY_RUN", "true")
import autotrade.app.connection as rl
from autotrade.listener import dedup as dc


class _FakeChannel:
    def __init__(self, msgs):
        self._msgs = msgs

    async def history(self, limit=50, after=None, oldest_first=True):
        for m in self._msgs:
            yield m


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # 清 _seen 去重状态，避免测试间串味
    dc._processed_msg_ids.clear()
    dc._processed_set.clear()
    # [refactor-change e 相关] 新结构 connection.client 由 main 注入（bind），
    # import 时是 None——挂一个可 setattr 的替身，让下面对 rl.client.get_channel
    # 的 patch 原样可用
    monkeypatch.setattr(rl, "client", SimpleNamespace(get_channel=lambda cid: None))
    yield


async def _run_backfill(monkeypatch, channel_msgs, enabled_ids):
    seen_ids = []

    async def _fake_handle(m):
        # 复用真 _seen 判定幂等，只记录"真正被处理"的 msg
        if dc._seen(m.id):
            return
        seen_ids.append(m.id)

    monkeypatch.setattr(rl, "handle_message", _fake_handle)
    monkeypatch.setattr(rl.registry, "enabled_channel_ids", lambda: enabled_ids)
    monkeypatch.setattr(
        rl.client, "get_channel", lambda cid: channel_msgs.get(cid)
    )
    rl._last_disconnect_wall = datetime.now(timezone.utc)
    await rl._backfill_missed()
    return seen_ids


class _Msg:
    def __init__(self, mid):
        self.id = mid


def test_backfill_replays_missed(monkeypatch):
    import asyncio
    ch = _FakeChannel([_Msg(101), _Msg(102)])
    seen = asyncio.run(
        _run_backfill(monkeypatch, {1: ch}, [1])
    )
    assert seen == [101, 102]


def test_backfill_idempotent_against_already_seen(monkeypatch):
    import asyncio
    # 101 已在 live 路径处理过 → 回补时 _seen 挡住，只放行 102
    dc._seen(101)
    ch = _FakeChannel([_Msg(101), _Msg(102)])
    seen = asyncio.run(
        _run_backfill(monkeypatch, {1: ch}, [1])
    )
    assert seen == [102]


def test_backfill_noop_without_disconnect_wall(monkeypatch):
    import asyncio
    ch = _FakeChannel([_Msg(201)])
    monkeypatch.setattr(rl, "handle_message", lambda m: None)
    monkeypatch.setattr(rl.registry, "enabled_channel_ids", lambda: [1])
    monkeypatch.setattr(rl.client, "get_channel", lambda cid: ch)
    rl._last_disconnect_wall = None  # 没有掉线起点 → 直接返回，不拉历史
    # 不应抛错
    asyncio.run(rl._backfill_missed())


def test_backfill_consumes_wall_timestamp(monkeypatch):
    import asyncio
    ch = _FakeChannel([])
    monkeypatch.setattr(rl, "handle_message", lambda m: None)
    monkeypatch.setattr(rl.registry, "enabled_channel_ids", lambda: [1])
    monkeypatch.setattr(rl.client, "get_channel", lambda cid: ch)
    rl._last_disconnect_wall = datetime.now(timezone.utc)
    asyncio.run(rl._backfill_missed())
    # 消费后清空，避免下次重登录重复回补
    assert rl._last_disconnect_wall is None
