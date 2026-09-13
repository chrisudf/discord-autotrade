#!/bin/zsh
# 每早 07:00（本机时区）跑：
#   1. 优雅停掉夜里的 listener
#   2. 把整晚 terminal log 存成桌面 txt
#   3. 从 trades.db 抽一份当晚信号/成交摘要
#
# 全是纯 shell、不依赖任何 app，保证取证一定发生。
#
# 曾经还有第 4 步：调 opus_review.sh 出一份 Opus 5 + xhigh 的自动复盘。
# 2026-08-17 去掉 —— 早上只留 txt 取证，复盘按需手动跑：
#   zsh ops/opus_review.sh $(date +%F)
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
# [9/5 实锤 -$714] 到期日判据要用 **ET 日期**，不是本机日期：摘要在 07:00 AEST
# 生成，那时美东还是前一天下午。用 TZ= 让 date 自己算，不写死偏移量（DST 自动对）。
TODAY_ET=$(TZ=America/New_York date +%Y-%m-%d)
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

-- [9/5 实锤 -\$714] NBIS / IONQ / NVDA 三张 9/4 到期的合约，在 9/4 和 9/5 两份
-- 摘要的"停机时仍未平的持仓"里都在，\`expiry\` 列也写着 2026-09-04 —— 但它们
-- 混在 7 行持仓中间，跟"还有两周到期"的 swing 长得一模一样，没人看出来。
-- 那晚 EOD 准时进窗、每 30s 重试到收盘，全部卡在 no-quote（OpenD 跟着断网一起
-- 死了），90 条"需要人工"的 TG 一条没发出去（101 次 ConnectError）。
-- 于是唯一还活着的告知路径就是这份摘要，而它当时什么都没强调。
--
-- 单列一节放最前面：**已经到期 / 今天到期但还没平**，这是唯一需要人今天动手的东西。
-- 空表 = 没有这类仓位，也是有效信息（别因为"通常是空的"就删掉这一节）。
.print '## ⚠️ 已过期 / 今日到期但仍未平（ET 日期 $TODAY_ET，需人工处理）'
SELECT option_code, channel_name, status, qty_remaining AS qty_left,
       avg_entry_price AS entry, expiry,
       CASE WHEN expiry < '$TODAY_ET' THEN '已过期' ELSE '今日到期' END AS urgency,
       apply_sl, eod_force_close,
       datetime(opened_at, 'localtime') AS opened_local
FROM positions
WHERE status IN ('OPEN', 'PARTIAL') AND expiry <= '$TODAY_ET'
ORDER BY expiry, opened_at;

.print ''
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
-- [8/14] status 必须含 PARTIAL：部分平仓过的仓位 status 会从 OPEN 变成
-- PARTIAL，老写法 \`WHERE status = 'OPEN'\` 把它们整行漏掉 —— 8/13 夜真实过夜
-- 是 6 个合约 10 张，摘要只显示了 4 个 8 张，SPCX 120P 在两张持仓表里完全隐身。
-- 复盘据此答"持仓状态"这一节，等于系统性少报被 trim 过的仓（恰恰是最该盯的那些）。
-- 判据与 position_mgr / watcher 选仓口径对齐（那边一直是 IN ('OPEN','PARTIAL')）。
SELECT option_code, channel_name, status, qty_remaining AS qty_left,
       avg_entry_price AS entry, tp_hits, eod_force_close, apply_sl,
       expiry, datetime(opened_at, 'localtime') AS opened_local
FROM positions
WHERE status IN ('OPEN', 'PARTIAL')
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

# ---------- 3a. 昨晚没送出去的 TG 告警 ----------
# [9/5 实锤 -$714] 断网那晚 TG 101 次 ConnectError，其中 90 条是 EOD 的
# "当日到期 + 拿不到报价 + 需要人工"。那些告警只存在于日志里，而日志没人当晚读。
# transport.py 现在把送不出去的告警追加到 $UNDELIVERED，这里把**昨晚那批**
# 顶到摘要最前面 —— 这是断网时唯一还活着的告知路径。
#
# 同一条文案在一次故障里会重复几十遍（EOD 每 30s 一轮），所以按正文归并计数，
# 只留首末时间。用 awk 不用 jq/python：本脚本的契约是纯 shell（见文件头）。
# 路径必须和 transport._undelivered_path() 同一个口径：那边认 LOG_DIR，
# 这边写死 $PROJ/logs 的话，任何设了 LOG_DIR 的部署都会静默漏掉全部未送达告警。
UNDELIVERED="${LOG_DIR:-$PROJ/logs}/undelivered_alerts.tsv"
if [[ -s "$UNDELIVERED" ]]; then
  UNDELIVERED_SECTION=$(mktemp)
  awk -F'\t' -v since="$SINCE_UTC" '
    $1 >= since {
      n[$2]++
      if (first[$2] == "") first[$2] = $1
      last[$2] = $1
    }
    END {
      for (m in n)
        printf "%d 次 | %s ~ %s | %s\n", n[m], first[m], last[m], m
    }
  ' "$UNDELIVERED" | sort -t'|' -k1,1nr > "$UNDELIVERED_SECTION"

  if [[ -s "$UNDELIVERED_SECTION" ]]; then
    { echo "## ⛔ 昨晚有 TG 告警没送出去（按正文归并，时间为 UTC）"
      echo "count | first ~ last | message"
      cat "$UNDELIVERED_SECTION"
      echo
      cat "$DIGEST"
    } > "$DIGEST.tmp" && mv "$DIGEST.tmp" "$DIGEST"
    log_ops "⛔ 昨晚有未送达 TG 告警，已顶进摘要（$(wc -l < "$UNDELIVERED_SECTION" | tr -d ' ') 组）"
  fi
  rm -f "$UNDELIVERED_SECTION"
fi

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
echo
echo "要复盘的话手动跑: zsh $PROJ/ops/opus_review.sh $STAMP"
