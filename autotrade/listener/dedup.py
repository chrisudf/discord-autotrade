"""listener 查重 registry 集中地（拆自 src/listener/discord_client.py）。

六个 registry：
  1. _processed_msg_ids/_processed_set（_seen）                —— msg_id 去重（edit / 重连重放）
  2. _signal_fps（_is_duplicate_signal）                       —— OPEN 信号指纹去重
  3. _close_fps（_is_duplicate_close / _unregister_close_fp） —— CLOSE 指纹去重（带回滚）
  4. _recent_raw（_is_duplicate_raw）                          —— 原文级去重
  5. _addon_alerted                                            —— add-on 告警节流
  6. _sized_entry_alerted                                      —— sized-entry 告警节流

查重语义（查即登记原子性、CLOSE 零成交回滚）自老模块逐字保留；
各查重函数原有的内联"惰性清过期 + 硬上限"提取为共享 _sweep_expired()（语义等价）。
"""
from collections import deque
from datetime import datetime, timedelta, timezone

from autotrade.utils.envcfg import env_int


def _sweep_expired(reg: dict, now, window, cap=None):
    """共享惰性 GC：清过期项；cap 给定时做硬上限保护（丢最老，恰好一个）。

    自各查重函数的内联版本提取，语义逐字等价：
      - 过期判定 `now - ts > window`（严格大于）
      - cap 淘汰：`len(reg) >= cap` 时 pop 掉 min(reg, key=reg.get)
    注意：_is_duplicate_close 的 cap 淘汰发生在查重**之后**、登记之前
    （若 fp 恰为最老条目，查重前淘汰会把 dup 误判为新信号），
    该处保留内联 cap 块，本 helper 只负责它的过期清理。
    """
    expired = [k for k, ts in reg.items() if now - ts > window]
    for k in expired:
        reg.pop(k, None)
    if cap is not None and len(reg) >= cap:
        oldest = min(reg, key=reg.get)
        reg.pop(oldest, None)


# ============================================================
# Dedup: bounded deque + set 查重，避免无限增长
# ============================================================
# 场景：同一条 message 被 on_message 和 on_message_edit 都触发；
#       或断线重连时 ready 阶段重放历史 message
_DEDUP_MAX = 2000
_processed_msg_ids: deque = deque(maxlen=_DEDUP_MAX)
_processed_set: set = set()


def _seen(msg_id: int) -> bool:
    """O(1) 检查 + 记录。返回 True 表示之前见过。"""
    if msg_id in _processed_set:
        return True
    if len(_processed_msg_ids) >= _DEDUP_MAX:
        # 即将顶出最老的 ID，同步从 set 删掉
        old = _processed_msg_ids[0]
        _processed_set.discard(old)
    _processed_msg_ids.append(msg_id)
    _processed_set.add(msg_id)
    return False


# ============================================================
# Dedup 层 2: signal fingerprint 去重 ===
# ============================================================
# 上面的 _seen() 只能拦 msg_id 重复（edit / 重连重放）。
# 但有一类场景 msg_id 不同、信号语义却相同，需要单独拦：
#
# 实测场景：
#   1. 翻译机器人独立中英文双发
#      MRVL 5/11 15:23:51（中文）+ 15:23:53（英文）间隔 2s，两条独立 msg
#   2. KC 同信号重发
#      NNE 5/11 17:57:55 + 17:58:03 间隔 8s
#   3. KC 价格修正（fill 通知）
#      MRVL 5/15 $1.10 → $.95，间隔 68s，按"订阅第一信号"原则只跟首条
#   4. 跨频道转发（KC 主频道 + enrich 翻译版同一信号）
#
# 设计要点：
#   - key = symbol|side|strike|expiry_date（不含 price/channel/tags）
#     → 价格修正 / 跨频道都能拦
#   - 5 分钟窗口（实测最长间隔 ~68s 价格修正场景，留 4x 余量）
#   - dict 存 timestamp：惰性 GC + 硬上限保护，避免内存无限增长
#
# 拦截位置：parse 成功后、风控前。拦得越早越好（省 risk_check / TG / broker）。
FINGERPRINT_WINDOW = timedelta(minutes=5)
_FP_MAX = 200
_signal_fps: dict[str, datetime] = {}


def _signal_fingerprint(sig: dict) -> str:
    """同 symbol + side + strike + expiry_date → 视为同信号"""
    return (
        f"{sig['symbol']}|{sig['side']}|"
        f"{sig['strike']}|{sig.get('expiry_date', '')}"
    )


