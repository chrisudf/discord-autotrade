"""配对 OPEN / CLOSE 得到完整 trade 流水，按频道分 CSV

输入：
    data/backfill_open.csv
    data/backfill_close.csv

输出：
    data/trades_KC.csv
    data/trades_enrich.csv
    （channel='other' 是早期测试数据，跳过）

配对逻辑（按频道独立，时间顺序）：
    - 每条 OPEN = 一个 trade
    - 每条 CLOSE 事件匹配 channel + symbol 的所有当前活跃 trade
    - BULK_TRIM 匹配 channel 内所有活跃 trade
    - 一个 trade 可挂多个 close 事件（trim 50% → trim 100% 的 ladder）
    - 累计 close pct >= 100 → 标记 status=closed_full
    - 累计 close pct < 100 且有 close → closed_partial
    - 无 close → never_closed

P&L 计算（无滑点假设）：
    pnl_first_pct  = (first_close_price - open_price) / open_price * 100
    pnl_max_pct    = (max_close_price - open_price) / open_price * 100
    pnl_weighted   = sum(close_pct * close_price) / sum(close_pct) 加权后再算 pnl

CSV 列：
    open_ts, channel, symbol, side, strike, expiry, open_price,
    close_ts, close_price, close_pct, close_lang, close_kind,
    num_closes, max_close_price, total_pct_closed,
    pnl_first_pct, pnl_max_pct, pnl_weighted_pct,
    status, hold_minutes,
    open_raw, close_raws

注意：
    pnl 用 KC 喊的 signal_price，没考虑买入滑点（broker 8-12%）和卖出滑点。
    真实情况下：buy fill ≈ entry × 1.08-1.12，sell fill ≈ signal × 0.95。
    所以 *实际* PnL ≈ pnl_pct - 5% to -15%（取决于哪档 slippage）。

跑法：
    python -m autotrade.ops.analyze_trades
"""
import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

ET_TZ = ZoneInfo("America/New_York")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
OPEN_CSV = DATA_DIR / "backfill_open.csv"
CLOSE_CSV = DATA_DIR / "backfill_close.csv"

OUT_COLS = [
    "open_ts", "channel", "symbol", "side", "strike", "expiry",
    "category", "dte", "open_price", "capital_cost",
    "close_ts", "close_price", "close_pct", "close_lang", "close_kind",
    "num_closes", "max_close_price", "total_pct_closed",
    "pnl_first_pct", "pnl_max_pct", "pnl_weighted_pct",
    "slip_buy_pct", "slip_sell_pct",
    "pnl_first_net_pct", "pnl_max_net_pct",   # 含滑点真实 PnL
    "pnl_source",
    "status", "hold_minutes",
    "open_raw", "close_raws",
]


def buy_slip(price: float) -> float:
    """同 broker._get_slippage_pct：低价合约 spread 宽，需要更大偏移确保 fill。"""
    if price is None:
        return 0.0
    if price < 1.5:
        return 0.12
    if price < 3.0:
        return 0.08
    return 0.05


SELL_SLIP = 0.05  # listener._calc_sell_limit 用的


def net_pnl(gross_pct: float, entry: float) -> float:
    """把毛 PnL（KC 喊的）换算成扣完两端滑点的真实 PnL。

    数学：
      买入实际 ≈ entry * (1 + buy_slip)
      卖出实际 ≈ exit  * (1 - sell_slip)
      gross = (exit - entry) / entry              ← KC 喊的或 signal_price 推的
      net   = (exit*(1-s) - entry*(1+b)) / (entry*(1+b))
            = ((1+gross)*(1-s)) / (1+b) - 1
    """
    if gross_pct is None or entry is None:
        return None
    gross_f = 1 + gross_pct / 100
    b = buy_slip(entry)
    return round((gross_f * (1 - SELL_SLIP) / (1 + b) - 1) * 100, 2)


def _categorize_for_row(open_ts: str, expiry_date_str: str, tags_str: str):
    """复用 positions_db.categorize 给 csv 行打类目 + DTE。"""
    from autotrade.storage.positions_db import categorize
    try:
        open_dt = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
        from datetime import timezone as _tz
        if open_dt.tzinfo is None:
            open_dt = open_dt.replace(tzinfo=_tz.utc)
        open_date_et = open_dt.astimezone(ET_TZ).date()
    except Exception:
        return "", None
    if not expiry_date_str:
        return "", None
    try:
        exp = datetime.fromisoformat(expiry_date_str).date()
    except Exception:
        return "", None
    dte = (exp - open_date_et).days
    tags = [t for t in (tags_str or "").split(";") if t]
    cat, _, _ = categorize(exp, open_date_et, tags)
    return cat, dte


