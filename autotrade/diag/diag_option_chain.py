"""测试取 MSFT 期权链

期权链（合约元数据：strikes/expiries 列表）不需要 OPRA 权限，
只需要基础股票行情即可。用来验证 OpenD 是否能识别期权合约。

用法：
    python -m autotrade.diag.diag_option_chain            # 默认 MSFT 近月
    python -m autotrade.diag.diag_option_chain AAPL 2026-07-17
"""
import sys
import os
from pathlib import Path

from datetime import date, timedelta
from moomoo import OpenQuoteContext, RET_OK


def main(symbol: str = "MSFT", expiry: str = None):
    """
    symbol: 不带 US. 前缀
    expiry: YYYY-MM-DD；None 时取最近的周五（或 +7 天）
    """
    # env 读取只在 main() 内
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)

    HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
    PORT = int(os.getenv("MOOMOO_PORT", 11111))

    if expiry is None:
        # 默认下个周五
        today = date.today()
        days_to_fri = (4 - today.weekday()) % 7 or 7
        expiry = (today + timedelta(days=days_to_fri)).isoformat()

    code = f"US.{symbol}"
    print(f"\n=== 取期权链 {code} expiry={expiry} ===")

    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        ret, data = ctx.get_option_chain(code=code, start=expiry, end=expiry)
        if ret != RET_OK:
            print(f"❌ get_option_chain failed: {data}")
            return

        if len(data) == 0:
            print(f"⚠️  返回 0 行 —— 可能 {expiry} 不是有效到期日")
            return

        print(f"✅ got {len(data)} contract(s)\n")
        print(f"[列名] {list(data.columns)}\n")

        # 按 strike 排序展示前 20 行
        if "strike_price" in data.columns:
            data = data.sort_values("strike_price")
        for _, row in data.head(20).iterrows():
            cols = {k: row.get(k) for k in ["code", "name", "strike_price",
                                             "option_type", "option_expiry_date_distance"]
                    if k in row}
            print(f"  {cols}")

        if len(data) > 20:
            print(f"  ... 共 {len(data)} 行（仅显示前 20）")

        # 抽一个 ATM 试探 quote
        if "code" in data.columns and len(data) > 0:
            sample = data.iloc[len(data) // 2]
            sample_code = sample["code"]
            print(f"\n=== 试探取一个合约的快照: {sample_code} ===")
            ret, qdata = ctx.get_market_snapshot([sample_code])
            if ret == RET_OK and len(qdata) > 0:
                qrow = qdata.iloc[0]
                print(f"  ✅ last={qrow.get('last_price')} "
                      f"bid={qrow.get('bid_price')} ask={qrow.get('ask_price')}")
            else:
                print(f"  ❌ {qdata}  ← OPRA 报价权限缺")
    finally:
        ctx.close()


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "MSFT"
    exp = sys.argv[2] if len(sys.argv) > 2 else None
    main(sym, exp)
