"""9/1 夜复盘回归（US RTH 2026-08-31，AEST 8/31 23:23 → 9/1 07:00）。

当晚只开一单：COIN 190C 9/4，entry 2.08 两张。02:42 T1 命中卖 1 张 @2.99
（已实现 +$91），03:12 T2 命中（last ≥ 4.16）但剩 1 张、trim 50% 取整为 0 →
runner-preserve 保留。喊单员把它喊成 "FIRST BAGGER OF THE WEEK"。

三件事各自独立地把保护挡掉了，本文件一节钉一件：

1. **T2 之后 runner 裸奔**：SL 仍锚在 entry（2.08 × 0.5 = 1.04），一张摸到
   4.16 的合约要跌回 1.04 才会被卖。T2 那条日志写着"后续交给 SL / EOD /
   喊单员平仓信号"，而当晚这三条腿全是虚的。

2. **喊单员两次减仓表述双语双漏**："Down to 1/2." 与 "Start securing some."
   （及各自的 ZH 孪生）四条全部 `[parser] no signal`，零告警。当晚零损失
   纯属巧合 —— TP T1 恰好比 "Down to 1/2" 早 7 分钟把我们打到同样的 1/2。

3. **147 行不可行动的 WARNING**：三条 broker 有 / DB 从无的外仓
   （NVDA 250C 9/18、SOFI 20C 27-01、UPS 130C 27-01，都是 LEAPS，本 bot
   只打 weekly/swing）每轮各一条 WARNING 连报 49 轮，把 broker_only
   这个信号训练成了背景色。
"""
import pytest

from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action
from autotrade.policy.positions import sl_ratchet_floor
from autotrade.position import reconciler

_HELD = {"AAPL", "AVGO", "COIN", "IONQ", "NVDA", "TSLA", "UNG"}

# 当晚 COIN 那张的真实数字
ENTRY = 2.08
SL_SLIP = 0.08          # SL_SELL_SLIP 缺省
T1, T2 = 1, 2           # tp_hits 位掩码


# ============================================================
# 1. SL 棘轮：已落袋的档位把止损底抬上来
# ============================================================

def test_last_nights_runner_was_naked_below_entry():
    """契约翻转：修改前的阈值就是 entry×0.5 = 1.04，一路裸奔到那里。

    这条断言的是**新行为**——T2 命中后止损底必须远高于旧的 1.04。
    """
    floor, _ = sl_ratchet_floor("weekly", T1 | T2, ENTRY, SL_SLIP)
    old_threshold = ENTRY * (1 - 0.50)
    assert old_threshold == pytest.approx(1.04, abs=0.01)
    assert floor > old_threshold
    # T2 = +100% 档 → 锁住上一级（+50%），含卖出滑点折算
    assert floor == pytest.approx(2.08 * 1.5 / 0.92, abs=0.01)


def test_t1_floor_is_true_breakeven_not_bare_entry():
    """T1 命中后 = 保本档。阈值是**触发价**，成交价还要减一个 slip，
    不折算的"保本止损"实际会亏掉那个滑点 —— 那就不叫保本。"""
    floor, desc = sl_ratchet_floor("weekly", T1, ENTRY, SL_SLIP)
    assert floor == pytest.approx(ENTRY / (1 - SL_SLIP), abs=0.01)
    assert floor > ENTRY
    assert "保本" in desc
    # 按这个底触发，实际卖出价正好回到 entry
    assert floor * (1 - SL_SLIP) == pytest.approx(ENTRY, abs=0.01)


