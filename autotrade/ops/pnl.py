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

5. **未了结的 qty_remaining** 默认单列成"在途成本"；加 `--mark` 用实时报价
   算未实现盈亏（默认不取价：morning_collect 07:00 本地 = 17:00 ET，盘已收、
   listener 刚被 SIGTERM，OpenD 多半也不在，取不到价是常态。摘要那条路径的
   契约是"只读 sqlite"，不能因为多一个数字就让它依赖 broker）。

6. **佣金按张计、买卖各一次。** REAL 下 moomoo 美股期权按合约张数收（佣金 +
   平台费），历史数据全是 SIMULATE 不计费，所以本工具同时给**毛利**（当时
   真实发生的）和**净额**（按 REAL 费率折算"如果这是真钱"）。两个都打出来，
   因为它们回答的是两个问题。
   **过期的合约没有卖出腿 → 只收买入那一次费。** 这条不是细节：一张最后卖在
   $0.01 的合约，卖出腿的费用就超过它的成交额本身（见 lesson #48 的便士单）。
   费率走 env，且**打在报表抬头上** —— 费率错了整张表都错，必须一眼看见。

跑法：
    python -m autotrade.ops.pnl                  # 全部历史
    python -m autotrade.ops.pnl --since 2026-09-08
    python -m autotrade.ops.pnl --et-date 2026-09-11   # 单个交易日
    python -m autotrade.ops.pnl --mark                 # 未了结仓位按实时报价标记
    python -m autotrade.ops.pnl --csv out.csv

费率（env，缺省是 moomoo 美股期权常见零售档，**上线前请按你的实际账户核对**）：
    OPT_COMMISSION_PER_CONTRACT=0.65
    OPT_PLATFORM_FEE_PER_CONTRACT=0.30
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


def fee_per_contract() -> float:
    """单张单腿费用 = 佣金 + 平台费。缺省是 moomoo 美股期权常见零售档。

    写坏了不静默退默认：费率是钱路口径，打印在抬头上让人自己核。
    """
    import os
    def _f(name, default):
        try:
            return max(0.0, float(os.getenv(name, default)))
        except ValueError:
            return float(default)
    return _f("OPT_COMMISSION_PER_CONTRACT", 0.65) + \
        _f("OPT_PLATFORM_FEE_PER_CONTRACT", 0.30)


def leg_fee(qty: int, event_type: str, per_contract: float) -> float:
    """一条卖出腿摊到的总费用（买入那次 + 卖出那次）。

    **过期没有卖出腿，只收买入一次。** 这条决定了"卖在 0.01 到底值不值"：
    一张 0.01 成交额 = $1，而卖出腿光费用就 ~$0.95/张 —— 净收益趋近于零，
    甚至为负（见 lesson #48）。把过期也收两次费会把这个结论算反。
    """
    legs = 1 if event_type == "EXPIRE" else 2
    return round(qty * per_contract * legs, 2)


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


def exit_legs(events: list, positions: dict,
              per_contract: "float | None" = None) -> list:
    """把卖出事件铺成逐腿账本。纯函数：喂 dict 就能测，不碰 DB。

    per_contract 缺省读 env（见 fee_per_contract）；测试里直接传值。
    """
    per_contract = fee_per_contract() if per_contract is None else per_contract
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
            "fee": leg_fee(qty, e["event_type"], per_contract),
            "net": round((price - entry) * qty * CONTRACT_MULTIPLIER
                         - leg_fee(qty, e["event_type"], per_contract), 2),
            "fabricated": is_fabricated(e),
        })
    return out


