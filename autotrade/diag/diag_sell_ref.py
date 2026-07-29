"""[0010] 验证 CLOSE 无价 fallback 的卖出参照价（生产机，只读行情，不下单）。

用法（生产机 OpenD 已开 + 已登录 + US MarketOptions Lv1 订阅生效）：
    python -m autotrade.diag.diag_sell_ref US.AVGO260725C415000
    python -m autotrade.diag.diag_sell_ref US.SPY260807C630000 US.QQQ260807P560000
    python -m autotrade.diag.diag_sell_ref            # 不带参数 = 遍历 DB 里的 OPEN 持仓

对每个 code 打印：
  - snapshot 原始行：bid / ask / last / volume / update_time / age(s)
  - 60s 新鲜度门（QUOTE_FRESHNESS_SEC）是否通过
  - get_sell_ref_price 的最终参照价（bid>0 优先，否则 last，都无/stale → None）
  - calc_sell_limit(avg_entry=0, signal_price=None, quote_ref=ref) 得到的实际挂单限价

生产机验收步骤（写给上线前的自己）：
  1. 盘中（RTH）对一个真实持仓跑：参照价应等于 bid（或 bid=0 的清淡合约
     退回 last），age 应 < 60s，最终限价 = 参照价 × (1 - SELL_SLIP)。
  2. 盘外跑：报价停在上次收盘 → age 过不了 60s 门 → 参照价 None。
     这是**预期行为**：盘外无价 CLOSE 依旧拒卖 + TG 人工接管（与 7/25
     之前语义一致），fallback 只在盘中有新鲜报价时放行。
  3. 故意跑一个不存在的 code：应打印 snapshot 失败 + 参照价 None，
     不应抛异常（_snapshot 的异常/退避机制全部继承）。
  4. 全程只建 quote_ctx（行情 socket），不碰交易通道——放心在 REAL 环境跑。

注意：脚本会在进程内强制 DRY_RUN=false，否则 get_sell_ref_price 走
mock env 路径测不到真链路。只影响本诊断进程，不写 .env。
"""
import os
import sys
import time
from pathlib import Path


def main(codes: "list[str]"):
    # env 读取只在 main() 内（对齐其它 diag 脚本）
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env", override=True)

    # 强制走真实 snapshot 路径：DRY_RUN=true 时 get_sell_ref_price 会
    # 短路到 MOCK_LAST_PRICE mock env，验证不到 OpenD 链路。
    # 本脚本只动行情（OpenQuoteContext），不创建交易 ctx，不会下单。
    if os.getenv("DRY_RUN", "true").lower() == "true":
        print("[diag_sell_ref] DRY_RUN=true 检测到，进程内临时置为 false（仅行情，只读）")
        os.environ["DRY_RUN"] = "false"

    from autotrade.broker import quote as bq
    from autotrade.broker.common import RET_OK, _quote_epoch
    from autotrade.policy.pricing import SELL_SLIP, calc_sell_limit

    if not codes:
        from autotrade.storage import positions_db
        positions_db.init()
        open_pos = positions_db.get_open_positions()
        codes = [p["option_code"] for p in open_pos]
        print(f"[diag_sell_ref] 无参数，取 DB OPEN 持仓 {len(codes)} 个: {codes}")
        if not codes:
            print("[diag_sell_ref] DB 无 OPEN 持仓，请显式传 option_code")
            return

    for code in codes:
        print(f"\n=== {code} ===")
        ret, df = bq._snapshot([code])
        if ret != RET_OK:
            print(f"  snapshot 失败: {df}")
        elif df is None or not hasattr(df, "iterrows") or len(df) == 0:
            print("  snapshot 空返回")
        else:
            now_ts = time.time()
            for _, row in df.iterrows():
                age = None
                try:
                    age = now_ts - _quote_epoch(row["update_time"])
                except Exception:
                    pass
                fresh = (age is not None and age <= bq.QUOTE_FRESHNESS_SEC)
                print(
                    f"  bid={row.get('bid_price')} ask={row.get('ask_price')} "
                    f"last={row.get('last_price')} vol={row.get('volume')}\n"
                    f"  update_time={row.get('update_time')} "
                    f"age={age if age is None else f'{age:.0f}s'} "
                    f"新鲜度门(≤{bq.QUOTE_FRESHNESS_SEC:.0f}s)="
                    f"{'通过' if fresh else '不通过(update_time 缺失时放行)'}"
                )

        ref = bq.get_sell_ref_price(code)
        limit = calc_sell_limit(0.0, None, ref)
        print(f"  → get_sell_ref_price = {ref}")
        print(
            f"  → calc_sell_limit(quote_ref) = {limit} "
            f"(= ref × (1 - SELL_SLIP {SELL_SLIP}))"
            if limit is not None
            else "  → 无参照价：CLOSE 无价信号将维持拒卖 + TG 人工接管"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
