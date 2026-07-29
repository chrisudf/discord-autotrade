"""env 数值解析容错(PR#1 review:三处裸 int()/float(os.getenv) 会因 .env
手滑抛 ValueError,而它们都在消息处理路径上)。

除了 helper 本身,还钉住三个真实站点"坏值不抛异常"——这才是 review 提的病:
helper 写对了但站点没换,病照旧。
"""
from datetime import timedelta

import pytest

from autotrade.utils.envcfg import env_float, env_int


# ---------------- helper 本体 ----------------

def test_env_float_reads_valid_value(monkeypatch):
    monkeypatch.setenv("X_SEC", "42.5")
    assert env_float("X_SEC", 300.0) == 42.5


def test_env_float_unset_returns_default(monkeypatch):
    monkeypatch.delenv("X_SEC", raising=False)
    assert env_float("X_SEC", 300.0) == 300.0


@pytest.mark.parametrize("bad", ["5min", "", "300,", "abc", "  "])
def test_env_float_malformed_falls_back(monkeypatch, bad):
    monkeypatch.setenv("X_SEC", bad)
    assert env_float("X_SEC", 300.0) == 300.0


def test_env_float_nan_falls_back(monkeypatch):
    # float("nan") 解析得过,但 `age > nan` 恒 False —— 闸门被悄悄废掉
    monkeypatch.setenv("X_SEC", "nan")
    assert env_float("X_SEC", 300.0) == 300.0


def test_env_float_below_minimum_falls_back(monkeypatch):
    monkeypatch.setenv("X_SEC", "0")
    assert env_float("X_SEC", 300.0, minimum=1.0) == 300.0


def test_env_int_rejects_float_string(monkeypatch):
    # "3600.0" 不静默截断成 3600:秒数写成小数多半是单位抄错了
    monkeypatch.setenv("X_SEC", "3600.0")
    assert env_int("X_SEC", 3600) == 3600


def test_env_int_negative_below_minimum_falls_back(monkeypatch):
    monkeypatch.setenv("X_LIMIT", "-5")
    assert env_int("X_LIMIT", 200, minimum=1) == 200


def test_env_int_zero_allowed_when_minimum_zero(monkeypatch):
    # 0 对 RUNNER_PRESERVE_ALERT_WINDOW_SEC 是合法配置(不节流)
    monkeypatch.setenv("X_WIN", "0")
    assert env_int("X_WIN", 3600, minimum=0) == 0


def test_bad_value_warns_only_once(monkeypatch):
    """per-call 语义下,坏值不去重会让每条消息刷一行同样的 warning。

    日志走 loguru,caplog 抓不到(会让断言恒真地"通过"),这里挂自己的 sink。
    """
    from autotrade.utils.logger import logger

    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING")
    try:
        monkeypatch.setenv("X_SEC", "oops")
        for _ in range(5):
            assert env_float("X_SEC", 300.0) == 300.0
    finally:
        logger.remove(sink_id)

    assert len([ln for ln in lines if "X_SEC" in ln]) == 1


def test_different_bad_values_each_warn(monkeypatch):
    # 去重按 (name, 原文):env 改了个新的坏值应当再响一次,而不是被永久静音
    from autotrade.utils.logger import logger

    lines: list[str] = []
    sink_id = logger.add(lines.append, level="WARNING")
    try:
        for bad in ("oops", "nope"):
            monkeypatch.setenv("X_SEC", bad)
            env_float("X_SEC", 300.0)
    finally:
        logger.remove(sink_id)

    assert len([ln for ln in lines if "X_SEC" in ln]) == 2


# ---------------- 三个真实站点 ----------------

def test_open_age_guard_survives_bad_env(monkeypatch):
    """OPEN_SIGNAL_MAX_AGE_SEC 坏值不能让年龄闸门抛。

    抛了会冒到 router 兜底 → 所有 OPEN 被静默丢掉(闸门变断路器)。
    """
    monkeypatch.setenv("OPEN_SIGNAL_MAX_AGE_SEC", "5min")
    assert env_float("OPEN_SIGNAL_MAX_AGE_SEC", 300.0, minimum=1.0) == 300.0


def test_runner_preserve_throttle_survives_bad_env(monkeypatch):
    from autotrade.listener.dedup import runner_preserve_should_alert

    monkeypatch.setenv("RUNNER_PRESERVE_ALERT_WINDOW_SEC", "1h")
    assert runner_preserve_should_alert("NVDA 900C") is True   # 首次
    assert runner_preserve_should_alert("NVDA 900C") is False  # 窗口内第二次


def test_runner_preserve_window_default_on_bad_env(monkeypatch):
    monkeypatch.setenv("RUNNER_PRESERVE_ALERT_WINDOW_SEC", "1h")
    assert timedelta(
        seconds=env_int("RUNNER_PRESERVE_ALERT_WINDOW_SEC", 3600, minimum=0)
    ) == timedelta(hours=1)


@pytest.mark.parametrize("bad", ["lots", "0", "-1"])
def test_backfill_limit_never_non_positive(monkeypatch, bad):
    """limit<=0 会让 history() 拉回空列表,而 len(msgs) >= limit 恒真 →
    每次都判"不完整"保留锚点,回补看似在跑实则一条不喂。"""
    from autotrade.app.connection import _BACKFILL_LIMIT_DEFAULT

    monkeypatch.setenv("BACKFILL_HISTORY_LIMIT", bad)
    limit = env_int("BACKFILL_HISTORY_LIMIT", _BACKFILL_LIMIT_DEFAULT, minimum=1)
    assert limit == _BACKFILL_LIMIT_DEFAULT
    assert limit >= 1
