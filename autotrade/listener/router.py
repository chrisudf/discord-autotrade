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
- on_message_edit **不下单**，只在"编辑后才成为可执行信号"时发 TG 让人工接管
  （8/5 RKLB 实锤：原消息无喊价被正确拒单，14s 后编辑补上 $1.35，当时只写了
  一行日志，整单丢失）。见 handle_message_edit。
- 一切异常都吞掉只 log，不让 Discord 链路崩
- broker.place_order 是同步函数，必须用 asyncio.to_thread 包

（重构说明：本模块只保留入口 crash barrier + 过滤/落库/raw-dedup/detect_action
路由；OPEN 编排在 open_flow，CLOSE 编排在 close_flow，事件注册在 app/main。）
"""
from datetime import datetime, timezone

from autotrade.config.channel_loader import registry
from autotrade.listener.close_flow import handle_close_signal
from autotrade.listener.dedup import (
    _is_duplicate_raw,
    _seen,
    _signal_fingerprint,
    edit_signal_should_alert,
)
from autotrade.listener.heuristics import _twin_of_recent_exec
from autotrade.listener.open_flow import process_open
from autotrade.notify.messages import format_edited_signal_alert, format_error
from autotrade.notify.transport import _safe_notify
from autotrade.parsing.signal_parser import detect_action, parse_signal
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


# ============================================================
# 消息编辑
# ============================================================
async def handle_message_edit(before, after):
    """编辑后才成为可执行信号 → TG 告警，**不下单**。

    [8/5 实锤丢单] enrich 23:51:00 发 "跟踪 $RKLB 每周 $80 看涨期权"（无喊价，
    按契约规则 3 正确拒单 + TG 告警），23:51:15 编辑该消息补上 "$1.35 填充 2%"。
    老实现只 `logger.info` 一行就返回，补进来的喊价从未进过解析链路 ——
    一张 RKLB weekly 80C @1.35 就这么丢了。

    为什么不直接喂回 handle_message 自动下单：
      1. `_seen(msg_id)` 是按 msg_id 去重的，编辑不改 id —— 原消息已处理过，
         再喂进去会被第 4 道过滤直接吞掉，看起来"接上了"实则一条不走；
      2. 绕过 _seen 就等于给同一 msg_id 开了第二条执行路径，与
         30s 原文去重 / 5min 指纹去重的语义全部错位；
      3. 编辑可能发生在几分钟甚至几小时后，限价会锚在早已走掉的喊价上
         （与 open_flow 信号年龄闸门要治的是同一个病）。
    所以这里只做"让人看见"，跟不跟由人定。真要自动化，先攒几晚误报率再说。

    只管 OPEN：编辑成平仓指令的情形没有实测语料，而 close 链路的误平代价
    远高于漏平（见 close_parser 顶部注释），不在没有证据时放开。
    """
    try:
        await _handle_message_edit_inner(before, after)
    except Exception:
        # 与 handle_message 同样的兜底：编辑路径再怎么样也不能崩 Discord 链路。
        # 这条路径不下单，所以只记日志不发 TG（避免故障时反复刷屏）。
        logger.exception("handle_message_edit crashed")


async def _handle_message_edit_inner(before, after):
    cid = after.channel.id
    if not registry.is_monitored(cid):
        return

    new_raw = after.content or ""
    old_raw = getattr(before, "content", "") or ""

    # Discord 会为"链接预览生成完毕"之类的纯 embed 变化也触发 edit 事件，
    # 此时 content 逐字未变 —— 那不是喊单员改了单，直接跳过。
    if new_raw == old_raw:
        return

    # 老行为保留：受监听频道的编辑一律留一行日志（复盘时的时间锚）
    logger.info(f"✏️  [edit] {after.channel.name}: {new_raw[:80]}")

    cfg = registry.get(cid)
    if not cfg.is_trigger_user(after.author.id):
        return
    if not new_raw.strip():
        return

    # 编辑成平仓指令不在本路径处理（见 docstring）
    if detect_action(new_raw) == "CLOSE":
        logger.info("[edit] 编辑后是 CLOSE 语义，本路径只管 OPEN，跳过")
        return

    msg_date_et = _extract_et_date(after)
    sig = parse_signal(new_raw, msg_ts=msg_date_et)
    if not isinstance(sig, dict) or sig.get("skip") or sig.get("price") is None:
        return

    # 编辑**前**就已经能解析 → 这是价格修正/错字修正，不是"补全了一个漏掉的
    # 信号"。原消息当时该下单已经下过，指纹去重也按"订阅第一信号"拦过了，
    # 再提醒只是噪音。
    old_sig = parse_signal(old_raw, msg_ts=msg_date_et) if old_raw.strip() else None
    if isinstance(old_sig, dict) and not old_sig.get("skip"):
        logger.info(
            f"[edit] 编辑前已可解析（{old_sig['symbol']} @ {old_sig.get('price')}"
            f" → {sig.get('price')}），按价格修正处理，不提醒"
        )
        return

    # 这一单刚从本频道成交过（多半是我们已经跟上的双语孪生）→ 不是漏单
    twin = _twin_of_recent_exec(new_raw, cid)
    if twin:
        logger.info(f"[edit] {twin} 刚从本频道成交过，编辑告警抑制（疑似孪生）")
        return

    fp = _signal_fingerprint(sig)
    if not edit_signal_should_alert(fp):
        logger.info(f"🔁 edit alert dedup: {fp}")
        return

    created = getattr(after, "created_at", None)
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_sec = (
        (datetime.now(timezone.utc) - created).total_seconds()
        if created is not None else 0.0
    )

    logger.warning(
        f"✏️  [edit] 编辑后成为可执行信号（未下单）: {sig['symbol']} "
        f"{sig['strike']}{sig['side'][0]} {sig.get('expiry')} @ {sig.get('price')}"
    )
    await _safe_notify(format_edited_signal_alert(
        cfg.name, sig["symbol"], sig["strike"], sig["side"],
        sig.get("expiry", ""), sig["price"], age_sec, new_raw,
    ))
