"""单笔成本上限 env-aware 行为锁定。

设计：
  - REAL: 永远 ≤ $1000，即使 env 设了更大值
  - SIMULATE / DRY_RUN: 用 env 值；不设给一个高数（$100000）
  - check_order 实时算 cap，不缓存

测试 strategy：用 monkeypatch.setattr 直接 patch module 常量绕开 .env 覆盖；
用 monkeypatch.setenv 控制 _effective_max_cost_per_order 内部读的 env。
"""
import pytest
import autotrade.risk as rm


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """每个测试独立 env 和 risk.db，避免 .env override / 跨测试熔断状态污染。"""
    monkeypatch.delenv("MOOMOO_TRD_ENV", raising=False)
    monkeypatch.delenv("MAX_COST_PER_ORDER", raising=False)
    # 切 DB 到 tmp，确保每个测试干净，不读写真 risk.db
    monkeypatch.setattr(rm, "DB_PATH", tmp_path / "test_risk.db")
    rm._init_db()  # 在新 DB 上建表
    # 让 Layer 3/4 永远不撞（我们只在测 Layer 2）
    monkeypatch.setattr(rm, "MAX_DAILY_COST", 1e9)
    monkeypatch.setattr(rm, "MAX_DAILY_ORDERS", 10_000)
    yield


# ===== _effective_max_cost_per_order 直接测 =====

def test_real_caps_at_1000_when_env_unset(monkeypatch):
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    assert rm._effective_max_cost_per_order() == 1000.0


def test_real_caps_at_1000_when_env_set_higher(monkeypatch):
    """env 设 5000 也不能突破硬卡"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.setenv("MAX_COST_PER_ORDER", "5000")
    assert rm._effective_max_cost_per_order() == 1000.0


def test_real_env_below_1000_uses_env_value(monkeypatch):
    """env 设了 500，REAL 用 500（min 取小，更保守也允许）"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.setenv("MAX_COST_PER_ORDER", "500")
    assert rm._effective_max_cost_per_order() == 500.0


def test_simulate_uses_env_value(monkeypatch):
    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    monkeypatch.setenv("MAX_COST_PER_ORDER", "5000")
    assert rm._effective_max_cost_per_order() == 5000.0


def test_simulate_unset_env_is_unlimited_ish(monkeypatch):
    """SIMULATE 默认给 100000 让测试灵活"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    assert rm._effective_max_cost_per_order() == 100000.0


def test_trd_env_case_insensitive(monkeypatch):
    """小写 'real' 也算 REAL"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "  real  ")
    assert rm._effective_max_cost_per_order() == 1000.0


# ===== check_order 端到端：Layer 1 用 setattr patch 绕过 =====

def test_check_order_real_rejects_over_1000(monkeypatch):
    """REAL: $12/contract × 1 = $1200 → Layer 2 拒"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.setattr(rm, "MAX_PRICE_PER_CONTRACT", 100.0)  # 让 Layer 1 过
    r = rm.check_order(price=12.0, qty=1)
    assert not r.passed
    assert "成本超限" in r.reason
    assert "1000" in r.detail


def test_check_order_real_allows_at_1000_boundary(monkeypatch):
    """REAL: $10 × 1 = $1000 边界（> 才拒）→ 通过"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.setattr(rm, "MAX_PRICE_PER_CONTRACT", 100.0)
    r = rm.check_order(price=10.0, qty=1)
    assert r.passed, f"边界 $1000 应允许，实际拒绝: {r.reason} / {r.detail}"


def test_check_order_simulate_allows_large_cost(monkeypatch):
    """SIMULATE: $50/contract × 1 = $5000 仍 OK（env 100000 cap）"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    monkeypatch.setenv("MAX_COST_PER_ORDER", "100000")
    monkeypatch.setattr(rm, "MAX_PRICE_PER_CONTRACT", 100.0)
    r = rm.check_order(price=50.0, qty=1)
    assert r.passed


def test_check_order_real_rejects_5000_env_user_set(monkeypatch):
    """证明 REAL 硬卡：即使 env MAX_COST_PER_ORDER=5000，$15 (cost=$1500) 仍被拒"""
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.setenv("MAX_COST_PER_ORDER", "5000")  # 用户瞎设
    monkeypatch.setattr(rm, "MAX_PRICE_PER_CONTRACT", 100.0)
    r = rm.check_order(price=15.0, qty=1)
    assert not r.passed
    assert "成本超限" in r.reason


def test_check_order_effective_price_closes_slippage_gap(monkeypatch):
    """REAL 硬顶必须按实际挂单价（含 slippage）判断，不能按信号价。

    历史缺口：信号价 $9.90 → cost $990 过检，但 broker 实际挂
    9.90 × 1.05 = $10.40 → 真实成本 $1040，穿透 $1000 硬顶。
    listener 现在传 effective_price=calc_limit_price(signal_price)。
    """
    from autotrade.policy.pricing import calc_limit_price

    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.setattr(rm, "MAX_PRICE_PER_CONTRACT", 100.0)

    # 不传 effective_price（老调用方兼容路径）：$990 过检
    assert rm.check_order(price=9.90, qty=1).passed

    # 传挂单价：9.90 → 10.40（5% 档），cost $1040 > $1000 → 拒
    est = calc_limit_price(9.90)
    assert est == 10.40
    r = rm.check_order(price=9.90, qty=1, effective_price=est)
    assert not r.passed
    assert "成本超限" in r.reason


def test_check_order_effective_price_low_price_12pct_band(monkeypatch):
    """低价合约 12% 档同样生效：$0.98 lotto × 10 张边界验证。"""
    from autotrade.policy.pricing import calc_limit_price

    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.setattr(rm, "MAX_PRICE_PER_CONTRACT", 100.0)

    # 0.98 → 1.10（12% 档），10 张 cost $1100 > $1000 → 拒
    # （按信号价算是 $980，会被放行——正是要堵的缺口）
    est = calc_limit_price(0.98)
    assert est == pytest.approx(1.10)
    r = rm.check_order(price=0.98, qty=10, effective_price=est)
    assert not r.passed