def _parse_ts(s: str) -> datetime:
    """统一返回 tz-aware UTC datetime。

    raw_signals 早期 row（修 bug 前）写入了 naive ts；新 row 带 Z 后缀。
    naive 视作 UTC，避免后续比较报 offset-naive vs offset-aware。
    """
    from datetime import timezone
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _f(x):
    """str → float，空/None → None"""
    if x in (None, "", "None"):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_trades(open_rows: list[dict], close_rows: list[dict]) -> list[dict]:
    """单频道内配对，返回 trade 列表（每个 OPEN = 一条）。"""
    # 时间合并事件流
    events = []
    for r in open_rows:
        events.append((_parse_ts(r["ts_utc"]), "O", r))
    for r in close_rows:
        events.append((_parse_ts(r["ts_utc"]), "C", r))
    events.sort(key=lambda x: x[0])

    # symbol → list of active trade dicts
    active: dict[str, list[dict]] = defaultdict(list)
    trades: list[dict] = []

    for ts, kind, evt in events:
        if kind == "O":
            entry_price = _f(evt["entry_price"])
            cat, dte = _categorize_for_row(
                evt["ts_utc"], evt.get("expiry_date", ""), evt.get("tags", "")
            )
            t = {
                "open_ts": evt["ts_utc"],
                "channel": evt["channel"],
                "symbol": evt["symbol"],
                "side": evt["side"],
                "strike": _f(evt["strike"]),
                "expiry": evt.get("expiry", ""),
                "category": cat,
                "dte": dte if dte is not None else "",
                "open_price": entry_price,
                "open_raw": evt["raw"],
                "_closes": [],
                "_total_pct": 0.0,
            }
            trades.append(t)
            active[evt["symbol"]].append(t)
        else:  # CLOSE
            pct = _f(evt["pct"]) or 0
            if evt["kind"] == "BULK_TRIM":
                targets = [t for queue in active.values() for t in queue]
            else:
                symbols = (evt["symbols"] or "").split(";")
                targets = [t for s in symbols for t in active.get(s, [])]

            for t in targets:
                t["_closes"].append((ts, evt))
                t["_total_pct"] += pct
                # close 达到/超过 100% → 不再活跃
                if t["_total_pct"] >= 100:
                    active[t["symbol"]] = [
                        x for x in active[t["symbol"]] if x is not t
                    ]

    # 生成输出 row
    out = []
    for t in trades:
        cls = t["_closes"]
        open_ts_dt = _parse_ts(t["open_ts"])
        entry = t["open_price"]

        if not cls:
            row = _base_row(t)
            row["status"] = "never_closed"
            out.append(row)
            continue

        # 第一次 close
        first_ts, first_evt = cls[0]
        first_price = _f(first_evt["signal_price"])
        # 所有 close 价格
        prices = [_f(c[1]["signal_price"]) for c in cls]
        prices = [p for p in prices if p is not None]
        max_price = max(prices) if prices else None
        # KC 报告的 PnL（带正负号 / "at entry" 解出）
        pnl_reports = [_f(c[1].get("signal_pnl_pct")) for c in cls]
        pnl_reports = [p for p in pnl_reports if p is not None]
        first_pnl_report = pnl_reports[0] if pnl_reports else None
        max_pnl_report = max(pnl_reports) if pnl_reports else None
        # 加权（按 pct）
        weighted_num = sum(
            (_f(c[1]["pct"]) or 0) * (_f(c[1]["signal_price"]) or 0)
            for c in cls if _f(c[1]["signal_price"]) is not None
        )
        weighted_den = sum(
            _f(c[1]["pct"]) or 0
            for c in cls if _f(c[1]["signal_price"]) is not None
        )
        weighted_price = (weighted_num / weighted_den) if weighted_den else None

        hold_min = (first_ts - open_ts_dt).total_seconds() / 60

        def _pnl_from_price(p):
            if p is None or entry is None or entry == 0:
                return None
            return round((p - entry) / entry * 100, 2)

        # PnL 来源优先级：
        # 1. signal_price 算（最准，能反映真实卖价）
        # 2. KC 报告的 signal_pnl_pct（隐含相对 entry，不一定跟我们买入价对齐）
        # 3. 都无 → None
        if first_price is not None:
            pnl_first = _pnl_from_price(first_price)
            pnl_source = "signal_price"
        elif first_pnl_report is not None:
            pnl_first = first_pnl_report
            pnl_source = "signal_pnl"
        else:
            pnl_first = None
            pnl_source = "none"

        if max_price is not None:
            pnl_max = _pnl_from_price(max_price)
        elif max_pnl_report is not None:
            pnl_max = max_pnl_report
        else:
            pnl_max = None

        pnl_weighted = _pnl_from_price(weighted_price)

        # 状态
        if t["_total_pct"] >= 100:
            status = "closed_full"
        elif prices or pnl_reports:
            status = "closed_partial"
        else:
            status = "no_exit_price"

        row = _base_row(t)
        row.update({
            "close_ts": first_evt["ts_utc"],
            "close_price": first_price,
            "close_pct": int(_f(first_evt["pct"]) or 0),
            "close_lang": first_evt.get("lang", ""),
            "close_kind": first_evt.get("kind", ""),
            "num_closes": len(cls),
            "max_close_price": max_price,
            "total_pct_closed": int(t["_total_pct"]),
            "pnl_first_pct": pnl_first,
            "pnl_max_pct": pnl_max,
            "pnl_weighted_pct": pnl_weighted,
            "pnl_first_net_pct": net_pnl(pnl_first, entry),
            "pnl_max_net_pct": net_pnl(pnl_max, entry),
            "pnl_source": pnl_source,
            "status": status,
            "hold_minutes": round(hold_min, 1),
            "close_raws": " || ".join(c[1]["raw"] for c in cls)[:400],
        })
        out.append(row)
    return out