@pytest.mark.parametrize("category,hits,expected_mult", [
    ("weekly", T1,      1.00),   # +50% 档命中 → 锁保本
    ("weekly", T1 | T2, 1.50),   # +100% 档命中 → 锁 +50%
    ("weekly", T2,      1.50),   # T1 被熔断跳过也照样按最高命中档算
    ("swing",  T1,      1.00),   # swing 阶梯是 +100/+200，同样的"回吐一级"
    ("swing",  T1 | T2, 2.00),
])
def test_ratchet_locks_one_rung_below_the_highest_hit_tier(category, hits, expected_mult):
    floor, _ = sl_ratchet_floor(category, hits, ENTRY, SL_SLIP)
    assert floor == pytest.approx(ENTRY * expected_mult / (1 - SL_SLIP), abs=0.01)


@pytest.mark.parametrize("category,hits,entry", [
    ("weekly", 0, ENTRY),        # 一档未中 → 不介入，阈值仍是 entry×(1-pct)
    ("lotto", T1 | T2, ENTRY),   # 无阶梯类目（LADDER 里没有）→ 不介入
    ("0dte", T1, ENTRY),
    ("weekly", T1, 0.0),         # 坏数据不许算出个假底价
    ("weekly", T1, None),
])
def test_ratchet_stays_out_of_the_way(category, hits, entry):
    """棘轮只在"确实落袋过一档"时说话，其余一律沉默交还旧阈值。"""
    floor, desc = sl_ratchet_floor(category, hits, entry, SL_SLIP)
    assert floor is None and desc == ""


def test_ratchet_never_loosens_the_stop():
    """不变式：棘轮只抬不放。

    sl_watcher 取 max(旧阈值, 棘轮底)，而棘轮底本身按定义 ≥ entry —— 只要
    STOP_LOSS_PCT > 0，旧阈值必定低于 entry，棘轮不可能把止损放松。
    这条钉的是那个"取更高者"的决定，将来有人把它改成无条件覆盖就会红。
    """
    for pct in (0.30, 0.50, 0.80):
        old = ENTRY * (1 - pct)
        floor, _ = sl_ratchet_floor("weekly", T1, ENTRY, SL_SLIP)
        assert floor > old


# ============================================================
# 2. "Down to 1/2." / "Start securing some." —— 双语双漏
# ============================================================

# 当晚原文（逐字，含 enrich 的破折号前缀）
DOWN_TO_EN = "$COIN - Down to 1/2."
DOWN_TO_ZH = "$COIN - 降至 1/2。"
SECURE_EN = "$COIN - Start securing some. Stress free trade to start the week."
SECURE_ZH = "$COIN - 开始确保一些。无压力交易以开始这一周。"


@pytest.mark.parametrize("text,pct", [
    (DOWN_TO_EN, 50),   # 剩 1/2 → 卖 50%（FRACTION_DOWN_TO_PATTERN 早就认得）
    (DOWN_TO_ZH, 50),
    (SECURE_EN, 33),    # "some" 无比例 → trim 默认 33
    (SECURE_ZH, 33),
])
def test_last_nights_missed_trims_now_route_and_parse(text, pct):
    """断言的是**下游结果**而不是"能解析了"：kind/symbols/pct 三样都要对。

    修改前四条全是 detect_action=OPEN → [parser] no signal，零告警。
    """
    assert detect_action(text) == "CLOSE"
    parsed = parse_close(text, _HELD)
    assert parsed is not None
    assert parsed["kind"] == "CLOSE"
    assert parsed["symbols"] == ["COIN"]
    assert parsed["pct"] == pct


# 反向护栏：这些**必须**继续走 OPEN / 解析不出平仓。
# 前两条是同一晚同一个标的的原文 —— 收裸 secure/确保 当场就会误平。
@pytest.mark.parametrize("text", [
    # 00:59 同一晚，纯鼓励
    "$COIN - This market has been extremely unforgiving. "
    "If you are green in this trade - make sure it stays that way.",
    "$COIN - 如果你在这笔交易中是盈利的 - 确保它保持这样。",
    # 7/23 AVGO 的移动止损子句（lessons 里明写着"EN 孪生本来就无 EN 动词，安全"）
    "stop at entry now to secure green trade",
    # 裸 down to：行情叙述与 KC 黑话，都没有比例
    "$SPY down to 2.50 support here",
    "$COIN - down to runners",
    # 到期日不是分数（与 _OUT_FRACTION_PATTERN 同一条定义域护栏）
    "$SPY down to 7/13 expiry",
    # 当晚其余闲聊，一条都不许变成指令
    "$COIN - Trust the process.",
    "$COIN - 相信这个过程。",
    "ANOTHER ONE💰",
])
def test_commentary_still_routes_to_open(text):
    assert detect_action(text) == "OPEN"


