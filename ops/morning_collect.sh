#!/bin/zsh
# 每早 07:00（本机时区）跑：
#   1. 优雅停掉夜里的 listener
#   2. 把整晚 terminal log 存成桌面 txt
#   3. 从 trades.db 抽一份当晚信号/成交摘要
#   4. 调 opus_review.sh 出一份 Opus 5 + xhigh 的权威复盘（markdown，自动弹开）
#
# 1-3 步是纯 shell、不依赖任何 app，保证取证一定发生。第 4 步就算失败也不影响
# 前三步的产物。
#
# 另有一条并行的路：Claude 的定时任务 autotrade-nightly-review（7:10）读同样这两份
# 产物，在对话里给一份可追问的复盘。它锁死 Sonnet 5（见 opus_review.sh 注释），
# 所以权威版走第 4 步的命令行。app 没开时定时任务会推迟到下次打开才跑，
# 但那时文件已经稳稳躺在桌面上了。
#
# 本脚本在 Terminal.app 窗口里执行，不是由 launchd 直接执行 ——
# launchd 读不了 ~/Desktop（TCC），中间隔着 launch_in_terminal.sh 这层垫片，
# 原因见那个文件的注释。所以这里可以放心读写项目目录和桌面。
#
# 手动演练：zsh ops/morning_collect.sh
set -u

# 路径一律从脚本位置推导，不写死 —— 换机器 / 换目录都不用改。
# zsh: ${0:A} = 本脚本的绝对路径，:h 取目录，两次 :h 到项目根。
PROJ="${0:A:h:h}"
OUT_DIR="${AUTOTRADE_OUT_DIR:-$HOME/Desktop/autotrade-logs}"
OPS_LOG="$PROJ/logs/ops.log"
STAMP="$(date +%Y-%m-%d)"

mkdir -p "$OUT_DIR" "$PROJ/logs"
log_ops() { echo "$(date '+%F %T') morning: $*" >> "$OPS_LOG"; }

# 匹配 Makefile 实际起的完整命令行，不能只匹配 "autotrade.app.main" ——
# 那样任何命令行里含这个串的进程都会被当成 listener，会误杀一个只是在
# grep 的 shell，而真正的 listener 反倒停不掉。见 night_run.sh 同名注释。
LISTENER_PAT='bin/python -m autotrade\.app\.main'

# ---------- 1. 停 listener ----------
# 先停再存：main.py 的 SIGTERM handler 会走 shutdown()，收尾日志也要进文件。
PIDS=$(pgrep -f "$LISTENER_PAT" || true)
if [[ -n "$PIDS" ]]; then
  log_ops "SIGTERM -> ${PIDS//$'\n'/ }"
  kill -TERM ${=PIDS} 2>/dev/null
  for _ in {1..60}; do
    pgrep -f "$LISTENER_PAT" > /dev/null || break
    sleep 1
  done
  if pgrep -f "$LISTENER_PAT" > /dev/null; then
    log_ops "graceful stop timed out after 60s, SIGKILL"
    pkill -KILL -f "$LISTENER_PAT"
  else
    log_ops "stopped cleanly"
  fi
else
  log_ops "no listener running (crashed overnight or never started?)"
fi
sleep 2   # 等 tee 把最后几行刷完

# ---------- 2. 存 log 到桌面 ----------
DEST="$OUT_DIR/autotrade_${STAMP}_overnight.txt"
SESSION_LOG=$(cat "$PROJ/logs/.current_session" 2>/dev/null || true)

if [[ -n "$SESSION_LOG" && -f "$SESSION_LOG" ]]; then
  cp "$SESSION_LOG" "$DEST"
  # 用完就删指针：万一某晚 night_run 压根没跑起来，第二天早上会走兜底分支
  # 并在 ops.log 里留痕，而不是把前天的 session log 当成昨晚的悄悄交上来。
  rm -f "$PROJ/logs/.current_session"
  log_ops "saved terminal log -> $DEST ($(wc -l < "$DEST" | tr -d ' ') lines)"
else
  # 兜底：终端 log 没了就拼 app 自己的日志（跨了午夜，要昨天+今天两份）
  YDAY=$(date -v-1d +%Y-%m-%d)
  cat "$PROJ/logs/app_${YDAY}.log" "$PROJ/logs/app_${STAMP}.log" > "$DEST" 2>/dev/null
  log_ops "session log missing, fell back to app_${YDAY}.log + app_${STAMP}.log"
fi

# ---------- 3. DB 摘要 ----------
# trades.db 存 UTC ISO8601；sqlite 的 'localtime' 修饰符按系统时区转，
# 不写死偏移量，换时区的机器也对。
# 取过去 9 小时（23:15 开跑到 07:00 是 7h45m，留点余量）。
SINCE_UTC=$(date -u -v-9H '+%Y-%m-%dT%H:%M:%SZ')
DIGEST="$OUT_DIR/digest_${STAMP}.txt"

# list 模式：box/markdown 模式会按终端宽度折行，长消息会被切成续行，喂给模型会串行。
# note/content 里的换行压平成 " / "，竖线换成 "/"，保证一行一条记录。
sqlite3 "$PROJ/data/trades.db" <<SQL > "$DIGEST" 2>&1
.mode list
.separator ' | '
.headers on

.print '## 当晚开仓（时间均为本机本地时区）'
SELECT option_code, channel_name, side, qty_total AS qty,
       avg_entry_price AS entry, status,
       datetime(opened_at, 'localtime') AS opened_local,
       COALESCE(datetime(closed_at, 'localtime'), '-') AS closed_local
FROM positions
WHERE opened_at >= '$SINCE_UTC'
ORDER BY opened_at;

.print ''
.print '## 当晚事件流'
SELECT datetime(ts, 'localtime') AS ts_local, option_code, event_type,
       qty_delta AS dq, price, pct, trigger_source,
       replace(replace(COALESCE(note,''), char(10), ' / '), '|', '/') AS note
FROM position_events
WHERE ts >= '$SINCE_UTC'
ORDER BY ts;

.print ''
.print '## 当晚下单记录（含失败）'
SELECT datetime(placed_at, 'localtime') AS placed_local, option_code, side,
       entry_price, qty, success, order_id,
       replace(replace(COALESCE(message,''), char(10), ' / '), '|', '/') AS message
FROM orders
WHERE placed_at >= '$SINCE_UTC'
ORDER BY placed_at;

.print ''
.print '## 当晚收到的全部原始消息（用来对照哪些没变成单）'
SELECT datetime(received_at, 'localtime') AS recv_local, msg_id, author,
       replace(replace(COALESCE(content,''), char(10), ' / '), '|', '/') AS content
FROM raw_signals
WHERE received_at >= '$SINCE_UTC'
ORDER BY received_at;

.print ''
.print '## 停机时仍未平的持仓（含更早开的）'
SELECT option_code, channel_name, qty_remaining AS qty_left,
       avg_entry_price AS entry, tp_hits, eod_force_close, apply_sl,
       expiry, datetime(opened_at, 'localtime') AS opened_local
FROM positions
WHERE status = 'OPEN'
ORDER BY opened_at;
SQL

log_ops "digest built -> $DIGEST"

echo
echo "取证完成："
echo "  整晚日志: $DEST"
echo "  数据摘要: $DIGEST"

# ---------- 4. Opus 5 权威复盘 ----------
# 定时任务那条路锁死 Sonnet 5（见 opus_review.sh 顶部注释），所以权威版走命令行。
# 两条路读的是同一份素材、同一份 review_prompt.md。
[[ "${1:-}" == "--no-review" ]] || zsh "$PROJ/ops/opus_review.sh" "$STAMP"