def _base_row(t: dict) -> dict:
    entry = t["open_price"]
    slip_b = round(buy_slip(entry) * 100, 1) if entry else ""
    capital = round(entry * (1 + buy_slip(entry)) * 100, 2) if entry else ""
    return {
        "open_ts": t["open_ts"],
        "channel": t["channel"],
        "symbol": t["symbol"],
        "side": t["side"],
        "strike": t["strike"],
        "expiry": t["expiry"],
        "category": t.get("category", ""),
        "dte": t.get("dte", ""),
        "open_price": entry,
        "capital_cost": capital,
        "slip_buy_pct": slip_b,
        "slip_sell_pct": round(SELL_SLIP * 100, 1),
        "open_raw": t["open_raw"],
        "close_ts": "", "close_price": "", "close_pct": "", "close_lang": "",
        "close_kind": "", "num_closes": 0, "max_close_price": "",
        "total_pct_closed": 0, "pnl_first_pct": "", "pnl_max_pct": "",
        "pnl_weighted_pct": "", "pnl_source": "",
        "pnl_first_net_pct": "", "pnl_max_net_pct": "",
        "hold_minutes": "", "close_raws": "",
    }


def _agg(vals):
    """安全聚合数值列表，返回 (n, avg, min, max)。空时 (0, None, None, None)。"""
    nums = [v for v in vals if isinstance(v, (int, float))]
    if not nums:
        return 0, None, None, None
    return len(nums), sum(nums) / len(nums), min(nums), max(nums)


