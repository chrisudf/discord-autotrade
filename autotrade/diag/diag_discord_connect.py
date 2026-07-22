"""Phase 1: verify Discord connection + multi-channel config loading.

跑法（cwd = repo 根）:
    python -m autotrade.diag.diag_discord_connect
"""
import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

import discord
from autotrade.config.channel_loader import registry

client = discord.Client()


@client.event
async def on_ready():
    print(f"\n✅ Logged in as: {client.user} (id={client.user.id})")
    print(f"📋 Monitored channels ({len(registry.enabled_channel_ids())}):")

    for cid in registry.enabled_channel_ids():
        cfg = registry.get(cid)
        ch = client.get_channel(cid)
        if ch is None:
            print(f"   ❌ {cfg.name} ({cid}) NOT visible — check membership/token")
        else:
            guild_name = ch.guild.name if ch.guild else "DM"
            print(f"   ✅ {cfg.name} ({cid}) → #{ch.name} @ {guild_name}")
            print(f"      trigger_users={cfg.trigger_user_ids}, qty={cfg.default_qty}, max_price={cfg.max_price}")

    print("\n👂 Listening... (Ctrl+C to stop)\n")


@client.event
async def on_message(message):
    cid = message.channel.id
    if not registry.is_monitored(cid):
        return  # 完全忽略非监控频道

    cfg = registry.get(cid)
    is_trigger = cfg.is_trigger_user(message.author.id)
    marker = "🎯 TRIGGER" if is_trigger else "📍 channel match, user mismatch"

    print(f"\n[{marker}] {message.author.name} (id={message.author.id}) in #{message.channel.name} ({cfg.name})")
    print(f"  Content: {message.content!r}")
    if message.embeds:
        print(f"  Embeds: {len(message.embeds)}")
    if message.attachments:
        print(f"  Attachments: {len(message.attachments)}")


async def main():
    # env 读取只在 main() 内，import 时零副作用
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)
    token = os.getenv("DISCORD_USER_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_USER_TOKEN not set in config/.env")
    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
