"""7/22-7/23 夜盘复盘回归（SIMULATE 首夜，语料取自当晚 listener 日志原文）。

覆盖当晚发现的四类问题：
  1. ZH 移动止损备注被误判为 CLOSE（03:19 AVGO，靠 runner-preserve +
     无喊价拒卖才没误卖）→ ZH_SL_ADJUST_CLAUSE_RE 防护
  2. ZH "出半" 漏路由（23:59 NBIS，EN "Out half" 一直能接）→ 词表补齐
  3. runner-preserve TG 一夜 6 条重复（AVGO 415c 单张仓）→ 通知节流
  4. 预警 TG 同步 await 垫高下单延迟 ~1.2s（滑点成本）→ notify_bg 后台发送
"""
import asyncio
from datetime import date, datetime, time as dt_time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from autotrade.listener import dedup
from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action

ET = ZoneInfo("America/New_York")


# ============================================================
# 1. ZH 移动止损备注 ≠ 平仓指令（7/23 03:19 原文）
# ============================================================

ZH_STOP_ADJUST = (
    "@everyone\nKC Trades Bot:AVGO 触及下一目标 +37% ✅ 止损设在现价以锁定盈利交易"
)


def test_zh_stop_adjust_note_is_not_close():
    """"止损设在现价以锁定盈利交易" 是移止损备注——"锁定"在止损子句里，不许触发 CLOSE。"""
    assert parse_close(ZH_STOP_ADJUST, {"AVGO"}) is None


def test_zh_stop_adjust_variants_not_close():
    for text in (
        "AVGO 止损移到保本",
        "把止损提到 1.80，AVGO 继续拿",
        "将止损设在入场价",
        "移动止损锁定利润",
    ):
        assert parse_close(text, {"AVGO"}) is None, text


def test_zh_real_trim_with_stop_note_still_closes():
    """真砍仓 + 止损备注：动作动词在止损子句之外，防护不能误伤。"""
    parsed = parse_close("减仓一半AVGO @2.20,止损移到保本", {"AVGO"})
    assert parsed is not None
    assert parsed["symbols"] == ["AVGO"]
    assert parsed["pct"] == 50
    assert parsed["signal_price"] == 2.2


def test_zh_lock_all_phrase_still_closes():
    """enrich 止盈口头禅 "$XOM 全部锁定"（7/17 语料）不受止损防护影响。"""
    parsed = parse_close("丰富：\n$XOM 全部锁定\n\n@everyone", {"XOM"})
    assert parsed is not None
    assert parsed["symbols"] == ["XOM"]


def test_zh_stop_triggered_then_sell_still_closes():
    """止损子句只抹到句读为止："止损触发，全部卖出" 的卖出动作要保留。"""
    parsed = parse_close("止损触发，全部卖出 $AVGO", {"AVGO"})
    assert parsed is not None
    assert parsed["pct"] == 100


# ============================================================
# 2. ZH "出半"（7/23 23:59 NBIS 原文，EN 孪生 "Out half"）
# ============================================================

NBIS_ZH = "enrich:\n$NBIS - 出半\n\n@everyone"
NBIS_EN = "enrich:\n$NBIS - Out half\n\n@everyone"


def test_zh_out_half_routes_to_close():
    assert detect_action(NBIS_ZH) == "CLOSE"


def test_zh_out_half_parses_pct50():
    parsed = parse_close(NBIS_ZH, {"NBIS"})
    assert parsed is not None
    assert parsed["kind"] == "CLOSE"
    assert parsed["symbols"] == ["NBIS"]
    assert parsed["pct"] == 50


def test_out_half_bilingual_fingerprint_parity():
    """EN/ZH 孪生必须解析出相同 (kind, symbols, pct)，否则 CLOSE 指纹 dedup 失效
    （7/10 "减仓一半"=33 vs "out half"=50 的老坑，别在 出半 上重演）。"""
    en = parse_close(NBIS_EN, {"NBIS"})
    zh = parse_close(NBIS_ZH, {"NBIS"})
    assert en is not None and zh is not None
    assert (en["kind"], en["symbols"], en["pct"]) == (zh["kind"], zh["symbols"], zh["pct"])


# ---- 7/23 对抗评审补充：出半 右边界 + 止损掩码边界 ----

