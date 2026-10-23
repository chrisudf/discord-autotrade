"""10/16、10/22 夜（review_2026-10-17.md、review_2026-10-23.md）：两晚都是电池没电。

1. 10/22 22:30 看门狗只说「在用电池」，22:46 就没电休眠：TG 要带电量和按当前耗电几点没电。
2. 10/16 没电关机、00:47 插电开机后没人拉 listener：night 任务加 RunAtLoad + 盘中补启动，
   垫片按 ONLY_BETWEEN 挡掉窗口外（白天开机、07:00 后唤醒）的那一次。
"""
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
SYS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/zsh\n" + body)
    path.chmod(0o755)


def _power(tmp_path: Path, batt: str, src: str = "Battery Power", lid: str = "No") -> str:
    """在沙盒里跑 watchdog.sh power（pmset/ioreg/curl 打桩），返回发出的 TG 正文，没发就是空串。"""
    shutil.copy(OPS / "watchdog.sh", tmp_path)
    (tmp_path / "tg.env").write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=y\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(bin_dir / "pmset", f"""case "$2" in
  ps) echo "Now drawing from '{src}'";;
  batt) echo " -InternalBattery-0 (id=1)\t{batt} present: true";;
esac
""")
    _stub(bin_dir / "ioreg", f'echo \'    "AppleClamshellState" = {lid}\'\n')
    sent = tmp_path / "sent.txt"
    _stub(tmp_path / "curl", f'for a in "$@"; do [[ $a == text=* ]] && print -r -- "${{a#text=}}" >> {sent}; done\nprintf 200\n')
    now = int(datetime(2026, 10, 22, 20, 30).timestamp())
    env = {"PATH": f"{bin_dir}:{SYS_PATH}", "WATCHDOG_CURL": str(tmp_path / "curl"),
           "WATCHDOG_NOW": str(now), "HOME": os.environ["HOME"]}
    subprocess.run(["/bin/zsh", str(tmp_path / "watchdog.sh"), "power"], env=env, check=True, capture_output=True)
    return sent.read_text() if sent.exists() else ""


def test_battery_that_dies_before_start_says_so(tmp_path):
    msg = _power(tmp_path, "20%; discharging; 0:16 remaining")
    assert msg.startswith("⛔ 现在就插电")
    assert "20%" in msg and "约 20:46 没电" in msg and "撑不到 23:11 的启动" in msg


def test_battery_that_dies_mid_session_says_so(tmp_path):
    msg = _power(tmp_path, "80%; discharging; 5:10 remaining", lid="Yes")
    assert msg.startswith("⛔")
    assert "约 01:40 没电" in msg and "撑不到 07:00 收工" in msg and "合着盖" in msg


def test_battery_that_lasts_the_night_is_a_plain_warning(tmp_path):
    msg = _power(tmp_path, "95%; discharging; 11:30 remaining")
    assert msg.startswith("⚠️ 今晚 23:11 listener 可能起不来")
    assert "95%" in msg and "撑不到" not in msg


def test_battery_without_estimate_still_reports_percent(tmp_path):
    msg = _power(tmp_path, "80%; discharging; (no estimate)")
    assert "在用电池（80%）" in msg


def test_ac_power_sends_nothing(tmp_path):
    assert _power(tmp_path, "100%; charged; 0:00 remaining", src="AC Power") == ""


def _hhmm(delta_min: int) -> str:
    return (datetime.now() + timedelta(minutes=delta_min)).strftime("%H:%M")


@pytest.fixture
def running_pat():
    """让垫片的 pgrep 守卫总能命中一个真进程：时间窗守卫哪天被改坏，测试只会失败，不会真开 Terminal。"""
    sleeper = subprocess.Popen(["sleep", "98765"])
    time.sleep(0.2)
    yield "sleep 98765"
    sleeper.kill()


def _launch(tmp_path: Path, window: str, skip_pat: str) -> str:
    shutil.copy(OPS / "launch_in_terminal.sh", tmp_path)
    env = {"PATH": SYS_PATH, "ONLY_BETWEEN": window, "SKIP_IF_RUNNING": skip_pat}
    return subprocess.run(["/bin/zsh", str(tmp_path / "launch_in_terminal.sh"), "/usr/bin/true"],
                          env=env, capture_output=True, text=True, check=True).stdout


def test_launcher_outside_window_opens_nothing(tmp_path, running_pat):
    out = _launch(tmp_path, f"{_hhmm(60)}-{_hhmm(120)}", running_pat)
    assert "不在" in out and "窗口内" in out


def test_launcher_inside_window_reaches_the_running_guard(tmp_path, running_pat):
    for window in (f"{_hhmm(-60)}-{_hhmm(60)}", f"{_hhmm(-60)}-{_hhmm(-120)}"):  # 2 小时宽 + 23 小时宽
        assert "已有进程匹配" in _launch(tmp_path, window, running_pat), window
