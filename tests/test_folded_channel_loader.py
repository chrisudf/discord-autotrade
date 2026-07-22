"""频道配置加载测试

（原 scripts/test_channel_loader.py 折叠为 pytest。断言依赖真实
config/channels.json（gitignored，含真实频道/用户 ID）；CI / 新 clone 用
channels.json.example 兜底时 KC 频道 ID 不存在——归 integration，默认不跑：
pytest -m integration tests/test_folded_channel_loader.py）
"""
import pytest

pytestmark = pytest.mark.integration

from autotrade.config.channel_loader import registry


def test_enabled_channels_resolvable():
    # Test 1: 列出所有启用频道（启用列表里的 id 必须能取回配置）
    enabled = registry.enabled_channel_ids()
    for ch_id in enabled:
        cfg = registry.get(ch_id)
        assert cfg is not None, f"启用频道 {ch_id} 取不到配置"


def test_kc_channel_lookup():
    # Test 2: 查询 KC 频道
    kc = registry.get(1443396572581859405)
    assert kc is not None, "KC 频道应该存在"
    # 原脚本打印项转断言：KC 本人是触发人，路人不是
    assert kc.is_trigger_user(1426229538722938880) is True
    assert kc.is_trigger_user(9999999999) is False


def test_missing_channel_returns_none():
    # Test 3: 查询不存在的频道
    fake = registry.get(9999999999)
    assert fake is None


def test_is_monitored():
    # Test 4: is_monitored 快速判断
    assert registry.is_monitored(1443396572581859405) is True
    assert registry.is_monitored(9999999999) is False
