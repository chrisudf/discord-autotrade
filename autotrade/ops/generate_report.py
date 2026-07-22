"""Daily report: read SQLite -> CSV + stats

跑法（cwd 必须是 repo 根，DB/输出路径按 cwd 相对解析）：
    python -m autotrade.ops.generate_report
"""
import sqlite3
import pandas as pd
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from autotrade.storage.logger_db import init as init_trades_db

DB = "data/trades.db"
OUT_DIR = Path("data/reports")


def generate_daily_report(target=None):
    target = target or date.today()
    conn = sqlite3.connect(DB)
    df = pd.read_sql(
        "SELECT * FROM orders WHERE date(placed_at) = ?",
        conn,
        params=[target.isoformat()],
    )
    conn.close()

    if df.empty:
        print(f"No orders for {target}")
        return

    csv = OUT_DIR / f"report_{target}.csv"
    df.to_csv(csv, index=False)

    print(f"=== Report {target} ===")
    print(f"Total:   {len(df)}")
    print(f"Success: {df['success'].sum()}")
    print(f"Failed:  {(df['success'] == 0).sum()}")
    print(f"Symbols: {df['symbol'].value_counts().to_dict()}")
    print(f"Saved -> {csv}")


def main():
    # [refactor-change f] mkdir/建表从 import 时移到入口显式执行。
    load_dotenv(Path("config/.env"))
    init_trades_db()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generate_daily_report()


if __name__ == "__main__":
    main()
