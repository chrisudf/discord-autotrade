"""
测试 moomoo OpenD 期权权限和期权链获取
分层测试：基础连接 → 期权链 → 报价 → 订阅

这是一个交互式集成诊断脚本，依赖本地运行的 OpenD + 真实 moomoo 连接。

两种跑法：
    python tests/test_option_chain.py                       # 直接当脚本跑（推荐）
    INTEGRATION_TESTS=1 pytest -m integration tests/test_option_chain.py
                                                            # 在 pytest 下也想跑就设 env

默认 pytest 会跳过这个文件，避免没 OpenD 时 CI 报错
（module-level skip + integration 标记双保险；pytest.ini 默认 -m "not integration"）。
"""
import os
import sys

import pytest

# 仅在 pytest 收集时跳过；直接 `python file.py` 不应触发 pytest.skip
# 否则会以 module-level Skipped exception 退出
if __name__ != "__main__" and not os.getenv("INTEGRATION_TESTS"):
    pytest.skip(
        "需要本地 OpenD + moomoo 连接；设 INTEGRATION_TESTS=1 强制跑",
        allow_module_level=True,
    )

# 交互式集成测试：即使设了 INTEGRATION_TESTS，默认 -m "not integration" 仍会反选
pytestmark = pytest.mark.integration

from datetime import datetime, timedelta
from moomoo import (
    OpenSecTradeContext, OpenQuoteContext,
    TrdEnv, TrdMarket, SecurityFirm,
    Market, OptionType, OptionCondType,
    RET_OK, RET_ERROR,
    SubType,
)

# ============ 配置 ============
HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
PORT = int(os.getenv("MOOMOO_PORT", "11111"))
SYMBOL = "MSFT"
MARKET = Market.US

