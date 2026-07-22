"""
Channel configuration loader.
读取 config/channels.json，提供按 channel_id 查询配置的接口。
"""
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from loguru import logger

# Discord snowflake：2015 年起最少 17 位，2024 年起 19 位，2026 年继续涨
# 不到 17 位几乎一定是 typo / 误删字符（曾出现 17 位真实 ID 但极少）
_MIN_SNOWFLAKE_LEN = 17
_TYPICAL_SNOWFLAKE_LEN = 19


@dataclass
class ChannelConfig:
    channel_id: int
    name: str
    trigger_user_ids: list[int]
    default_qty: int
    max_price: float
    enabled: bool

    def is_trigger_user(self, user_id: int) -> bool:
        return user_id in self.trigger_user_ids


class ChannelRegistry:
    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[2] / "config" / "channels.json"
        self.config_path = config_path
        self._channels: dict[int, ChannelConfig] = {}
        self.reload()

    def reload(self):
        if not self.config_path.exists():
            raise FileNotFoundError(f"channels.json 不存在: {self.config_path}")
        with open(self.config_path) as f:
            raw = json.load(f)
        self._channels = {}
        for cid_str, cfg in raw.items():
            cid = int(cid_str)
            # snowflake 格式预检：太短一定是 typo
            if len(cid_str) < _MIN_SNOWFLAKE_LEN:
                logger.error(
                    f"[channel_loader] ❌ channel_id={cid_str} 看起来不是有效的 Discord snowflake "
                    f"（长度 {len(cid_str)} < {_MIN_SNOWFLAKE_LEN}，疑似 typo）— name={cfg.get('name')!r}"
                )
            elif len(cid_str) != _TYPICAL_SNOWFLAKE_LEN:
                logger.warning(
                    f"[channel_loader] channel_id={cid_str} 长度 {len(cid_str)} 不是常见 19 位 "
                    f"（极少数早期频道可能短于 19 位，请确认）— name={cfg.get('name')!r}"
                )
            for uid in cfg["trigger_user_ids"]:
                uid_str = str(uid)
                if len(uid_str) < _MIN_SNOWFLAKE_LEN:
                    logger.error(
                        f"[channel_loader] ❌ trigger_user_id={uid_str} 长度 {len(uid_str)} "
                        f"< {_MIN_SNOWFLAKE_LEN}，疑似 typo — channel={cfg.get('name')!r}"
                    )
            self._channels[cid] = ChannelConfig(
                channel_id=cid,
                name=cfg["name"],
                trigger_user_ids=[int(u) for u in cfg["trigger_user_ids"]],
                default_qty=int(cfg["default_qty"]),
                max_price=float(cfg["max_price"]),
                enabled=bool(cfg.get("enabled", True)),
            )

    def get(self, channel_id: int) -> Optional[ChannelConfig]:
        return self._channels.get(channel_id)

    def is_monitored(self, channel_id: int) -> bool:
        cfg = self._channels.get(channel_id)
        return cfg is not None and cfg.enabled

    def all_channel_ids(self) -> list[int]:
        return list(self._channels.keys())

    def enabled_channel_ids(self) -> list[int]:
        return [cid for cid, cfg in self._channels.items() if cfg.enabled]


# 全局单例
registry = ChannelRegistry()


async def validate_channels(client) -> list[tuple[int, str, str, bool]]:
    """在 on_ready 里调用，REST 校验每个 enabled 频道是否真实存在 + 可访问。

    用 fetch_channel (REST) 而非 get_channel (cache)：
    cache 在 on_ready 时可能未填充，None 既可能是真无效也可能是未缓存，区分不开。
    REST 调用能明确返回 404 (无效 id) / 403 (无权限) / 200 (有效)。

    Args:
        client: discord.Client 实例（已 logged in）

    Returns:
        失败列表 [(channel_id, name, reason, definitive), ...]；空表示全部 OK。
        definitive=True 表示确定性失败（404/403，配置错了）；
        False 表示瞬时失败（网络抖动/限流/Discord 5xx）——重连后可能自愈，
        调用方**不应**据此退出进程。
    """
    import discord  # 局部 import 避免 channel_loader 强依赖 discord 库（单测可绕过）
    failures: list[tuple[int, str, str, bool]] = []
    enabled = registry.enabled_channel_ids()
    logger.info(f"Monitoring {len(enabled)} channel(s):")
    for cid in enabled:
        cfg = registry.get(cid)
        try:
            ch = await client.fetch_channel(cid)
        except discord.NotFound:
            logger.error(f"  ❌ {cfg.name} (id={cid}) 404 NOT FOUND — channel id 错误或频道已删")
            failures.append((cid, cfg.name, "NotFound (id 错或频道已删)", True))
            continue
        except discord.Forbidden:
            logger.error(f"  ❌ {cfg.name} (id={cid}) 403 FORBIDDEN — token 无权限访问")
            failures.append((cid, cfg.name, "Forbidden (token 无权限/未加入服务器)", True))
            continue
        except Exception as e:
            logger.error(f"  ❌ {cfg.name} (id={cid}) fetch error: {type(e).__name__}: {e}")
            failures.append((cid, cfg.name, f"{type(e).__name__}: {e}", False))
            continue

        guild = ch.guild.name if getattr(ch, "guild", None) else "DM"
        logger.info(
            f"  ✅ {cfg.name} ({cid}) → #{ch.name} @ {guild} "
            f"qty={cfg.default_qty} max_price={cfg.max_price}"
        )
    return failures
