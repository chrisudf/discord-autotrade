"""持仓对账脚本

用途：把本地 trades.db 里的 OPEN 持仓和 moomoo broker 真实持仓对账。
7/2 后发现本地 DB 与 broker 严重脱钩，原因：
  - 期权 ITM 到期时 OCC/moomoo 自动 exercise → 期权仓被清 + 拿到正股
  - 我们代码里没有 exercise/expiry 同步机制，本地 DB 一直挂着 OPEN
  - 后续 close 信号基于 stale 本地状态，可能挂卖不存在的期权 → 假 fill 或 naked short

行为：
  1. 拉 broker position_list_query
  2. 对每个本地 OPEN，若 broker 没这个 code 或 qty=0 → 标记本地 CLOSED (note="broker sync: 期权已消失，可能到期 exercise")
  3. 对 broker 里有但本地没的 → 打 WARNING 列出（stray positions，运营手动处理）
  4. --dry-run 只打印不写库；不带则真写

跑法：
  python -m autotrade.ops.sync_positions [--dry-run]

⚠️ 建议每次 listener 启动前跑一次，或者加进 cron。
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from autotrade.utils.logger import logger, setup_logging
from autotrade.storage import positions_db
from moomoo import OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK

# broker 常量（TRD_ENV_STR/OPEND_HOST/OPEND_PORT/ACC_ID）在模块 import 时读 env，
# 必须先 load_dotenv 再 import —— 老脚本在模块顶按这个顺序做，现在 dotenv 移进
# main()，broker import 也随之延迟到 main() 里。
bc = None


def _query_broker_positions() -> dict:
    """返回 {option_code: qty}。仅期权（有 c/p 数字后缀），股票排除。"""
    env = TrdEnv.SIMULATE if bc.TRD_ENV_STR == "SIMULATE" else TrdEnv.REAL
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=bc.OPEND_HOST, port=bc.OPEND_PORT,
        security_firm=SecurityFirm.FUTUINC,
    )
    try:
        ret, df = ctx.position_list_query(trd_env=env, acc_id=bc.ACC_ID)
        if ret != RET_OK:
            raise RuntimeError(f"position_list_query failed: {df}")
        out = {}
        strays_stock = []
        strays_option = []
        if df is None or len(df) == 0:
            return out, strays_stock, strays_option
        for _, row in df.iterrows():
            code = row["code"]
            qty = int(row["qty"])
            if qty <= 0:
                continue
            # 期权 code 模式：US.SYM<YYMMDD><C|P><strike*1000> 至少 15 位后缀
            # 简单区分：包含 C 或 P + 数字 且总长 > 12
            if _looks_like_option(code):
                out[code] = qty
                strays_option.append((code, qty, float(row.get("cost_price", 0))))
            else:
                strays_stock.append((code, qty, float(row.get("cost_price", 0))))
        return out, strays_stock, strays_option
    finally:
        ctx.close()


def _looks_like_option(code: str) -> bool:
    """粗略判断 US 期权代码：US.SYMBOL<数字><C|P><数字>"""
    if not code.startswith("US."):
        return False
    body = code[3:]
    # 找最后一个 C 或 P 后面接数字的位置
    import re
    return bool(re.search(r"[CP]\d{6,}$", body))


def sync(dry_run: bool = False):
    logger.info("=" * 60)
    logger.info(f"🔄 Position sync {'(DRY RUN)' if dry_run else ''}")
    logger.info("=" * 60)

    # 1. broker 状态
    logger.info("Fetching broker positions...")
    try:
        broker_positions, strays_stock, strays_option_actual = _query_broker_positions()
    except Exception as e:
        logger.error(f"❌ broker query failed: {e}")
        sys.exit(1)

    logger.info(f"  broker OPEN options: {len(broker_positions)}")
    for code, qty in broker_positions.items():
        logger.info(f"    • {code} qty={qty}")

    if strays_stock:
        logger.warning(f"  ⚠️  broker 里 {len(strays_stock)} 个孤儿正股（很可能是期权到期 exercise 换来的）:")
        for code, qty, cost in strays_stock:
            logger.warning(f"    • {code} qty={qty} cost={cost}")
        logger.warning("      → 建议：登 moomoo 手动清掉这些股票（不然会一直裸放）")

    # 2. 本地 DB OPEN 状态
    local_positions = positions_db.get_open_positions()
    logger.info(f"\n  local DB OPEN positions: {len(local_positions)}")
    for pos in local_positions:
        code = pos["option_code"]
        qty = pos["qty_remaining"]
        marker = "✅" if code in broker_positions else "❌ MISSING FROM BROKER"
        logger.info(f"    {marker} {code} qty={qty}")

    # 3. 对账
    stale = [p for p in local_positions if p["option_code"] not in broker_positions]
    logger.info("=" * 60)
    logger.info(f"📊 Reconcile summary:")
    logger.info(f"    broker OPEN options: {len(broker_positions)}")
    logger.info(f"    local  OPEN positions: {len(local_positions)}")
    logger.info(f"    stale (local OPEN, broker gone): {len(stale)}")

    # 4. 处理 stale
    if not stale:
        logger.info("  ✅ 无需 sync，本地跟 broker 一致")
        return

    logger.warning(f"\n即将把 {len(stale)} 个本地 stale 仓位标记为 CLOSED:")
    for pos in stale:
        logger.warning(
            f"  • {pos['option_code']} qty={pos['qty_remaining']} "
            f"expiry={pos.get('expiry')} — 本地 OPEN 但 broker 没有"
        )

    if dry_run:
        logger.info("\n🚧 DRY RUN — 无写入")
        return

    # 真写入：用 fill_price=0 表示 sync 而非成交
    for pos in stale:
        try:
            positions_db.record_close(
                option_code=pos["option_code"],
                qty_sold=pos["qty_remaining"],
                fill_price=0.0,
                trigger_source="broker_sync",
                note="broker no longer has this position (auto-exercise / expired / manual close)",
            )
            logger.info(f"  ✅ marked CLOSED: {pos['option_code']}")
        except Exception as e:
            logger.error(f"  ❌ failed to close {pos['option_code']}: {e}")

    logger.info("=" * 60)
    logger.info("✅ Sync done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = parser.parse_args()

    # load_dotenv 在 main() 里、broker import 之前（常量 import 时读 env）
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env")
    global bc
    import autotrade.broker.common as bc  # noqa: E402

    setup_logging(Path("logs"))
    # [refactor-change f] storage 不再 import 时建表，入口显式 init()
    positions_db.init()

    sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
