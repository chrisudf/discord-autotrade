"""
Discord 连接健康(scripts/run_listener.py 229-429 逐字搬运):
断线防抖 / 重连风暴(storm)/ 慢性掉线(churn)检测、gateway close-code
捕获、完整重登录后的历史回补(_backfill_missed)。

模块级 client 由 app.main(组合根)通过 bind() 注入——本模块 import 时
不建连接、不挂 handler、无任何副作用。on_disconnect / on_resumed 在这里
只是普通 async 函数,由 app.main 注册到唯一 discord.Client 上。
tests 以 `import autotrade.app.connection as rl` 使用。
"""
from datetime import datetime, timedelta, timezone

from loguru import logger

from autotrade.config.channel_loader import registry
# handle_message 以裸名 import:_backfill_missed 必须经由本模块的
# `handle_message` 全局调用,tests(test_backfill)才能 monkeypatch 它。
from autotrade.listener.router import handle_message
from autotrade.notify.transport import send_telegram

# 唯一 discord.Client 由 app.main 创建后注入;import 时保持 None。
client = None


def bind(c) -> None:
    """app.main 组合根注入唯一 discord.Client(_backfill_missed 用它拉频道)。"""
    global client
    client = c


# 诊断断线原因：6/18 出现 29 次重连（vs 之前 2-4 次/天）
# discord.py-self 不会自动 log reason，得自己挂 event handler。
#
# 6/24 观察：on_disconnect 总是成对触发（< 1s 内两次），疑似 discord.py-self
# 内部 WS + HTTP 两路 close 各发一次 event。加防抖：< 3s 内的重复只算一次。
# 同时记录 disconnect → reconnect 用时，方便后续判断网络/库/系统层问题。
import time as _time
import re as _re
import logging as _stdlib_logging

_DISCONNECT_DEBOUNCE_SEC = 3.0
_last_disconnect_ts: float = 0.0  # monotonic 秒
_last_disconnect_wall: "datetime | None" = None  # 挂钟时间，供 history(after=) 回补用
_disconnect_in_progress: bool = False
_last_close_code: str = ""  # 最近一次 WS 关闭码（由下面的 logging 捕获填充）

# Storm 检测：60s 窗口内出现 >= 3 次真断线 → TG 告警一次
# 背景：6/30 04:36-04:43 出现 7min identify-rate-limit storm，bot 实际离线 7min。
# 这个窗口里若 KC 发信号会丢。debounce 只过滤成对 callback，storm 是更深层故障。
_STORM_WINDOW_SEC = 60.0
_STORM_THRESHOLD = 3
_recent_disconnects: list = []   # monotonic 时间戳 list
_storm_notified_at: float = 0.0  # 防止 storm 期间 TG 重复轰炸
_STORM_NOTIFY_COOLDOWN_SEC = 300.0  # 5 分钟内只告警一次

# 慢性 churn 检测：storm 只抓「60s 内 3 次」的急促风暴，抓不住 7/20 那种
# 每 15-20 分钟一次、持续整夜的慢性掉线（storm 从没触发，一整晚零告警）。
# 这里用 30min 滚动窗口补上：累计 >= 5 次 → TG 告警，30min 内只发一次。
_CHURN_WINDOW_SEC = 1800.0
_CHURN_THRESHOLD = 5
_churn_disconnects: list = []
_churn_notified_at: float = 0.0
_CHURN_NOTIFY_COOLDOWN_SEC = 1800.0


# 关闭码速查（来自 RFC 6455 + Discord）：
#   1000 normal closure（干净，库主动 reconnect）
#   1001 going away（peer 主动关）
#   1006 abnormal closure（没收到 close frame）—— Mac WiFi 睡 / NAT 超时 / 网络丢包典型
#   4000 unknown error / 4001 unknown opcode / 4002 decode error
#   4003 not authenticated / 4004 authentication failed
#   4007 invalid seq / 4008 rate limited / 4009 session timeout
#   4010-4014 invalid params（shard / version / 等）
# 1006 集中 → 网络/系统层；4xxx 集中 → Discord 侧 / 自身账号问题
_CLOSE_CODE_RE = _re.compile(r"\b(\d{4})\b")


class _DiscordGatewayLogCapture(_stdlib_logging.Handler):
    """抓 discord.py-self 内部 gateway log 里的 close code。

    discord.py-self 自己用 stdlib logging 打 'Webocket Closed with 1006' 这类，
    不走我们的 loguru。这里附一个 handler 转发关键消息过来，并提取关闭码
    放到 _last_close_code，供下次 on_disconnect 打到 log 里关联。
    """
    def emit(self, record):
        global _last_close_code
        try:
            msg = record.getMessage()
            lower = msg.lower()
            if not any(k in lower for k in ("close", "disconnect", "reconnect", "resumed")):
                return
            m = _CLOSE_CODE_RE.search(msg)
            if m:
                _last_close_code = m.group(1)
                logger.warning(f"[gateway code={_last_close_code}] {msg[:200]}")
            else:
                # 不带 code 但是 close/resume 类，info 级
                logger.info(f"[gateway] {msg[:200]}")
        except Exception:
            pass


def _install_gateway_log_capture():
    """启动时调一次。不要重复挂否则会重复输出。"""
    h = _DiscordGatewayLogCapture()
    h.setLevel(_stdlib_logging.INFO)
    # 挂在 discord 根 logger，覆盖 discord.client / discord.gateway / discord.http
    _stdlib_logging.getLogger("discord").addHandler(h)
    # 同时确保它的 level 够低能看到 INFO+
    _stdlib_logging.getLogger("discord").setLevel(_stdlib_logging.INFO)


