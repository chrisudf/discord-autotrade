#!/bin/zsh
# 每早 07:00（本机时区）由 launchd 触发。
#   1. 优雅停掉夜里的 listener
#   2. 把整晚 terminal log 存成桌面 txt
#   3. 从 trades.db 抽一份当晚信号/成交摘要
#   4. Opus 5 headless 复盘 -> markdown 报告
#   5. 打开报告，并开一个 Terminal 接着那次会话继续追问
#
# 手动演练（跑 1-3 步，不烧 token、不开窗口）：
#   zsh ops/morning_review.sh --no-claude
set -u

NO_CLAUDE=0
[[ "${1:-}" == "--no-claude" ]] && NO_CLAUDE=1

# 路径一律从脚本位置推导，不写死 —— 换机器 / 换目录都不用改。
# zsh: ${0:A} = 本脚本的绝对路径，:h 取目录，两次 :h 到项目根。
PROJ="${0:A:h:h}"
OUT_DIR="${AUTOTRADE_OUT_DIR:-$HOME/Desktop/autotrade-logs}"
OPS_LOG="$PROJ/logs/ops.log"
STAMP="$(date +%Y-%m-%d)"

# launchd 的 PATH 不继承登录 shell，claude 常装在 nvm 目录下，两头都找一遍。
CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
  CLAUDE_BIN=$(ls -t "$HOME"/.nvm/versions/node/*/bin/claude 2>/dev/null | head -1)
fi

mkdir -p "$OUT_DIR" "$PROJ/logs"
log_ops() { echo "$(date '+%F %T') morning: $*" >> "$OPS_LOG"; }

# ---------- 1. 停 listener ----------
# 先停再存：main.py 的 SIGTERM handler 会走 shutdown()，收尾日志也要进文件。
PIDS=$(pgrep -f "autotrade.app.main" || true)
if [[ -n "$PIDS" ]]; then
  log_ops "SIGTERM -> ${PIDS//$'\n'/ }"
  kill -TERM ${=PIDS} 2>/dev/null
  for _ in {1..60}; do
    pgrep -f "autotrade.app.main" > /dev/null || break
    sleep 1
  done
  if pgrep -f "autotrade.app.main" > /dev/null; then
    log_ops "graceful stop timed out after 60s, SIGKILL"
    pkill -KILL -f "autotrade.app.main"
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
DIGEST="$OUT_DIR/.digest_${STAMP}.txt"

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

if (( NO_CLAUDE )); then
  log_ops "--no-claude: 跳过复盘与开窗"
  echo "演练完成："
  echo "  桌面日志: $DEST"
  echo "  数据摘要: $DIGEST"
  exit 0
fi

# ---------- 4. Opus 5 复盘 ----------
# 日志已经存好了，复盘跑不了也别把前面的成果吞掉 —— 留痕后正常退出。
if [[ -z "$CLAUDE_BIN" || ! -x "$CLAUDE_BIN" ]]; then
  log_ops "找不到 claude CLI，跳过复盘。日志仍已存到 $DEST"
  exit 0
fi

SID=$(uuidgen | tr 'A-Z' 'a-z')
REPORT="$OUT_DIR/review_${STAMP}.md"
echo "$SID" > "$OUT_DIR/.last_session_id"

PROMPT=$(sed -e "s|{{LOG}}|$DEST|g" \
             -e "s|{{DIGEST}}|$DIGEST|g" \
             -e "s|{{REPORT}}|$REPORT|g" \
             -e "s|{{PROJ}}|$PROJ|g" \
             -e "s|{{DATE}}|$STAMP|g" \
             "$PROJ/ops/review_prompt.md")

cd "$PROJ"
"$CLAUDE_BIN" -p "$PROMPT" \
  --model claude-opus-5 \
  --session-id "$SID" \
  --add-dir "$OUT_DIR" \
  --permission-mode acceptEdits \
  --allowedTools "Read" "Grep" "Glob" "Write" \
                 "Bash(sqlite3:*)" "Bash(grep:*)" "Bash(rg:*)" "Bash(awk:*)" \
                 "Bash(sed:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)" "Bash(sort:*)" \
  > "$OUT_DIR/.review_stdout_${STAMP}.log" 2>&1
RC=$?
log_ops "claude review exit=$RC -> $REPORT"

# ---------- 5. 摊开给人看 ----------
[[ -f "$REPORT" ]] && open "$REPORT"

# 接着 headless 那次会话开交互窗口，上下文都在，可以直接追问。
# 同样用 .command + open（osascript 那条路要 Automation 授权，见 night_run.sh 注释）。
CMD_FILE="$PROJ/ops/.morning_review.command"
cat > "$CMD_FILE" <<EOF
#!/bin/zsh
cd "$PROJ" || exit 1
echo "昨晚复盘报告: $REPORT"
echo "接续 headless 复盘会话，可直接追问。"
echo
exec "$CLAUDE_BIN" --model claude-opus-5 --resume "$SID"
EOF
chmod +x "$CMD_FILE"

/usr/bin/open -a Terminal "$CMD_FILE"
log_ops "interactive session opened (resume $SID)"
