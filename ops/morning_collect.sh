#!/bin/zsh
# 每早 07:00（本机时区）跑：
#   1. 优雅停掉夜里的 listener
#   2. 把整晚 terminal log 存成桌面 txt
#   3. 从 trades.db 抽一份当晚信号/成交摘要
#   4. 调 opus_review.sh 出一份 Opus 5 + xhigh 的复盘（markdown，自动弹开）
#
# 1-3 步是纯 shell、不依赖任何 app，保证取证一定发生。第 4 步就算失败也不影响
# 前三步的产物。
#
# 第 4 步是唯一的复盘路径。曾经并行跑过 Claude 定时任务 autotrade-nightly-review
# （7:10，同一份素材、同一份 review_prompt.md），2026-08-06 停用 —— 它也跑 Opus 5，
# 等于同一件事花两份钱。原因和取舍见 opus_review.sh 顶部注释。
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
#
# stderr **不再** 混进 $DIGEST（老写法是 `> "$DIGEST" 2>&1`）：写错一个列名不会
# 让脚本失败，只会把 `Error: no such column: xxx` 悄悄写进摘要，那一节就变成一行
# 错误字符串交给复盘，而复盘只会觉得"当晚没有这类记录"。改成 stderr 单独收，
# 非空就大声报进 ops.log 并在摘要顶部留一条醒目的提示。
DIGEST_ERR=$(mktemp)
sqlite3 "$PROJ/data/trades.db" <<SQL > "$DIGEST" 2>"$DIGEST_ERR"
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

if [[ -s "$DIGEST_ERR" ]]; then
  log_ops "⚠️ digest SQL 报错: $(tr '\n' ' ' < "$DIGEST_ERR")"
  # 顶部插一条，让复盘一眼看见"这份摘要不完整"，而不是把缺失当成"当晚没有"
  { echo "⚠️⚠️⚠️ 本摘要生成时 SQL 有报错，下面某些小节可能是空的或不完整："
    sed 's/^/    /' "$DIGEST_ERR"
    echo
    cat "$DIGEST"
  } > "$DIGEST.tmp" && mv "$DIGEST.tmp" "$DIGEST"
fi
rm -f "$DIGEST_ERR"
log_ops "digest built -> $DIGEST"

# ---------- 3b. 日志摘要（折叠重复行）----------
# [8/13 + 8/14] 两晚连续被同一条拒单循环刷屏（1918 / 812 组）。日志摘要要解决
# 两件事：
#   1. **DB 摘要看不见卖出侧。** orders 表只记买单 —— 8/13 那晚它 success=0 是
#      0 行，1918 次拒单和 EOD 卖单在整个 trades.db 里查无此事。只读 $DIGEST
#      会得出"3 单全成、一夜平静"的结论，风暴只存在于终端日志里。
#   2. opus_review.sh 的工具集是 Read/Grep/Glob/Write，**没有 Bash** —— 复盘
#      自己压不了日志。那就在这里先压好。
# 折叠只动"同一条记录重复 > 20 次"的部分，安静的夜晚输出与输入逐字节相同。
LOGDIGEST="$OUT_DIR/logdigest_${STAMP}.txt"
zsh "$PROJ/ops/build_logdigest.sh" "$DEST" "$LOGDIGEST" > /dev/null 2>&1
log_ops "logdigest built -> $LOGDIGEST ($(wc -l < "$LOGDIGEST" | tr -d ' ') lines, 原始 $(wc -l < "$DEST" | tr -d ' '))"

echo
echo "取证完成："
echo "  整晚日志: $DEST"
echo "  数据摘要: $DIGEST"
echo "  日志摘要: $LOGDIGEST"

# ---------- 4. Opus 5 复盘 ----------
# 唯一一条复盘路径。曾经并行的 Claude 定时任务已停用，原因见 opus_review.sh 顶部注释。
[[ "${1:-}" == "--no-review" ]] || zsh "$PROJ/ops/opus_review.sh" "$STAMP"
