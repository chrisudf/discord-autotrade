"""
broker 错误关键字集中（从 moomoo_client 拆分，元组 byte-identical）。

四组关键字：stale-session、definitely-missing（合约确认不存在）、
no-permission（无期权行情权限）、限频/配额。

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


def _is_definitely_missing(msg: str) -> bool:
    s = (msg or "").lower()
    return any(h in s for h in _DEFINITELY_MISSING_HINTS)