# 老 run_listener 在 import 时直接调用 _install_gateway_log_capture()；
# 现在由 app.main（组合根）在启动时显式调用一次——import 本模块不再挂 handler。


async def on_disconnect():
    global _last_disconnect_ts, _last_disconnect_wall, _disconnect_in_progress
    global _last_close_code, _storm_notified_at, _churn_notified_at
    now = _time.monotonic()
    if _disconnect_in_progress and (now - _last_disconnect_ts) < _DISCONNECT_DEBOUNCE_SEC:
        # 同一次断线的成对回调，抑制重复日志
        return
    _last_disconnect_ts = now
    # 只在"未处于断线中"时记挂钟起点——重登录成功会清空它，避免连环掉线
    # 把回补起点越推越晚（要覆盖从**第一次**掉线到恢复的整段）
    if _last_disconnect_wall is None:
        _last_disconnect_wall = datetime.now(timezone.utc)
    _disconnect_in_progress = True
    code_suffix = f" code={_last_close_code}" if _last_close_code else ""
    logger.warning(f"⚠️  Discord on_disconnect fired (websocket dropped){code_suffix}")
    _last_close_code = ""

    # storm 检测：60s 窗口里累计 >= 3 次 → TG 告警一次
    _recent_disconnects.append(now)
    cutoff = now - _STORM_WINDOW_SEC
    while _recent_disconnects and _recent_disconnects[0] < cutoff:
        _recent_disconnects.pop(0)
    if len(_recent_disconnects) >= _STORM_THRESHOLD:
        if now - _storm_notified_at >= _STORM_NOTIFY_COOLDOWN_SEC:
            _storm_notified_at = now
            n = len(_recent_disconnects)
            logger.error(
                f"🌀 Discord storm: {n} disconnects in last "
                f"{_STORM_WINDOW_SEC:.0f}s — bot may be offline soon"
            )
            try:
                # 用 plain text 避免 markdown 转义出意外
                await send_telegram(
                    f"🌀 Discord 重连风暴：{_STORM_WINDOW_SEC:.0f}s 内 {n} 次断线。\n"
                    f"可能进入 identify-rate-limit 退避（最长 ~3min）。"
                    f"建议盯一下盘，必要时手动重启 listener。",
                    parse_mode=None,
                )
            except Exception as e:
                logger.warning(f"storm TG notify failed: {e}")

    # 慢性 churn 检测：30min 窗口里累计 >= 5 次 → TG 告警一次
    _churn_disconnects.append(now)
    churn_cutoff = now - _CHURN_WINDOW_SEC
    while _churn_disconnects and _churn_disconnects[0] < churn_cutoff:
        _churn_disconnects.pop(0)
    if len(_churn_disconnects) >= _CHURN_THRESHOLD:
        if now - _churn_notified_at >= _CHURN_NOTIFY_COOLDOWN_SEC:
            _churn_notified_at = now
            n = len(_churn_disconnects)
            logger.error(
                f"🌀 Discord churn: {n} disconnects in last "
                f"{_CHURN_WINDOW_SEC/60:.0f}min — network likely flapping"
            )
            try:
                await send_telegram(
                    f"🌀 Discord 慢性掉线：{_CHURN_WINDOW_SEC/60:.0f} 分钟内 {n} 次断线。\n"
                    f"多为本机网络抖动（WiFi 省电 / 路由器丢空闲连接）。"
                    f"完整重登录期间的信号已尝试自动回补，但建议检查网络。",
                    parse_mode=None,
                )
            except Exception as e:
                logger.warning(f"churn TG notify failed: {e}")


async def _backfill_missed():
    """完整重登录后回补掉线窗口内漏掉的消息。

    on_resumed 会重放 gateway 事件，但 on_ready（完整 re-IDENTIFY）不会——
    session 已丢，那段时间 KC 发的信号 on_message 根本收不到（7/20 实锤）。
    这里按 _last_disconnect_wall 拉各监听频道的历史重新喂给 handle_message；
    handle_message 顶部的 _seen(msg_id) 去重保证重放幂等，不会重复下单。
    """
    global _last_disconnect_wall
    since = _last_disconnect_wall
    _last_disconnect_wall = None  # 消费掉，避免下次重登录重复回补
    if since is None:
        return
    # 往前多看 30s 安全余量：宁可多喂（_seen 挡住）也不漏边界消息
    after = since - timedelta(seconds=30)
    total = 0
    for cid in registry.enabled_channel_ids():
        ch = client.get_channel(cid)
        if ch is None:
            continue
        try:
            async for m in ch.history(limit=50, after=after, oldest_first=True):
                total += 1
                await handle_message(m)
        except Exception as e:
            logger.warning(f"[backfill] history fetch failed for {cid}: {e}")
    if total:
        logger.info(
            f"[backfill] replayed {total} message(s) since {after.isoformat()} "
            f"(_seen dedup 保证幂等)"
        )


def _log_reconnect_time(label: str):
    """on_ready / on_resumed 复用：算 disconnect→reconnect 用时"""
    global _last_disconnect_ts, _disconnect_in_progress
    if not _disconnect_in_progress:
        return
    elapsed_ms = (_time.monotonic() - _last_disconnect_ts) * 1000
    logger.info(f"🔄 Discord {label} after {elapsed_ms:.0f}ms")
    _disconnect_in_progress = False


async def on_resumed():
    global _last_disconnect_wall
    # resume 已重放 gateway 事件，这段掉线不需要回补 → 清掉起点，
    # 避免随后的完整重登录把已重放的区间又拉一遍
    _last_disconnect_wall = None
    _log_reconnect_time("session resumed")
