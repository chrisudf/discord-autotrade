# autotrade/ops/backtest_parser.py
"""
Parser 回测：抓历史消息，跑当前 parser 逻辑，统计准确率。

只看 channels.json 里 trigger_user_ids 指定的作者，保持和生产链路一致。
输出 data/parser_backtest.json，含：
  - summary: 总数 / 解析成功 / 被规则跳过 / 真漏报
  - parsed: 成功解析的完整 signal dict
  - skipped: 被规则 3/4 过滤的（持仓汇报 / 价格区间）
  - missed: 真没解析出来的（重点观察，用来改正则）

跑法：
    python -m autotrade.ops.backtest_parser

⚠️ self-bot 限制：跑回测时不能同时跑 listener（同一 token 不能开两个 client）
"""
import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import discord
from dotenv import load_dotenv

from autotrade.parsing.signal_parser import parse_signal
from autotrade.config.channel_loader import registry
from autotrade.utils.logger import logger, setup_logging

ROOT = Path(__file__).resolve().parents[2]

# ===== 静音 parser 日志（回测刷屏太烦）=====
logging.getLogger("autotrade.parsing.signal_parser").setLevel(logging.ERROR)

# TOKEN/HISTORY_LIMIT 在 main() 里 load_dotenv 之后才读（老脚本在模块顶
# load_dotenv 后读，现 dotenv 移进 main()，读 env 也随之延迟）。
TOKEN = None
HISTORY_LIMIT = 1000
OUTPUT_PATH = ROOT / "data" / "parser_backtest.json"

# 把跳过原因从 parser 日志里捞出来的关键词（保持和 parser 同步）
HOLDING_KEYWORDS = ["holding", "remaining", "into tomorrow", "持仓"]
PRICE_RANGE_HINTS = [" - $", "to $", "~ $"]


def _classify_skip_reason(content: str) -> str:
    """猜测被 parser pre-filter 跳过的原因，仅用于回测分类。"""
    lower = content.lower()
    if any(kw in lower for kw in HOLDING_KEYWORDS):
        return "holding"
    # 简化判断：parser 真正用的是 PRICE_RANGE_PATTERN 正则，这里只是分类
    if any(hint in content for hint in PRICE_RANGE_HINTS):
        return "price_range"
    return "unknown"


def _serialize_signal(sig: dict) -> dict:
    """signal dict 里的 date 对象转 ISO 字符串，方便 JSON 序列化。"""
    out = {}
    for k, v in sig.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif k == "raw":
            # raw 是整段消息，前面已经记了 content，这里省掉
            continue
        else:
            out[k] = v
    return out


