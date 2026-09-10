"""9/10 复盘（US RTH 2026-09-09）。ashley 频道上线第一晚。

当晚 0 开仓、0 下单；9 条 :RedAlert: 开仓信号 **100% 全丢**，18 条原始消息
（中英双播）全部落在 `[parser] no signal`。三件事：

1. **ticker 裸写不带 $。** B 系列六个变体首 token 一律 `\\$([A-Z]{1,5})\\b`，
   能吃裸 ticker 的 A/C 要求 `195c` 紧凑写法、D 要求 NDTE —— ashley 三种形状
   一个都不沾。不是词表漂移，是一个从没被覆盖过的新形状。

2. **补上 $ 之后，9 条里 3 条会静默买错到期日。** `EXPIRATION NEXT WEEK`
   全仓库零逻辑（ARM 会买成本周五）；`$110 CALLS 10/2` 的 MM/DD 写在
   calls 后面，B0 够不着、B3 认得，可 B3 排在无日期兜底 B2 **后面** ——
   谁在前谁赢。丢单至少有 Parse failed 告警，买错合约一个字都不会说。

3. **开仓喊话自带的止损从来没人读。** 9 条里 3 条写着 "STOP LOSS AT $x"。
   lesson #38 落的 manual_stop 只有 close_flow 一个写入口，读的是**后续那条
   独立的**止损喊话。更糟：INTC 110C 10/2 的 DTE=23 → categorize 归 swing →
   apply_sl=False → 连 SL watcher 都不进。一个明说了的止损被丢掉两次。

出场侧另有一笔账（TSLA 385C / DELL 570C 双实锤，见 last_spare_trim_decision
的 docstring）：qty=2 开仓 → 喊单员在 +6.3% / +12.8% 各拿走一张 → 剩下单张
让 T1/T2 两档全部空转 → 按棘轮底出在 +50%，回吐 ≈ 已实现。闸门本次落地但
**默认关**（LAST_SPARE_TRIM_MIN_PNL_PCT=0），钱路口径要单独拍板。

逐字语料在 tests/corpus/2026-09-09.jsonl（18 行）；本文件只测语料表达不了的
东西：pattern 优先级、消费端、开关矩阵。
"""
import os
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.listener import close_flow, dedup, open_flow
from autotrade.parsing.signal_parser import parse_signal
from autotrade.policy.positions import last_spare_trim_decision
from autotrade.position import sl_watcher
from autotrade.position.sell_executor import Outcome
from autotrade.storage import positions_db

WED = date(2026, 9, 9)          # 周三：本周五 = 9/11，下周五 = 9/18
ASHLEY = "ashley"


def _uniq(prefix: str) -> str:
    return f"US.{prefix}{datetime.now().strftime('%H%M%S%f')}C001000"


def _open(code, symbol, entry, qty=2, category="weekly", apply_sl=True,
          channel=ASHLEY):
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=110.0, side="CALL",
        expiry=date(2026, 10, 2), qty=qty, fill_price=entry,
        category=category, apply_sl=apply_sl, eod_force_close=False, tags=[],
        channel_name=channel, msg_id="m1",
    )
    return code


# ============================================================
# 1. pattern 优先级：带日期的必须赢过无日期兜底
# ============================================================

def test_explicit_mmdd_after_calls_beats_the_no_date_fallback():
    """契约翻转：改动前这条返回 9/11（B2 抢走），现在必须是 10/2。

    语料行 ashley_intc110_mmdd_* 断的是最终 expiry_date；这里钉死**为什么** ——
    B2 不要求日期，只要排在 B3 前面就会把一切都吃掉。顺序即语义。
    """
    r = parse_signal(":RedAlert: INTC - $110 CALLS 10/2 $4.70", WED)
    assert r["expiry_date"] == date(2026, 10, 2)
    assert r["expiry"] == "10/2"


def test_no_date_still_falls_back_to_this_friday():
    """反向不变量：B3 前移不许把无日期那条也带走。"""
    r = parse_signal(":RedAlert: SKHY - $195 CALLS EXPIRATION THIS WEEK $2.90", WED)
    assert r["expiry_date"] == date(2026, 9, 11)


def test_next_week_is_a_week_past_this_friday():
    """契约翻转：改动前 next week 与 this week 得到同一个 9/11。"""
    r = parse_signal(":RedAlert: ARM - $300 CALLS EXPIRATION NEXT WEEK $4.90", WED)
    assert r["expiry_date"] == date(2026, 9, 18)


