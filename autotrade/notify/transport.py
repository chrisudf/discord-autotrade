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
from datetime import datetime, timezone
from pathlib import Path

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


# ============ 送不出去的告警落盘 ============
# [7/31 postmortem 的第二层] watchdog.py 顶部那段的结论是"告警链路不能只挂在
# 日志上"，当时补的第二条通道就是 TG。9/3 和 9/5 两晚证明这个结论还差一层：
# **TG 自己就是死掉的那个**，而它的兜底又变回了一行日志。
#
# 9/5 实测：本机断网（Discord + OpenD + Telegram 同时死），EOD 15:50 ET 准时
# 进窗、每 30s 重试到收盘，三张当日到期合约全部卡在 no-quote，
# 90 条"需要人工处理"的告警外加 101 次 ConnectError —— 全部只剩日志里的
# WARNING 行，而日志要等第二天早上才有人读。三张合约当天到期，$714。
#
# 这套系统所有 fail-closed 的设计（熔断 / no-quote 拒卖 / 成交价闸门 /
# 买单超时）形状都是"停手 + 大声喊人"。喊人这条腿跟根因一起死的时候，
# 它们全部退化成纯粹的"停手"。这里给它补一条不依赖网络的腿。
#
# 格式刻意是单行 TSV 不是 JSONL：morning_collect.sh 的契约是"全是纯 shell、
# 不依赖任何 app"（见其文件头），jq / python 都不能用。正文里的换行压成 " / "，
# 与 digest 的 note/content 列同一个约定。
_UNDELIVERED_NAME = "undelivered_alerts.tsv"
# 落盘失败不能反过来放倒调用方，但也不能无声——每进程只抱怨一次。
_undelivered_broken = False
# 7/31 是磁盘写满引发的事故。断网期间这个文件是唯一还在增长的东西，
# 给它一个上限：超了就停写（日志仍在），不要为了记录告警把磁盘写满。
_UNDELIVERED_MAX_BYTES = 5 * 1024 * 1024


def _undelivered_path() -> Path:
    """per-call 读 env，跟 settings.log_dir 同一个口径（缺省 <repo>/logs）。"""
    log_dir = os.getenv("LOG_DIR") or str(Path(__file__).resolve().parents[2] / "logs")
    return Path(log_dir) / _UNDELIVERED_NAME


def _record_undelivered(text: str) -> None:
    """把一条没送出去的告警追加到本地文件。**任何情况下都不抛。**"""
    global _undelivered_broken
    try:
        path = _undelivered_path()
        if path.exists() and path.stat().st_size >= _UNDELIVERED_MAX_BYTES:
            if not _undelivered_broken:
                _undelivered_broken = True
                logger.error(f"[notify] {path} 已超 {_UNDELIVERED_MAX_BYTES} 字节，停止落盘")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = str(text).replace("\t", " ").replace("\n", " / ")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{ts}\t{line}\n")
    except Exception as e:
        if not _undelivered_broken:
            _undelivered_broken = True
            logger.error(f"[notify] 落盘未送达告警失败: {type(e).__name__}: {e}")


async def send_telegram(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """发送 Telegram 消息。

    Args:
        text: 已经按 parse_mode 转义好的文本
        parse_mode: "MarkdownV2" / "HTML" / None（纯文本）

    Returns:
        True 成功；False 失败（失败只记日志 + 落盘，不抛异常）
    """
    # 未配置不算"送不出去"：那是部署问题，落盘只会在 DRY_RUN / 测试里刷垃圾。
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("[Telegram] BOT_TOKEN 或 CHAT_ID 未配置，跳过通知")
        return False

    ok = await _send_telegram_inner(text, parse_mode)
    if not ok:
        _record_undelivered(text)
    return ok


async def _send_telegram_inner(text: str, parse_mode: str) -> bool:
    """原 send_telegram 的正文（配置检查已上移到调用方）。"""
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
                # [ROADMAP P1 #15] 成功原本只记 DEBUG，而错误路径上有一批**裸调**
                # send_telegram 的地方（不走 notify() 包装、不打 `[notify] TG sent`）
                # —— 日志里于是完全查不到"发没发"。8/14 的复盘据此得出
                # "操作者手机上零告警"的**反向结论**（实际很可能收到了近千条）。
                # 提到 INFO：多一行日志的成本，远低于"复盘把告警数量看反"。
                logger.info(f"[Telegram] 发送成功: {text[:50]}")
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
                    logger.info("[Telegram] 纯文本 fallback 成功")
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

    ok = _send_telegram_sync_inner(text, parse_mode)
    if not ok:
        _record_undelivered(text)
    return ok


def _send_telegram_sync_inner(text: str, parse_mode: str) -> bool:
    """原 send_telegram_sync 的正文（loop / 配置检查已上移到调用方）。"""
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
            logger.warning(f"[notify] TG send returned False（已落盘待早间摘要）: {head}")
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