def test_zh_out_half_year_commentary_not_close():
    """"冲出半年新高" 是行情评论——裸子串匹配会误卖 50%（对抗评审实锤）。"""
    assert parse_close("$AVGO 冲出半年新高 @3.50，拿住剩下的", {"AVGO"}) is None
    assert parse_close("$NVDA 冲出半年新高", {"NVDA"}) is None
    # 带该评论的开仓消息不许被误路由成 CLOSE（否则开仓信号静默丢失）
    assert detect_action("NVDA 冲出半年新高 买入 NVDA 190c 8/21 @ 2.50") != "CLOSE"


def test_zh_out_half_cang_still_closes():
    parsed = parse_close("$NBIS 出半仓", {"NBIS"})
    assert parsed is not None
    assert parsed["pct"] == 50


def test_zh_stop_clause_space_separated_sell_survives():
    """止损子句后接空格/emoji/破折号分隔的真卖出指令必须活下来（对抗评审实锤：
    掩码原来只认标点，"止损触发 全部卖出" 整句被吞 → 真平仓静默丢失）。"""
    cases = [
        ("止损触发 全部卖出 $AVGO", 100),
        ("止损触发 ❌ 全部卖出 $AVGO", 100),
        ("止损打掉 - 出清 $NBIS", 100),
        ("把止损移到保本 减仓一半 $AVGO @2.20", 50),
        ("止损触发了 $AVGO 的单子，全部卖出", 100),
    ]
    for text, pct in cases:
        parsed = parse_close(text, {"AVGO", "NBIS"})
        assert parsed is not None, text
        assert parsed["pct"] == pct, text


def test_zh_fangzhi_sunshi_not_masked():
    """"防止损失" 是 防止+损失，不是止损调整——左边界 lookbehind（对抗评审实锤）。"""
    parsed = parse_close("为防止损失扩大 卖出 $AVGO", {"AVGO"})
    assert parsed is not None
    assert parsed["symbols"] == ["AVGO"]


def test_zh_stop_note_between_verb_and_price_keeps_price():
    """止损备注夹在卖出指令和价格之间：掩码止于空白，价格必须保留
    （否则可执行的 close 退化成"无价格参照手动接管"）。"""
    parsed = parse_close("卖出 $AVGO 止损位2.0 @2.45", {"AVGO"})
    assert parsed is not None
    assert parsed["signal_price"] == 2.45


# ---- 第二轮对抗评审：recap 顺序 / 出半 左边界 / 掩码白名单 ----

def test_zh_jiang_ba_recap_survives_mask():
    """"将把" 是未来意图 recap 标记——掩码若吃掉它的"把"，计划类消息会变成
    真卖单（第二轮评审 blocker）。recap 判定必须在任何改写之前跑原文。"""
    assert parse_close("我将把止损上移至保本，若跌破就全部卖出 $AVGO", {"AVGO"}) is None
    assert parse_close("接下来将把止损移到成本价，锁定 $AVGO 这笔盈利交易", {"AVGO"}) is None


def test_zh_out_half_ascii_pattern_commentary_not_close():
    """"走出半V型反转" 的 出半 是"走出"的一部分，ASCII 跟随右边界拦不住，
    靠左边界 lookbehind 拦（第二轮评审）。"""
    assert parse_close("$SPY 走出半V型反转，继续持有", {"SPY"}) is None
    assert parse_close("$SPY 走出半 V 型反转，继续持有", {"SPY"}) is None
    assert detect_action("$SPY 走出半V型反转，继续持有") != "CLOSE"


def test_zh_out_half_cang_routes_to_close():
    """"出半仓" 必须能路由（第二轮评审：STRONG_CLOSE_RE 原来漏了 仓 变体，
    parse 能力在生产路径上不可达）。"""
    assert detect_action("enrich:\n$NBIS - 出半仓\n\n@everyone") == "CLOSE"


def test_zh_out_half_cang_price_idiom():
    """"出半仓于2.45" 对齐 7/9 语料 "减半仓于2.45"：pct=50 且价格保留。"""
    parsed = parse_close("出半仓于2.45 $NBIS", {"NBIS"})
    assert parsed is not None
    assert parsed["pct"] == 50
    assert parsed["signal_price"] == 2.45


def test_zh_sell_half_cang_fingerprint():
    """"卖出半仓"：出半 左边界让位给 卖出 动词，pct 靠 半仓 拿 50，
    与 EN 孪生 "sold half"=50 指纹对齐。"""
    parsed = parse_close("卖出半仓 $NBIS @2.45", {"NBIS"})
    assert parsed is not None
    assert parsed["pct"] == 50


