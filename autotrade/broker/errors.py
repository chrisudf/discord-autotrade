"""
broker 错误关键字集中（从 moomoo_client 拆分，元组 byte-identical）。

五组关键字：stale-session、definitely-missing（合约确认不存在）、
no-permission（无期权行情权限）、限频/配额、确定性拒单（重试无意义）。

纯常量模块：不 import 任何兄弟模块，common/trade/quote 单向依赖它。
"""

# moomoo 会话/账户失效返回的关键字（小写匹配）。命中时 reset ctx + 重试一次。
# 之所以放白名单：避免把"价格无效""超额"等业务拒单也当成 stale 反复重试。
_STALE_SESSION_HINTS = (
    "no one available account",   # SIMULATE 会话过期 / 账户挂起
    "account is not unlock",      # 真盘 unlock 状态丢失
    "session",                    # 通用 session 失效
    "not login",                  # OpenD 失连
)

# 明确表示"合约不存在"的错误关键字。只有命中这些才 fail-closed 拒单；
# 其余失败（quota backoff / 网络抖动 / OpenD 重启中 / 未知错误）一律 fail-open
# 放行给 broker 自己判 —— 预校验是优化不是闸门，它挂了不能把合法信号拒掉。
_DEFINITELY_MISSING_HINTS = (
    "unknown stock", "cannot find", "no such", "invalid stock", "stock not exist",
)

# 无期权行情权限提示词（小写匹配）。命中 → 长退避 / validate 降级 fail-open。
_NO_PERMISSION_HINTS = ("no permission", "quote permission")

# 限频/配额提示词（小写匹配）。7/8 实测 moomoo 的限频报错原文是
# "request failed due to high frequency. Maximum 60 times per 30 seconds."
# ——不含 quota/limit 字样，旧关键词接不住 → watcher 不退避持续硬打。
_RATE_LIMIT_HINTS = ("quota", "limit", "high frequency", "frequent")


# 确定性卖出拒单：**同样的单再下一百次也是同样的结果**，重试纯粹是刷屏。
# [8/13 MU] naked-short 拒单（broker 0 长仓 / DB 说 2 张）在 TP 侧被以 5s 一轮
# 的节奏重试了 1918 次，从 00:11 刷到 06:00。这类拒单的根因是 DB 与 broker
# 脱钩或合约压根不存在，只能靠对账 / 人工介入解决，退避多久都不会自愈——
# 所以 retry_guard 命中这组词直接熔断，不走"退避 N 次再熔断"。
# 与 _STALE_SESSION_HINTS 的白名单思路一致：宁可漏判成"瞬时"（照旧退避重试），
# 也不要把真瞬时的失败错判成确定性而永久停掉一条止盈路径。
#
# 刻意**不**收进来的近邻：`naked-short check failed (position_list_query
# exception)` —— 那是查询链路自己抖了（OpenD 重启中 / 超时），不是对仓位的
# 判断，重试有意义，归瞬时组走退避。
_DETERMINISTIC_REJECT_HINTS = (
    "naked-short refused",   # broker 长仓不足，本地 DB 虚高
)


def _is_definitely_missing(msg: str) -> bool:
    s = (msg or "").lower()
    return any(h in s for h in _DEFINITELY_MISSING_HINTS)


def is_deterministic_reject(msg: str) -> bool:
    """拒单原因是否确定性（重试无意义）。合约确认不存在也算。"""
    s = (msg or "").lower()
    return (any(h in s for h in _DETERMINISTIC_REJECT_HINTS)
            or _is_definitely_missing(s))