client = discord.Client()
results = {}  # channel_id -> dict


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")

    enabled_ids = registry.enabled_channel_ids()
    logger.info(f"Backtesting {len(enabled_ids)} channel(s), limit={HISTORY_LIMIT}/channel")

    for cid in enabled_ids:
        cfg = registry.get(cid)
        ch = client.get_channel(cid)
        if ch is None:
            logger.error(f"❌ {cfg.name} ({cid}) NOT visible, skip")
            results[str(cid)] = {
                "channel_name": cfg.name,
                "error": "channel not visible",
            }
            continue

        logger.info(f"\n📊 [{cfg.name}] {ch.guild.name} #{ch.name}")
        logger.info(f"   trigger_user_ids: {cfg.trigger_user_ids}")

        parsed = []
        skipped = []
        missed = []
        total_from_author = 0
        all_messages = 0

        try:
            async for msg in ch.history(limit=HISTORY_LIMIT):
                all_messages += 1

                # 只看触发作者（和生产逻辑一致）
                if not cfg.is_trigger_user(msg.author.id):
                    continue

                total_from_author += 1
                content = msg.content or ""

                if not content.strip():
                    # 纯 embed / 附件 —— 当前 parser 不支持
                    missed.append({
                        "ts": msg.created_at.isoformat(),
                        "author": str(msg.author),
                        "content": "(empty content, embeds/attachments only)",
                        "embeds": len(msg.embeds),
                        "attachments": len(msg.attachments),
                    })
                    continue

                # signal = parse_signal(content)
                signal = parse_signal(msg.content, msg_ts=msg.created_at.date() if msg.created_at else None)

                if signal:
                    parsed.append({
                        "ts": msg.created_at.isoformat(),
                        "author": str(msg.author),
                        "content": content,
                        "signal": _serialize_signal(signal),
                    })
                else:
                    # 判断是被 pre-filter 跳过的，还是真漏报
                    reason = _classify_skip_reason(content)
                    if reason == "unknown":
                        missed.append({
                            "ts": msg.created_at.isoformat(),
                            "author": str(msg.author),
                            "content": content,
                        })
                    else:
                        skipped.append({
                            "ts": msg.created_at.isoformat(),
                            "author": str(msg.author),
                            "content": content,
                            "reason": reason,
                        })

        except discord.Forbidden:
            logger.error(f"   ❌ no permission to read history")
            results[str(cid)] = {
                "channel_name": cfg.name,
                "error": "no permission",
            }
            continue
        except Exception as e:
            logger.exception(f"   ❌ error during history fetch: {e}")
            results[str(cid)] = {
                "channel_name": cfg.name,
                "error": str(e),
            }
            continue

        skip_holding = sum(1 for s in skipped if s["reason"] == "holding")
        skip_range = sum(1 for s in skipped if s["reason"] == "price_range")
        parse_rate = (
            f"{len(parsed) / total_from_author * 100:.1f}%"
            if total_from_author else "n/a"
        )

        summary = {
            "channel_name": cfg.name,
            "channel_id": str(cid),
            "guild": ch.guild.name if ch.guild else "DM",
            "channel": ch.name,
            "all_messages_scanned": all_messages,
            "from_trigger_author": total_from_author,
            "parsed": len(parsed),
            "skipped_holding": skip_holding,
            "skipped_price_range": skip_range,
            "missed": len(missed),
            "parse_rate": parse_rate,
        }

        logger.info(
            f"   总扫描: {all_messages} | 作者消息: {total_from_author} | "
            f"✅ 解析: {len(parsed)} ({parse_rate}) | "
            f"⏭️  跳过: holding={skip_holding} range={skip_range} | "
            f"❌ 漏报: {len(missed)}"
        )

        results[str(cid)] = {
            "summary": summary,
            "parsed": parsed,
            "skipped": skipped,
            "missed": missed,
        }

    # ===== 写文件 =====
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "history_limit_per_channel": HISTORY_LIMIT,
            "channels": results,
        }, f, ensure_ascii=False, indent=2)

    # ===== 汇总表 =====
    print("\n" + "=" * 80)
    print(f"{'Channel':<30} {'Author':>8} {'Parsed':>8} {'Holding':>8} {'Range':>8} {'Missed':>8} {'Rate':>8}")
    print("-" * 80)
    total = defaultdict(int)
    for cid, data in results.items():
        if "error" in data:
            print(f"{data['channel_name']:<30} ERROR: {data['error']}")
            continue
        s = data["summary"]
        print(
            f"{s['channel_name']:<30} "
            f"{s['from_trigger_author']:>8} "
            f"{s['parsed']:>8} "
            f"{s['skipped_holding']:>8} "
            f"{s['skipped_price_range']:>8} "
            f"{s['missed']:>8} "
            f"{s['parse_rate']:>8}"
        )
        total["author"] += s["from_trigger_author"]
        total["parsed"] += s["parsed"]
        total["holding"] += s["skipped_holding"]
        total["range"] += s["skipped_price_range"]
        total["missed"] += s["missed"]
    print("-" * 80)
    overall_rate = (
        f"{total['parsed'] / total['author'] * 100:.1f}%"
        if total["author"] else "n/a"
    )
    print(
        f"{'TOTAL':<30} "
        f"{total['author']:>8} "
        f"{total['parsed']:>8} "
        f"{total['holding']:>8} "
        f"{total['range']:>8} "
        f"{total['missed']:>8} "
        f"{overall_rate:>8}"
    )
    print("=" * 80)
    print(f"\n📄 详细结果已写入: {OUTPUT_PATH}")
    print("\n下一步：")
    print(f"  cat {OUTPUT_PATH} | jq '.channels | to_entries[] | .value.missed[:5]'")
    print("  → 看漏报样本，决定要不要改 parser 正则")

    await client.close()


async def main():
    global TOKEN, HISTORY_LIMIT
    load_dotenv(ROOT / "config" / ".env", override=True)
    setup_logging(Path("logs"))
    TOKEN = os.getenv("DISCORD_USER_TOKEN")
    HISTORY_LIMIT = int(os.getenv("BACKTEST_HISTORY_LIMIT", "1000"))
    if not TOKEN:
        raise RuntimeError("DISCORD_USER_TOKEN not configured")
    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