@pytest.mark.parametrize("text", [
    # 裸 "next week" 在行情评论里太常见 —— 同夜 enrich 就有 "我明天也会关注
    # $PLTR 的下行"这类语境。只认"到期词 + next week"，不认裸词。
    ":RedAlert: SKHY - $195 CALLS EXPIRATION THIS WEEK $2.90, may run into next week",
    ":RedAlert: SKHY - $195 CALLS $2.90 will look at this again next week",
])
def test_bare_next_week_does_not_shift_the_expiry(text):
    """反向护栏：把"持有到下周"读成"下周到期"= 静默买错合约。"""
    assert parse_signal(text, WED)["expiry_date"] == date(2026, 9, 11)


def test_dollar_prefixed_shapes_are_untouched():
    """不变量：老形状（ticker 自带 $）一个字都不许变。"""
    r = parse_signal("$MRVL $252.50 SCALP***** calls $1.69 weekly", WED)
    assert (r["symbol"], r["strike"], r["price"]) == ("MRVL", 252.5, 1.69)
    assert r["expiry_date"] == date(2026, 9, 11)


# ============================================================
# 2. 开仓喊话自带的止损：抽取 + **消费端**
# ============================================================

@pytest.mark.asyncio
async def test_declared_stop_in_the_open_message_is_recorded():
    """9 条里 3 条把止损写在开仓那条消息里，当晚一个字都没接住。"""
    code = _open(_uniq("INTA"), "INTA", entry=4.94)
    open_flow._apply_declared_stop(
        ":RedAlert: INTC - $110 CALLS 10/2 $4.70, STOP LOSS AT $4.20 @everyone",
        code, 4.94,
    )
    assert positions_db.get(code)["manual_stop"] == 4.20


def test_an_open_message_without_a_stop_records_nothing():
    """反向护栏：没写止损的不许凭空造一个。"""
    code = _open(_uniq("SKHA"), "SKHA", entry=3.13)
    open_flow._apply_declared_stop(
        ":RedAlert: SKHY - $195 CALLS EXPIRATION THIS WEEK $2.90 @everyone",
        code, 3.13,
    )
    assert positions_db.get(code)["manual_stop"] is None


@pytest.mark.asyncio
async def test_a_swing_with_a_declared_stop_enters_the_watch_list():
    """消费端 —— 缺了它前面那条就是空转（lesson #22 的形状）。

    INTC 110C 10/2 在 9/9 的 DTE=23 → categorize 归 swing → apply_sl=False →
    原本连 SL watcher 的名单都进不来。喊单员明说了 $4.20 也照样裸奔。
    """
    code = _open(_uniq("SWG"), "SWGX", entry=4.94, qty=1,
                 category="swing", apply_sl=False)
    positions_db.set_manual_stop(code, 4.20)
    sl_watcher._triggered.discard(code)
    os.environ["SL_SELL_SLIP"] = "0.08"
    sold = []

    def fake_sell(option_code, qty, limit_price, **kw):
        sold.append((option_code, qty, limit_price))
        return {"success": True, "qty": qty, "price": limit_price,
                "order_id": "X", "code": option_code}

    # 触发价 = 4.20 / (1 - 8%) = 4.57；last=4.50 已经跌破
    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=lambda codes: {c: (4.50 if c == code else None)
                                          for c in codes}), \
         patch("autotrade.position.sl_watcher.place_sell_order", side_effect=fake_sell), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    assert sold, "声明了绝对止损价的 swing 必须进看护名单"
    evt = [e for e in positions_db.get_events(code)
           if e["trigger_source"] == "sl_polling"]
    assert evt and "喊单员声明止损" in (evt[0]["note"] or "")


@pytest.mark.asyncio
async def test_a_swing_without_a_declared_stop_is_still_not_watched():
    """反向不变量：本次改的**不是**"给 swing 加止损"（那是钱路决定，
    ROADMAP P1 §16）。没有声明价的 swing 必须逐字维持裸奔现状。"""
    code = _open(_uniq("SWH"), "SWHX", entry=6.48, qty=1,
                 category="swing", apply_sl=False)
    sl_watcher._triggered.discard(code)
    sold = []

    with patch("autotrade.position.sl_watcher.get_last_prices",
               side_effect=lambda codes: {c: 0.01 for c in codes}), \
         patch("autotrade.position.sl_watcher.place_sell_order",
               side_effect=lambda **kw: sold.append(kw)), \
         patch("autotrade.position.sl_watcher.send_telegram", new_callable=AsyncMock):
        await sl_watcher._sl_tick()

    assert not sold, "-99.8% 也不许动：swing 裸奔是已知且刻意保留的现状"