# ============================================================
# CLOSE dedup —— 中英文双发 / 同信号重发 拦截
# ============================================================
# 场景：KC 机器人翻译流程会发英文 + 中文两条；
#       现在 ZH parser 也能解析了，两条都会触发，需要二次拦截。
# key = (kind, sorted symbols, pct)  —— 不带 lang，跨语言去重
# 窗口 60s（7/8 调整，原 5min）：
#   - 双语孪生/重发实测间隔 ≤8.5s，60s 有 7x 余量
#   - 5min 窗口误伤真实的连续 trim：7/8 "BANG! Trimmed AAPL @ 2.00" 是
#     82s 后的第二次 trim（价格都不同），被当 dup 拦掉——qty≥2 时会漏跟
_close_fps: dict[tuple, "datetime"] = {}
_CLOSE_FP_MAX = 100
CLOSE_FP_WINDOW = timedelta(seconds=60)


def _close_fingerprint(parsed: dict) -> tuple:
    """生成 CLOSE 信号 fingerprint。"""
    syms = tuple(sorted(parsed.get("symbols") or []))
    return (parsed["kind"], syms, parsed["pct"])


def _is_duplicate_close(parsed: dict) -> tuple[bool, float]:
    """查重 + 登记，**原子**（单 event loop，中间无 await）。

    7/8 教训：0005 曾把登记挪到 handler 末尾（执行成功后），但 KC bot 双发
    间隔 ~0.9s < handler 耗时 ~1.2s（含 TG await）→ 第二条穿过 dup 检查
    双重处理（连续两夜实锤，qty≥2 时会 trim 两次）。登记必须和查重同步做。

    broker 失败时的重试语义（0005 的目的）改由回滚实现：handler 末尾发现
    "零成交且有 broker 失败" → _unregister_close_fp，让下一条孪生重试。
    """
    fp = _close_fingerprint(parsed)
    now = datetime.now(timezone.utc)

    _sweep_expired(_close_fps, now, CLOSE_FP_WINDOW)

    prev_ts = _close_fps.get(fp)
    if prev_ts is not None:
        return True, (now - prev_ts).total_seconds()

    # cap 淘汰保留在查重之后、登记之前（与老版顺序一致，见 _sweep_expired 注释）
    if len(_close_fps) >= _CLOSE_FP_MAX:
        oldest = min(_close_fps, key=_close_fps.get)
        _close_fps.pop(oldest, None)
    _close_fps[fp] = now
    return False, 0.0


def _unregister_close_fp(parsed: dict):
    """回滚指纹：broker 失败且零成交时调用，让双语孪生版本充当天然重试。"""
    _close_fps.pop(_close_fingerprint(parsed), None)


def _is_duplicate_signal(sig: dict) -> tuple[bool, float]:
    """
    返回 (是否重复, 距上次秒数)。

    每次调用都先惰性清理过期项 + 硬上限保护，
    极端情况下（窗口内来 200+ 不同信号）丢最老的。
    """
    fp = _signal_fingerprint(sig)
    now = datetime.now(timezone.utc)

    # 1. 惰性清理过期项（最多 200 个，O(n) 可接受）
    # 2. 硬上限保护
    _sweep_expired(_signal_fps, now, FINGERPRINT_WINDOW, cap=_FP_MAX)

    # 3. 查重
    prev_ts = _signal_fps.get(fp)
    if prev_ts is not None:
        ago = (now - prev_ts).total_seconds()
        return True, ago

    # 4. 记录
    _signal_fps[fp] = now
    return False, 0.0


# ============================================================
# 原文级消息去重
# ============================================================
# 7/14 起源频道把每条消息发两遍（EN×2 + ZH×2）。指纹 dedup 挡住了重复下单，
# 但 parse-fail 告警、close-skipped TG、日志全部双份。同频道同原文 30s 内
# 只处理第一条。窗口刻意短：KC 隔几分钟重发同文本（如同价再 trim）是
# 真实场景，不能误吞；实测双发间隔 1-5s，30s 足够。
_RAW_DEDUP_WINDOW = timedelta(seconds=30)
_recent_raw: dict[tuple[int, str], datetime] = {}


def _is_duplicate_raw(cid: int, raw: str) -> bool:
    """同频道同原文在窗口内出现过 → True（并顺手清过期条目）。"""
    now = datetime.now(timezone.utc)
    _sweep_expired(_recent_raw, now, _RAW_DEDUP_WINDOW)
    key = (cid, raw)
    if key in _recent_raw:
        return True
    _recent_raw[key] = now
    return False