def test_secure_purpose_clause_still_means_full_close_not_a_new_trim():
    """8/18 AMZN 原文里 "to secure small green trade" 是目的状语。

    它本来就因为 "out the rest" 判成 100% 全平 —— 新加的 securing 分支
    不许把它的 pct 改掉（那会让一次全平退化成 33% trim）。
    """
    text = "out the rest of AMZN to secure small green trade ✅"
    parsed = parse_close(text, _HELD | {"AMZN"})
    assert parsed["symbols"] == ["AMZN"]
    assert parsed["pct"] == 100


def test_holding_recap_is_still_not_a_close():
    """不变式（lesson #24）：04:31 / 05:43 的 "holding 1/4 into tomorrow"
    是**状态陈述**，当晚被 `[parser] skip (holding/remaining)` 挡下，这是
    有意的设计，不在本次修改范围 —— 新加的 down-to 分支不许把它带进 CLOSE。
    """
    for text in (
        "$COIN - I'll be holding 1/4 of my contracts into tomorrow. "
        "Zero risk. Looking for a gap-up.",
        "Multi-bagger. Holding 1/4 into tomorrow. Love this daily chart.",
        "多倍收益。明天持有1/4。喜欢这个日线图。",
    ):
        assert parse_close(text, _HELD) is None


# ============================================================
# 3. 外仓 vs 幽灵仓
# ============================================================

# 当晚每轮各刷一条 WARNING、连刷 49 轮的三条（都是 LEAPS，从没进过 trades.db）
FOREIGN = ["US.NVDA260918C250000", "US.SOFI270115C20000", "US.UPS270115C130000"]


def test_never_seen_broker_positions_are_foreign_not_drift():
    diffs = reconciler.diff_positions(
        [], {c: 1 for c in FOREIGN}, known_codes=set())
    assert {d["kind"] for d in diffs} == {reconciler.KIND_FOREIGN}


def test_a_code_we_once_held_is_still_a_real_ghost():
    """区分点是**成因**不是严重程度：开过的仓 broker 侧还在 = 记账脱钩，
    这类必须继续每轮 WARNING（0016 的有意设计）。"""
    diffs = reconciler.diff_positions(
        [], {"US.MU260815C945000": 2}, known_codes={"US.MU260815C945000"})
    assert [d["kind"] for d in diffs] == [reconciler.KIND_BROKER_ONLY]


def test_known_codes_omitted_keeps_0016_behaviour_verbatim():
    """不传 known_codes = 不区分。老调用方（含 ops/sync_positions 那侧的
    纯函数矩阵）行为逐字不变。"""
    diffs = reconciler.diff_positions([], {c: 1 for c in FOREIGN})
    assert {d["kind"] for d in diffs} == {reconciler.KIND_BROKER_ONLY}


def test_foreign_report_says_it_will_never_be_handled():
    """TG 文案要说清楚"不会自动处理"——否则读的人以为下一轮对账会收拾它。"""
    diffs = reconciler.diff_positions(
        [], {FOREIGN[0]: 1}, known_codes=set())
    text = reconciler._format_report(diffs)
    assert FOREIGN[0] in text
    assert "外仓" in text and "不会自动处理" in text


# ============================================================
# 4. 消费端：棘轮真的会让 sl_watcher 动手
#    （纯函数对了不算数 —— lesson #22 的教训：write-only 策略层是空转）
# ============================================================

