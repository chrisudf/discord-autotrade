"""已实现盈亏账本：从 position_events 还原每一笔开仓 / 平仓 / 盈亏。

存在的理由：**这个数字以前从来没被算过。** 9/10、9/11、9/12 三份复盘里的
"已实现 -$112"、"-$1,478" 全是手算的，而手算意味着每次口径可能都不一样、
没人能复现、也没法按频道回答"这三个频道哪个在赚钱"。

既有三个 ops 工具一个都答不了：
  - analyze_trades.py  读 data/backfill_*.csv（文件不存在，跑不起来），而且算的是
                       **喊单员喊价**的 PnL，不是我们的成交价 —— 两者隔着 8-12%
                       买入滑点 + 5-10% 卖出滑点，是两个不同的问题；
  - generate_report.py 只统计 orders 表的条数/成功数（且 orders **只记买单**）；
  - show_today.py      列原始信号。

口径（每一条都是刻意的，改之前先读完）：

1. **成本锚在 `positions.avg_entry_price`，不是 OPEN 事件的 price。**
   OPEN 记的是**挂单限价**，真实成交价由 fill_checker 事后回填成 FILL_ADJUST
   （61 条），avg_entry_price 是被它维护的那个数。拿 OPEN.price 算会系统性
   高估成本（9/10 夜 MSTR：OPEN 2.86 是限价，实成 2.78）。加仓（ADD_ON）
   同理由 avg_entry_price 摊平。

2. **按卖出腿逐条实现，归到该腿自己的 ET 交易日。**
   realized = qty × (exit_price - avg_entry) × 100。一笔 9/8 开、9/9 平的仓，
   盈亏算在 9/9 —— 这是"当天赚了多少"的口径。部分平仓天然被它处理。

3. **EXPIRE 的 price 是 NULL，按 0 计**（真归零，不是缺数据），**且归日用
   合约到期日、不用事件时间戳**。expiry_sweep 是"凌晨跨日后就清"，跑在
   **次日早晨**：LITE 1030C 9/11 到期，EXPIRE 事件的 ts 是 9/12 09:11 ET。
   按时间戳归日会让每一笔过期损失都落到后一天 —— 到期日当天系统性少记、
   次日凭空多出一笔。第一版就是这么写的，对 9/11 少算了 $460（与当晚复盘
   手算的 -$1,478 差的正是这个数），靠交叉验证才发现。

4. **`broker_sync` 且 price=0 的平仓单独列出，不计入合计。**
   那是 reconciler 的自动落账，日志自己标着"非成交价"（9/10 夜 AMZN 250C
   就是这么被误平的，在库里留下一笔 -$630 的假账）。把它算进总盈亏，
   等于让一次读数错误永久污染业绩统计。**闸门 4 只防复发，不追溯**，
   所以这一条要长期留着。

5. **未了结的 qty_remaining 不算盈亏**，单列成"在途成本"——它既不是赚也不是亏。

跑法：
    python -m autotrade.ops.pnl                  # 全部历史
    python -m autotrade.ops.pnl --since 2026-09-08
    python -m autotrade.ops.pnl --et-date 2026-09-11   # 单个交易日
    python -m autotrade.ops.pnl --csv out.csv
"""
import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "trades.db"

CONTRACT_MULTIPLIER = 100
EXIT_TYPES = ("TRIM", "CLOSE", "EXPIRE")