# ============================================================
# 3. 最后一张备用合约的浮盈闸门（默认关）
# ============================================================

def test_gate_preserves_the_last_spare_below_threshold():
    """9/8 的 TSLA：entry 3.35，喊单员在 3.56（+6.3%）喊 trim。"""
    d, why = last_spare_trim_decision(3.35, 3.56, None, 25.0)
    assert d == "PRESERVE" and "+6.3%" in why


def test_gate_lets_the_trim_through_above_threshold():
    d, _ = last_spare_trim_decision(3.20, 4.80, None, 25.0)
    assert d == "TRIM"


@pytest.mark.parametrize("entry,quote", [(3.35, None), (0.0, 3.56), (None, 3.56)])
def test_gate_fails_open_not_closed(entry, quote):
    """方向与 strategy_b_decision **相反**，是刻意的：本闸门误拦一次真该减的
    仓，比漏拦一次更贵（6/30 SPY 748c 那类提前平掉的教训）。"""
    assert last_spare_trim_decision(entry, quote, None, 25.0)[0] == "TRIM"


@pytest.mark.asyncio
async def test_gate_off_reproduces_todays_behaviour():
    """开关关掉（默认）→ 逐字旧行为：照常减 1 张，且**不取报价**。"""
    os.environ["DRY_RUN"] = "true"
    os.environ.pop("LAST_SPARE_TRIM_MIN_PNL_PCT", None)
    code = _open(_uniq("GOFF"), "GOFF", entry=3.35, qty=2)
    dedup._close_fps.clear()
    quoted = []

    with patch.object(close_flow, "_safe_notify", new_callable=AsyncMock), \
         patch.object(close_flow, "get_sell_ref_price",
                      side_effect=lambda c: quoted.append(c)), \
         patch.object(close_flow, "place_sell_order",
                      return_value={"success": True, "message": "ok",
                                    "order_id": "1", "code": code,
                                    "qty": 1, "price": 3.56}):
        await close_flow.handle_close_signal(
            "GOFF OUT 33% at 3.56 @everyone", msg_id=910001, channel_name=ASHLEY)

    assert positions_db.get(code)["qty_remaining"] == 1
    assert quoted == [], "关掉时连报价都不该取（喊话自带 3.56，限价也用不着 quote）"
    positions_db.record_close(code, 1, 3.56, "manual", note="ut")


@pytest.mark.asyncio
async def test_gate_on_holds_the_last_spare():
    """打开后：+6.3% 达不到 25% → 不减，仓位仍是 2 张，结局 SPARE_PRESERVED。"""
    os.environ["DRY_RUN"] = "true"
    os.environ["LAST_SPARE_TRIM_MIN_PNL_PCT"] = "25"
    code = _open(_uniq("GON"), "GONX", entry=3.35, qty=2)
    dedup._close_fps.clear()
    sold = []
    try:
        with patch.object(close_flow, "_safe_notify", new_callable=AsyncMock) as tg, \
             patch.object(close_flow, "get_sell_ref_price", return_value=3.56), \
             patch.object(close_flow, "place_sell_order",
                          side_effect=lambda **kw: sold.append(kw)):
            await close_flow.handle_close_signal(
                "GONX OUT 33% @everyone", msg_id=910002, channel_name=ASHLEY)
            blob = " ".join(str(c) for c in tg.call_args_list)
    finally:
        os.environ.pop("LAST_SPARE_TRIM_MIN_PNL_PCT", None)

    assert not sold
    assert positions_db.get(code)["qty_remaining"] == 2
    # 文案不许复用 runner-preserve 那条 —— 它写死了"各剩 1 张"，这里还剩 2 张
    # transport 的 Markdown 转义会把连字符写成 "last\\-spare"，断词根不断连字符
    assert "spare 保留" in blob and "各剩 1 张" not in blob
    positions_db.record_close(code, 2, 3.56, "manual", note="ut")


def test_spare_preserved_counts_as_executed():
    """不变量：新结局必须算"处理过了"，否则会落到 "no matching open
    positions" 兜底文案（6/18 IWM 的误导形状）。"""
    assert Outcome.SPARE_PRESERVED is not Outcome.NOT_FOUND
