"""
Discord 连接健康(scripts/run_listener.py 229-429 逐字搬运):
断线防抖 / 重连风暴(storm)/ 慢性掉线(churn)检测、gateway close-code
捕获、完整重登录后的历史回补(_backfill_missed)。

模块级 client 由 app.main(组合根)通过 bind() 注入——本模块 import 时
不建连接、不挂 handler、无任何副作用。on_disconnect / on_resumed 在这里
只是普通 async 函数,由 app.main 注册到唯一 discord.Client 上。
tests 以 `import autotrade.app.connection as rl` 使用。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger

from autotrade.config.channel_loader import registry
# handle_message 以裸名 import:_backfill_missed 必须经由本模块的
# `handle_message` 全局调用,tests(test_backfill)才能 monkeypatch 它。
from autotrade.listener.router import handle_message
from autotrade.notify.transport import send_telegram
from autotrade.notify.watchdog import notify_tick_error, notify_tick_ok
from autotrade.storage.logger_db import processed_msg_ids_since
from autotrade.utils.envcfg import env_int

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
    # [7/28] churn 记账改用**挂钟**：monotonic 在系统睡眠中不走（macOS），
    # 整夜睡眠+dark-wake 时 "30min 窗口" 实际横跨 8 小时——7/27 夜收盘时
    # 报 "32 disconnects in last 30min"，32 是整夜累计。storm（60s 短窗）
    # 保持 monotonic 不受影响。cooldown 同步改挂钟基准。
    now_wall = _time.time()
    _churn_disconnects.append(now_wall)
    churn_cutoff = now_wall - _CHURN_WINDOW_SEC
    while _churn_disconnects and _churn_disconnects[0] < churn_cutoff:
        _churn_disconnects.pop(0)
    if len(_churn_disconnects) >= _CHURN_THRESHOLD:
        if now_wall - _churn_notified_at >= _CHURN_NOTIFY_COOLDOWN_SEC:
            _churn_notified_at = now_wall
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


# ============================================================
# [7/28 事故] 睡眠/挂起检测(alive heartbeat)
# ============================================================
# 四夜"16-20 分钟节拍器式掉线"的真相是 **macOS 系统睡眠**(dark-wake 周期):
# 睡眠期间 gateway 死、watcher 停摆;醒来才检测到断线,而回补锚
# (_last_disconnect_wall)记在"检测到断线"= 醒来那刻往前 30s——
# **整段睡眠期的消息落在回补窗口之外,静默丢失**(7/27 夜 00:48-09:07
# 两个频道"零消息"多半就是这么丢的)。monotonic 时钟同样在睡眠中不走,
# churn 计数因此失真(已改挂钟,见 on_disconnect)。
#
# 心跳任务每 15s 记一次挂钟;两拍间隔 > 90s ⇒ 刚经历睡眠/挂起:
#   1) 把回补锚回拨到跳变前最后一拍(覆盖整段睡眠期);
#   2) client 就绪则立即回补(否则留给重登录后的 on_ready 回补);
#   3) TG 告警(1h 节流):睡眠期间保护缺位,必须让人知道。
# 治本还是别让 Mac 睡:caffeinate -is make run。
_ALIVE_BEAT_SEC = 15.0
_ALIVE_GAP_SEC = 90.0
_SLEEP_ALERT_COOLDOWN_SEC = 3600.0
_last_alive_wall: "datetime | None" = None
_sleep_alerted_wall: "datetime | None" = None


async def _on_alive_gap(prev: datetime, now: datetime):
    """心跳断档处理。prev=跳变前最后一拍,now=当前。可单测。"""
    global _last_disconnect_wall, _sleep_alerted_wall
    gap = (now - prev).total_seconds()
    logger.error(
        f"😴 [alive] 挂钟跳变 {gap:.0f}s(系统睡眠/进程挂起)——"
        f"期间 watcher 停摆、消息未接收;回补锚回拨至 {prev.isoformat()}"
    )
    # 回补锚取更早者:睡眠可能横跨多次断线检测
    if _last_disconnect_wall is None or _last_disconnect_wall > prev:
        _last_disconnect_wall = prev
    # client 就绪才立即回补;未就绪(醒来还在重连)则保留锚,
    # 重登录后的 on_ready → _backfill_missed 会消费它
    if client is not None and getattr(client, "is_ready", lambda: False)():
        try:
            await _backfill_missed()
        except Exception:
            logger.exception("[alive] gap 回补失败(重登录后仍会重试)")
    if (_sleep_alerted_wall is None
            or (now - _sleep_alerted_wall).total_seconds() >= _SLEEP_ALERT_COOLDOWN_SEC):
        _sleep_alerted_wall = now
        try:
            await send_telegram(
                f"😴 检测到系统睡眠/挂起 {gap/60:.0f} 分钟。\n"
                f"睡眠期间 SL/TP/EOD watcher 停摆、Discord 消息不接收，"
                f"已尝试回补漏掉的消息。\n"
                f"跑 bot 请用 `caffeinate -is make run` 防止 Mac 入睡。",
                parse_mode=None,
            )
        except Exception as e:
            logger.warning(f"[alive] sleep TG notify failed: {e}")


async def run_alive_heartbeat():
    """后台心跳循环,由 app.main 作为常驻 task 启动(与 watcher 同级强引用)。"""
    global _last_alive_wall
    _last_alive_wall = datetime.now(timezone.utc)
    logger.info(f"[alive] heartbeat started: beat={_ALIVE_BEAT_SEC:.0f}s gap>{_ALIVE_GAP_SEC:.0f}s 判睡眠")
    while True:
        try:
            await asyncio.sleep(_ALIVE_BEAT_SEC)
            now = datetime.now(timezone.utc)
            prev = _last_alive_wall
            _last_alive_wall = now
            if prev is not None and (now - prev).total_seconds() > _ALIVE_GAP_SEC:
                await _on_alive_gap(prev, now)
            notify_tick_ok("alive")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            notify_tick_error("alive", e)


async def startup_backfill(minutes: int):
    """[7/28] 启动回补:script 晚启动期间(实测晚 50 分钟)的消息一条收不到,
    也没有任何机制找回。minutes>0 时把回补锚拨到 now-N 分钟,复用重登录
    回补管线。陈旧 OPEN 由 open_flow 的信号年龄闸门降级为告警(不追高),
    CLOSE 照常执行(还持着就该平)——迟到的平仓好过不平。"""
    global _last_disconnect_wall
    if minutes <= 0:
        return
    anchor = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    if _last_disconnect_wall is None or _last_disconnect_wall > anchor:
        _last_disconnect_wall = anchor
    logger.info(f"[backfill] 启动回补:重放最近 {minutes} 分钟的频道历史")
    await _backfill_missed()


# 每频道每次回补最多拉多少条。50 太小:睡眠/晚启动的窗口能到几十分钟,
# 活跃频道轻松超过(0008 原版只是告警,消息照丢)。
_BACKFILL_LIMIT_DEFAULT = 200


async def _backfill_missed():
    """完整重登录后回补掉线窗口内漏掉的消息。

    on_resumed 会重放 gateway 事件，但 on_ready（完整 re-IDENTIFY）不会——
    session 已丢，那段时间 KC 发的信号 on_message 根本收不到（7/20 实锤）。
    这里按 _last_disconnect_wall 拉各监听频道的历史重新喂给 handle_message。

    幂等两层：进程内 _seen(msg_id)，跨进程 raw_signals 水位线(见下方 seen_ids)。

    [7/28 修正] 抓取失败时**不消费锚点**。原版一进函数就把
    _last_disconnect_wall 置 None,而睡眠刚醒那一刻 gateway 往往还没活过来
    (_on_alive_gap 判 is_ready() 为真只说明 ready 事件设过,不代表连接还在)——
    每个频道的 history() 各自抛异常被 except 吞掉,函数正常返回,锚点却已经
    没了。随后重登录的 on_ready 回补拿到 since=None 直接 return,
    **整段睡眠期的消息就此静默丢失**——恰好是 0008 要治的那个病。
    """
    global _last_disconnect_wall
    since = _last_disconnect_wall
    if since is None:
        return
    if client is None:
        logger.warning("[backfill] client 未注入,保留锚点等下次重试")
        return
    # 往前多看 30s 安全余量：宁可多喂（去重挡住）也不漏边界消息
    after = since - timedelta(seconds=30)
    # minimum=1:limit<=0 传给 history() 会拉回空列表,而下面 len(msgs) >= limit
    # 恒真 → 每次都判"不完整"保留锚点,回补看似在跑实则一条不喂,静默丢整段。
    limit = env_int("BACKFILL_HISTORY_LIMIT", _BACKFILL_LIMIT_DEFAULT, minimum=1)

    # 跨进程去重水位线:重启后 _seen 是空的,若不查库,startup_backfill 会把
    # 上一次运行已经执行过的 trim 再执行一遍(CLOSE 无年龄闸门)。
    try:
        seen_ids = processed_msg_ids_since(after)
    except Exception as e:
        # 查不到就退回"只有内存去重"——比不回补强,但要留痕
        logger.warning(f"[backfill] 读 raw_signals 水位线失败,仅靠内存去重: {e}")
        seen_ids = set()

    total = 0
    skipped = 0
    incomplete = False
    for cid in registry.enabled_channel_ids():
        ch = client.get_channel(cid)
        if ch is None:
            # 醒来/重连途中频道缓存还没建好 —— 这次回补不完整,锚点留着
            logger.warning(f"[backfill] channel {cid} 尚未就绪,保留锚点重试")
            incomplete = True
            continue
        try:
            # 新→旧拉取,再倒序重放。oldest_first=True + limit 的组合正好拿反:
            # 窗口内消息超过 limit 时被丢掉的是**最新**那几条(最该跟的那几条)。
            # 反过来拉,截断掉的是最老的,并且 discord.py 的 after 谓词一过界
            # 就 break,不会翻整个频道历史。
            msgs = [
                m async for m in ch.history(limit=limit, after=after, oldest_first=False)
            ]
        except Exception as e:
            logger.warning(f"[backfill] history fetch failed for {cid}: {e}")
            incomplete = True
            continue
        if len(msgs) >= limit:
            incomplete = True
            logger.warning(
                f"[backfill] channel {cid} 命中 {limit} 条上限,"
                f"更早的消息可能仍有遗漏(窗口起点 {after.isoformat()});"
                f"需要更长窗口请调 BACKFILL_HISTORY_LIMIT"
            )
        for m in reversed(msgs):  # 倒回时间正序重放
            if str(m.id) in seen_ids:
                skipped += 1
                continue
            total += 1
            await handle_message(m)

    if incomplete:
        # 锚点不消费:留给下一次 on_ready / 心跳重试。这里不再往前推,
        # 只保证不比 since 更晚(可能期间又有新的断线把锚点拨得更早)。
        if _last_disconnect_wall is None or _last_disconnect_wall > since:
            _last_disconnect_wall = since
        logger.warning(
            f"[backfill] 本次回补不完整,保留锚点 {since.isoformat()} 待重试"
        )
    elif _last_disconnect_wall == since:
        # 消费掉，避免下次重登录重复回补。只清"我们刚回补的那个锚":
        # 回补途中 _on_alive_gap 可能把锚回拨得更早(又睡了一觉),
        # 那段窗口还没回补过,不能顺手清掉。
        _last_disconnect_wall = None

    if total or skipped:
        logger.info(
            f"[backfill] replayed {total} message(s) since {after.isoformat()} "
            f"(另跳过 {skipped} 条上次运行已处理的)"
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