def summarize(channel: str, trades: list[dict]) -> list[str]:
    """返回 markdown 段落列表。"""
    lines = [f"\n## {channel}"]
    n = len(trades)
    closed = [t for t in trades if t["status"] in ("closed_full", "closed_partial")]
    never = [t for t in trades if t["status"] == "never_closed"]
    no_px = [t for t in trades if t["status"] == "no_exit_price"]
    with_pnl = [t for t in closed if isinstance(t["pnl_first_pct"], (int, float))]

    lines.append(
        f"- 总 trade: **{n}**（closed {len(closed)} / never {len(never)} / no-price {len(no_px)}）"
    )
    if not with_pnl:
        lines.append("- ⚠️ 无可算 PnL 的 trade")
        for line in lines:
            print(line)
        return lines

    # 毛 PnL
    n_gross, avg_g, _, _ = _agg([t["pnl_first_pct"] for t in with_pnl])
    _, avg_max_g, _, _ = _agg([t["pnl_max_pct"] for t in with_pnl])
    wins_gross = sum(1 for t in with_pnl if t["pnl_first_pct"] > 0)

    # 净 PnL (含滑点)
    n_net, avg_n, _, _ = _agg([t["pnl_first_net_pct"] for t in with_pnl])
    _, avg_max_n, _, _ = _agg([t["pnl_max_net_pct"] for t in with_pnl])
    wins_net = sum(
        1 for t in with_pnl
        if isinstance(t["pnl_first_net_pct"], (int, float)) and t["pnl_first_net_pct"] > 0
    )

    # Hold time
    _, avg_hold, min_hold, max_hold = _agg([t["hold_minutes"] for t in with_pnl])

    lines.append("")
    lines.append(f"### 毛 PnL（KC 信号价 / 报告 PnL，无滑点）")
    lines.append(f"- 胜率 first-close: **{wins_gross/n_gross*100:.0f}%** ({wins_gross}/{n_gross})")
    lines.append(f"- 平均 first-close: **{avg_g:+.2f}%**")
    lines.append(f"- 平均 max-close:   **{avg_max_g:+.2f}%**")

    lines.append("")
    lines.append(f"### 净 PnL（扣 broker 买入 8-12% / 卖出 5% 滑点）")
    lines.append(f"- 胜率 first-close: **{wins_net/n_net*100:.0f}%** ({wins_net}/{n_net})")
    lines.append(f"- 平均 first-close: **{avg_n:+.2f}%**")
    lines.append(f"- 平均 max-close:   **{avg_max_n:+.2f}%**")

    lines.append("")
    lines.append(f"### 持仓时长（min）")
    lines.append(f"- 平均 {avg_hold:.0f} / 最短 {min_hold:.0f} / 最长 {max_hold:.0f}")

    # 按 category 分
    from collections import defaultdict
    by_cat = defaultdict(list)
    for t in with_pnl:
        by_cat[t.get("category", "?")].append(t)
    if len(by_cat) > 1:
        lines.append("")
        lines.append(f"### 按 category 分")
        for cat, ts in sorted(by_cat.items()):
            n_, avg_, _, _ = _agg([x["pnl_first_pct"] for x in ts])
            _, avg_n_, _, _ = _agg([x["pnl_first_net_pct"] for x in ts])
            lines.append(f"- **{cat}** (n={n_}): 毛 {avg_:+.2f}% / 净 {avg_n_:+.2f}%")

    # Trade detail（小样本时全列）
    if n <= 12:
        lines.append("")
        lines.append("### Trade 明细")
        lines.append("| symbol | side | strike | expiry | cat | DTE | entry | first_close | gross | net | status |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for t in trades:
            g = t.get("pnl_first_pct"); g = f"{g:+.2f}%" if isinstance(g,(int,float)) else "—"
            n_ = t.get("pnl_first_net_pct"); n_ = f"{n_:+.2f}%" if isinstance(n_,(int,float)) else "—"
            close = t.get("close_price") or t.get("pnl_source") or "—"
            lines.append(
                f"| {t['symbol']} | {t['side'][:1]} | {t['strike']} | "
                f"{t['expiry']} | {t.get('category','')} | {t.get('dte','')} | "
                f"{t['open_price']} | {close} | {g} | {n_} | {t['status']} |"
            )

    for line in lines:
        print(line)
    return lines


def main():
    opens = list(csv.DictReader(open(OPEN_CSV)))
    closes = list(csv.DictReader(open(CLOSE_CSV)))

    by_ch_open = defaultdict(list)
    by_ch_close = defaultdict(list)
    for r in opens:
        by_ch_open[r["channel"]].append(r)
    for r in closes:
        by_ch_close[r["channel"]].append(r)

    # CLOSE 可能在多 channel 间漂移（BULK_TRIM 或翻译版本归到不同 channel），
    # 处理时仍按 channel 独立配对，避免 KC 喊的 close 错配到 enrich 的 open。

    report_lines = [
        "# 回测报告",
        f"生成时间: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "**口径说明**：",
        "- 毛 PnL = KC 喊的卖出价 / KC 报告 PnL（-15% / at entry），不含滑点",
        "- 净 PnL = ((1 + 毛/100) × (1 - 5%) / (1 + buy_slip) - 1) × 100",
        "- buy_slip 按 broker 实际档：<$1.5→12% / <$3→8% / ≥$3→5%",
        "- 仅统计有 close 信号且 (signal_price 或 signal_pnl_pct 可解) 的 trade",
    ]

    for channel in ("KC-期权-波段", "enrich"):
        out_path = DATA_DIR / f"trades_{channel.replace('-', '_')}.csv"
        opens_ch = by_ch_open.get(channel, [])
        closes_ch = by_ch_close.get(channel, [])
        trades = build_trades(opens_ch, closes_ch)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=OUT_COLS, extrasaction="ignore")
            w.writeheader()
            for t in trades:
                w.writerow(t)
        logger.info(f"wrote {len(trades)} trades → {out_path.name}")
        report_lines.extend(summarize(channel, trades))

    report_path = DATA_DIR / "backtest_report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    logger.info(f"wrote report → {report_path}")


if __name__ == "__main__":
    main()
