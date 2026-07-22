"""Discord token 体检：长度/引号/空格/分段数，排查复制粘贴事故。

跑法（cwd = repo 根）:
    python -m autotrade.diag.diag_token
"""
from dotenv import load_dotenv
import os


def main():
    load_dotenv('config/.env')
    t = os.getenv('DISCORD_USER_TOKEN', '')

    print(f'Token length: {len(t)}')
    print(f'First 10 chars: {repr(t[:10])}')
    print(f'Last 5 chars: {repr(t[-5:])}')
    print(f'Has spaces: {" " in t}')
    print(f'Has quotes: {chr(34) in t or chr(39) in t}')
    print(f'Segments (split by .): {len(t.split("."))}')
    print(f'Starts with Bot/Bearer: {t.startswith(("Bot ", "Bearer "))}')


if __name__ == "__main__":
    main()
