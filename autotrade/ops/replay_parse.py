"""全历史原始消息回放：改解析器前后各跑一次，对比结果（只读生产库）。

    python -m autotrade.ops.replay_parse --out /tmp/before.json     # 在 main 上
    python -m autotrade.ops.replay_parse --out /tmp/after.json      # 在改动分支上
    python -m autotrade.ops.replay_parse --diff /tmp/before.json /tmp/after.json

corpus 只锁已知的几百条；这个把 raw_signals 里几千条全过一遍，改动只该动到预期的那几条。
worktree 里没有 data/，用 --db 指向主目录的 trades.db。
"""
import argparse
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action, parse_signal
from autotrade.storage.logger_db import DB_PATH
from autotrade.utils.logger import logger

ET = ZoneInfo("America/New_York")


def _summary(text: str, et_day, held: set) -> list:
    sig = parse_signal(text, msg_ts=et_day)
    if isinstance(sig, dict) and not sig.get("skip"):
        sig = [sig["symbol"], sig["strike"], sig["side"], str(sig["expiry_date"]), sig["price"]]
    elif isinstance(sig, dict):
        sig = f"skip:{sig['skip']}"
    close = parse_close(text, held)
    close = close and [close.get("symbols"), close.get("pct")]
    return [detect_action(text), sig, close]


def dump(db: str, out: str) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    held = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM positions")}
    rows = {}
    for mid, text, ts in con.execute("SELECT msg_id, content, received_at FROM raw_signals"):
        day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET).date()
        rows[mid] = [text[:120]] + _summary(text, day, held)
    json.dump(rows, open(out, "w"), ensure_ascii=False, default=str)
    print(f"{len(rows)} 条 → {out}")


def diff(before: str, after: str) -> None:
    a, b = json.load(open(before)), json.load(open(after))
    changed = [k for k in a if k in b and a[k][1:] != b[k][1:]]
    print(f"共 {len(a)} 条，变化 {len(changed)} 条")
    for k in changed:
        print(f"  {a[k][0]!r}\n    before {a[k][1:]}\n    after  {b[k][1:]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--out")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()
    logger.remove()   # 解析器每条都打日志，几千条会把输出淹掉
    if args.diff:
        diff(*args.diff)
    elif args.out:
        dump(args.db, args.out)
    else:
        ap.error("要么 --out，要么 --diff")


if __name__ == "__main__":
    main()
