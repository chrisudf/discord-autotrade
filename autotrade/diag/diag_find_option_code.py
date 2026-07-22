"""
查询 moomoo 模拟盘 SPY 期权链，找出可用合约

跑法（cwd = repo 根）:
    python -m autotrade.diag.diag_find_option_code
"""
import sys

from moomoo import OpenQuoteContext, OptionType


def main():
    # env 读取只在 main() 内（老脚本整个 body 跑在 import 时，现收进 main）
    from dotenv import load_dotenv
    load_dotenv("config/.env", override=True)

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

    # 1. 查 SPY 期权到期日列表
    print("=" * 60)
    print("查询 SPY 可用到期日...")
    ret, data = ctx.get_option_expiration_date(code="US.SPY")
    if ret == 0:
        print(data.head(20))
    else:
        print(f"❌ {data}")
        ctx.close()
        sys.exit(1)

    # 2. 取第一个到期日（最近的），拉期权链
    first_exp = data.iloc[0]["strike_time"]
    print(f"\n用最近到期日: {first_exp}")
    print("=" * 60)
    print("查询期权链（前 20 个）...")
    ret, chain = ctx.get_option_chain(
        code="US.SPY",
        start=first_exp,
        end=first_exp,
        option_type=OptionType.PUT,
    )
    if ret == 0:
        print(chain[["code", "name", "strike_price"]].head(20))
    else:
        print(f"❌ {chain}")

    ctx.close()


if __name__ == "__main__":
    main()