# ============ 工具 ============
def section(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")

def show_ret(ret, data, label=""):
    if ret != RET_OK:
        print(f"❌ {label} 失败: {data}")
        return False
    print(f"✅ {label} 成功")
    return True

# ============ Test 1: 基础连接 ============
def test_connection():
    section("Test 1: OpenD 连接")
    try:
        quote_ctx = OpenQuoteContext(host=HOST, port=PORT)
        ret, data = quote_ctx.get_global_state()
        if show_ret(ret, data, "get_global_state"):
            print(f"   市场状态: {data}")
        return quote_ctx
    except Exception as e:
        print(f"❌ 连接异常: {e}")
        sys.exit(1)

# ============ Test 2: 期权到期日列表 ============
def test_expiration_dates(quote_ctx):
    section(f"Test 2: 获取 {SYMBOL} 期权到期日列表")
    ret, data = quote_ctx.get_option_expiration_date(
        code=f"US.{SYMBOL}"
    )
    if not show_ret(ret, data, "get_option_expiration_date"):
        return []
    
    print(f"   到期日总数: {len(data)}")
    print(f"   前 10 个到期日:")
    for _, row in data.head(10).iterrows():
        print(f"     {row['strike_time']}  (option_expiry_date_distance={row.get('option_expiry_date_distance', 'N/A')})")
    
    return data['strike_time'].tolist()

# ============ Test 3: 期权链 ============
def test_option_chain(quote_ctx, expiry_date):
    section(f"Test 3: 获取 {SYMBOL} {expiry_date} 期权链")
    
    ret, data = quote_ctx.get_option_chain(
        code=f"US.{SYMBOL}",
        start=expiry_date,
        end=expiry_date,
        option_type=OptionType.ALL,
        option_cond_type=OptionCondType.ALL,
    )
    if not show_ret(ret, data, "get_option_chain"):
        return None
    
    calls = data[data['option_type'] == 'CALL']
    puts = data[data['option_type'] == 'PUT']
    
    print(f"   合约总数: {len(data)} (CALL: {len(calls)}, PUT: {len(puts)})")
    
    if len(calls) > 0:
        # 显示 ATM 附近 5 个 CALL
        strikes = sorted(calls['strike_price'].unique())
        mid_idx = len(strikes) // 2
        print(f"\n   ATM 附近 CALL 合约 (中间 5 个 strike):")
        sample_strikes = strikes[max(0,mid_idx-2):mid_idx+3]
        for strike in sample_strikes:
            row = calls[calls['strike_price'] == strike].iloc[0]
            print(f"     {row['code']}  strike={strike}")
    
    return data

# ============ Test 4: 期权报价 (snapshot) ============
def test_option_snapshot(quote_ctx, option_codes):
    section(f"Test 4: 获取期权 snapshot 报价")
    
    sample = option_codes[:3]
    print(f"   测试合约: {sample}")
    
    ret, data = quote_ctx.get_market_snapshot(sample)
    if not show_ret(ret, data, "get_market_snapshot"):
        return False
    
    for _, row in data.iterrows():
        print(f"\n   {row['code']}")
        print(f"     last_price: {row.get('last_price', 'N/A')}")
        print(f"     bid: {row.get('bid_price', 'N/A')} x {row.get('bid_vol', 'N/A')}")
        print(f"     ask: {row.get('ask_price', 'N/A')} x {row.get('ask_vol', 'N/A')}")
        print(f"     volume: {row.get('volume', 'N/A')}")
        print(f"     open_interest: {row.get('option_open_interest', 'N/A')}")
        print(f"     iv: {row.get('option_implied_volatility', 'N/A')}")
    
    return True

# ============ Test 5: 期权订阅 + 实时报价 ============
def test_option_subscribe(quote_ctx, option_codes):
    section(f"Test 5: 订阅期权实时报价")
    
    sample = option_codes[:1]
    print(f"   订阅合约: {sample}")
    
    ret, data = quote_ctx.subscribe(sample, [SubType.QUOTE])
    if not show_ret(ret, data, "subscribe"):
        return False
    
    # 拉一次实时报价
    ret, data = quote_ctx.get_stock_quote(sample)
    if not show_ret(ret, data, "get_stock_quote"):
        return False
    
    for _, row in data.iterrows():
        print(f"\n   {row['code']}")
        print(f"     last_price: {row.get('last_price', 'N/A')}")
        print(f"     update_time: {row.get('data_date', 'N/A')} {row.get('data_time', 'N/A')}")
    
    quote_ctx.unsubscribe(sample, [SubType.QUOTE])
    return True

# ============ Test 6: 交易权限（关键，看期权交易权限）============
def test_trade_permission():
    section("Test 6: 期权交易权限")
    try:
        trd_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=HOST, port=PORT,
            security_firm=SecurityFirm.FUTUINC,
        )
        
        # 模拟盘账户列表
        ret, data = trd_ctx.get_acc_list()
        if show_ret(ret, data, "get_acc_list"):
            print(f"\n   账户列表:")
            for _, row in data.iterrows():
                print(f"     acc_id={row['acc_id']}, trd_env={row['trd_env']}, "
                      f"acc_type={row.get('acc_type', 'N/A')}, "
                      f"sim_acc_type={row.get('sim_acc_type', 'N/A')}, "
                      f"trdmarket_auth={row.get('trdmarket_auth', 'N/A')}")
        
        trd_ctx.close()
    except Exception as e:
        print(f"❌ 交易上下文异常: {e}")

# ============ Main ============
def main():
    print(f"测试目标: {SYMBOL}")
    print(f"OpenD: {HOST}:{PORT}")
    print(f"时间: {datetime.now()}")
    
    # 1. 连接
    quote_ctx = test_connection()
    
    # 2. 到期日
    expiry_dates = test_expiration_dates(quote_ctx)
    if not expiry_dates:
        print("\n⚠️  无法获取到期日，权限问题或合约代码错误")
        quote_ctx.close()
        return
    
    # 3. 期权链（用最近的到期日）
    nearest_expiry = expiry_dates[0]
    print(f"\n   使用最近到期日: {nearest_expiry}")
    option_data = test_option_chain(quote_ctx, nearest_expiry)
    if option_data is None or len(option_data) == 0:
        quote_ctx.close()
        return
    
    option_codes = option_data['code'].tolist()
    
    # 4. snapshot 报价
    test_option_snapshot(quote_ctx, option_codes)
    
    # 5. 订阅实时报价
    test_option_subscribe(quote_ctx, option_codes)
    
    quote_ctx.close()
    
    # 6. 交易权限
    test_trade_permission()
    
    print("\n" + "="*60)
    print("测试完成")
    print("="*60)

if __name__ == "__main__":
    main()