def test_zh_stop_clause_cjk_punct_and_glue_separated_sell_survives():
    """掩码白名单字符类：破折号/省略号/emoji/零宽空格粘连的真卖出必须活下来
    （第二轮评审：这些分隔符在否定字符类里穷举不完，白名单反向枚举才稳）。"""
    cases = [
        ("止损打掉——全部卖出 $AVGO", 100),
        ("止损上移…全部卖出 $AVGO", 100),
        ("止损上移✅全部卖出 $AVGO", 100),
        ("止损触发​全部卖出 $AVGO", 100),
    ]
    for text, pct in cases:
        parsed = parse_close(text, {"AVGO"})
        assert parsed is not None, text
        assert parsed["pct"] == pct, text


def test_zh_stop_clause_glued_symbol_survives():
    """掩码止于 $：止损备注粘着 $SYMBOL 时符号必须活下来
    （第二轮评审："减仓一半，止损上移到$NVDA成本线" 原来整个吞掉）。"""
    parsed = parse_close("减仓一半，止损上移到$NVDA成本线", {"NVDA"})
    assert parsed is not None
    assert parsed["symbols"] == ["NVDA"]
    assert parsed["pct"] == 50


# ============================================================
# 3. runner-preserve TG 节流（7/23 夜 AVGO 415c 6 连发）
# ============================================================

def test_runner_preserve_throttle_window():
    dedup._runner_preserve_alerted.clear()
    t0 = datetime.now(timezone.utc)
    label = "AVGO 415.0C"
    # 首次 → 发；窗口内（默认 3600s）重复 → 压掉；别的仓位不受影响；过窗 → 再发
    assert dedup.runner_preserve_should_alert(label, now=t0) is True
    assert dedup.runner_preserve_should_alert(label, now=t0 + timedelta(minutes=30)) is False
    assert dedup.runner_preserve_should_alert("NBIS 250.0C", now=t0 + timedelta(minutes=30)) is True
    assert dedup.runner_preserve_should_alert(label, now=t0 + timedelta(hours=2)) is True


def test_runner_preserve_throttle_env_window(monkeypatch):
    dedup._runner_preserve_alerted.clear()
    monkeypatch.setenv("RUNNER_PRESERVE_ALERT_WINDOW_SEC", "60")
    t0 = datetime.now(timezone.utc)
    assert dedup.runner_preserve_should_alert("X 1C", now=t0) is True
    assert dedup.runner_preserve_should_alert("X 1C", now=t0 + timedelta(seconds=30)) is False
    assert dedup.runner_preserve_should_alert("X 1C", now=t0 + timedelta(seconds=90)) is True


# ============================================================
# 4. 预警 TG 不阻塞下单（7/22 夜 NBIS 1944ms / AVGO 1609ms 延迟）
# ============================================================

AVGO_OPEN_RAW = "@everyone\nKC Trades Bot:LOTTO DAY TRADE\n\nAVGO 415c 2DTE @ 1.65"


async def test_notify_bg_delivers_and_holds_strong_ref(monkeypatch):
    from autotrade.notify import transport

    release = asyncio.Event()
    delivered = []

    async def slow_notify(msg):
        await release.wait()
        delivered.append(msg)

    monkeypatch.setattr(transport, "_safe_notify", slow_notify)
    transport.notify_bg("hello")
    assert len(transport._bg_tasks) == 1  # 强引用在 set 里，GC 吃不掉
    release.set()
    await asyncio.gather(*transport._bg_tasks)
    assert delivered == ["hello"]
    assert not transport._bg_tasks  # done_callback 清理


