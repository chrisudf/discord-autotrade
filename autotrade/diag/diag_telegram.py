"""
Telegram 通知测试
跑通这个 = Telegram 接入成功

跑法（cwd = repo 根）:
    python -m autotrade.diag.diag_telegram
"""
import asyncio
from pathlib import Path


async def main():
    # transport 的 BOT_TOKEN/CHAT_ID 在 import 时读 env（老 telegram_client 自己
    # import 时 load_dotenv，现已移除）→ 先加载 .env 再 import。
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)

    from autotrade.notify.transport import send_telegram
    from autotrade.notify.messages import (
        format_signal_alert,
        format_order_filled,
        format_risk_blocked,
        format_error,
        format_daily_summary,
    )

    print("=" * 60)
    print("Telegram 通知测试")
    print("=" * 60)

    # Test 1: 纯文本
    print("\n[1/5] 测试纯文本...")
    ok = await send_telegram("✅ Discord copytrade 系统启动测试")
    print(f"    结果: {'OK' if ok else 'FAIL'}")

    # Test 2: 信号通知
    print("\n[2/5] 测试信号通知...")
    msg = format_signal_alert(
        channel_name="KC-期权-波段",
        symbol="AMZN", strike=260, expiry="2026-07-17",
        side="C", price=1.5, qty=1, action="OPEN"
    )
    ok = await send_telegram(msg)
    print(f"    结果: {'OK' if ok else 'FAIL'}")

    # Test 3: 下单成功
    print("\n[3/5] 测试下单成功通知...")
    msg = format_order_filled(
        symbol="AMZN", strike=260, side="C", expiry="2026-07-17",
        fill_price=1.52, qty=1, order_id="MOCK-12345"
    )
    ok = await send_telegram(msg)
    print(f"    结果: {'OK' if ok else 'FAIL'}")

    # Test 4: 风控拦截
    print("\n[4/5] 测试风控拦截通知...")
    msg = format_risk_blocked(
        reason="单张合约价格超限",
        detail="信号价 $6.5 > 上限 $5.0"
    )
    ok = await send_telegram(msg)
    print(f"    结果: {'OK' if ok else 'FAIL'}")

    # Test 5: 错误通知
    print("\n[5/5] 测试错误通知...")
    msg = format_error("discord_listener", "Connection lost: WebSocket closed")
    ok = await send_telegram(msg)
    print(f"    结果: {'OK' if ok else 'FAIL'}")

    print("\n" + "=" * 60)
    print("打开 Telegram 检查是否收到 5 条消息")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
