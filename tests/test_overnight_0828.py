"""8/28 夜复盘回归（US RTH 2026-08-27，AEST 8/27 23:15 → 8/28 07:00）。

当晚 0 条 ERROR、586 行日志、2 笔开仓全部成交且滑点为负。信号侧三个缺陷：

1. **一条讲 UNG 的战报把 AAPL 卖了**（唯一真正动了钱的一条）。02:17 的
   "got so wrapped up in SPY and AAPL I didn't see that UNG order filled for
   1.90 trim ..." 抽出 symbols=['AAPL','UNG']、price=1.9 —— 1.90 是 **UNG**
   的成交价，却被拿去给 **AAPL 330c** 定限价（×0.95 = 1.80 挂单卖出），
   而同一时段喊单员报的 AAPL 330c 是 5.80-6.00。
   两层各修一处：
     a. `didn't see/notice` 进 RECAP_PATTERNS —— "我当时没看见" 是最硬的
        事后叙述，不可能同时是要人跟单的指令；
     b. 多标的 + 单一喊价 → 丢弃喊价改用实时报价（_drop_unattributable_price）。
        喊价是单个合约的属性，一句话两个标的就没有可靠依据归属它。
        **不拦执行**：多标的平仓是既有契约，不可归属的是价格不是意图。

2. **`$ALAB - Scale out.` 整条走 OPEN 分支**（漏平）。ACTION_VERBS 与
   STRONG_CLOSE_RE 都只有 -ing 形，接不住祈使式。代价比一般漏平重：ALAB 是
   category=lotto，按设计不挂 TP 也不挂 SL，喊单员的平仓信号是它**唯一**的
   主动出场路径。那条信号发出时 +300%，14 分钟后 +400%，当天到期。

3. **lotto 的"放飞"实际等于"没人看"**。补 _alert_no_ladder_winners：
   无 TP 阶梯的仓位（lotto / 0dte）大幅盈利时发 TG，只提醒不卖。
"""
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action
from autotrade.position import tp_watcher

# 逐字原文
UNG_RECAP = ("got so wrapped up in SPY and AAPL I didn’t see that UNG order "
             "filled for 1.90 trim as it approached the 10.75 level 😂💰 "
             "I have 6 contracts left by the way to let run or stop in profit")
ALAB_SCALE_OUT = "$ALAB - Scale out. Congrats to all!!!"
_HELD = {"AAPL", "UNG", "ALAB", "SPY", "TSLA", "MSFT"}


# ============================================================
# 1. 战报不得被当成指令
# ============================================================

def test_ung_fill_recap_does_not_close_anything():
    """8/28 唯一动了钱的一条：这条战报当晚把 AAPL 330c 卖了一张。"""
    assert parse_close(UNG_RECAP, _HELD) is None


@pytest.mark.parametrize("text", [
    "didn't see that UNG order filled for 1.90 trim",
    "I did not notice AAPL hit 6.00, trimmed there",
    "never caught the SPY pop, sold at 2.15",
])
def test_first_person_negated_perception_is_recap(text):
    assert parse_close(text, _HELD) is None


@pytest.mark.parametrize("text", [
    # 反向护栏：条件式 see/notice 是真指令，不许被新 recap 规则误伤
    "see if SPY holds 3.50 then trim here",
    "trimmed SPY @ 2.15",
])
def test_real_instructions_survive_the_recap_rule(text):
    assert parse_close(text, _HELD) is not None, f"真指令被误伤: {text}"


# ============================================================
# 2. 多标的 + 单一喊价 → 丢价不丢意图
# ============================================================

def test_multi_symbol_single_price_drops_the_price():
    """价格无法归属时丢弃喊价（改用实时报价），但平仓意图保留。"""
    parsed = parse_close("Trimmed $TSLA 420c and $MSFT here @ 7.00", _HELD)
    assert parsed is not None, "多标的平仓是既有契约，不能整条拒掉"
    assert parsed["symbols"] == ["TSLA", "MSFT"]
    assert parsed["signal_price"] is None, "串台的喊价必须被丢掉"
    assert parsed["price_unattributable"] is True


