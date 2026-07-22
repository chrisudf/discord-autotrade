"""信号合理性守卫（纯函数层：不 import broker/storage/listener/notify）。

来源（逐字搬运，只改模块归属）：
- src/listener/discord_client.py 的 _SHORT_TAGS / _SHORT_TAG_MAX_DTE /
  _suspicious_long_dte
"""

# day_trade/scalp/lotto/0dte 隐含的 DTE 上限。30 天 = 宽松到不会误伤
# "周内 lotto 放到下下周五"，又足以拦住跨年滚动（>300 天）
_SHORT_TAG_MAX_DTE = 30
_SHORT_TAGS = {"day_trade", "scalp", "lotto", "0dte"}


def _suspicious_long_dte(signal: dict, msg_date) -> "str | None":
    """短线标签 + 解析出的 DTE > 30 天 → 返回原因文本（不下单），否则 None。"""
    expiry_d = signal.get("expiry_date")
    if not expiry_d or not msg_date:
        return None
    hit_tags = _SHORT_TAGS & set(signal.get("tags") or [])
    if not hit_tags:
        return None
    dte = (expiry_d - msg_date).days
    if dte <= _SHORT_TAG_MAX_DTE:
        return None
    return (
        f"{signal.get('symbol')} {signal.get('strike')}"
        f"{(signal.get('side') or '?')[0]} 解析到期日 {expiry_d}"
        f"（DTE={dte}），但标签 {sorted(hit_tags)} 是短线信号"
    )