def et_date(ts: str) -> str:
    """position_events.ts 是 UTC ISO（_utc_iso()）→ ET 自然日。

    必须转时区：本机是 AEST，直接切字符串会把 09:38 ET 的成交算到第二天
    （9/10 复盘里那些 ET 换算全是手工做的，这里固化下来）。
    """
    s = (ts or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return (ts or "")[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET_TZ).date().isoformat()


def is_fabricated(row: dict) -> bool:
    """reconciler 自动落账写的 0 价 —— 不是成交价（见模块 docstring 口径 4）。"""
    return (row.get("trigger_source") == "broker_sync"
            and not row.get("price"))


def exit_legs(events: list, positions: dict) -> list:
    """把卖出事件铺成逐腿账本。纯函数：喂 dict 就能测，不碰 DB。"""
    out = []
    for e in events:
        if e["event_type"] not in EXIT_TYPES or e["qty_delta"] >= 0:
            continue
        pos = positions.get(e["option_code"])
        if not pos:
            continue
        qty = -e["qty_delta"]
        entry = float(pos["avg_entry_price"] or 0.0)
        price = float(e["price"] or 0.0)          # EXPIRE 的 NULL → 0，真归零
        cost = entry * qty * CONTRACT_MULTIPLIER
        # 归日：过期用**合约到期日**，其余用成交时间戳（见口径 2 / 3）
        day = (pos.get("expiry") or et_date(e["ts"])) \
            if e["event_type"] == "EXPIRE" else et_date(e["ts"])
        out.append({
            "et_date": day,
            "option_code": e["option_code"],
            "channel": pos.get("channel_name") or "?",
            "category": pos.get("category") or "?",
            "event": e["event_type"],
            "trigger": e.get("trigger_source") or "",
            "qty": qty,
            "entry": round(entry, 2),
            "exit": round(price, 2),
            "cost": round(cost, 2),
            "realized": round((price - entry) * qty * CONTRACT_MULTIPLIER, 2),
            "fabricated": is_fabricated(e),
        })
    return out


def open_exposure(positions: dict) -> list:
    """未了结的在途成本 —— 既不是赚也不是亏，单列。"""
    return [
        {"option_code": c, "channel": p.get("channel_name") or "?",
         "category": p.get("category") or "?", "qty": p["qty_remaining"],
         "entry": round(float(p["avg_entry_price"] or 0), 2),
         "cost": round(float(p["avg_entry_price"] or 0) * p["qty_remaining"]
                       * CONTRACT_MULTIPLIER, 2),
         "expiry": p.get("expiry") or "?"}
        for c, p in sorted(positions.items())
        if (p.get("qty_remaining") or 0) > 0
    ]


def group_sum(legs: list, key: str) -> list:
    """按某个维度汇总（捏造价的腿不进合计，见口径 4）。"""
    agg = defaultdict(lambda: {"n": 0, "qty": 0, "cost": 0.0, "realized": 0.0})
    for l in legs:
        if l["fabricated"]:
            continue
        a = agg[l[key]]
        a["n"] += 1
        a["qty"] += l["qty"]
        a["cost"] += l["cost"]
        a["realized"] += l["realized"]
    rows = []
    for k, a in agg.items():
        pct = (a["realized"] / a["cost"] * 100) if a["cost"] else 0.0
        rows.append({key: k, **{kk: round(vv, 2) for kk, vv in a.items()},
                     "pct": round(pct, 1)})
    return sorted(rows, key=lambda r: r[key])


def load(db_path=DB_PATH, since=None, until=None):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    positions = {r["option_code"]: dict(r)
                 for r in conn.execute("SELECT * FROM positions")}
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM position_events ORDER BY ts")]
    conn.close()
    legs = exit_legs(events, positions)
    if since:
        legs = [l for l in legs if l["et_date"] >= since]
    if until:
        legs = [l for l in legs if l["et_date"] <= until]
    return legs, positions


def format_report(legs: list, positions: dict, show_legs=True) -> str:
    good = [l for l in legs if not l["fabricated"]]
    bad = [l for l in legs if l["fabricated"]]
    out = []
    if show_legs:
        out.append("## 逐腿账本（ET 交易日）")
        out.append(f"{'ET日期':<11}{'频道':<15}{'合约':<24}{'事件':<7}"
                   f"{'张':>3}{'入':>7}{'出':>7}{'成本':>9}{'已实现':>9}")
        for l in sorted(legs, key=lambda x: (x["et_date"], x["option_code"])):
            flag = "  ⚠️捏造价" if l["fabricated"] else ""
            out.append(
                f"{l['et_date']:<11}{l['channel']:<15}{l['option_code']:<24}"
                f"{l['event']:<7}{l['qty']:>3}{l['entry']:>7.2f}{l['exit']:>7.2f}"
                f"{l['cost']:>9.0f}{l['realized']:>+9.0f}{flag}")
        out.append("")

    total = sum(l["realized"] for l in good)
    cost = sum(l["cost"] for l in good)
    pct = (total / cost * 100) if cost else 0.0
    out.append("## 合计")
    out.append(f"  已实现盈亏 : ${total:+,.0f}   "
               f"（投入成本 ${cost:,.0f}，回报 {pct:+.1f}%，{len(good)} 条卖出腿）")

    exposure = open_exposure(positions)
    if exposure:
        ex_cost = sum(e["cost"] for e in exposure)
        out.append(f"  在途未了结 : ${ex_cost:,.0f}（{len(exposure)} 个仓位，"
                   f"既不是赚也不是亏）")
        for e in exposure:
            out.append(f"      {e['option_code']:<24}{e['channel']:<15}"
                       f"{e['qty']} 张 @ {e['entry']:.2f}  到期 {e['expiry']}")

    if bad:
        out.append("")
        out.append(f"  ⚠️ 另有 {len(bad)} 条 **捏造价** 平仓未计入合计"
                   f"（reconciler 自动落账写的 0，不是成交价）：")
        for l in bad:
            out.append(f"      {l['et_date']}  {l['option_code']}  "
                       f"{l['qty']} 张 @ 入 {l['entry']:.2f} —— "
                       f"若按 0 计会虚记 ${-l['entry'] * l['qty'] * 100:+,.0f}")

    for key, title in (("channel", "按频道"), ("category", "按类目"),
                       ("et_date", "按 ET 交易日")):
        rows = group_sum(good, key)
        if not rows:
            continue
        out.append("")
        out.append(f"## {title}")
        for r in sorted(rows, key=lambda x: x["realized"]):
            out.append(f"  {str(r[key]):<15}{r['realized']:>+10,.0f}  "
                       f"（成本 ${r['cost']:>9,.0f}  {r['pct']:>+6.1f}%  "
                       f"{r['n']} 腿）")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="已实现盈亏账本")
    ap.add_argument("--since", help="ET 起始日 YYYY-MM-DD")
    ap.add_argument("--until", help="ET 截止日 YYYY-MM-DD")
    ap.add_argument("--et-date", help="只看这一个 ET 交易日")
    ap.add_argument("--csv", help="逐腿账本写到 CSV")
    ap.add_argument("--no-legs", action="store_true", help="只看汇总")
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args(argv)
    since, until = (a.et_date, a.et_date) if a.et_date else (a.since, a.until)

    legs, positions = load(a.db, since, until)
    if a.csv and legs:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(legs[0].keys()))
            w.writeheader()
            w.writerows(legs)
        print(f"逐腿账本 → {a.csv}（{len(legs)} 行）")
    if not legs:
        print("（该区间没有平仓腿）")
        # 仍然打在途，否则"没有平仓"会被误读成"没有仓位"
        print(format_report([], positions, show_legs=False))
        return 0
    print(format_report(legs, positions, show_legs=not a.no_legs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
