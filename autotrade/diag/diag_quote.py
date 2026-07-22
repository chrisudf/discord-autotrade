"""验证 moomoo OpenD 期权行情接口

用法（OpenD 已开 + 已登录）：
    python -m autotrade.diag.diag_quote US.HOOD260620C40000

支持多个 code：
    python -m autotrade.diag.diag_quote US.SPY260620P600000 US.IREN260620C60000

会打印 last_price / bid / ask / volume / 报价时间，
据此判断是否能给 SL/EOD watcher 提供真实报价。

如果返回 ret != RET_OK 或字段全空：
- 检查 moomoo 账号期权行情权限（LV1 是否订阅）
- 远期合约 SIMULATE 可能直接报 Cannot find
- 试一个近月 + 高流动性的（SPY/QQQ）排除 code 格式问题
"""
import sys
import os
from pathlib import Path

from moomoo import OpenQuoteContext, RET_OK, SubType


def main(codes: list[str]):
    # env 读取只在 main() 内
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)

    HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
    PORT = int(os.getenv("MOOMOO_PORT", 11111))

    print(f"[quote] connecting to OpenD {HOST}:{PORT}")
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        # 期权代码（含 C/P 大写）必须先 subscribe 才能拿 snapshot
        is_option = any("C" in c.split(".", 1)[-1] or "P" in c.split(".", 1)[-1]
                        and any(ch.isdigit() for ch in c) and len(c) > 12 for c in codes)
        if is_option:
            print(f"[quote] subscribing {codes}")
            ret_s, msg = ctx.subscribe(codes, [SubType.QUOTE])
            if ret_s != RET_OK:
                print(f"⚠️  subscribe failed: {msg}")
                # 继续尝试 snapshot，看错误信息

        ret, data = ctx.get_market_snapshot(codes)
        if ret != RET_OK:
            print(f"❌ get_market_snapshot failed: {data}")
            return
        print(f"\n✅ got {len(data)} row(s):\n")
        for _, row in data.iterrows():
            print(
                f"  code={row.get('code'):<30} "
                f"last={row.get('last_price')} "
                f"bid={row.get('bid_price')} "
                f"ask={row.get('ask_price')} "
                f"vol={row.get('volume')} "
                f"time={row.get('update_time')}"
            )
        # 列名导出（方便对照 moomoo 文档）
        print(f"\n[columns] {list(data.columns)[:20]}...")
    finally:
        ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # 默认拿几个常见高流动性测试
        codes = ["US.SPY", "HK.00700"]
        print(f"no args, using defaults: {codes}")
    else:
        codes = sys.argv[1:]
    main(codes)
