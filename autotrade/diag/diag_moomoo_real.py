"""
moomoo 真实下单测试（模拟盘）

前置:
  1. OpenD 已启动并登录模拟盘
  2. .env: MOOMOO_ACC_ID=321501  MOOMOO_TRD_ENV=SIMULATE
  3. pip show moomoo-api  确认 SDK 装了

流程:
  1. 构造 option_code (SPY 6/15 700P，模拟盘确认存在)
  2. 下一单（极低限价不会成交，方便观察）
  3. 查状态
  4. 提示用户去 moomoo App 看 → 手动撤单

跑法（cwd = repo 根）:
    python -m autotrade.diag.diag_moomoo_real
"""
import os
import time
from datetime import date


def main():
    # DRY_RUN/env 改动全部在 main() 内（import 时零副作用）。
    # 顺序保持老脚本原样：先读 shell env 的 DRY_RUN（在 load_dotenv 之前！），
    # 再加载 .env，再强制关 DRY_RUN，最后才 import broker（常量 import 时读 env）。
    DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

    from dotenv import load_dotenv
    load_dotenv("config/.env", override=True)

    # 强制关闭 DRY_RUN（在 import broker 之前）
    os.environ["DRY_RUN"] = "false"

    from autotrade.broker.trade import place_order, query_order_status, close_ctx
    from autotrade.broker.common import SDK_AVAILABLE, TRD_ENV_STR, ACC_ID
    from autotrade.policy.pricing import build_option_code

    print("=" * 60)
    print("moomoo 真实下单测试（模拟盘）")
    print(f"SDK 可用: {SDK_AVAILABLE}")
    print(f"DRY_RUN: {DRY_RUN}")
    print(f"TRD_ENV: {TRD_ENV_STR}")
    print(f"ACC_ID: {ACC_ID}")
    print("=" * 60)

    if not SDK_AVAILABLE:
        print("❌ moomoo SDK 未安装，pip install moomoo-api")
        return
    if DRY_RUN:
        print("❌ DRY_RUN 仍为 true，请检查 .env")
        return
    if ACC_ID == 0:
        print("❌ MOOMOO_ACC_ID 未配置，请在 .env 加 MOOMOO_ACC_ID=321501")
        return
    if TRD_ENV_STR != "SIMULATE":
        print(f"⚠️  非模拟盘 ({TRD_ENV_STR})，要继续？")
        if input("输入 yes 确认: ").strip() != "yes":
            return

    # ---- 测试信号 ----
    # 用 diag_find_option_code 确认过的真实合约：SPY 6/15 700P
    # 限价 0.01 保证不成交，方便手动撤
    test_signal = {
        "symbol": "SPY",
        "strike": 700.0,
        "side": "PUT",
        "expiry_date": date(2026, 6, 15),
        "price": 0.01,
        "tags": ["TEST"],
    }

    code = build_option_code(
        test_signal["symbol"], test_signal["expiry_date"],
        test_signal["strike"], test_signal["side"],
    )
    print(f"\n测试合约: {code}")
    print(f"  symbol   = {test_signal['symbol']}")
    print(f"  strike   = {test_signal['strike']}")
    print(f"  side     = {test_signal['side']}")
    print(f"  expiry   = {test_signal['expiry_date']}")
    print(f"  signal $ = {test_signal['price']}")
    print(f"  limit $  = {test_signal['price'] * 1.05:.4f} (含 5% 滑点)")

    confirm = input("\n确认下单？(yes/no): ").strip()
    if confirm != "yes":
        print("已取消")
        return

    # ---- 下单 ----
    print("\n[1/3] 下单中...")
    result = place_order(test_signal, qty=1)
    print(f"  success    = {result['success']}")
    print(f"  message    = {result['message']}")
    print(f"  order_id   = {result['order_id']}")
    print(f"  code       = {result['code']}")
    print(f"  qty        = {result['qty']}")
    print(f"  price      = {result['price']}")

    if not result["success"]:
        print("\n❌ 下单失败，结束")
        close_ctx()
        return

    order_id = result["order_id"]

    # ---- 查状态 ----
    print(f"\n[2/3] 等 3 秒后查询订单 {order_id} 状态...")
    time.sleep(3)
    status = query_order_status(order_id)
    print(f"  success           = {status['success']}")
    print(f"  status            = {status['status']}")
    print(f"  filled_qty        = {status['filled_qty']}")
    print(f"  filled_avg_price  = {status['filled_avg_price']}")
    print(f"  message           = {status['message']}")

    # ---- 提示手动撤单 ----
    print(f"\n[3/3] 请打开 moomoo App 检查订单 {order_id}")
    print("       确认看到挂单后 → 手动撤单 → 回车继续")
    input()

    close_ctx()
    print("\n✅ 测试结束")


if __name__ == "__main__":
    main()
