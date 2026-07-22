"""
主入口（唯一组合根）：连接 Discord，监听所有 enabled 频道，触发 handle_message
用法：
  python -m autotrade.app.main
  # 或 console script（pyproject [project.scripts]）：autotrade

环境变量（config/.env）:
  DISCORD_USER_TOKEN  必填
  DRY_RUN        true/false（默认 true，安全起见）
  MOOMOO_TRD_ENV SIMULATE/REAL

组合根职责（其余模块 import 一律无副作用）：
  load .env 一次（经 load_settings）→ setup_logging → storage/risk 显式 init
  → preflight → 建唯一 discord.Client → 注入 router/connection → 注册事件
  → signal handlers → 过期仓位清扫 → watchers（强引用）→ client.start

事件 handler 以 scripts/run_listener.py 的版本为准（backfill/storm/churn 版），
老 src/main.py 与 discord_client 的模块级 client/事件注册不搬。
"""
import asyncio
import os
import signal
import sys
from pathlib import Path

from loguru import logger

from autotrade.settings import load_settings
from autotrade.utils.logger import setup_logging

ENV_PATH = Path(__file__).resolve().parents[2] / "config" / ".env"


# ============ Discord Client ============

# 唯一 discord.Client，由 main() 创建（不在 import 时建，保证
# `import autotrade.app.main` 无副作用）；handler / shutdown 经模块全局引用。
client = None


# on_ready 每次重连都会触发（discord.py-self 约 20-30 min 一次），
# 只在首次连接时发 TG 启动通知，避免刷屏。
_startup_notified = False


async def on_ready():
    global _startup_notified
    logger.info(f"✅ Discord logged in as: {client.user} (id={client.user.id})")

    # 仅首次 on_ready 做完整频道校验（REST fetch），重连只 log 不重新探测
    if _startup_notified:
        _log_reconnect_time("logged back in")
        # on_ready（而非 on_resumed）= 完整重登录，gateway session 已丢，
        # 掉线窗口内的消息**不会被重放**（7/20 一夜 ~25 次完整重登录 ×3s ≈
        # 75s 盲区）。主动拉频道历史回补，_seen(msg_id) 去重保证幂等。
        await _backfill_missed()
        return
    _startup_notified = True

    failures = await validate_channels(client)
    enabled_count = len(registry.enabled_channel_ids())

    if failures:
        lines = "\n".join(
            f"• {name} (id={cid}): {reason}" for cid, name, reason, _ in failures
        )
        try:
            await send_telegram(
                f"⚠️ 频道配置校验失败\n"
                f"{len(failures)}/{enabled_count} 个频道无法解析:\n{lines}\n"
                f"请检查 config/channels.json",
                parse_mode=None,
            )
        except Exception as e:
            logger.warning(f"Telegram channel-failure notify failed: {e}")
        # 只有全部失败且全部确定性（404/403）才退出；瞬时失败重连自愈
        all_definitive = all(definitive for _, _, _, definitive in failures)
        if len(failures) == enabled_count and all_definitive:
            logger.error(
                "❌ 所有 enabled 频道都确定性校验失败，listener 没有消息源 — 退出。"
                " 修复 config/channels.json 后重启。"
            )
            await client.close()
            return

    try:
        await send_telegram(
            f"🟢 Listener 启动\n"
            f"账号: {client.user}\n"
            f"监听: {enabled_count - len(failures)}/{enabled_count} 频道有效\n"
            f"DRY_RUN: {os.getenv('DRY_RUN', 'true')}",
            parse_mode=None,
        )
    except Exception as e:
        logger.warning(f"Telegram startup notify failed: {e}")


async def on_message(message):
    try:
        await handle_message(message)
    except Exception as e:
        logger.exception(f"on_message crashed: {e}")
        try:
            await send_telegram(format_error("on_message", str(e)))
        except Exception:
            pass


async def on_message_edit(before, after):
    # 暂不触发下单，只记录（防止 KC 改单价导致重复触发）
    if registry.is_monitored(after.channel.id):
        logger.info(f"✏️  [edit] {after.channel.name}: {after.content[:80]}")


# on_disconnect / on_resumed 的实现（防抖/storm/churn/回补起点管理）在
# autotrade.app.connection；这里是薄委托——discord.py 按函数名注册事件，
# 名字必须恰好叫 on_disconnect / on_resumed。
async def on_disconnect():
    await _connection_on_disconnect()


async def on_resumed():
    await _connection_on_resumed()


async def on_error(event_name, *args, **kwargs):
    logger.exception(f"❌ Discord on_error in '{event_name}'")


# ============ 优雅退出 ============

_shutting_down = False


async def shutdown():
    # 幂等：第二次 ^C 不再重入（否则两条 shutdown 协程并发关 ctx/client，
    # close_ctx 竞态、日志错乱，正是 7/20 连按两次 ^C 的场景）
    global _shutting_down
    if _shutting_down:
        logger.info("已在退出中，忽略重复信号")
        return
    _shutting_down = True
    logger.info("收到退出信号，关闭 Discord client...")
    try:
        await send_telegram("🔴 *Listener 退出*")
    except Exception:
        pass
    # moomoo SDK 的连接线程是非 daemon：不关掉的话 asyncio.run 结束后
    # threading._shutdown 会 lock.acquire() 挂死，每次都要连按 ^C 硬杀
    # （连续 5 晚复现）。先关 broker 线程再关 Discord。
    try:
        await asyncio.to_thread(close_ctx)
    except Exception:
        logger.exception("close_ctx failed (continuing shutdown)")
    await client.close()


