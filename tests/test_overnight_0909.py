"""9/9 夜复盘的两条（US RTH 2026-09-08）。

当晚三笔：TSLA 385C（KC）、MRVL 245C、DELL 570C（enrich），已实现 -$140。
两条缺陷都在**出场侧**，都不是"策略看错方向"：

1. **`ACTION_VERBS` 里的 `"trim "` 拿尾空格当词边界。**
   00:09:51 KC 喊 `TSLA 3.90 💵 new high of day trim, stop at entry now`，
   `trim,` 匹配不上 `"trim "` → `_has_action_verb` False → `parse_close` 返 None，
   而 `detect_action` 的 `trim(?:med|ming)?\\b` 认得它 —— 又一次"路由认得、
   解析不认得"。更糟的是它传染了中文：ZH 孪生 2 秒后解析**完全正确**
   （pct=33 price=3.9），却被 zh-twin guard 挡掉了 —— 那道防护的前提是
   "EN 判为非平仓指令是可信的"，这次 EN 是误判，整条指令消失。
   当晚零损失纯属巧合（只剩 1 张，runner-preserve 本来也会跳过）。

2. **喊单员声明的止损从来没人接。** 一周内第三次：
       9/2  trimmed Tesla 2.95, stop is at 2.35 now
       9/9  TSLA 3.90 new high of day trim, stop at entry now
       9/9  $DELL - 减持一半，止损设置为保本
   三次的减仓那半都执行了。`ZH_SL_ADJUST_CLAUSE_RE` 把这类子句**抹掉**
   （免得误判成平仓），抹完就没有下文。后果可量化：DELL 剩的那张至今挂在
   entry×0.5 = 1.60，而喊单员说的是保本 3.20 —— $160 的敞口差。
"""
import os
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.listener import close_flow, dedup
from autotrade.parsing.close_parser import (
    STOP_AT_BREAKEVEN,
    parse_close,
    parse_stop_adjust,
)
from autotrade.parsing.signal_parser import detect_action
from autotrade.position import sl_watcher
from autotrade.storage import positions_db

KC = "KC-期权-波段"
# 当晚 00:09:51 的原文（含 @everyone 前缀与 emoji，逐字）
TSLA_TRIM_EN = ("@everyone\nKC Trades Bot:TSLA 3.90 💵 new high of day trim, "
                "stop at entry now to secure green trade")
TSLA_TRIM_ZH = ("@everyone\nKC Trades Bot：TSLA 3.90 💵 日内新高减仓，"
                "止损设在入场点以锁定盈利交易")
DELL_TRIM_EN = "enrich:\n$DELL - Taking off 1/2 here and stop set to break-even."
DELL_TRIM_ZH = "enrich:\n$DELL - 在这里减持一半，止损设置为保本。"


def _uniq(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open(code, symbol, entry, qty=2, channel=KC, strike=385.0, side="CALL"):
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=strike, side=side,
        expiry=date(2026, 9, 18), qty=qty, fill_price=entry,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name=channel, msg_id="m1",
    )
    return code


# ============================================================
# 1. 裸动词的词边界：动词后面跟标点不许再漏
# ============================================================

def test_last_nights_trim_now_parses():
    """契约翻转：这条当晚 parse_close 返回 None，现在必须解析出来。"""
    assert detect_action(TSLA_TRIM_EN) == "CLOSE"
    r = parse_close(TSLA_TRIM_EN, {"TSLA"})
    assert r is not None, "9/9 00:09:51 的原文"
    assert r["symbols"] == ["TSLA"]
    assert r["pct"] == 33


@pytest.mark.parametrize("text", [
    "KC Trades Bot:TSLA 3.90 day trim, stop at entry",   # 逗号
    "KC Trades Bot:TSLA 3.90 nice trim.",                # 句号
    "KC Trades Bot:TSLA 3.90 trim!",                     # 感叹号
    "KC Trades Bot:TSLA 3.90 trim\nrunners only",        # 换行
    "KC Trades Bot:trim TSLA here 3.90",                 # 原本就能的祈使式
])
def test_bare_verb_followed_by_punctuation(text):
    """尾空格当词边界 = 标点/换行/句末后面的动词全漏。"""
    assert parse_close(text, {"TSLA"}) is not None, text


@pytest.mark.parametrize("text", [
    "KC Trades Bot:nice scout report on TSLA 3.90",
    "KC Trades Bot:circuit breaker hit TSLA 3.90",
])
def test_word_boundary_does_not_widen_into_other_words(text):
    """`"cut "` 的尾空格本来是防 scout/circuit 的 —— \\b 必须同样防住。"""
    assert parse_close(text, {"TSLA"}) is None, text


def test_the_zh_twin_is_no_longer_collateral_damage():
    """EN 误判会经 zh-twin guard 传染中文，当晚整条指令因此消失。

    EN 修好之后，中文这一侧本来就一直是对的。
    """
    assert parse_close(TSLA_TRIM_ZH, {"TSLA"}) is not None


# ============================================================
# 2. 喊单员声明的止损
# ============================================================

