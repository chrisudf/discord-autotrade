"""8/10 夜复盘回归（US RTH 2026-08-10，AEST 8/10 23:15 → 8/11 07:00）。

当晚 1 次开仓（NOW，成交质量很好）、1 次止盈（XOM）、0 条 ERROR。三个缺陷：

1. **真漏单 DELL**（parser 层）——"$DELL - weekly - $3.50 - $530 calls" 用
   **字段分隔符**写法，"$3.50 - $530" 逐字命中 PRICE_RANGE_PATTERN 被当成喊价
   区间跳过。根因不在区间正则而在**倒序归一化没触发**：
   `_INVERTED_PRICE_STRIKE_RE` 要求两个 $数字之间只能是空白（`\\s+`），
   本例中间是 " - "。放开成 `[\\s\\-–—]+` 后文本先被改写成规范序
   "$DELL - weekly - $530 calls $3.50"，区间正则自然不再命中——归一化跑在
   `_has_price_range` 之前，**一处改动同时解决两层**，PRICE_RANGE_PATTERN 不动。
   逐字语料见 tests/corpus/2026-08-10.jsonl 的 dell_scaling_in_{zh,en}。

2. **主动 skip 完全静默**（listener 层）——上面那条做到了零下单 + 零告警 +
   零 WARNING（`open_flow` 的 skip 分支只有 `logger.debug`），人工毫无补救机会。
   修 1 只修好**已知**的一种写法，这条是安全网：skip 时若文本仍命中"三件套"
   启发式（$TICKER + calls/puts + 喊价）就 WARNING + TG。

3. **单张持仓 T1 = 全平**（position 层）——`tp_watcher` 的
   `max(1, round(qty*trim/100))` 在 qty_remaining==1 时把"卖 0 张（让 runner 跑）"
   抬成"卖 1 张（全平）"，XOM 因此落袋 +92% 而喊单员拿到 +300%（~$185/张），
   且 T2 永远不会触发。同一教训 close 路径早已用 ~$330/合约学过
   （`policy/positions.calc_qty_to_sell` 的 `remaining == 1 → return 0`）。

另有一条**耦合**缺口：收盘前 "$DELL - … - chopping in half"（ZH "削减一半"）
双语双漏，detect_action 压根没路由成 CLOSE。单独看当晚零损失（无 DELL 持仓），
但修了 1 之后 DELL 能自动买入，平仓动词再接不住就是"买得进卖不出"——该标的
当日收盘 -52%。必须与 1 同批落地，故本文件一并钉住。

配套配置变更（2026-08-11，模拟盘阶段）：channels.json `default_qty` 1→2，
让"卖剩余 50%"在整数上成立。回滚清单见 ROADMAP §0.0。
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from autotrade.listener import dedup, open_flow
from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action, parse_signal
from autotrade.position import tp_watcher
from autotrade.storage import positions_db

D = date(2026, 8, 10)          # 周一；weekly → 2026-08-14（周五）
FRI = date(2026, 8, 14)


# ============================================================
# 1. 倒序归一化的分隔符（DELL 类漏单）
# ============================================================
# 逐字原文的双语解析结果由语料门断言（corpus/2026-08-10.jsonl），
# 这里只钉"放开分隔符**没有**顺手放开别的东西"——负例才是这次改动的风险面。

@pytest.mark.parametrize("raw", [
    "$DELL - weekly - $3.50 - $530 calls",
    "$DELL – weekly – $3.50 – $530 calls",      # en dash
    "$DELL — weekly — $3.50 — $530 calls",      # em dash
    "$DELL weekly $3.50 $530 calls",            # 老的纯空白形态（7/31 AAOI）
])
def test_inverted_price_survives_any_field_separator(raw):
    sig = parse_signal(raw, msg_ts=D)
    assert sig is not None and not sig.get("skip"), f"应解析成功: {sig}"
    assert (sig["symbol"], sig["strike"], sig["side"]) == ("DELL", 530.0, "CALL")
    assert sig["price"] == 3.50
    assert sig["expiry_date"] == FRI


@pytest.mark.parametrize("raw", [
    "$3.50 - $4.00 calls",        # 真喊价区间：两数都带小数且 3.50 < 4.00，
                                  # 前两道护栏全过 —— 靠 ×10 数量级护栏挡住
    "$1.20 - $1.50 calls",
    "$740 - $745 calls",          # 价差写法（护栏 1：喊价必须带小数点）
])
def test_real_price_range_still_skipped(raw):
    """放开分隔符**新引入**的误判形状必须被第三道护栏挡回区间 pre-filter。

    换反的后果是"用 $3.50 的限价买 $4.00 行权价的合约"——比漏单贵得多。
    """
    sig = parse_signal(raw, msg_ts=D)
    assert isinstance(sig, dict) and sig.get("skip") == "price_range", (
        f"期望 skip(price_range)，实际: {sig}"
    )


def test_strike_price_never_swapped_when_magnitudes_are_close():
    """×10 护栏的边界：strike 不足 price 的 10 倍就不交换。"""
    from autotrade.parsing.signal_parser import _normalize_inverted_price
    assert _normalize_inverted_price("$3.50 - $34 calls") == "$3.50 - $34 calls"
    assert _normalize_inverted_price("$3.50 - $35 calls") == "$35 calls $3.50"


# ============================================================
# 2. 主动 skip 不再静默（安全网）
# ============================================================

def _wire(monkeypatch, placed, alerts):
    async def fake_notify(msg):
        alerts.append(msg)

    dedup._signal_fps.clear()
    dedup._parser_skip_alerted.clear()
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
            {"success": True, "order_id": "T1", "code": "US.T", "qty": 2, "price": 3.5},
        )[1],
    )
    monkeypatch.setattr(open_flow, "log_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow, "record_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow.position_mgr, "on_order_filled", lambda **kw: None)
    monkeypatch.setattr(open_flow.fill_checker, "spawn", lambda coro: coro.close())


async def _run(raw, msg_id=810000):
    message = SimpleNamespace(id=msg_id, created_at=datetime.now(timezone.utc))
    cfg = SimpleNamespace(name="enrich", default_qty=2, max_price=2000.0)
    await open_flow.process_open(
        message, raw, cfg, 1, datetime.now(timezone.utc), D
    )


# 真喊价区间 + 方向词 + 喊价 —— 修 1 之后仍然（正确地）被 skip，
# 但形状上"像信号"，正是安全网该出声的场景
RANGE_SHAPED = "enrich:\n$SPY - $3.50 - $4.00 calls\n\n@everyone $alert"


async def test_signal_shaped_skip_now_alerts(monkeypatch):
    """8/10 的核心教训：pre-filter 吞掉的东西必须留一个人工补救窗口。"""
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    await _run(RANGE_SHAPED)
    assert placed == [], "skip 的语义不变——只加告警，绝不改成下单"
    # TG 文案走 MarkdownV2 转义（"pre-filter" 会变成 "pre\\-filter"），
    # 断言取不被转义的中文片段
    assert any("未自动下单" in a for a in alerts), alerts


async def test_skip_alert_deduped_across_twins(monkeypatch):
    """中英孪生 + 编辑重发只发一条（当晚同一条 DELL 消息进来 2 次）。"""
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    await _run(RANGE_SHAPED, msg_id=810001)
    await _run(RANGE_SHAPED, msg_id=810002)
    assert len([a for a in alerts if "未自动下单" in a]) == 1, alerts


@pytest.mark.parametrize("raw", [
    # 当晚全部 skip/闲聊语料：加了告警也不该多出一条 TG（噪音评估的回归钉）
    "enrich:\n$UBER - Boom. Holding my 30%\n\n@everyone $alert",
    "enrich:\n$UBER - Cheers. Life changing trade.\n\n@everyone $alert",
    "enrich:\n$SPY levels for the day 8/10/2026:\n\nBlue zone= $772.42, $774.82\n"
    "Green targets = $776.81, $778.32\n\n@everyone $alert",
])
async def test_ordinary_skips_stay_silent(monkeypatch, raw):
    placed, alerts = [], []
    _wire(monkeypatch, placed, alerts)
    await _run(raw)
    assert placed == []
    assert not any("未自动下单" in a for a in alerts), alerts


# ============================================================
# 3. TP 单张持仓保留 runner（XOM 的 $185/张）
# ============================================================

def _pos(symbol: str, qty: int, entry: float = 0.89) -> str:
    code = f"US.{symbol}260814C160000"
    positions_db.open_or_add(
        option_code=code, symbol=symbol, strike=160.0, side="CALL",
        expiry=FRI, qty=qty, fill_price=entry,
        category="swing", apply_sl=False, eod_force_close=False, tags=[],
        channel_name="ut", msg_id="m_0810",
    )
    return code


async def _trigger(code, last, trim_pct=50, tier_bit=1, threshold=1.00):
    """跑一次 T{tier_bit}，返回 (卖单参数列表, TG 文本列表)。"""
    tp_watcher._triggered_this_tick.clear()
    calls, tgs = [], []

    def fake_sell(**kw):
        calls.append(kw)
        return {"success": True, "order_id": "X1", "code": code,
                "qty": kw["qty"], "price": kw["limit_price"]}

    async def fake_tg(msg, **kw):
        tgs.append(msg)

    with patch("autotrade.position.tp_watcher.place_sell_order", side_effect=fake_sell), \
         patch("autotrade.position.tp_watcher.send_telegram", side_effect=fake_tg):
        await tp_watcher._trigger_tp(
            positions_db.get(code), last, threshold, trim_pct, tier_bit, 0.05
        )
    return calls, tgs


async def test_single_contract_t1_preserves_runner_instead_of_full_close():
    """8/10 XOM 逐字重演：entry $0.89、last $1.80 触发 swing T1（+100% 卖 50%）。

    改动前：`selling 1/1` 整仓平掉，落袋 +92%，喊单员同标的 +300%。
    改动后：不卖，仓位完整留给 T2 / SL / EOD / 喊单员平仓信号。
    """
    code = _pos("XOM", qty=1)
    calls, tgs = await _trigger(code, 1.80)
    assert calls == [], "剩 1 张时 trim 50% 等于全平 —— 必须不卖"
    assert positions_db.get(code)["qty_remaining"] == 1
    assert any("runner" in t.lower() for t in tgs), tgs


async def test_preserved_tier_is_marked_so_it_stops_re_evaluating():
    """置位是为了别在 5s 一轮的 tick 里重复算重复刷屏（判定不会翻转：
    qty_remaining 只减不增）。"""
    code = _pos("XOMB", qty=1)
    await _trigger(code, 1.80)
    assert positions_db.get(code)["tp_hits"] & 1, "该档必须已置位"
    calls, tgs = await _trigger(code, 1.90)
    assert calls == [] and tgs == [], "同档第二次必须完全静默"


async def test_two_contracts_make_the_ladder_work():
    """qty=2（2026-08-11 起的模拟盘配置）下 T1 才真的是"卖一半"。"""
    code = _pos("XOMC", qty=2)
    calls, _ = await _trigger(code, 1.80)
    assert len(calls) == 1 and calls[0]["qty"] == 1, calls
    assert positions_db.get(code)["qty_remaining"] == 1
    # 剩下那张到 T2 时同样保留 runner——不会在 +200% 被"卖 50%"全平
    calls2, _ = await _trigger(code, 2.67, tier_bit=2, threshold=2.00)
    assert calls2 == []
    assert positions_db.get(code)["qty_remaining"] == 1


async def test_full_close_tier_still_sells_everything():
    """runner-preserve 只拦非全平档：trim=100 的档位照常清仓（真反转信号不受影响）。"""
    code = _pos("XOMD", qty=1)
    calls, _ = await _trigger(code, 1.80, trim_pct=100)
    assert len(calls) == 1 and calls[0]["qty"] == 1
    assert positions_db.get(code)["qty_remaining"] == 0


# ============================================================
# 4. "chopping in half" 平仓（与第 1 节耦合，必须同批）
# ============================================================

CHOP_EN = ("$DELL - gross price action into the end of the day - "
           "I will hold a 1% lotto position - chopping in half")
CHOP_ZH = "$DELL - 到今天结束的毛价行动 - 我将持有1%的彩票头寸 - 削减一半"


@pytest.mark.parametrize("raw", [CHOP_EN, CHOP_ZH])
def test_chop_in_half_is_a_close_at_fifty_percent(raw):
    assert detect_action(raw) == "CLOSE"
    parsed = parse_close(raw, {"DELL", "NOW", "UBER"})
    assert parsed is not None and parsed["symbols"] == ["DELL"]
    # 50 而不是 33（默认值），也不是 1（"1% lotto position" 是仓位标注）——
    # 双语 pct 不一致时谁先到按谁执行，是隐性错平
    assert parsed["pct"] == 50


@pytest.mark.parametrize("raw", [
    "Today was a choppier day, but nothing we can't learn from",   # 当晚收盘复盘
    "$SPY chopping around the 9EMA all morning",
    "choppy price action into the close",
    "$SPY chopped up today",
])
def test_bare_chop_is_market_chatter_not_a_close(raw):
    """裸 chop/choppy 是"横盘震荡"黑话。收进动词表 = 每条行情吐槽都变平仓指令。"""
    assert detect_action(raw) == "OPEN"


@pytest.mark.parametrize("raw,pct", [
    ("Trimming 50% of position in $NOW", 50),
    ("Trimmed $SPY 25% here", 25),
    ("$NOW - out half here @ 2.10", 50),
])
def test_pct_size_adjective_relaxation_did_not_eat_real_ratios(raw, pct):
    """PCT_PATTERN 放宽（允许 "1% lotto position" 夹一个形容词）的风险面：
    用通配 \\w+ 会把 "50% **of** position" 的比例也排除掉、悄悄掉回默认 33。
    白名单收词就是为了钉死这条。"""
    parsed = parse_close(raw, {"NOW", "SPY"})
    assert parsed is not None and parsed["pct"] == pct
