"""风控模块单元测试
覆盖 4 道防线 + 熔断 + 重置

（原 scripts/test_risk_manager.py 搁浅 assert 脚本，折叠为 pytest；期望值原样保留。

折叠适配说明：
- conftest 已把 rm.DB_PATH 隔离到 tmp——每个测试独立 DB，原脚本靠执行顺序
  传递的熔断状态（Test 6 继承 Test 5）在 test_reset 内自行重建。
- [refactor-change f] 原脚本 import 时 load_dotenv 读到 config/.env 的
  MAX_COST_PER_ORDER=500 等值；import-time load_dotenv 已移除，这里用
  fixture 显式钉住等价配置，保证 "600 > 500" 等期望不随 shell 环境漂移。
- 原脚本末尾的 cleanup()（清真实 risk.db）不再需要——tmp DB 用完即弃。）
"""
import pytest

import autotrade.risk as rm
from autotrade.risk import (
    check_order, record_order, get_daily_stats,
    is_circuit_broken, manual_reset_today,
)


@pytest.fixture(autouse=True)
def _pin_risk_config(monkeypatch):
    """钉住原脚本运行时的风控配置（原值来自 config/.env）。"""
    monkeypatch.delenv("MOOMOO_TRD_ENV", raising=False)   # → SIMULATE 路径
    monkeypatch.setenv("MAX_COST_PER_ORDER", "500")       # Layer 2 上限
    monkeypatch.setattr(rm, "MAX_PRICE_PER_CONTRACT", 5.0)
    monkeypatch.setattr(rm, "MAX_DAILY_COST", 2000.0)
    monkeypatch.setattr(rm, "MAX_DAILY_ORDERS", 10)
    yield


def test_pass():
    """Test 1: 正常订单应该通过"""
    r = check_order(price=1.5, qty=1, symbol="AMZN")
    assert r.passed, f"应该通过但被拦截: {r.reason}"


def test_price_limit():
    """Test 2: 单张价格超限"""
    r = check_order(price=6.0, qty=1, symbol="AMZN")
    assert not r.passed, "应该拦截但通过了"
    assert r.reason == "单张合约价格超限"


def test_cost_per_order_limit():
    """Test 3: 单笔成本超限"""
    # price=3, qty=2, cost=600 > 500
    r = check_order(price=3.0, qty=2, symbol="AMZN")
    assert not r.passed, "应该拦截但通过了"
    assert r.reason == "单笔订单成本超限"


def test_daily_order_count():
    """Test 4: 当日下单次数达上限"""
    manual_reset_today()  # 先清空

    # 连下 MAX_DAILY_ORDERS 次小单
    for i in range(rm.MAX_DAILY_ORDERS):
        r = check_order(price=0.5, qty=1, symbol=f"TEST{i}")
        assert r.passed, f"第 {i+1} 次应该通过"
        record_order(price=0.5, qty=1, symbol=f"TEST{i}")

    # 第 N+1 次应该被拦截
    r = check_order(price=0.5, qty=1, symbol="OVERFLOW")
    assert not r.passed
    assert r.block_rest_of_day

    # 后续任何订单都该被拦截
    r2 = check_order(price=0.1, qty=1, symbol="BLOCKED")
    assert not r2.passed
    assert r2.reason == "当日已熔断"


def test_daily_cost_limit(monkeypatch):
    """Test 5: 当日累计成本达上限 → **只拒这一笔，不停全天**。

    [9/16 契约翻转] 本用例前身断言 `block_rest_of_day is True`。那让成本上限
    和次数上限共用同一个总闸，而两者描述的是相反情形：次数撞线多半是出事了
    （8/13 夜 1918 次拒单），成本撞线只是预算花完了。
    连续三晚（9/10 / 9/14 / 9/15）的代价：9/15 那晚 02:35 撞线后连挡 4 条信号，
    其中两条自带止损价。见 lesson #51。
    """
    manual_reset_today()
    # 上限设 1800 而不是 2000：预算要**留出一点空间**，否则"贵的被拒、便宜的
    # 仍能过"这个契约根本构造不出来（花满 $2000 时连 $10 的单都过不了，
    # 那样的绿是假绿 —— 第一版就这么写错过）。
    monkeypatch.setattr(rm, "MAX_DAILY_COST", 1800.0)

    # 下 3 笔 $500，累计 $1500，剩余预算 $300
    for i in range(3):
        r = check_order(price=5.0, qty=1, symbol=f"BIG{i}")
        assert r.passed, f"第 {i+1} 笔 ($500) 应该通过"
        record_order(price=5.0, qty=1, symbol=f"BIG{i}")

    # 再来一笔 $500 会超（1500+500 > 1800）→ 拒，但**不熔断**
    r = check_order(price=5.0, qty=1, symbol="OVERFLOW")
    assert not r.passed, "超预算那一笔照样不下"
    assert not r.block_rest_of_day, "预算耗尽不是异常，不该停全天"
    assert not is_circuit_broken(), "不许写熔断记录"

    # **本条是这次改动的全部意义**：预算还够的更便宜信号仍然下得出去
    # （1500+100 ≤ 1800）。改之前它会被上一笔株连，整晚再也下不了单。
    r = check_order(price=1.0, qty=1, symbol="CHEAP")
    assert r.passed, "预算还够的更便宜信号必须照常通过"


def test_daily_order_count_still_halts_the_day():
    """反向不变量：次数上限**仍然**熔断全天 —— 它的语义是"出事了"。

    9/16 只拆开了成本那一条，这条一个字都不许动（8/13 夜的 1918 次拒单
    正是它该管的形状）。
    """
    manual_reset_today()
    for i in range(10):
        record_order(price=0.10, qty=1, symbol=f"TINY{i}")
    r = check_order(price=0.10, qty=1, symbol="ELEVENTH")
    assert not r.passed and r.block_rest_of_day
    assert is_circuit_broken()


def test_reset():
    """Test 6: 手动重置"""
    # 原脚本依赖上一个测试遗留的熔断状态；pytest 每测试独立 DB，这里显式重建
    rm._trigger_circuit_breaker("ut: 折叠脚本状态重建")
    assert is_circuit_broken(), "应该处于熔断状态（继承上一个测试）"

    manual_reset_today()

    assert not is_circuit_broken(), "重置后不应该熔断"

    r = check_order(price=1.0, qty=1, symbol="AFTER_RESET")
    assert r.passed


def test_stats():
    """Test 7: 统计查询"""
    manual_reset_today()
    record_order(price=1.5, qty=1, symbol="A")
    record_order(price=2.0, qty=1, symbol="B")

    stats = get_daily_stats()
    assert stats["order_count"] == 2
    assert stats["total_cost"] == 350  # 150 + 200
    assert stats["remaining_orders"] == rm.MAX_DAILY_ORDERS - 2
    assert stats["remaining_cost"] == rm.MAX_DAILY_COST - 350