@pytest.mark.parametrize("text,expect", [
    ("KC Trades Bot:trimmed Tesla 2.95, stop is at 2.35 now", 2.35),      # 9/2 EN
    ("KC交易机器人：特斯拉减仓2.95，止损现设于2.35", 2.35),                  # 9/2 ZH
    (TSLA_TRIM_EN, STOP_AT_BREAKEVEN),                                    # 9/9 EN
    (TSLA_TRIM_ZH, STOP_AT_BREAKEVEN),                                    # 9/9 ZH
    (DELL_TRIM_EN, STOP_AT_BREAKEVEN),                                    # 9/9 DELL EN
    (DELL_TRIM_ZH, STOP_AT_BREAKEVEN),                                    # 9/9 DELL ZH
    ("KC Trades Bot:raising stop to 3.00", 3.00),
])
def test_declared_stops_are_extracted(text, expect):
    r = parse_stop_adjust(text)
    assert r is not None, text
    assert r["stop"] == expect


@pytest.mark.parametrize("text,why", [
    ("trimmed TSLA @ 3.75 they even dipped it to 3.00", "只是减仓，没说止损"),
    ("KC Trades Bot:stopped out at 2.35", "stopped 是平仓动词，不是设止损"),
    ("$MRVL - trimming all", "全平，无止损声明"),
    ("", "空串"),
])
def test_these_carry_no_stop(text, why):
    assert parse_stop_adjust(text) is None, why


def test_manual_stop_only_ever_ratchets_up():
    """喊单员往上移止损是锁利润；旧消息重放不许把已收紧的保护放开。"""
    code = _open(_uniq("MS1"), "MSX", entry=3.20)
    try:
        assert positions_db.set_manual_stop(code, 3.20) is True
        assert positions_db.get(code)["manual_stop"] == 3.20
        assert positions_db.set_manual_stop(code, 2.00) is False, "不许放松"
        assert positions_db.get(code)["manual_stop"] == 3.20
        assert positions_db.set_manual_stop(code, 3.60) is True, "抬高照收"
        assert positions_db.get(code)["manual_stop"] == 3.60
    finally:
        positions_db.record_close(code, 2, 3.20, "manual", note="ut")


@pytest.mark.asyncio
async def test_dell_replay_records_breakeven_against_our_own_entry():
    """9/9 04:40 DELL 原文重放：减半执行了，止损那半当晚没人接。

    "break-even" 锚在**我们自己的成交均价**上（喊单员说的是他的入场，
    语义是"回到不亏"）。
    """
    os.environ["DRY_RUN"] = "true"
    code = _open(_uniq("DELL"), "DELLX", entry=3.20, channel="enrich",
                 strike=570.0)
    dedup._close_fps.clear()

    with patch.object(close_flow, "_safe_notify", new_callable=AsyncMock), \
         patch.object(close_flow, "place_sell_order",
                      return_value={"success": True, "message": "submitted",
                                    "order_id": "1", "code": code,
                                    "qty": 1, "price": 3.61}):
        await close_flow.handle_close_signal(
            DELL_TRIM_EN.replace("$DELL", "$DELLX"),
            msg_id=909001, channel_name="enrich")

    pos = positions_db.get(code)
    assert pos["manual_stop"] == 3.20, "保本 = 我们的成本，不是喊单员的"
    positions_db.record_close(code, pos["qty_remaining"], 3.20, "manual", note="ut")


@pytest.mark.asyncio
async def test_sl_uses_the_declared_stop_instead_of_entry_times_half():
    """当晚 DELL 剩的那张挂在 1.60，喊单员说的是保本 3.20。

    折算：触发价 = 3.20 / (1 - 8%) = 3.48 —— 触发之后还要挨一个卖出滑点，
    不折算的"保本止损"会亏掉一个 slip（同 sl_ratchet_floor 的换算）。
    """
    code = _open(_uniq("MS2"), "MSY", entry=3.20, qty=1)
    positions_db.set_manual_stop(code, 3.20)
    sl_watcher._triggered.discard(code)
    os.environ["STOP_LOSS_PCT"] = "0.50"
    os.environ["SL_SELL_SLIP"] = "0.08"

    def quotes(codes):
        return {c: (3.40 if c == code else None) for c in codes}

    sold = []

    def fake_sell(option_code, qty, limit_price, **kw):
        sold.append((option_code, qty, limit_price))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": "X", "code": option_code}

    with patch("autotrade.position.sl_watcher.get_last_prices", side_effect=quotes), \
         patch("autotrade.position.sl_watcher.place_sell_order", side_effect=fake_sell), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    # 老行为：threshold = 3.20 × 0.5 = 1.60，last=3.40 远在其上 → 一动不动
    assert sold, "声明的止损必须把底抬到 3.48，last=3.40 已经跌破"
    evt = [e for e in positions_db.get_events(code)
           if e["trigger_source"] == "sl_polling"]
    assert evt and "喊单员声明止损" in (evt[0]["note"] or "")


@pytest.mark.asyncio
async def test_declared_stop_never_loosens_a_tighter_threshold():
    """反向不变量：声明的止损低于既有阈值时，一个字都不许动。"""
    code = _open(_uniq("MS3"), "MSZ", entry=3.20, qty=1)
    positions_db.set_manual_stop(code, 1.00)      # 比 entry×0.5=1.60 还低
    sl_watcher._triggered.discard(code)
    os.environ["STOP_LOSS_PCT"] = "0.50"

    def quotes(codes):
        return {c: (1.70 if c == code else None) for c in codes}

    sell_mock = AsyncMock()
    with patch("autotrade.position.sl_watcher.get_last_prices", side_effect=quotes), \
         patch("autotrade.position.sl_watcher.place_sell_order", side_effect=sell_mock), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    sell_mock.assert_not_called()
    assert positions_db.get(code)["status"] in ("OPEN", "PARTIAL")
    positions_db.record_close(code, 1, 3.20, "manual", note="ut")
