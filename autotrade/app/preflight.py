"""
启动前检查(scripts/run_listener.py 的 preflight() 逐字搬运)。
组合根 app.main 在 load_dotenv 之后调用;返回 Discord token。
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

from loguru import logger

from autotrade.broker.quote import (
    QUOTE_DELAYED,
    QUOTE_ERROR,
    QUOTE_NO_PERMISSION,
    QUOTE_OK,
    probe_quote_access,
)
from autotrade.broker.trade import probe_broker
from autotrade.config.channel_loader import registry
from autotrade.risk import get_daily_stats
from autotrade.utils.envcfg import env_int


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    """跑一条 git，失败一律返回空串。启动路径上不许因为环境问题崩掉。"""
    try:
        out = subprocess.run(
            ("git", "-C", str(_REPO_ROOT)) + args,
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def build_identity() -> str:
    """"这一晚跑的到底是哪个构建" —— 一行说清。

    [8/14] 那晚 listener 在 23:15 起来，`d6fe50c` 的熔断和自动落账在 00:53
    才提交 —— 跑的是旧进程，两个修复一个都没生效。而**日志里完全看不出来**：
    复盘是靠比对 commit 时间戳和 session start 才推断出来的，属于事后考古。
    夜里出问题时第一个要排除的就是"改的东西到底上没上"，这行日志把它变成
    零成本的一眼可见。

    dirty 标记同样要紧：工作区有未提交改动时，HEAD 并不能代表实际跑的代码
    （8/13 那晚跑的就是 fa3a994 + 一批未提交的 parser 修复）。
    """
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        return "unknown（非 git 仓库或 git 不可用）"
    when = _git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")
    dirty = "有未提交改动 ⚠️" if _git("status", "--porcelain") else "干净"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    parts = [sha]
    if branch and branch != "HEAD":
        parts.append(f"@{branch}")
    if when:
        parts.append(f"({when})")
    parts.append(f"工作区{dirty}")
    return " ".join(parts)


def _probe_quote_with_timeout(timeout_sec: int) -> tuple[str, str]:
    """跑 probe_quote_access，超时就放弃并让启动继续。

    用裸 daemon 线程而不是 ThreadPoolExecutor：executor 的 __exit__ 会
    shutdown(wait=True) 去 join 那个挂住的 worker，超时就白设了；而
    concurrent.futures 的 worker 线程是非 daemon，进程退出时还会再 join 一次。
    daemon 线程两头都不拦 —— SDK 自己会超时收场（9/10 夜是 16m45s 后
    以 KeepAliveFail 结束）。

    探测失败/超时都不改变启动结果：调用方只 log，不 sys.exit。
    """
    result: list = []
    t = threading.Thread(
        target=lambda: result.append(probe_quote_access()),
        name="quote-probe", daemon=True,
    )
    t.start()
    t.join(timeout=timeout_sec)
    if result:
        return result[0]
    return QUOTE_ERROR, (
        f"探测超过 {timeout_sec}s 未返回，按未知处理并继续启动"
        "（OPRA 是否可用以盘中 watcher 实际取价为准）"
    )


def preflight() -> str:
    """启动前检查，返回 token"""
    token = os.getenv("DISCORD_USER_TOKEN", "").strip()
    if not token:
        logger.error("❌ DISCORD_USER_TOKEN 未配置")
        sys.exit(1)

    enabled_ids = registry.enabled_channel_ids()
    if not enabled_ids:
        logger.error("❌ config/channels.json 没有任何 enabled 频道")
        sys.exit(1)

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    trd_env = os.getenv("MOOMOO_TRD_ENV", "SIMULATE")

    # 实盘/模拟盘真单模式都需要 ACC_ID。空着启动 → 接到信号才报错的悲剧
    # （6/18 IWM 卖单失败就是这个原因）。
    if not dry_run:
        try:
            acc_id = int(os.getenv("MOOMOO_ACC_ID", 0))
        except ValueError:
            acc_id = 0
        if acc_id == 0:
            logger.error(
                "❌ DRY_RUN=False 但 MOOMOO_ACC_ID 未配置或为 0。\n"
                "   检查 config/.env 里 MOOMOO_ACC_ID=<数字> 是否存在且非零。\n"
                "   不强制 exit 反而下单时才暴露，会丢真实信号。"
            )
            sys.exit(1)

    logger.info("=" * 60)
    logger.info("🚀 Discord Copytrade Listener 启动")
    logger.info("=" * 60)
    # 构建标识排在最前：夜里出问题时第一个要排除的就是"改的东西到底上没上"
    logger.info(f"构建          = {build_identity()}")
    logger.info(f"DRY_RUN       = {dry_run}  {'(不会真下单)' if dry_run else '⚠️  真实下单!'}")
    logger.info(f"MOOMOO_TRD_ENV = {trd_env}")
    logger.info(f"监听频道数    = {len(enabled_ids)}")
    for cid in enabled_ids:
        cfg = registry.get(cid)
        logger.info(f"  • {cfg.name} (id={cid}, qty={cfg.default_qty}, "
                    f"max_price=${cfg.max_price}, triggers={cfg.trigger_user_ids})")

    # max_price 高额提示。不再 sys.exit——真盘单笔成本由 risk_manager Layer 2 硬卡 $1000，
    # 单张价 × 100 × qty 任何超过 $1000 的订单都会被 check_order 拒绝。
    # channels.json 的 max_price 现在主要服务 SIMULATE 测试灵活性。
    HIGH_MAX_PRICE_THRESHOLD = 50.0  # >$50/张就当成"明显放宽了 Layer 1"
    high_price_channels = [
        (cid, registry.get(cid))
        for cid in enabled_ids
        if registry.get(cid).max_price > HIGH_MAX_PRICE_THRESHOLD
    ]
    if high_price_channels:
        is_simulate = trd_env.strip().upper() == "SIMULATE"
        for cid, cfg in high_price_channels:
            if is_simulate or dry_run:
                tag = "OK · SIMULATE"
            else:
                tag = "REAL · Layer 2 $1000 兜底"
            logger.warning(
                f"  ⚠️  {cfg.name} max_price=${cfg.max_price} > ${HIGH_MAX_PRICE_THRESHOLD} [{tag}]"
            )
        if not is_simulate and not dry_run:
            logger.warning(
                "   注意：REAL 单笔成本被 risk_manager 硬卡 $1000，超额订单会在 check_order 被拒绝"
            )

    # Broker 启动探测：避免昨晚那种"运行一夜才发现 broker 链路是死的"
    logger.info("─" * 60)
    logger.info("🔍 Broker 健康探测...")
    ok, msg = probe_broker()
    if ok:
        logger.info(f"  ✅ {msg}")
    else:
        logger.error(f"  ❌ {msg}")
        if not dry_run:
            logger.error("DRY_RUN=False 但 broker 不可用，拒绝启动。修复后重试。")
            sys.exit(1)
        else:
            logger.warning("DRY_RUN=True，broker 探测失败但允许继续（不会真下单）")

    # 期权行情订阅探测：决定 SL/TP/EOD watcher 真盘是否真能工作
    # 不阻塞启动，只 log；运营自己决定是否升级订阅
    #
    # [9/10 实锤] "不阻塞启动"过去只是注释，没有任何东西在兑现它：
    # probe_quote_access 是同步 SDK 调用，那一晚它挂了 **16 分 45 秒**
    # （23:11:13 → 23:27:59），最后以 OpenD `KeepAliveFail` 收场。这段时间
    # watcher 没起、Discord 没登录 —— 而它吃掉的恰好是开盘前唯一不可压缩的
    # 那段时间（当晚紧接着又睡了 19 分钟，两段加起来横跨 09:30 ET 开盘，
    # 首单落在开盘后 18 分钟）。
    # 探测本身只是"看看行情拿不拿得到"，拿不到也应该把启动走完。丢进线程
    # 加超时：超时就按 QUOTE_ERROR 记一条，继续启动。挂住的那个线程是
    # daemon，SDK 自己会超时退出，不拦进程。
    logger.info("🔍 OPRA 行情订阅探测...")
    quote_status, quote_msg = _probe_quote_with_timeout(
        env_int("PREFLIGHT_QUOTE_TIMEOUT_SEC", 45, minimum=1))
    if quote_status == QUOTE_OK:
        logger.info(f"  ✅ {quote_msg}")
    elif quote_status == QUOTE_DELAYED:
        logger.warning(f"  ⚠️  delayed-data tier:\n{quote_msg}")
    elif quote_status == QUOTE_NO_PERMISSION:
        logger.warning(f"  ⚠️  no permission:\n{quote_msg}")
    else:  # QUOTE_ERROR
        logger.error(f"  ❌ {quote_msg}")

    stats = get_daily_stats()
    from autotrade.risk import _effective_max_cost_per_order
    per_order_cap = _effective_max_cost_per_order()
    logger.info(f"今日风控      : {stats['order_count']}/{stats['max_orders']} 单, "
                f"${stats['total_cost']}/${stats['max_cost']}")
    logger.info(f"单笔成本上限  : ${per_order_cap:,.0f} (REAL 硬卡 $1000，SIMULATE 不限)")
    if stats["circuit_broken"]:
        logger.warning(f"🚨 当日已熔断: {stats['circuit_reason']}")
    logger.info("=" * 60)

    return token
