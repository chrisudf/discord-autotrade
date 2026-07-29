"""从 data/trades.db raw_signals 导出未标注的 corpus skeleton JSONL（生产机跑）。

用途：金语料回放门（tests/corpus/ + tests/test_corpus_replay.py，见 docs/CORPUS.md）
的采集端。四夜 SIMULATE 的经验：复盘时从 listener 日志里手抄消息又慢又容易
丢空格/emoji（7/23 "出半" 右边界之争就是被一个看不见的零宽字符坑过）——
raw_signals 表存的是 on_message 原文，从库里导才是逐字。

跑法（生产机，只读 sqlite，不连 broker/Discord）：
    python -m autotrade.ops.export_corpus --since 2026-07-27
    python -m autotrade.ops.export_corpus --since 2026-07-27 --until 2026-07-28 \\
        --out data/corpus_2026-07-27.jsonl

输出：每行一条 skeleton——
    {"name": "...", "text": <原文>, "open_symbols": [],
     "msg_date": <ET 日期>, "expect": null,
     "suggest": {"detect": <当前 detect_action 结果>}}

skeleton 语义（刻意设计成"不标注就进不了门"）：
  - expect=null：回放 harness 对 expect 非 dict 的行直接报错——
    没人工定案的行**不可能**静默混进语料门。
  - suggest.detect 只是当前行为的机读建议，人工确认后搬进 expect；
    open_symbols 必须由人补当晚实际持仓（库里没有这个历史快照，
    而 CLOSE 白名单语义完全依赖它——宁可让人补，不能让机器猜）。
  - msg_date 用 received_at 的 ET 日期（与 parser 回放的 msg_ts 口径一致，
    backfill_history 同款换算）。

工作流全文见 docs/CORPUS.md。
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from autotrade.parsing.signal_parser import detect_action
from autotrade.storage.logger_db import DB_PATH

ET_TZ = ZoneInfo("America/New_York")


def _to_et_date(received_at: str):
    """raw_signals.received_at (UTC ISO with Z) → ET date。naive 视作 UTC
    （与 ops.backfill_history 相同的历史兼容口径：修 bug 前的早期行没带 Z）。"""
    dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET_TZ).date()


def load_raw_signals(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT msg_id, author, content, received_at FROM raw_signals "
            "ORDER BY received_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def build_skeletons(rows: list[dict], since, until) -> list[dict]:
    """过滤日期窗口 → skeleton 行。纯函数，测试直接喂 rows。"""
    out = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        try:
            et_date = _to_et_date(r["received_at"])
        except (ValueError, TypeError):
            # 脏 received_at 不值得让整次导出失败——跳过并继续
            continue
        if et_date < since or et_date > until:
            continue
        out.append({
            # msg_id 全局唯一（Discord snowflake），name 不会撞
            "name": f"raw_{et_date.isoformat()}_{r['msg_id']}",
            "text": content,
            "open_symbols": [],
            "msg_date": et_date.isoformat(),
            "expect": None,
            "suggest": {"detect": detect_action(content)},
        })
    return out


def write_jsonl(skeletons: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in skeletons:
            # ensure_ascii=False：中文原文保持可读，人工标注时不对着 \uXXXX 猜
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="导出 raw_signals → corpus skeleton JSONL（人工补 expect 后进 tests/corpus/）",
    )
    parser.add_argument("--since", required=True,
                        help="起始 ET 日期（含），YYYY-MM-DD")
    parser.add_argument("--until", default=None,
                        help="结束 ET 日期（含），默认=since（单夜导出）")
    parser.add_argument("--out", default=None,
                        help="输出路径，默认 data/corpus_export_<since>.jsonl")
    parser.add_argument("--db", default=None,
                        help=f"sqlite 路径，默认 {DB_PATH}")
    args = parser.parse_args(argv)

    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    until = datetime.strptime(args.until, "%Y-%m-%d").date() if args.until else since
    db_path = Path(args.db) if args.db else DB_PATH
    out_path = (
        Path(args.out) if args.out
        else db_path.parent / f"corpus_export_{since.isoformat()}.jsonl"
    )

    if not db_path.exists():
        print(f"[export_corpus] DB 不存在: {db_path}", file=sys.stderr)
        return 1

    skeletons = build_skeletons(load_raw_signals(db_path), since, until)
    write_jsonl(skeletons, out_path)
    print(f"[export_corpus] {len(skeletons)} rows → {out_path}")
    print("[export_corpus] 下一步：人工补 open_symbols/expect（suggest 仅供参考），"
          "确认后并入 tests/corpus/，见 docs/CORPUS.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