def mark_open(positions: dict, marks: dict,
              per_contract: "float | None" = None) -> list:
    """未了结仓位按实时报价标记。marks: {option_code: last|None}。

    取不到价的**不猜**（照 CLOSE 无价拒卖同一哲学）：标成 None 单列，
    绝不拿 entry 或上次的价顶上去 —— 一个编出来的浮盈比没有数字更糟。
    卖出侧的费用算进去：未实现盈亏的意义是"现在平掉能拿回多少"。
    """
    per_contract = fee_per_contract() if per_contract is None else per_contract
    out = []
    for code, p in sorted(positions.items()):
        qty = p.get("qty_remaining") or 0
        if qty <= 0:
            continue
        entry = float(p["avg_entry_price"] or 0.0)
        mark = marks.get(code)
        cost = round(entry * qty * CONTRACT_MULTIPLIER, 2)
        row = {"option_code": code, "channel": p.get("channel_name") or "?",
               "category": p.get("category") or "?", "qty": qty,
               "entry": round(entry, 2), "expiry": p.get("expiry") or "?",
               "cost": cost, "mark": None, "unrealized": None, "net": None}
        if mark is not None:
            fee = round(qty * per_contract, 2)   # 只剩卖出那一腿
            row["mark"] = round(float(mark), 2)
            row["unrealized"] = round(
                (float(mark) - entry) * qty * CONTRACT_MULTIPLIER, 2)
            row["net"] = round(row["unrealized"] - fee, 2)
        out.append(row)
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
    agg = defaultdict(lambda: {"n": 0, "qty": 0, "cost": 0.0,
                               "realized": 0.0, "fee": 0.0, "net": 0.0})
    for l in legs:
        if l["fabricated"]:
            continue
        a = agg[l[key]]
        a["n"] += 1
        for f in ("qty", "cost", "realized", "fee", "net"):
            a[f] += l[f]
    rows = []
    for k, a in agg.items():
        pct = (a["net"] / a["cost"] * 100) if a["cost"] else 0.0
        rows.append({key: k, **{kk: round(vv, 2) for kk, vv in a.items()},
                     "pct": round(pct, 1)})
    return sorted(rows, key=lambda r: r[key])


def fetch_marks_stale(codes: list) -> dict:
    """看盘用的取价：**绕过 60s 新鲜度门**，返回 {code: (price, age_sec)}。

    为什么要有第二条路：QUOTE_FRESHNESS_SEC 那道门是保护**下单定价**的
    （按几分钟前的 bid 挂单等于白送，见 get_sell_ref_price 的注释），
    而"我这两张现在什么价"是**看盘**不是下单 —— 盘前/周末跑的时候，
    上一个收盘价是完全有意义的信息，被门挡掉只会逼人去写临时脚本。

    所以这条路只在报表里用，**永远不进任何下单路径**，且输出必须带上
    报价年龄：一个不标年龄的陈旧价，和一个实时价长得一模一样。
    bid 优先（卖出参照），无 bid 退 last —— 与 get_sell_ref_price 同序。
    """
    if not codes:
        return {}
    _load_env()
    out = {}
    try:
        import time as _t
        from autotrade.broker import quote as q
        if q._is_dry_run():
            print("（DRY_RUN 生效，不取真实报价）", file=sys.stderr)
            return {}
        # **一次批量**，不是逐个 describe_quote：后者是 N 次单 code snapshot，
        # 4 个 code 就撞上了限频退避（实测挂住 >100s）。既有代码对 snapshot
        # 配额很在意（见 get_sell_ref_price 的"不开第二条 snapshot 路径"），
        # 这里同样只打一次。
        ret, df = q._snapshot(list(codes))
        if ret != q.RET_OK or df is None or not hasattr(df, "iterrows"):
            print(f"（snapshot 失败: {str(df)[:120]}）", file=sys.stderr)
            return {}
        import pandas as pd
        now = _t.time()
        for _, row in df.iterrows():
            code = row.get("code")
            if code not in codes:
                continue
            def _n(v):
                return None if (v is None or pd.isna(v)) else float(v)
            bid, last = _n(row.get("bid_price")), _n(row.get("last_price"))
            ref = bid if (bid and bid > 0) else last
            if not ref or ref <= 0:
                continue
            age = None
            try:
                age = now - q._quote_epoch(row["update_time"])
            except Exception:
                pass
            out[code] = (ref, age)
    except Exception as e:
        print(f"（取价失败: {type(e).__name__}: {e}）", file=sys.stderr)
    return out


