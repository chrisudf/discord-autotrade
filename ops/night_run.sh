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

# 已经在跑就不再开第二个（手动启动过 / launchd 重复触发）。
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
caffeinate -i make run 2>&1 | tee -a "$SESSION_LOG"

echo "===== session end $(date '+%F %T %Z') =====" | tee -a "$SESSION_LOG"
log_ops "exited"
