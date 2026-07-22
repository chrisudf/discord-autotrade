"""按 ET 显示今日所有 raw_signals / orders / daily_orders

时区基准（2026-06-16 确认）：
- raw_signals.received_at / orders.placed_at: AEST naive (datetime.now())
- daily_orders.ts: ET 带 -04:00 显式时区
- daily_orders.trading_date: ET 自然日字符串

跑法：
    python -m autotrade.ops.show_today [YYYY-MM-DD]
"""
import sys
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from autotrade.storage.logger_db import init as init_trades_db
from autotrade.risk import init as init_risk_db

ET = ZoneInfo("America/New_York")
LOCAL_TZ = ZoneInfo("Australia/Sydney")  # Mac 本地

ROOT = Path(__file__).resolve().parents[2]
TRADES_DB = ROOT / "data" / "trades.db"
RISK_DB = ROOT / "data" / "risk.db"


def _parse_to_et(s: str) -> datetime:
    """兼容：'...Z' / '...+10:00' / '...-04:00' / naive(澳洲)"""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(ET)


def show(target_et_date: str):
    print(f"\n{'='*70}")
    print(f"  ET Trading Date: {target_et_date}")
    print(f"{'='*70}\n")

    # ---- raw_signals ----
    print("📥 raw_signals (按 ET 过滤)")
    print("-" * 70)
    conn = sqlite3.connect(TRADES_DB)
    rows = conn.execute(
        "SELECT msg_id, author, content, received_at FROM raw_signals "
        "ORDER BY received_at"
    ).fetchall()
    count = 0
    for msg_id, author, content, received_at in rows:
        dt_et = _parse_to_et(received_at)
        if dt_et is None or dt_et.strftime("%Y-%m-%d") != target_et_date:
            continue
        count += 1
        preview = (content or "").replace("\n", " ")[:80]
        print(f"  [{dt_et.strftime('%H:%M:%S')} ET] {author[:18]:18s} | {preview}")
    if count == 0:
        print("  (无)")
    print(f"  共 {count} 条\n")
    conn.close()

    # ---- orders ----
    print("📤 orders (broker 下单流水)")
    print("-" * 70)
    conn = sqlite3.connect(TRADES_DB)
    rows = conn.execute(
        "SELECT id, symbol, side, strike, expiry, entry_price, qty, "
        "option_code, success, order_id, placed_at FROM orders ORDER BY placed_at"
    ).fetchall()
    count = 0
    for r in rows:
        (oid, sym, side, strike, expiry, price, qty, code, success,
         order_id, placed_at) = r
        dt_et = _parse_to_et(placed_at)
        if dt_et is None or dt_et.strftime("%Y-%m-%d") != target_et_date:
            continue
        count += 1
        flag = "✅" if success else "❌"
        print(f"  [{dt_et.strftime('%H:%M:%S')} ET] id={oid} {flag} "
              f"{sym} {strike}{(side or '?')[0]} {expiry} @{price} x{qty} "
              f"| {order_id}")
    if count == 0:
        print("  (无)")
    print(f"  共 {count} 笔\n")
    conn.close()

    # ---- daily_orders (已 ET) ----
    print("💰 daily_orders (风控记账，原生 ET)")
    print("-" * 70)
    conn = sqlite3.connect(RISK_DB)
    rows = conn.execute(
        "SELECT id, ts, symbol, strike, side, expiry, price, qty, cost, "
        "channel_name FROM daily_orders WHERE trading_date=? ORDER BY ts",
        (target_et_date,),
    ).fetchall()
    total_cost = 0
    for r in rows:
        (oid, ts, sym, strike, side, expiry, price, qty, cost, ch) = r
        total_cost += cost or 0
        ts_short = ts.split("T")[1][:8] if "T" in ts else ts
        print(f"  [{ts_short} ET] id={oid} {sym} {strike}{(side or '?')[0]} "
              f"{expiry} @{price} x{qty} = ${cost:.0f} | {ch}")
    if not rows:
        print("  (无)")
    print(f"  共 {len(rows)} 笔 / 总成本 ${total_cost:.2f}\n")

    cb = conn.execute(
        "SELECT reason, triggered_at FROM daily_circuit_breaker "
        "WHERE trading_date=?", (target_et_date,)
    ).fetchone()
    if cb:
        print(f"🚨 熔断: {cb[0]} @ {cb[1]}\n")
    conn.close()


def main():
    # [refactor-change f] storage/risk 不再 import 时建表，入口显式 init()。
    # load_dotenv 只在 main() 里（本脚本不读 env，加载仅为与其它 ops 入口一致）。
    load_dotenv(ROOT / "config" / ".env")
    init_trades_db()
    init_risk_db()

    target = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).strftime("%Y-%m-%d")
    show(target)


if __name__ == "__main__":
    main()
