#!/bin/zsh
# 每晚 23:15（本机时区）由 launchd 触发。
# 在新 Terminal 窗口跑 `caffeinate -i make run`，输出同时 tee 到 session log。
#
# 为什么不用 osascript `tell application "Terminal" to do script`：
# 那条路要 Automation (Apple Events) 的 TCC 授权，在 launchd / 非交互上下文里
# 会直接卡死等一个没人能点的授权弹窗。`.command` 文件 + `open -a Terminal`
# 走 LaunchServices，不碰 Apple Events，无需任何授权。
set -u

# 路径一律从脚本位置推导，不写死 —— 换机器 / 换目录都不用改。
# zsh: ${0:A} = 本脚本的绝对路径，:h 取目录，两次 :h 到项目根。
PROJ="${0:A:h:h}"
SESSION_LOG="$PROJ/logs/session_$(date +%Y-%m-%d).log"
CMD_FILE="$PROJ/ops/.night_session.command"
OPS_LOG="$PROJ/logs/ops.log"

mkdir -p "$PROJ/logs"
log_ops() { echo "$(date '+%F %T') night_run: $*" >> "$OPS_LOG"; }

# 已经在跑就不再开第二个（手动启动过 / launchd 重复触发）
if pgrep -f "autotrade.app.main" > /dev/null; then
  log_ops "listener already running, skip"
  exit 0
fi

# 让早上的脚本知道该收哪个文件
echo "$SESSION_LOG" > "$PROJ/logs/.current_session"

# PYTHONUNBUFFERED=1 必须有：接了管道后 python 默认块缓冲，
# 不设的话日志要攒够几 KB 才落盘，早上 7 点收尾会丢掉最后一段。
cat > "$CMD_FILE" <<EOF
#!/bin/zsh
cd "$PROJ" || exit 1
{
  echo
  echo "===== session start \$(date '+%F %T %Z') ====="
} >> "$SESSION_LOG"
export PYTHONUNBUFFERED=1
caffeinate -i make run 2>&1 | tee -a "$SESSION_LOG"
echo "===== session end \$(date '+%F %T %Z') =====" | tee -a "$SESSION_LOG"
EOF
chmod +x "$CMD_FILE"

/usr/bin/open -a Terminal "$CMD_FILE"
log_ops "launched -> $SESSION_LOG"
