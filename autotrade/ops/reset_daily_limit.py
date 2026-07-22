"""
紧急重置当日风控
用法：
  python -m autotrade.ops.reset_daily_limit          # 只清熔断标记，保留订单记录（推荐）
  python -m autotrade.ops.reset_daily_limit --hard   # 清熔断 + 清订单记录（慎用）
"""
import argparse
from pathlib import Path

from dotenv import load_dotenv


def main():
    # load_dotenv 在 main() 里、risk import 之前：autotrade.risk 的 MAX_* 常量
    # 在 import 时读 env（老 risk_manager 自己 import 时 load_dotenv，现移除）。
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)
    from autotrade.risk import (
        clear_circuit_breaker_only,
        manual_reset_today,
        get_daily_stats,
        is_circuit_broken,
        init as init_risk_db,
    )
    # [refactor-change f] risk 不再 import 时建表，入口显式 init()
    init_risk_db()

    parser = argparse.ArgumentParser()
    parser.add_argument("--hard", action="store_true",
                        help="清除熔断+订单记录（默认只清熔断）")
    args = parser.parse_args()

    stats_before = get_daily_stats()
    print(f"\n当前状态:")
    print(f"  交易日: {stats_before['trading_date']}")
    print(f"  已下单: {stats_before['order_count']} 笔")
    print(f"  累计成本: ${stats_before['total_cost']}")
    print(f"  熔断状态: {'🚨 已熔断' if stats_before['circuit_broken'] else '✅ 正常'}")

    confirm = input("\n确认重置? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("取消")
        return

    if args.hard:
        result = manual_reset_today()
        print(f"\n🔴 硬重置完成:")
        print(f"  清除熔断: {result['circuit_breaker_cleared']} 条")
        print(f"  清除订单: {result['orders_cleared']} 条")
    else:
        cleared = clear_circuit_breaker_only()
        print(f"\n🟢 软重置完成: 熔断标记 {'已清除' if cleared else '本来就没有'}")
        print(f"  订单记录保留（继续累计当日成本和次数）")

    stats_after = get_daily_stats()
    print(f"\n重置后:")
    print(f"  熔断状态: {'🚨 已熔断' if stats_after['circuit_broken'] else '✅ 正常'}")
    print(f"  剩余次数: {stats_after['remaining_orders']}")
    print(f"  剩余成本: ${stats_after['remaining_cost']}")


if __name__ == "__main__":
    main()