def _load_env() -> None:
    """取价前必须先加载 config/.env。

    [9/14 实锤] 第一版漏了这一步，后果不是报错而是**静默说谎**：
    quote._is_dry_run() 在 DRY_RUN 未设时默认 True（对下单是安全默认），
    于是 get_last_prices 走 mock 分支、对每个 code 返回 None，报表整整齐齐
    打出四行"（无报价）"。我当场把它解释成"新鲜度门挡掉了盘前陈旧报价"——
    听起来完全合理，而真相是**根本没去查**。同一时刻直接调 _snapshot 拿到的是
    TSLA last=2.78 bid=2.75、AVGO last=0.04 bid=0.02。
    一个"查了但没有"和"压根没查"长得一模一样的输出，是这类脚本最坏的失败形态。
    其它 ops 脚本（generate_report / reset_daily_limit / backtest_parser）
    都在 main() 里 load_dotenv，这里对齐它们。
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env",
                    override=False)
    except Exception:
        pass      # 没装 dotenv / 没有 .env 都不该炸掉账本的只读部分


def fetch_marks(codes: list) -> dict:
    """给未了结仓位取实时报价。**只在 --mark 时调用** —— 见口径 5。"""
    if not codes:
        return {}
    _load_env()
    try:
        from autotrade.broker.quote import _is_dry_run, get_last_prices
        if _is_dry_run():
            # 不许把"没查"伪装成"查了没有"——见 _load_env 的注释
            print("（DRY_RUN 生效，不取真实报价；未实现盈亏留空）",
                  file=sys.stderr)
            return {}
        return get_last_prices(codes)
    except Exception as e:      # OpenD 不在 / 无权限 / 收盘 —— 都不该炸掉账本
        print(f"（取价失败，未实现盈亏留空: {type(e).__name__}: {e}）",
              file=sys.stderr)
        return {}


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


def format_report(legs: list, positions: dict, show_legs=True,
                  marks: "dict | None" = None, ages: "dict | None" = None) -> str:
    per = fee_per_contract()
    good = [l for l in legs if not l["fabricated"]]
    bad = [l for l in legs if l["fabricated"]]
    out = [f"（费率：${per:.2f}/张/腿 —— 佣金 + 平台费，"
           f"env OPT_COMMISSION_PER_CONTRACT / OPT_PLATFORM_FEE_PER_CONTRACT；"
           f"历史成交是 SIMULATE 不计费，净额是按 REAL 费率折算）", ""]

    if show_legs and legs:
        out.append("## 逐腿账本（ET 交易日）")
        out.append(f"{'ET日期':<11}{'频道':<15}{'合约':<24}{'事件':<7}"
                   f"{'张':>3}{'入':>7}{'出':>7}{'成本':>9}{'毛利':>8}{'费':>7}{'净额':>9}")
        for l in sorted(legs, key=lambda x: (x["et_date"], x["option_code"])):
            flag = "  ⚠️捏造价" if l["fabricated"] else ""
            out.append(
                f"{l['et_date']:<11}{l['channel']:<15}{l['option_code']:<24}"
                f"{l['event']:<7}{l['qty']:>3}{l['entry']:>7.2f}{l['exit']:>7.2f}"
                f"{l['cost']:>9.0f}{l['realized']:>+8.0f}{l['fee']:>7.2f}"
                f"{l['net']:>+9.0f}{flag}")
        out.append("")

    gross = sum(l["realized"] for l in good)
    fees = sum(l["fee"] for l in good)
    net = sum(l["net"] for l in good)
    cost = sum(l["cost"] for l in good)
    out.append("## 已实现")
    out.append(f"  毛利     : ${gross:+,.0f}   （投入成本 ${cost:,.0f}，"
               f"{len(good)} 条卖出腿）")
    out.append(f"  佣金      : ${fees:,.2f}  （按张按腿；过期无卖出腿只收一次）")
    out.append(f"  **净额**  : ${net:+,.0f}   "
               f"（回报 {(net / cost * 100) if cost else 0:+.1f}%）")

    rows = mark_open(positions, marks or {}, per)
    if rows:
        out.append("")
        priced = [r for r in rows if r["mark"] is not None]
        ex_cost = sum(r["cost"] for r in rows)
        out.append(f"## 未了结（{len(rows)} 个仓位，在途成本 ${ex_cost:,.0f}）")
        if marks is None:
            out.append("  （未取报价 —— 加 --mark 标记未实现盈亏）")
        for r in rows:
            if r["mark"] is None:
                mk = "     —        —        —   " + (
                    "（无报价）" if marks is not None else "")
            else:
                age = (ages or {}).get(r["option_code"])
                # 不标年龄的陈旧价和实时价长得一模一样 —— 必须标
                age_s = ""
                if age is not None and age > 60:
                    age_s = (f"  ⏳{age/3600:.1f}h前" if age < 86400
                             else f"  ⏳{age/86400:.1f}天前")
                mk = (f"{r['mark']:>7.2f}{r['unrealized']:>+9.0f}"
                      f"{r['net']:>+9.0f}{age_s}")
            out.append(f"  {r['option_code']:<24}{r['channel']:<15}"
                       f"{r['qty']:>2}张 @{r['entry']:>6.2f}  到期 {r['expiry']}"
                       f"  {mk}")
        if priced:
            u_gross = sum(r["unrealized"] for r in priced)
            u_net = sum(r["net"] for r in priced)
            out.append(f"  未实现小计（{len(priced)}/{len(rows)} 个有报价）: "
                       f"毛 ${u_gross:+,.0f} / 净 ${u_net:+,.0f}")
            if len(priced) < len(rows):
                out.append("  ⚠️ 有仓位取不到价，**不猜**：上面的小计不含它们")
            out.append(f"  合计（已实现净额 + 未实现净额）: ${net + u_net:+,.0f}")

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
        grp = group_sum(good, key)
        if not grp:
            continue
        out.append("")
        out.append(f"## {title}（净额）")
        for r in sorted(grp, key=lambda x: x["net"]):
            out.append(f"  {str(r[key]):<15}{r['net']:>+10,.0f}  "
                       f"（毛 {r['realized']:>+8,.0f}  费 {r['fee']:>7,.2f}  "
                       f"成本 ${r['cost']:>9,.0f}  {r['pct']:>+6.1f}%  {r['n']} 腿）")
    return "\n".join(out)


def _release_quote_ctx() -> None:
    """取完价把 quote 链路关掉。

    [9/14 实锤] 不关的后果不是报错：报表整整 66 行**全都打完了**，然后进程
    挂在那儿退不出去（OpenQuoteContext 起了非 daemon 线程），最后被 timeout
    以 exit=124 杀掉。放在 morning_collect 那种脚本里就是每天早上挂一个僵尸
    进程 —— 而且因为输出是完整的，看起来完全正常。
    """
    try:
        from autotrade.broker.quote import _reset_quote_ctx
        _reset_quote_ctx()
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="已实现 + 未实现盈亏账本")
    ap.add_argument("--since", help="ET 起始日 YYYY-MM-DD")
    ap.add_argument("--until", help="ET 截止日 YYYY-MM-DD")
    ap.add_argument("--et-date", help="只看这一个 ET 交易日")
    ap.add_argument("--mark", action="store_true",
                    help="给未了结仓位取实时报价，算未实现盈亏（需要 OpenD）")
    ap.add_argument("--stale-ok", action="store_true",
                    help="配合 --mark：绕过 60s 新鲜度门，用上次收盘价看盘"
                         "（盘前/周末唯一能看到数字的方式，输出会标报价年龄）")
    ap.add_argument("--csv", help="逐腿账本写到 CSV")
    ap.add_argument("--no-legs", action="store_true", help="只看汇总")
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args(argv)
    since, until = (a.et_date, a.et_date) if a.et_date else (a.since, a.until)

    legs, positions = load(a.db, since, until)
    marks, ages = None, {}
    if a.mark:
        codes = [c for c, p in positions.items()
                 if (p.get("qty_remaining") or 0) > 0]
        if a.stale_ok:
            raw = fetch_marks_stale(codes)
            marks = {c: v[0] for c, v in raw.items()}
            ages = {c: v[1] for c, v in raw.items()}
        else:
            marks = fetch_marks(codes)
    if a.csv and legs:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(legs[0].keys()))
            w.writeheader()
            w.writerows(legs)
        print(f"逐腿账本 → {a.csv}（{len(legs)} 行）")
    if not legs:
        print("（该区间没有平仓腿）")
    print(format_report(legs, positions, show_legs=not a.no_legs and bool(legs),
                        marks=marks, ages=ages))
    if a.mark:
        _release_quote_ctx()
    return 0


if __name__ == "__main__":
    sys.exit(main())
