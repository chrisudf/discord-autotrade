"""美东时区工具。

ET_TZ / today_et 集中于此(原先散落在 discord_client / position.manager 等模块,
函数体来自 src/position/manager.py 的 _today_et,逐字)。
各 storage 模块自己的 _utc_iso 原样保留在原模块(行为有分歧,本期不统一)。
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")


def today_et() -> date:
    return datetime.now(timezone.utc).astimezone(ET_TZ).date()
