"""干跑测试：不连真 Discord，喂 FakeMessage 走全链路

覆盖场景：
  1. 正常信号（监听频道 + 触发用户）→ 应下单
  2. 非监听频道 → 应忽略
  3. 监听频道但非触发用户 → 应忽略
  4. 解析失败的垃圾内容 → 应发 Telegram 报错但不下单
  5. 价格超 channel max_price → 风控拦截
  6. 重复 message id → dedup 跳过
  7. CLOSE 信号 → 跳过（不下单）
  8. 多信号 → 取第一个

（原 scripts/test_full_flow.py 折叠为 pytest。依赖真实 config/channels.json
的 KC/enrich 频道 ID + Telegram 通知的人工观察，无硬断言——归 integration，
默认不跑：pytest -m integration tests/test_folded_full_flow.py）
"""
from dataclasses import dataclass, field

import pytest

pytestmark = pytest.mark.integration

from autotrade.listener.router import handle_message
from autotrade.listener import dedup as dc_module  # === [新增] 用于 reset state ===


# ============================================================
# FakeMessage：模拟 discord.Message，只暴露 handle_message 用到的字段
# ============================================================
@dataclass
class FakeAuthor:
    id: int
    name: str


@dataclass
class FakeChannel:
    id: int
    name: str


@dataclass
class FakeMessage:
    id: int
    channel: FakeChannel
    author: FakeAuthor
    content: str
    embeds: list = field(default_factory=list)
    attachments: list = field(default_factory=list)


# ============================================================
# 测试数据
# ============================================================
KC_CHANNEL = FakeChannel(id=1443396572581859405, name="kc-期权-波段-s0")
ENRICH_CHANNEL = FakeChannel(id=1426514716943187979, name="enrich-期权-波段-s0")
RANDOM_CHANNEL = FakeChannel(id=9999999999, name="random")

KC_USER = FakeAuthor(id=1426229538722938880, name="KC")
RANDOM_USER = FakeAuthor(id=8888888888, name="random_user")


# msg_id 必须每次不同，否则 dedup 会跳过
_MSG_ID_COUNTER = [1_000_000]


def next_msg_id() -> int:
    _MSG_ID_COUNTER[0] += 1
    return _MSG_ID_COUNTER[0]


# === [新增] 清空 listener 内部状态，保证 case 之间相互独立 ===
@pytest.fixture(autouse=True)
def _reset_state():
    """清空 fingerprint 指纹 + msg_id dedup + processed set，避免 case 间互相干扰"""
    dc_module._signal_fps.clear()
    dc_module._processed_msg_ids.clear()
    dc_module._processed_set.clear()
    yield


# ============================================================
# Test cases（观察式冒烟：不抛异常即通过，输出/TG 人工核对）
# ============================================================
async def test_1_normal_signal():
    # [1] 正常信号：KC 在 kc 频道发 IREN 60C
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="IREN 60c 6/14 @ 2.50",
    )
    await handle_message(msg)


async def test_2_unmonitored_channel():
    # [2] 非监听频道：应该完全忽略（没有任何输出 = 正确忽略）
    msg = FakeMessage(
        id=next_msg_id(),
        channel=RANDOM_CHANNEL,
        author=KC_USER,
        content="IREN 60c 6/14 @ 2.50",
    )
    await handle_message(msg)


async def test_3_wrong_user():
    # [3] 监听频道但非触发用户：应该忽略（没有下单 = 正确忽略）
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=RANDOM_USER,
        content="IREN 60c 6/14 @ 2.50",
    )
    await handle_message(msg)


async def test_4_parse_fail():
    # [4] 垃圾内容：parse 失败应发 Telegram 报错
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="今天行情真烂，等等再说",
    )
    await handle_message(msg)


async def test_5_price_too_high():
    # [5] 价格 $8 > channel max_price $5：风控应拦截
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="AMZN 200c 6/20 @ 8.00",
    )
    await handle_message(msg)


async def test_6_dedup():
    # [6] 重复 message_id：dedup 应跳过第二次（case 内部需要保留状态）
    fixed_id = next_msg_id()
    msg = FakeMessage(
        id=fixed_id,
        channel=KC_CHANNEL,
        author=KC_USER,
        content="MSFT 500c 6/20 @ 3.00",
    )
    # 第一次：
    await handle_message(msg)
    # 第二次（同 id）：应该静默跳过
    await handle_message(msg)


async def test_7_close_signal():
    # [7] CLOSE 信号：应跳过不下单
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="Closed IREN 60c for +30%",
    )
    await handle_message(msg)


async def test_8_multi_signal():
    # [8] 多信号：应取第一个
    msg = FakeMessage(
        id=next_msg_id(),
        channel=KC_CHANNEL,
        author=KC_USER,
        content="IREN 60c 6/14 @ 2.50, AMZN 200c 6/20 @ 3.50",
    )
    await handle_message(msg)
