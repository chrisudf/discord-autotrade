"""诊断期权报价权限状态

打印：
- OpenD 连接 / 登录信息
- 全局订阅状态（subscribe quota / 已订阅 codes）
- 股票报价（baseline）
- 期权报价（验证 OPRA）

跑法：
    python -m autotrade.diag.diag_quote_permission
"""
import os
from pathlib import Path

from moomoo import OpenQuoteContext, RET_OK, SubType


def main():
    # env 读取只在 main() 内（老脚本整个 body 跑在 import 时，现收进 main）
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)

    HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
    PORT = int(os.getenv("MOOMOO_PORT", 11111))

    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        print(f"\n=== 1. 全局状态 ===")
        ret, data = ctx.get_global_state()
        if ret == RET_OK:
            # 期权权限相关字段
            keys = ["market_us", "qot_logined", "trd_logined",
                    "program_status_type", "qot_svr_ver", "server_ver"]
            for k in keys:
                if k in data:
                    print(f"  {k}: {data[k]}")
        else:
            print(f"  ❌ {data}")

        print(f"\n=== 2. 订阅配额（show_subscription）===")
        ret, data = ctx.query_subscription()
        if ret == RET_OK:
            print(f"  {data}")
        else:
            print(f"  ❌ {data}")

        print(f"\n=== 3. 股票报价（基线，应该成功）===")
        ret, data = ctx.get_market_snapshot(["US.SPY"])
        if ret == RET_OK:
            row = data.iloc[0]
            print(f"  ✅ US.SPY last={row['last_price']} bid={row['bid_price']} ask={row['ask_price']}")
        else:
            print(f"  ❌ {data}")

        print(f"\n=== 4. 期权报价（验证 OPRA）===")
        # 用一个高流动性近月期权
        test_codes = [
            "US.SPY260620C600000",
            "US.MSFT260717C440000",
            "US.AAPL260620C190000",
        ]
        for code in test_codes:
            ret_s, msg = ctx.subscribe([code], [SubType.QUOTE])
            if ret_s == RET_OK:
                print(f"  ✅ subscribed {code}")
                ret, data = ctx.get_market_snapshot([code])
                if ret == RET_OK and len(data) > 0:
                    row = data.iloc[0]
                    print(f"     last={row.get('last_price')} bid={row.get('bid_price')} "
                          f"ask={row.get('ask_price')} time={row.get('update_time')}")
                else:
                    print(f"     snapshot fail: {data}")
            else:
                print(f"  ❌ {code}: {msg}")
            break  # 一个能验证就够
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