# ============================================================
# 告警节流 registry（消费端在 open_flow 的 parse-fail triage）
# ============================================================
# 双语双发 dedup：同 symbol 5 分钟内只提醒一次（7/6 场景是 4 连发）
_ADDON_ALERT_WINDOW = timedelta(minutes=5)
_addon_alerted: dict[str, datetime] = {}

# 双语双发 dedup：同 symbol 5 分钟只提醒一次（enrich 中英×2 一口气 4 条）
_sized_entry_alerted: dict[str, datetime] = {}

# [refactor-change](d) 两个告警节流 dict 获得与其它 registry 相同的惰性 GC（有界）：
# 老版只写不清，长期运行 symbol 键会无限累积。open_flow 在读写前调
# _sweep_expired(reg, now, _ADDON_ALERT_WINDOW, cap=_ALERT_THROTTLE_MAX)。
_ALERT_THROTTLE_MAX = 200

# runner-preserve 通知节流：同一仓位的"跳过 trim"TG 在窗口内只发一次。
# 7/23 实测：AVGO 415c 单张仓一夜 6 条一模一样的 runner-preserve TG——
# KC 每次喊 trim（02:00/02:18/02:34/03:12/03:19/03:51/05:06）都触发一条。
# 跳过动作本身每次照常执行并留 log；这里只压 TG 重复。
# 窗口 RUNNER_PRESERVE_ALERT_WINDOW_SEC（默认 3600s，per-call 读，改 .env 重启生效）。
_runner_preserve_alerted: dict[str, datetime] = {}


# 陈旧 OPEN(回补重放)告警节流：同 symbol 5 分钟内只提醒一次。
# 语义等同 _sized_entry_alerted，独立一个 registry 是为了不让"没下单的告警"
# 去顶掉真实入场告警的节流位。
_stale_open_alerted: dict[str, datetime] = {}


# 编辑后成为信号的告警节流：同一张合约 5 分钟内只提醒一次。
# 独立 registry 的理由同 _stale_open_alerted —— 这条路径**不下单**，
# 绝不能去占 _signal_fps 的坑：一旦占了，5 分钟内那张合约真来了实时信号
# 会被当成孪生静默丢掉（拿"没下单的告警"顶掉真实入场，是 7/28 已经踩过
# 一次的形状）。
_edit_signal_alerted: dict[str, datetime] = {}


def edit_signal_should_alert(fp: str, now: "datetime | None" = None) -> bool:
    """查即登记：窗口内同一合约第二次起返回 False。

    key 用 _signal_fingerprint(sig)（symbol|side|strike|expiry_date）而不是
    symbol —— 喊单员连着编辑同一条消息两次（先补价再改错字）会重复触发，
    但换了合约就该另外提醒一次。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    _sweep_expired(_edit_signal_alerted, now, _ADDON_ALERT_WINDOW,
                   cap=_ALERT_THROTTLE_MAX)
    if fp in _edit_signal_alerted:
        return False
    _edit_signal_alerted[fp] = now
    return True


def stale_open_should_alert(symbol: str, now: "datetime | None" = None) -> bool:
    """查即登记：窗口内同 symbol 第二次起返回 False（双语孪生只告警一次）。"""
    if now is None:
        now = datetime.now(timezone.utc)
    _sweep_expired(_stale_open_alerted, now, _ADDON_ALERT_WINDOW,
                   cap=_ALERT_THROTTLE_MAX)
    if symbol in _stale_open_alerted:
        return False
    _stale_open_alerted[symbol] = now
    return True


def runner_preserve_should_alert(pos_label: str, now: "datetime | None" = None) -> bool:
    """查即登记（同 _is_duplicate_* 的原子语义）：窗口内同仓位第二次起返回 False。

    pos_label 用 close_flow 拼的 "SYM strikeC/P" 展示串做 key——
    与 TG 文案同粒度，同一合约不同 pct 的重复提醒一并压掉。
    """
    # minimum=0 而非 1:0 是合法配置(不节流,每次都提醒)。
    window = timedelta(
        seconds=env_int("RUNNER_PRESERVE_ALERT_WINDOW_SEC", 3600, minimum=0)
    )
    if now is None:
        now = datetime.now(timezone.utc)
    _sweep_expired(_runner_preserve_alerted, now, window, cap=_ALERT_THROTTLE_MAX)
    if pos_label in _runner_preserve_alerted:
        return False
    _runner_preserve_alerted[pos_label] = now
    return True
