#!/bin/zsh
# 用 Opus 5 + xhigh effort 出一份复盘，写成 markdown 并弹开。
# 由 morning_collect.sh 在收尾取证之后调用；也可以手动跑（复盘昨晚：不带参数）。
#
# 这是**唯一**一条复盘路径。曾经并行跑过一条 Claude app 的定时任务
# autotrade-nightly-review（同一份 review_prompt.md、同一份素材），
# 理由是那时候 app 把定时任务的模型写死成 Sonnet 5，拿不到 Opus。
# 2026-08-06 复查发现该限制已经没了（当天定时任务 29 次 API 调用全是 opus-5），
# 于是两条路变成同一件事跑两遍 Opus —— 一个早上烧掉 5 小时额度的一半多。
# 定时任务已停用（SKILL.md 还在，想要可追问的对话版就手动跑一次）。
#
# 命令行仍然是首选载体：--model / --settings 能硬控模型和 effort，
# 不受 app 偏好影响；不依赖 app 开着；产物直接落盘。
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