async def test_signal_alert_does_not_block_order(monkeypatch):
    """慢 TG（预警走 notify_bg → transport._safe_notify）不得垫高下单路径。"""
    from autotrade.listener import open_flow
    from autotrade.notify import transport

    dedup._signal_fps.clear()

    tg_release = asyncio.Event()
    alert_sent = []

    async def slow_transport_notify(msg):
        await tg_release.wait()
        alert_sent.append(msg)

    # 预警走 transport 命名空间（notify_bg 内部）；订单结果等同步通知走
    # open_flow 命名空间的裸名 —— 分开打桩，恰好验证预警不在同步路径上
    monkeypatch.setattr(transport, "_safe_notify", slow_transport_notify)
    sync_notes = []

    async def fast_notify(msg):
        sync_notes.append(msg)

    monkeypatch.setattr(open_flow, "_safe_notify", fast_notify)

    placed = []

    def fake_place_order(signal, qty=None):
        placed.append(signal["symbol"])
        return {"success": True, "order_id": "T1", "code": "US.TEST", "qty": 1, "price": 1.78}

    monkeypatch.setattr(open_flow, "place_order", fake_place_order)
    monkeypatch.setattr(
        open_flow, "check_order",
        lambda **kw: SimpleNamespace(passed=True, reason="", detail=""),
    )
    monkeypatch.setattr(open_flow, "log_order", lambda *a, **kw: None)
    monkeypatch.setattr(open_flow, "record_order", lambda *a, **kw: None)
    monkeypatch.setattr(
        open_flow.position_mgr, "on_order_filled", lambda **kw: None,
    )
    monkeypatch.setattr(open_flow.fill_checker, "spawn", lambda coro: coro.close())

    message = SimpleNamespace(id=20260723)
    cfg = SimpleNamespace(name="KC-期权-波段", default_qty=1, max_price=1000.0)
    t0 = datetime.now(timezone.utc)

    # TG 永远没放行的情况下，整条开仓路径必须在超时内完成
    await asyncio.wait_for(
        open_flow.process_open(
            message, AVGO_OPEN_RAW, cfg, 1517754746427936869, t0, date(2026, 7, 22)
        ),
        timeout=5.0,
    )
    assert placed == ["AVGO"]
    assert alert_sent == []  # 预警仍挂起 —— 证明它没有垫在下单前面

    tg_release.set()
    await asyncio.gather(*transport._bg_tasks)
    assert len(alert_sent) == 1  # 放行后预警照常送达


# ============================================================
# 5. 行情探测：盘外不再误报 delayed-data tier（7/22 22:30 AEST 启动即误报）
# ============================================================

from autotrade.broker.quote import (  # noqa: E402
    QUOTE_DELAYED,
    QUOTE_OK,
    _freshness_verdict,
    _last_rth_close_utc,
)


def _utc(y, m, d, hh, mm, tz=ET):
    return datetime(y, m, d, hh, mm, tzinfo=tz).astimezone(timezone.utc)


def test_freshness_rth_realtime_ok():
    now = _utc(2026, 7, 22, 10, 0)  # 周三盘中
    status, _ = _freshness_verdict(60.0, now, "US.SPY260805C630000")
    assert status == QUOTE_OK


def test_freshness_rth_delayed_flagged():
    now = _utc(2026, 7, 22, 10, 0)
    status, msg = _freshness_verdict(1200.0, now, "US.SPY260805C630000")
    assert status == QUOTE_DELAYED
    assert "delayed" in msg


def test_freshness_premarket_stale_is_indeterminate_ok():
    """7/22 场景重演：ET 盘前 08:30 探测，age≈距昨收 16.5h → 盘外无法判定，不告警。"""
    now = _utc(2026, 7, 22, 8, 30)
    gap = (now - _last_rth_close_utc(now)).total_seconds()
    status, msg = _freshness_verdict(gap + 300, now, "US.SPY260805C630000")
    assert status == QUOTE_OK
    assert "盘外" in msg


def test_freshness_off_hours_truly_stale_still_flagged():
    """盘外但数据比上一交易时段还旧（超 4h 容差）→ 仍要告警。"""
    now = _utc(2026, 7, 22, 8, 30)
    gap = (now - _last_rth_close_utc(now)).total_seconds()
    status, _ = _freshness_verdict(gap + 5 * 3600, now, "US.SPY260805C630000")
    assert status == QUOTE_DELAYED


def test_freshness_weekend_ok():
    now = _utc(2026, 7, 26, 12, 0)  # 周日
    gap = (now - _last_rth_close_utc(now)).total_seconds()
    assert gap > 40 * 3600  # 上一收盘是周五 16:00
    status, _ = _freshness_verdict(gap + 60, now, "US.SPY260805C630000")
    assert status == QUOTE_OK


def test_last_rth_close_walks_back_weekend_and_open_day():
    # 周日 → 上周五 16:00 ET
    close = _last_rth_close_utc(_utc(2026, 7, 26, 12, 0))
    assert close.astimezone(ET).date() == date(2026, 7, 24)
    assert close.astimezone(ET).time() == dt_time(16, 0)
    # 交易日盘后 → 当天 16:00
    close = _last_rth_close_utc(_utc(2026, 7, 22, 18, 0))
    assert close.astimezone(ET).date() == date(2026, 7, 22)
