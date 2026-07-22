"""
端到端测试：mock Discord 消息 → handle_message → 真实 moomoo 下单
覆盖 test_full_flow 没测到的 DRY_RUN=false 分支

跑法（cwd = repo 根）:
    python -m autotrade.diag.diag_handle_message_real
"""
import asyncio
import os
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]


# ============ Mock Discord 对象 ============
class MockAuthor:
    def __init__(self, user_id: int, name: str = "kc_trader"):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = False


class MockChannel:
    def __init__(self, channel_id: int, name: str = "kc-signals"):
        self.id = channel_id
        self.name = name


class MockGuild:
    def __init__(self):
        self.id = 999
        self.name = "MockGuild"


class MockMessage:
    def __init__(self, content: str, channel_id: int, author_id: int):
        self.content = content
        self.channel = MockChannel(channel_id)
        self.author = MockAuthor(author_id)
        self.guild = MockGuild()
        self.id = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.created_at = datetime.now(timezone.utc)
        self.edited_at = None


# ============ 测试用例 ============
async def run_test():
    # DRY_RUN/env 改动只在这里（import 时零副作用）：
    # 强制加载 .env 并 override DRY_RUN，之后才 import listener/broker
    #（broker 常量在 import 时读 env）。
    from dotenv import load_dotenv
    load_dotenv(ROOT / "config" / ".env", override=True)
    os.environ["DRY_RUN"] = "false"  # 🔥 关键：强制真实下单分支

    from autotrade.listener.router import handle_message
    from autotrade.broker.trade import close_ctx
    # [refactor-change f] storage/risk 不再 import 时建表，入口显式 init()
    from autotrade.storage.logger_db import init as init_trades_db
    from autotrade.storage.positions_db import init as init_positions_db
    from autotrade.risk import init as init_risk_db
    init_trades_db()
    init_positions_db()
    init_risk_db()

    print("=" * 60)
    print("端到端测试：handle_message → 真实 moomoo 下单")
    print("=" * 60)

    # 环境确认
    print(f"\n[env] DRY_RUN          = {os.getenv('DRY_RUN')}")
    print(f"[env] MOOMOO_TRD_ENV   = {os.getenv('MOOMOO_TRD_ENV')}")
    print(f"[env] MOOMOO_ACC_ID    = {os.getenv('MOOMOO_ACC_ID')}")
    print(f"[env] MAX_PRICE        = {os.getenv('MAX_PRICE_PER_CONTRACT')}")

    # 用 KC 频道 id（channels.json 里配置的）
    KC_CHANNEL_ID = 1443396572581859405
    TRIGGER_USER_ID = 1426229538722938880

    # 构造一条肯定能解析 + 风控通过的信号
    # SPY 700p 06/19 @ $0.01 → 限价 $0.0105，模拟盘不会成交
    content = "SPY 700p 06/19 @ 0.01"

    print(f"\n[mock] channel_id = {KC_CHANNEL_ID}")
    print(f"[mock] author_id  = {TRIGGER_USER_ID}")
    print(f"[mock] content    = {content!r}")

    confirm = input("\n⚠️  将真实下单到 moomoo 模拟盘，继续？[y/N] ").strip().lower()
    if confirm != "y":
        print("已取消")
        return

    msg = MockMessage(content, KC_CHANNEL_ID, TRIGGER_USER_ID)

    print("\n" + "-" * 60)
    print("调用 handle_message ...")
    print("-" * 60)

    try:
        await handle_message(msg)
        print("\n" + "-" * 60)
        print("✅ handle_message 执行完成（无异常）")
        print("-" * 60)
    except Exception as e:
        print(f"\n❌ handle_message 抛异常：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return

    print("\n📋 请确认：")
    print("  1. moomoo App 是否看到 SPY 260618 PUT 700 挂单？")
    print("  2. Telegram 是否收到 '信号' 和 '下单成功' 两条消息？")
    print("  3. data/trades.db 是否有新记录？")
    print("\n⚠️  确认后请去 moomoo App 手动撤单！")

    # 优雅关闭 moomoo ctx
    await asyncio.to_thread(close_ctx)
    print("\n[done] moomoo ctx 已关闭")


def main():
    asyncio.run(run_test())


if __name__ == "__main__":
    main()
