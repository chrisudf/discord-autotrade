"""
Telegram 通知模块

设计要点：
- 全部走 async + 模块级 AsyncClient（复用 TCP/TLS 连接）
- MarkdownV2 + escape_md，避免 Discord 原文里的 _ * ` 之类导致 400
- 429 限速时按 retry_after sleep 后重试一次
- 失败只 log 不抛，绝不阻塞主流程
"""
import os
import re
import asyncio
import httpx
from typing import Optional
from loguru import logger

# [refactor-change] 原 import-time 的 ENV_PATH + dotenv 加载已移除（契约规则 5 / 变化 f）：
# .env 由 app/main.py（及 ops/diag 脚本的 main()）统一加载，库代码 import 无副作用。
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TIMEOUT = 5.0
MAX_RETRY_ON_429 = 1

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
# 串行化发送，规避 Telegram 单 chat ~1 msg/s 限速
_send_lock = asyncio.Lock()


def _api_url() -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


async def aclose():
    """关闭模块级 client。在程序退出时调用。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# MarkdownV2 完整保留字符列表（含 `*` `_` `` ` ``——这些虽然我们也用作格式，
# 但在"外部输入"片段里必须转义，否则用户数据里恰好有 `_` 会被 Telegram 当下划线解析）。
# 关键约束：escape_md() 只能对外部输入（频道名 / symbol / 错误文本 / raw 等）调用，
# 绝不能对我们模板里主动写的 `*粗体*` / `` `code` `` 这种结构字符调用——否则格式会被吃掉。
# 完整列表见 https://core.telegram.org/bots/api#markdownv2-style
_MDV2_ESCAPE = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(s) -> str:
    """转义 MarkdownV2 里所有 reserved 字符。

    给"用户输入"用（频道名、symbol、错误文本、raw 等）。
    不给我们自己拼的 `*粗体*` 这种结构字符用。
    """
    if s is None:
        return ""
    text = str(s)
    return re.sub(rf"([{re.escape(_MDV2_ESCAPE)}])", r"\\\1", text)


async def send_telegram(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """发送 Telegram 消息。

    Args:
        text: 已经按 parse_mode 转义好的文本
        parse_mode: "MarkdownV2" / "HTML" / None（纯文本）

    Returns:
        True 成功；False 失败（失败只记日志，不抛异常）
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("[Telegram] BOT_TOKEN 或 CHAT_ID 未配置，跳过通知")
        return False

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    client = await _get_client()
    url = _api_url()

    async with _send_lock:
        for attempt in range(MAX_RETRY_ON_429 + 1):
            try:
                resp = await client.post(url, json=payload)
            except httpx.TimeoutException:
                logger.error(f"[Telegram] 超时 ({TIMEOUT}s)")
                return False
            except httpx.HTTPError as e:
                # 注意：不要直接把 e 塞日志——其 repr 会包含 request.url（含 token）
                logger.error(f"[Telegram] 网络错误: {type(e).__name__}")
                return False
            except Exception as e:
                logger.error(f"[Telegram] 异常: {type(e).__name__}: {e}")
                return False

            if resp.status_code == 200:
                logger.debug(f"[Telegram] 发送成功: {text[:50]}")
                return True

            if resp.status_code == 429 and attempt < MAX_RETRY_ON_429:
                retry_after = 1
                try:
                    retry_after = int(resp.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    pass
                logger.warning(f"[Telegram] 429 限流，{retry_after}s 后重试")
                await asyncio.sleep(retry_after)
                continue

            # 400 = 解析失败，fallback 到纯文本重发
            if resp.status_code == 400 and parse_mode:
                logger.warning(f"[Telegram] {parse_mode} 解析失败，fallback 纯文本: {resp.text[:160]}")
                payload.pop("parse_mode", None)
                try:
                    resp2 = await client.post(url, json=payload)
                except Exception as e:
                    logger.error(f"[Telegram] fallback 请求异常: {type(e).__name__}: {e}")
                    return False
                if resp2.status_code == 200:
                    logger.debug("[Telegram] 纯文本 fallback 成功")
                    return True
                logger.error(f"[Telegram] fallback 也失败 status={resp2.status_code} body={resp2.text[:200]}")
                return False

            logger.error(f"[Telegram] 发送失败 status={resp.status_code} body={resp.text[:200]}")
            return False

    return False


# ============ 同步包装 ============
# 仅供「真同步」入口使用（比如 main 启动前的健康检查脚本）。
# async 调用方请直接 `await send_telegram(...)`，不要绕道这里。

def send_telegram_sync(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """同步调用。**用纯同步 httpx，不复用模块级 async client/lock。**

    历史 bug：旧实现是 `asyncio.run(send_telegram(...))`，每次新建 event loop。
    `send_telegram` 里有模块级 `asyncio.Lock` 和 `httpx.AsyncClient`，第一次
    调用把它们绑到 loopA，loopA 退出后再次调用，loop B 操作"loopA 的锁/客户端"
    会抛 "RuntimeError: ... bound to a different event loop" 或留下未关闭的
    socket。改用纯同步 httpx.Client 后整个调用与 async 状态完全隔离。

    限制：仍然不能在 event loop 内调用（async 上下文请直接 await）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "send_telegram_sync 不能在 event loop 内调用，请直接 await send_telegram(...)"
        )

    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("[Telegram] BOT_TOKEN 或 CHAT_ID 未配置，跳过通知")
        return False

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    url = _api_url()
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
    except httpx.TimeoutException:
        logger.error(f"[Telegram-sync] 超时 ({TIMEOUT}s)")
        return False
    except httpx.HTTPError as e:
        logger.error(f"[Telegram-sync] 网络错误: {type(e).__name__}")
        return False
    except Exception as e:
        logger.error(f"[Telegram-sync] 异常: {type(e).__name__}: {e}")
        return False

    if resp.status_code == 200:
        logger.debug(f"[Telegram-sync] 发送成功: {text[:50]}")
        return True

    # 400 解析失败 → 纯文本兜底重发一次
    if resp.status_code == 400 and parse_mode:
        logger.warning(f"[Telegram-sync] {parse_mode} 解析失败，fallback 纯文本: {resp.text[:160]}")
        payload.pop("parse_mode", None)
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp2 = client.post(url, json=payload)
            if resp2.status_code == 200:
                return True
            logger.error(f"[Telegram-sync] fallback 也失败 status={resp2.status_code} body={resp2.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"[Telegram-sync] fallback 请求异常: {type(e).__name__}: {e}")
            return False

    logger.error(f"[Telegram-sync] 发送失败 status={resp.status_code} body={resp.text[:200]}")
    return False


# ============================================================
# 工具：Telegram 通知
# ============================================================
async def _safe_notify(msg: str, parse_mode: str = "MarkdownV2"):
    """发 Telegram，失败只 log 不抛。

    return 值打 log 是为了让运营在 log 里能确认 TG 链路是否工作
    （send_telegram 成功只在 debug 级；6/23 OSCR 拒单 TG 是否发出去看不出）。

    parse_mode 透传给 send_telegram，缺省与它一致。加这个参数是为了让
    「正文没按 MarkdownV2 转义、必须走纯文本」的告警也能享受上面那行
    可见性 —— 8/5 夜的睡眠告警就是这种：它直连 send_telegram(parse_mode=None)
    绕开本函数，结果全晚最该让人知道的一条告警，运维在日志里查不到送没送到。
    """
    head = msg.replace("\n", " ")[:60]
    try:
        ok = await send_telegram(msg, parse_mode=parse_mode)
        if ok:
            logger.info(f"[notify] TG sent: {head}")
        else:
            logger.warning(f"[notify] TG send returned False: {head}")
    except Exception as e:
        logger.error(f"[notify] TG raised: {type(e).__name__}: {e} (msg head: {head})")


# [7/23] 后台通知：给"不该阻塞交易路径"的消息用（目前：开仓前的解析成功预警）。
# 7/22 夜实测：NBIS 信号→挂单 1944ms / AVGO 1609ms，其中 ~1.2s 是同步等预警 TG
# 的 round-trip——对快速拉升的合约就是纯滑点成本。
# 强引用 set + done_callback（同 fill_checker.spawn 的教训：裸 create_task
# 只有弱引用，GC 可能吃掉未完成的发送任务）。
_bg_tasks: set = set()


def notify_bg(msg: str) -> None:
    """fire-and-forget 版 _safe_notify。必须在 event loop 内调用。

    语义与 await _safe_notify(msg) 相同（永不抛、成败留 log），
    仅不阻塞调用方；消息可能晚于后续同步通知到达（如"下单成功"）。
    """
    task = asyncio.create_task(_safe_notify(msg))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