import os                                                    # noqa: E402
from datetime import date, datetime                           # noqa: E402
from unittest.mock import AsyncMock, patch                    # noqa: E402

from autotrade.position import sl_watcher                     # noqa: E402
from autotrade.storage import positions_db                    # noqa: E402


def _open_coin_runner(code: str, tp_hits: int):
    """复刻当晚 T1 之后的残局：weekly、entry 2.08、剩 1 张、档位已置位。"""
    positions_db.open_or_add(
        option_code=code, symbol="COIN", strike=190.0, side="CALL",
        expiry=date(2026, 9, 4), qty=2, fill_price=ENTRY,
        category="weekly", apply_sl=True, eod_force_close=False, tags=[],
        channel_name="enrich", msg_id="m1",
    )
    positions_db.record_close(option_code=code, qty_sold=1, fill_price=2.99,
                              trigger_source="tp_polling", note="T1")
    for bit in (T1, T2):
        if tp_hits & bit:
            positions_db.mark_tp_hit(code, bit)


def _quotes_for(code, price):
    return lambda codes: {c: (price if c == code else None) for c in codes}


@pytest.mark.asyncio
async def test_watcher_sells_the_runner_at_the_ratchet_floor(monkeypatch):
    """当晚形状 + 一次回撤到 3.00：旧行为一动不动（1.04 才卖），新行为落袋。"""
    code = f"US.RATCHET{datetime.now().strftime('%H%M%S%f')}C190000"
    _open_coin_runner(code, T1 | T2)
    sl_watcher._triggered.discard(code)
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    monkeypatch.setenv("SL_RATCHET_AFTER_TP", "1")

    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_for(code, 3.00)), \
         patch("autotrade.position.sl_watcher.place_sell_order",
               return_value={"success": True, "qty": 1, "price": 2.76,
                             "order_id": "RATCHET_1", "code": code}), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    pos = positions_db.get(code)
    assert pos["status"] == "CLOSED"
    evt = [e for e in positions_db.get_events(code)
           if e["trigger_source"] == "sl_polling"]
    assert len(evt) == 1 and evt[0]["qty_delta"] == -1


@pytest.mark.asyncio
async def test_ratchet_off_reproduces_last_nights_silence(monkeypatch):
    """开关关掉 = 逐字回到 9/1 那晚：3.00 时一张不卖，一路裸奔到 1.04。"""
    code = f"US.NORATCHET{datetime.now().strftime('%H%M%S%f')}C190000"
    _open_coin_runner(code, T1 | T2)
    sl_watcher._triggered.discard(code)
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    monkeypatch.setenv("SL_RATCHET_AFTER_TP", "0")

    sell = AsyncMock()
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_for(code, 3.00)), \
         patch("autotrade.position.sl_watcher.place_sell_order", side_effect=sell), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    sell.assert_not_called()
    assert positions_db.get(code)["status"] == "PARTIAL"
    positions_db.record_close(code, qty_sold=1, fill_price=3.00,
                              trigger_source="manual", note="ut cleanup")


@pytest.mark.asyncio
async def test_ratchet_does_not_touch_a_position_that_never_hit_a_tier(monkeypatch):
    """反向安全属性：一档未中的仓位阈值不变，3.00 时当然不许卖。"""
    code = f"US.NOTIER{datetime.now().strftime('%H%M%S%f')}C190000"
    _open_coin_runner(code, 0)
    sl_watcher._triggered.discard(code)
    monkeypatch.setenv("STOP_LOSS_PCT", "0.50")
    monkeypatch.setenv("SL_RATCHET_AFTER_TP", "1")

    sell = AsyncMock()
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=_quotes_for(code, 3.00)), \
         patch("autotrade.position.sl_watcher.place_sell_order", side_effect=sell), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    sell.assert_not_called()
    positions_db.record_close(code, qty_sold=1, fill_price=3.00,
                              trigger_source="manual", note="ut cleanup")
