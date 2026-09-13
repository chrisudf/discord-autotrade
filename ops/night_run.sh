#!/bin/zsh
# 每晚 23:15（本机时区）跑 listener。
#
# 本脚本在 Terminal.app 窗口里执行，不是由 launchd 直接执行 ——
# launchd 读不了 ~/Desktop（TCC），中间隔着 launch_in_terminal.sh 这层垫片，
# 原因见那个文件的注释。所以这里可以放心读写项目目录。
set -u

# 路径从脚本位置推导，不写死 —— 换机器 / 换目录都不用改。
PROJ="${0:A:h:h}"
SESSION_LOG="$PROJ/logs/session_$(date +%Y-%m-%d).log"
OPS_LOG="$PROJ/logs/ops.log"

mkdir -p "$PROJ/logs"
log_ops() { echo "$(date '+%F %T') night_run: $*" >> "$OPS_LOG"; }

# ===== 互斥锁：唯一一个原子的"占坑"动作 =====
#
# [9/11 实锤 -$338] 那晚跑了**两个** listener（pid 30739/30740，ops.log 两行
# `night_run: starting` 时间戳同为 23:27:25）。指纹去重是进程内的，两个进程
# 互不知情 —— `$BE $275C` 被同一毫秒下了两单（order 2105543/2105544），
# 一笔 0DTE 彩票买成 4 张，亏损从 -$338 变成 -$676；卖出侧另有 3 次
# `Not enough positions`（一个进程卖掉、另一个拿到拒单）。
#
# 成因是 TOCTOU，不是守卫写错了：机器在 23:11/15/20/25 四个触发点时都睡着，
# launchd 把错过的任务攒到唤醒后**同一秒**一起放。下面那道 pgrep 和垫片里
# 那道查的都是 `bin/python -m autotrade.app.main` —— 一个**还没被拉起来的**
# 进程。同一秒里谁都看不见谁，两道全过。**检查和占坑必须是同一个原子动作**，
# 而 pgrep 只是检查。
#
# launchd 自己的单实例保证也盖不住：垫片 `exec open -a Terminal` 之后立刻退出，
# launchd 认为这一轮结束就放下一轮；真正干活的本脚本在 Terminal 那边。
#
# 用 mkdir：POSIX 下它是原子的，成功即独占（macOS 没有自带 flock(1)）。
LOCK="$PROJ/logs/.night.lock"

_lock_holder_alive() {
  # 陈旧锁判定要**两个**条件都成立才算"真的有人在跑"。只看 kill -0 不够：
  # 重启后 PID 会被复用，撞上一个无关进程就会让当晚永远起不来 —— 那个失败
  # 方向比多跑一个进程更贵（整夜不交易，且没有任何告警）。
  local pid=$1
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -o command= -p "$pid" 2>/dev/null | grep -q "night_run" || return 1
  return 0
}

if ! mkdir "$LOCK" 2>/dev/null; then
  holder=$(cat "$LOCK/pid" 2>/dev/null || true)
  if _lock_holder_alive "$holder"; then
    log_ops "lock held by pid $holder, skip"
    echo "已有实例持锁在跑（pid $holder），本窗口不重复启动。"
    sleep 5
    exit 0
  fi
  # 陈旧锁（上次崩溃/断电留下的）：清掉重抢。重抢仍可能输给并发的另一个
  # 进程，输了就安静退出 —— 那说明它已经接手了。
  log_ops "stale lock (pid=${holder:-?}) → 清理后重抢"
  rm -rf "$LOCK"
  if ! mkdir "$LOCK" 2>/dev/null; then
    log_ops "lock race lost after cleanup, skip"
    echo "抢锁失败（另一个实例刚拿到），本窗口退出。"
    sleep 5
    exit 0
  fi
fi
echo $$ > "$LOCK/pid"
# HUP 也要收：关 Terminal 窗口走的是 SIGHUP，漏了它锁会残留到下一次启动，
# 那时才靠陈旧锁判定兜底 —— 能兜住，但白白多一轮 log 噪音。
trap 'rm -rf "$LOCK"' EXIT INT TERM HUP

# 已经在跑就不再开第二个（手动启动过 / launchd 重复触发）。
#
# [9/11] 这道 pgrep **保留**，和上面的锁互补不重复：锁只覆盖经由本脚本的启动，
# 手动敲 `make run` 不持锁，只有 pgrep 看得见。
#
# 匹配的是 Makefile 实际起的完整命令行 `.venv311/bin/python -m autotrade.app.main`，
# 不能只匹配 "autotrade.app.main"：那样任何命令行里含这个串的进程都会误判 ——
# 包括你自己敲的 `pgrep -f autotrade.app.main`、`grep autotrade.app.main`，
# 结果就是当晚启动被静默跳过。（实测踩过。）
LISTENER_PAT='bin/python -m autotrade\.app\.main'

if pgrep -f "$LISTENER_PAT" > /dev/null; then
  log_ops "listener already running, skip"
  echo "listener 已在运行，本窗口不重复启动。"
  sleep 5
  exit 0
fi

# 让早上的脚本知道该收哪个文件
echo "$SESSION_LOG" > "$PROJ/logs/.current_session"
log_ops "starting -> $SESSION_LOG"

cd "$PROJ" || exit 1
{
  echo
  echo "===== session start $(date '+%F %T %Z') ====="
} >> "$SESSION_LOG"

# PYTHONUNBUFFERED=1 必须有：接了管道后 python 默认块缓冲，
# 不设的话日志要攒够几 KB 才落盘，早上 7 点收尾会丢掉最后一段。
export PYTHONUNBUFFERED=1
# caffeinate 的 -i 只挡**空闲**睡眠，挡不住系统睡眠 —— 8/5 夜实测：进程在
# 23:29:40 还是被睡进去了 8 分 44 秒（alive 心跳报 "挂钟跳变 524s"），
# 恰好横跨 09:30 ET 开盘钟，那段时间 SL/TP/EOD watcher 全停、Discord 消息不收。
# 加 -s（阻止系统睡眠，接电源时生效；纯电池下 macOS 会忽略它）。
# connection.py 里 alive 心跳那段注释推荐的也正是 `caffeinate -is`。
#
# 注意这只保住**运行期间**。launchd 定的 23:15 若赶上 Mac 已经睡了，任务会被
# 推迟到唤醒才跑（ops.log 实测 8/4 23:25:42、8/5 23:28:52 起，晚 10-14 分钟，
# 距开盘只剩一分多钟）。那个要靠定时唤醒解决，见 ops/README 或：
#   sudo pmset repeat wakeorpoweron MTWRF 23:05:00
caffeinate -is make run 2>&1 | tee -a "$SESSION_LOG"

echo "===== session end $(date '+%F %T %Z') =====" | tee -a "$SESSION_LOG"
log_ops "exited"
