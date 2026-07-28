"""全局测试隔离：把所有 SQLite DB 切到 tmp_path。

背景（docs/TODO.md P0，2026-07-03）：
tests 里 positions_db.open_or_add(...) 直接写生产 data/trades.db，
7/3 sync 时发现 130 个 TEST/ADD/EOD/MGR/SL 等假 OPEN 仓位残留。
这些假仓位会进 get_open_symbols() 白名单和 BULK_TRIM 遍历，
真盘时 close 信号会对假 option_code 挂卖单（naked-short check 拒单 + 刷 TG）。

autouse fixture：每个测试拿到独立 tmp DB + 完整 schema，测试之间零共享。
[refactor-change f] 老结构里各 DB 模块 import 时对真实路径跑过一次 _init_db()
（CREATE TABLE IF NOT EXISTS，无害）；新结构 import 无副作用（建表移到显式
init），patch 掉 DB_PATH 后照旧重跑 _init_db() 建 tmp schema——这里现在是
测试进程里唯一的建表点。

test_risk_cost_cap.py 自己的 _isolate_env fixture 也 patch rm.DB_PATH——
两个 autouse 叠加无冲突（都指向 tmp 文件，后设的生效）。
"""

# ---- channels.json 兜底（隐私补丁之后真实文件不入库） ----
# config/channels.json 已 gitignore（真实频道/用户 ID 不进仓库），
# 新 clone / CI 上不存在。channel_loader 在 import 时就加载 registry，
# 缺文件会让整个测试收集失败——这里在任何 autotrade 模块 import 前从
# example 模板兜底。生产路径不受影响：缺文件依然大声报错。
from pathlib import Path as _Path
import shutil as _shutil

_cfg_dir = _Path(__file__).resolve().parents[1] / "config"
_channels = _cfg_dir / "channels.json"
_example = _cfg_dir / "channels.json.example"
if not _channels.exists() and _example.exists():
    _shutil.copy(_example, _channels)

import pytest

import autotrade.risk as risk_manager
from autotrade.storage import logger_db, positions_db


@pytest.fixture(autouse=True)
def _isolate_dbs(monkeypatch, tmp_path):
    monkeypatch.setattr(positions_db, "DB_PATH", tmp_path / "trades.db")
    positions_db._init_db()

    # logger_db.DB_PATH 是 CWD 相对路径 Path("data/trades.db")，
    # 从仓库根跑测试时和 positions_db 是同一个物理文件——一样要隔离
    monkeypatch.setattr(logger_db, "DB_PATH", tmp_path / "logger.db")
    logger_db._init_db()

    monkeypatch.setattr(risk_manager, "DB_PATH", tmp_path / "risk.db")
    risk_manager._init_db()

    yield


@pytest.fixture(autouse=True)
def _clear_runner_preserve_throttle():
    # 7/23 新增的 runner-preserve TG 节流是模块级 dict——不清的话
    # 先跑的测试会把后跑测试的同名仓位 TG 压掉（顺序相关的假失败）
    from autotrade.listener import dedup
    dedup._runner_preserve_alerted.clear()
    yield
    dedup._runner_preserve_alerted.clear()


@pytest.fixture(autouse=True)
def _clear_envcfg_warned():
    # envcfg 的"同一坏值只告警一次"去重集同样是模块级——断言 warning 的测试
    # 会被先跑的测试压掉（同 _clear_runner_preserve_throttle 的坑）
    from autotrade.utils import envcfg
    envcfg._warned.clear()
    yield
    envcfg._warned.clear()
