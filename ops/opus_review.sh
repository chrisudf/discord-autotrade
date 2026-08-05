#!/bin/zsh
# 用 Opus 5 + xhigh effort 出一份权威复盘，写成 markdown 并弹开。
# 由 morning_collect.sh 在收尾取证之后调用；也可以手动跑（复盘昨晚：不带参数）。
#
# 为什么不用 Claude 的定时任务来跑 Opus：
# 实测这个 app 版本把定时任务的模型写死成 Sonnet 5 —— 20 个会话里唯二的两个
# sonnet 就是那两次定时任务，其余普通会话全是 opus-5。settings.json 的 model、
# SKILL.md frontmatter 的 model、app 的模型选择器，对它统统无效。
# 命令行的 --model / --settings 是唯一能硬控模型和 effort 的地方。
#
# 定时任务仍然保留，跑的是同一份 review_prompt.md，给你一份可追问的对话版；
# 这个脚本给的是权威版。两者素材相同、口径相同，只是模型和载体不同。
set -u

PROJ="${0:A:h:h}"
OUT_DIR="${AUTOTRADE_OUT_DIR:-$HOME/Desktop/autotrade-logs}"
OPS_LOG="$PROJ/logs/ops.log"
STAMP="${1:-$(date +%Y-%m-%d)}"

log_ops() { echo "$(date '+%F %T') opus_review: $*" >> "$OPS_LOG"; }

CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
  CLAUDE_BIN=$(ls -t "$HOME"/.nvm/versions/node/*/bin/claude 2>/dev/null | head -1)
fi
if [[ -z "$CLAUDE_BIN" || ! -x "$CLAUDE_BIN" ]]; then
  log_ops "找不到 claude CLI，跳过"
  echo "找不到 claude CLI，跳过 Opus 复盘。"
  exit 0
fi

LOG="$OUT_DIR/autotrade_${STAMP}_overnight.txt"
DIGEST="$OUT_DIR/digest_${STAMP}.txt"
REPORT="$OUT_DIR/review_${STAMP}.md"

if [[ ! -f "$LOG" || ! -f "$DIGEST" ]]; then
  log_ops "素材缺失（$LOG / $DIGEST），跳过"
  echo "取证文件不齐，跳过 Opus 复盘。"
  exit 0
fi

SID=$(uuidgen | tr 'A-Z' 'a-z')
echo "$SID" > "$OUT_DIR/.last_opus_session_id"

read -r -d '' PROMPT <<EOF || true
复盘 discord-autotrade 昨晚（$STAMP 收盘）的运行情况。

素材：
- 整晚终端日志：$LOG
- 数据库摘要（开仓/事件/下单/收到的全部原始消息/未平持仓，本机本地时区）：$DIGEST

评判标准和输出格式以 $PROJ/ops/review_prompt.md 为准，先把它读完再动手，严格照办。
它说"输出位置由调用方指定"——**本次要求写成 markdown 文件到 $REPORT**，不要只在
stdout 上回答。

这套系统在管真钱。宁可多花时间把证据核准，也不要给一个看起来漂亮但没核对过的结论。
写完在 stdout 上把 TL;DR 重复一遍。
EOF

log_ops "start (opus-5/xhigh, session $SID)"
cd "$PROJ"

# --model / --settings 是硬控制，不受 app 偏好影响。
# 工具只给读 + 写报告；不给 Bash —— 摘要里已有当晚全部数据库记录，用不到 sqlite3。
"$CLAUDE_BIN" -p "$PROMPT" \
  --model claude-opus-5 \
  --settings '{"effortLevel":"xhigh"}' \
  --session-id "$SID" \
  --add-dir "$OUT_DIR" \
  --permission-mode acceptEdits \
  --allowedTools "Read" "Grep" "Glob" "Write" \
  > "$OUT_DIR/.opus_stdout_${STAMP}.log" 2>&1
RC=$?
log_ops "exit=$RC -> $REPORT"

if [[ -f "$REPORT" ]]; then
  open "$REPORT"
  echo "Opus 5 复盘已生成并打开：$REPORT"
else
  echo "Opus 复盘没出来（exit=$RC），看 $OUT_DIR/.opus_stdout_${STAMP}.log"
fi
