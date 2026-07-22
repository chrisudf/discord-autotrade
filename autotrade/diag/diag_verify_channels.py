"""验证 config/channels.json 配置 + 抓历史跑 parser dry-run。

- 不下单，不写库，不发 TG，只在 stdout 打印。
- 每个 enabled channel 拉最近 N 条消息，逐条：
    1. 检查 message.channel.id == 配置 channel_id（应当永远匹配）
    2. 标注 author.id 是否在 trigger_user_ids 里
    3. 触发用户的消息丢 parser 跑一遍（signal_parser + close_parser 的 detect_action）

跑法：
    python -m autotrade.diag.diag_verify_channels [--limit 30]

权限 404 说明频道关闭了 READ_MESSAGE_HISTORY（KC 付费频道老问题），换浏览器肉眼对 ID。
"""
import os
import sys
import asyncio
import argparse
from pathlib import Path

import discord
from dotenv import load_dotenv

from autotrade.config.channel_loader import registry
from autotrade.parsing.signal_parser import parse_signal, detect_action
from autotrade.parsing.close_parser import parse_close

ENV_PATH = Path(__file__).resolve().parents[2] / "config" / ".env"


def short(s: str, n: int = 120) -> str:
    s = (s or "").replace("\n", " ⏎ ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def run_parser(content: str) -> str:
    """跑 OPEN + CLOSE 两个 parser，返回一行人类可读结论。"""
    try:
        action = detect_action(content)
    except Exception as e:
        action = f"detect_action ERROR: {e}"

    open_sig = None
    try:
        open_sig = parse_signal(content)
    except Exception as e:
        open_sig = {"error": str(e)}

    close_sig = None
    try:
        # 用空白名单跑，仅看 parser 是否把它判为 close
        close_sig = parse_close(content, open_symbols=set())
    except Exception as e:
        close_sig = {"error": str(e)}

    parts = [f"action={action}"]
    if isinstance(open_sig, dict) and open_sig.get("symbol"):
        # parse_signal 返回的字段叫 price，不是 entry_price（之前写错导致一直显示 None）
        parts.append(
            f"OPEN[{open_sig.get('symbol')} {open_sig.get('strike')}{open_sig.get('side')} "
            f"{open_sig.get('expiry')} @ ${open_sig.get('price')} tags={open_sig.get('tags')}]"
        )
    elif open_sig is None:
        parts.append("OPEN=None")
    elif isinstance(open_sig, dict) and "error" in open_sig:
        parts.append(f"OPEN_ERR={open_sig['error']}")
    else:
        parts.append("OPEN=skip")

    if isinstance(close_sig, dict) and close_sig.get("kind"):
        parts.append(
            f"CLOSE[kind={close_sig.get('kind')} symbols={close_sig.get('symbols')} "
            f"pct={close_sig.get('pct')}]"
        )
    return " | ".join(parts)


class VerifyClient(discord.Client):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
        self._done = False

    async def on_ready(self):
        if self._done:
            return
        self._done = True
        print(f"\n=== Logged in as {self.user} (id={self.user.id}) ===\n")
        try:
            for cid in registry.enabled_channel_ids():
                await self._verify_channel(cid)
        finally:
            await self.close()

    async def _verify_channel(self, cid: int):
        cfg = registry.get(cid)
        print(f"\n--- channel {cid} ({cfg.name}) ---")
        print(f"    trigger_user_ids: {cfg.trigger_user_ids}")
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except discord.Forbidden:
                print("    ❌ Forbidden: token 没权限看这个频道（没加入服务器或被踢）")
                return
            except discord.NotFound:
                print("    ❌ NotFound: channel id 错或频道被删")
                return
            except Exception as e:
                print(f"    ❌ fetch_channel error: {type(e).__name__}: {e}")
                return
        print(f"    resolved: {ch} (type={type(ch).__name__})")

        try:
            msgs = [m async for m in ch.history(limit=self.limit)]
        except discord.Forbidden:
            print("    ❌ history Forbidden: 频道关闭了 READ_MESSAGE_HISTORY")
            print("       建议：浏览器肉眼对 channel id + 触发账号 user id")
            return
        except Exception as e:
            print(f"    ❌ history error: {type(e).__name__}: {e}")
            return

        if not msgs:
            print("    ⚠️  history 为空（频道刚建或无消息）")
            return

        print(f"    fetched {len(msgs)} msgs")

        trigger_set = set(cfg.trigger_user_ids)
        cid_mismatch = 0
        trigger_msgs = 0
        author_seen: dict[int, str] = {}

        for m in msgs:
            if m.channel.id != cid:
                cid_mismatch += 1
            author_seen.setdefault(m.author.id, str(m.author))
            is_trigger = m.author.id in trigger_set
            if is_trigger:
                trigger_msgs += 1
                ts = m.created_at.strftime("%m-%d %H:%M")
                mark = "✅"
                result = run_parser(m.content)
                print(f"      {mark} [{ts}] msg={m.id}  {result}")
                print(f"           raw: {short(m.content)}")

        print(f"\n    summary: total={len(msgs)} trigger_user_hits={trigger_msgs} "
              f"channel_id_mismatch={cid_mismatch}")
        print(f"    authors seen in last {len(msgs)} msgs:")
        for aid, name in author_seen.items():
            mark = "🎯" if aid in trigger_set else " "
            print(f"      {mark} {aid} = {name}")
        if trigger_msgs == 0:
            print(f"\n    ⚠️  最近 {len(msgs)} 条没有 trigger_user_id 的消息。可能：")
            print(f"        - trigger_user_ids 配错（对比上面 authors seen）")
            print(f"        - 触发账号近期没发消息（拉更多消息 --limit 100 再试）")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30, help="每个频道拉多少条")
    args = parser.parse_args()

    # env 读取只在 main() 内
    load_dotenv(ENV_PATH, override=True)
    token = os.getenv("DISCORD_USER_TOKEN")
    if not token:
        print("ERROR: DISCORD_USER_TOKEN not in config/.env")
        sys.exit(1)

    client = VerifyClient(limit=args.limit)
    try:
        await client.start(token)
    except KeyboardInterrupt:
        pass
    finally:
        if not client.is_closed():
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
