"""Simulate end-to-end flow without connecting to Discord

跑法（cwd = repo 根）:
    python -m autotrade.diag.diag_listener_flow

注意：刻意不 load_dotenv —— 老脚本从不加载 .env，broker 走 shell env
（DRY_RUN 默认 true → place_order 干跑）。加载 .env 可能把 DRY_RUN 翻成
false 造成真下单，行为保持原样。
"""
import asyncio

from autotrade.parsing.signal_parser import parse_signal, detect_action
from autotrade.broker.trade import place_order
from autotrade.storage.logger_db import log_raw_signal, log_order, init as init_trades_db
from datetime import datetime, timezone


async def simulate(raw_msg: str, msg_id: str = "TEST_001"):
    print(f"\n{'='*70}\nSimulating: {raw_msg}")
    t0 = datetime.now(timezone.utc)
    log_raw_signal(msg_id, "test_user", raw_msg, t0)

    action = detect_action(raw_msg)
    print(f"Action: {action}")
    if action == "CLOSE":
        print("-> Skip (CLOSE TODO)")
        return

    signal = parse_signal(raw_msg)
    if not signal:
        print("-> Parse failed")
        return

    if isinstance(signal, list):
        print(f"-> {len(signal)} signals, taking first")
        signal = signal[0]

    result = await asyncio.to_thread(place_order, signal)

    log_order(msg_id, signal, result)
    print(f"-> Order result: {result}")


async def main():
    # [refactor-change f] storage 不再 import 时建表，入口显式 init()
    init_trades_db()

    samples = [
        ("AMZN 260c 7/17 @ 2.64 swing", "T001"),
        ("MSFT 440c 7/17 @ 3.05 swing, I like the 420c 7/17 @ 6.05", "T002"),
        ("Lotto $IREN 0DTE $60 calls $.68", "T003"),
        ("closed AMZN 260c for +50%", "T004"),
        ("garbage message", "T005"),
    ]
    for raw, mid in samples:
        await simulate(raw, mid)


if __name__ == "__main__":
    asyncio.run(main())
