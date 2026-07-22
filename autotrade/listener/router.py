"""
Discord self-bot 监听器（多频道版）

主链路：
  Discord message
    ↓ channel 过滤（registry.is_monitored）
    ↓ user 过滤（cfg.is_trigger_user）
    ↓ dedup（防止 edit/重连重复处理）
    ↓ parse_signal（传 msg_ts 用 ET 日期）
    ↓ risk_manager.check_order
    ↓ broker.place_order
    ↓ record_order ← 仅 success=True 才记（避免污染配额）
    ↓ telegram 通知

注意：
- on_message_edit 只记录日志，不重新触发下单
- 一切异常都吞掉只 log，不让 Discord 链路崩
- broker.place_order 是同步函数，必须用 asyncio.to_thread 包

（重构说明：本模块只保留入口 crash barrier + 过滤/落库/raw-dedup/detect_action
路由；OPEN 编排在 open_flow，CLOSE 编排在 close_flow，事件注册在 app/main。）
"""
from datetime import datetime, timezone

from autotrade.config.channel_loader import registry
from autotrade.listener.close_flow import handle_close_signal
from autotrade.listener.dedup import _is_duplicate_raw, _seen
from autotrade.listener.open_flow import process_open
from autotrade.notify.messages import format_error
from autotrade.notify.transport import _safe_notify
from autotrade.parsing.signal_parser import detect_action
from autotrade.storage.logger_db import log_raw_signal
from autotrade.utils.logger import logger
from autotrade.utils.timeutil import ET_TZ

# [refactor-change](e) client 由组合根注入：app/main 创建唯一 discord.Client 后
# 调 bind_client()。老版本模块自建的 client 从未 login（run_listener 用的是
# 自己的 client），self-message 过滤是死代码；注入后过滤生效。
client = None


def bind_client(c):
    """[refactor-change](e) 注入 app.main 创建的唯一 discord.Client。"""
    global client
    client = c


# ============================================================
# 工具：从 message 提取 ET 日期（给 parser 用）
# ============================================================
def _extract_et_date(message) -> "date":
    """
    Discord message.created_at 是 UTC aware datetime（discord.py 保证）。
    转 ET 后取 date，用于 parser 计算 expiry（如 weekly → _next_friday）。

    FakeMessage（test_full_flow）可能没 created_at，fallback 到 utcnow。
    """
    created = getattr(message, "created_at", None)
    if created is None:
        created = datetime.now(timezone.utc)
    elif created.tzinfo is None:
        # 极端兜底（不应发生）：discord.py 始终返回 aware
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(ET_TZ).date()


# ============================================================
# 核心处理函数
# ============================================================
async def handle_message(message):
    """
    处理一条消息。message 可以是真 discord.Message，也可以是 FakeMessage。
    需要的字段：
        message.id (int)
        message.channel.id (int)
        message.channel.name (str)
        message.author.id (int)
        message.author.name (str)
        message.content (str)
        message.embeds (list, optional)
        message.attachments (list, optional)
    """
    try:
        await _handle_message_inner(message)
    except Exception as e:
        # 最后防线：任何未预料的异常都不能让信号静默消失。
        # discord.py 会把 event handler 的异常吞进默认 on_error（只写日志），
        # TG 侧完全看不到 —— 模块 docstring 承诺的"一切异常都吞掉只 log"
        # 在这里兑现，并显式报警让人工接管。
        logger.exception("handle_message crashed")
        raw = getattr(message, "content", "") or ""
        await _safe_notify(format_error(
            "handle_message crashed",
            f"{type(e).__name__}: {e}\n\nraw: {raw[:200]}",
        ))


async def _handle_message_inner(message):
    t0 = datetime.now(timezone.utc)

    # ---- 过滤 1：忽略自己发的消息 ----
    # [refactor-change](e) client 经 bind_client 注入后此过滤生效
    # （老版 client.user 恒为 None，过滤是死代码）；未注入时跳过判断。
    if client is not None and client.user and message.author.id == client.user.id:
        return

    # ---- 过滤 2：channel 必须在监听列表 ----
    cid = message.channel.id
    if not registry.is_monitored(cid):
        return

    cfg = registry.get(cid)

    # ---- 过滤 3：作者必须是触发用户 ----
    if not cfg.is_trigger_user(message.author.id):
        return

    # ---- 过滤 4：dedup ----
    if _seen(message.id):
        logger.debug(f"Skip duplicate msg {message.id}")
        return

    raw = message.content or ""
    if not raw.strip():
        # 空内容可能是纯 embed / 纯附件（图片信号）—— 现在 parser 不支持，先 skip
        logger.warning(
            f"Empty content from {message.author.name}, "
            f"embeds={len(getattr(message, 'embeds', []))}, "
            f"attachments={len(getattr(message, 'attachments', []))}"
        )
        return

    logger.info(f"📩 [{cfg.name}] {message.author.name}: {raw}")

# 落库原始信号（即使后面失败也留底）
    try:
        log_raw_signal(message.id, message.author.name, raw, t0)
    except Exception as e:
        logger.error(f"log_raw_signal failed: {e}")

    # ---- 原文级去重 ----
    # 7/14 起源频道每条消息双发（EN×2 + ZH×2，一个信号 4 条）。交易路径有
    # 指纹 dedup 兜底，但 parse-fail 的 TG 告警会跟着双响（7/14 00:13 两条
    # 一模一样的 looks-like-signal）。同频道同内容 30s 内只处理一次；
    # 放在 log_raw_signal 之后——raw_signals 照常留底，复盘不丢原文。
    if _is_duplicate_raw(cid, raw):
        logger.info(f"🔁 duplicate raw message skipped (30s window): {raw[:60]}")
        return

    # ---- 检测 OPEN / CLOSE ----
    action = detect_action(raw)
    if action == "CLOSE":
        await handle_close_signal(
            raw, message.id, channel_name=cfg.name, channel_id=cid,
        )
        return

    # ---- OPEN 编排（解析 → triage → 风控 → 下单 → 通知，见 open_flow）----
    # === [改动 Bug C] 传 msg_ts=ET 日期，避免 fallback 到 AEST 本地 ===
    # msg_date_et 在 router 侧提取后传入：_extract_et_date 归本模块，
    # open_flow 不反向 import（避免循环依赖）。
    msg_date_et = _extract_et_date(message)
    await process_open(message, raw, cfg, cid, t0, msg_date_et)