def setup_signal_handlers(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))


# ============ 主入口 ============

# [refactor-change] c: watcher task 强引用。asyncio 对 task 只持弱引用，
# 裸 create_task 不存返回值可能被 GC 静默回收（watcher 无声消失）。
# 模块级 set 持强引用 + done_callback discard。
_watcher_tasks: set = set()


async def main():
    global client
    global registry, validate_channels, handle_message
    global send_telegram, format_error, close_ctx
    global _backfill_missed, _log_reconnect_time
    global _connection_on_disconnect, _connection_on_resumed
    global discord

    # [组合根] .env 只在这里加载一次（load_settings 内部 load_dotenv，
    # override=True）；库代码一律禁止 import-time load_dotenv。
    settings = load_settings(ENV_PATH)
    setup_logging(settings.log_dir)

    # [refactor-change] f: storage/risk 不再在 import 时建表/mkdir，
    # 组合根显式 init（tests 由 conftest 调用）。
    from autotrade import risk
    from autotrade.storage import logger_db, positions_db
    positions_db.init()
    logger_db.init()
    risk.init()

    # 业务模块必须**晚于** load_dotenv 才能 import：broker.common /
    # notify.transport 在 import 时读环境变量做模块级常量（ACC_ID、
    # TELEGRAM_BOT_TOKEN 等），提前 import 会拿到没加载 .env 的空值。
    # 等价于老 run_listener「顶层先 load_dotenv 再 import」的顺序。
    # 名字提升为本模块全局，上面的 handler / shutdown 以裸名调用
    # （tests 可在 autotrade.app.main 上 monkeypatch）。
    import discord
    from autotrade.app.connection import (
        _backfill_missed,
        _install_gateway_log_capture,
        _log_reconnect_time,
        bind,
        on_disconnect as _connection_on_disconnect,
        on_resumed as _connection_on_resumed,
    )
    from autotrade.app.preflight import preflight
    from autotrade.broker.trade import close_ctx
    from autotrade.config.channel_loader import registry, validate_channels
    from autotrade.listener.router import bind_client, handle_message
    from autotrade.notify.messages import format_error
    from autotrade.notify.transport import send_telegram
    from autotrade.position.eod_watcher import run_eod_watcher, sweep_expired_and_notify
    from autotrade.position.sl_watcher import run_sl_watcher
    from autotrade.position.tp_watcher import run_tp_watcher

    token = preflight()

    # 建唯一 discord.Client 并注入
    client = discord.Client()
    # [refactor-change] e: router 的 self-message 过滤因 client 注入而从死代码变活
    bind_client(client)
    bind(client)
    # gateway close-code 捕获：老 run_listener 在 import 时挂，现在组合根挂一次
    _install_gateway_log_capture()

    # 事件注册（discord.py 按函数名注册；handler 定义见上方模块级函数，
    # run_listener 的版本为准）
    client.event(on_ready)
    client.event(on_message)
    client.event(on_message_edit)
    client.event(on_disconnect)
    client.event(on_resumed)
    client.event(on_error)

    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop)

    # 过期仓位清扫必须在 watchers 之前：7/13 整夜 SL/TP 第一轮 tick 就对
    # 7/10 过期的 DELL 取快照 → 报错 → 300s backoff 循环，validate 连带 fail-open。
    try:
        await sweep_expired_and_notify()
    except Exception:
        logger.exception("startup expiry sweep failed (continuing)")

    # 保护性 watcher：SL 止损 / EOD 到期强平 / TP 分批止盈。
    # 之前只有 src.main（start_listener）启动它们，而这个生产入口一直没起——
    # src.main 又被硬卡禁止在 REAL 运行，等于真盘持仓完全没有自动保护。
    # watcher 内部自带 try/except + DRY_RUN 无报价时 no-op，起在这里是安全的。
    # [refactor-change] c: 持强引用，防 GC 回收。
    for task in (
        asyncio.create_task(run_sl_watcher(), name="sl_watcher"),
        asyncio.create_task(run_eod_watcher(), name="eod_watcher"),
        asyncio.create_task(run_tp_watcher(), name="tp_watcher"),
    ):
        _watcher_tasks.add(task)
        task.add_done_callback(_watcher_tasks.discard)
    logger.info("🛡️  watchers started: sl / eod / tp")

    try:
        await client.start(token)
    except discord.LoginFailure:
        logger.error("❌ Discord 登录失败：token 失效或被风控")
        try:
            await send_telegram("❌ *Listener 启动失败*\nDiscord token 失效")
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Discord client crashed: {e}")
        try:
            await send_telegram(format_error("Discord client", str(e)))
        except Exception:
            pass


def cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户 Ctrl+C 退出")


if __name__ == "__main__":
    cli()