def test_single_symbol_price_is_untouched():
    """单标的时喊价照常使用 —— 新规则不许波及绝大多数正常情形。"""
    parsed = parse_close("trimmed SPY @ 2.15", _HELD)
    assert parsed["signal_price"] == 2.15
    assert parsed["price_unattributable"] is False


# ============================================================
# 3. scale out（祈使式）
# ============================================================

def test_scale_out_routes_to_close():
    """路由与解析同进同退：detect_action 和 parse_close 都要认。"""
    assert detect_action(ALAB_SCALE_OUT) == "CLOSE"


def test_scale_out_parses_as_close():
    parsed = parse_close(ALAB_SCALE_OUT, _HELD)
    assert parsed is not None, "$ALAB - Scale out. 当晚整条走了 OPEN 分支"
    assert parsed["symbols"] == ["ALAB"]


@pytest.mark.parametrize("text", ["Scaled out half of $SPY here", "Scaling out $SPY"])
def test_scale_out_variants(text):
    assert parse_close(text, _HELD) is not None


def test_future_scale_out_still_recap():
    """"will scale out later" 是未来意图，仍然不许下单。"""
    assert parse_close("will scale out $SPY later", _HELD) is None


# ============================================================
# 4. 无阶梯仓位（lotto / 0dte）的高盈利提醒
# ============================================================

def _lotto(code="US.ALAB260828C300000", entry=2.77, qty=2, cat="lotto"):
    return {"option_code": code, "avg_entry_price": entry, "qty_remaining": qty,
            "category": cat, "apply_sl": False}


@pytest.mark.asyncio
async def test_no_ladder_winner_alerts_once_then_on_doubling(monkeypatch):
    """ALAB 的形状：+300% 提醒一次，+400% 不重复刷，+800% 再提醒。"""
    monkeypatch.setenv("NO_LADDER_ALERT_PCT", "200")
    tp_watcher.reset_no_ladder_alerts()
    pos = _lotto()
    tg = AsyncMock()
    with patch.object(tp_watcher, "send_telegram", tg):
        # +300%
        await tp_watcher._alert_no_ladder_winners([pos], {pos["option_code"]: 11.08})
        assert tg.await_count == 1
        # +400% —— 没翻倍，不重复
        await tp_watcher._alert_no_ladder_winners([pos], {pos["option_code"]: 13.85})
        assert tg.await_count == 1
        # +800% —— 翻倍了，再提醒
        await tp_watcher._alert_no_ladder_winners([pos], {pos["option_code"]: 24.93})
        assert tg.await_count == 2
    body = tg.await_args[0][0]
    assert "不会自动止盈" in body


@pytest.mark.asyncio
async def test_no_ladder_alert_silent_below_threshold(monkeypatch):
    monkeypatch.setenv("NO_LADDER_ALERT_PCT", "200")
    tp_watcher.reset_no_ladder_alerts()
    pos = _lotto()
    tg = AsyncMock()
    with patch.object(tp_watcher, "send_telegram", tg):
        await tp_watcher._alert_no_ladder_winners([pos], {pos["option_code"]: 5.00})
    tg.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_ladder_alert_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("NO_LADDER_ALERT_PCT", "0")
    tp_watcher.reset_no_ladder_alerts()
    pos = _lotto()
    tg = AsyncMock()
    with patch.object(tp_watcher, "send_telegram", tg):
        await tp_watcher._alert_no_ladder_winners([pos], {pos["option_code"]: 99.0})
    tg.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_ladder_alert_tolerates_missing_quote_and_zero_entry(monkeypatch):
    """安全属性：它在 watcher tick 里，缺报价 / 脏数据都不许抛。"""
    monkeypatch.setenv("NO_LADDER_ALERT_PCT", "200")
    tp_watcher.reset_no_ladder_alerts()
    tg = AsyncMock()
    with patch.object(tp_watcher, "send_telegram", tg):
        await tp_watcher._alert_no_ladder_winners(
            [_lotto(code="US.NOQUOTE"), _lotto(code="US.ZERO", entry=0)],
            {"US.ZERO": 5.0},
        )
    tg.assert_not_awaited()
