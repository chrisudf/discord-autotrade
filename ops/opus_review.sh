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
LOGDIGEST="$OUT_DIR/logdigest_${STAMP}.txt"
REPORT="$OUT_DIR/review_${STAMP}.md"

if [[ ! -f "$LOG" || ! -f "$DIGEST" ]]; then
  log_ops "素材缺失（$LOG / $DIGEST），跳过"
  echo "取证文件不齐，跳过 Opus 复盘。"
  exit 0
fi

# 日志摘要正常由 morning_collect.sh 的 3b 步生成；手动补跑旧日期时它不存在，
# 这里现补一份（纯读原始日志，幂等）。
if [[ ! -f "$LOGDIGEST" ]]; then
  log_ops "logdigest 缺失，现场生成"
  zsh "$PROJ/ops/build_logdigest.sh" "$LOG" "$LOGDIGEST" > /dev/null 2>&1 \
    || log_ops "⚠️ logdigest 生成失败，复盘将回退到读原始日志"
fi

SID=$(uuidgen | tr 'A-Z' 'a-z')
echo "$SID" > "$OUT_DIR/.last_opus_session_id"

read -r -d '' PROMPT <<EOF || true
复盘 discord-autotrade 昨晚（$STAMP 收盘）的运行情况。

素材（**先读日志摘要和数据库摘要，原始日志只在需要细节时按需回查**）：
- 日志摘要（统计 + ERROR 直方图 + 折叠掉重复刷屏之后的整晚日志全文）：$LOGDIGEST
- 数据库摘要（开仓/事件/下单/收到的全部原始消息/未平持仓，本机本地时区）：$DIGEST
- 整晚终端日志原文（可能上千行，多数情况下不需要通读）：$LOG

评判标准和输出格式以 $PROJ/ops/review_prompt.md 为准，先把它读完再动手，严格照办。
它说"输出位置由调用方指定"——**本次要求写成 markdown 文件到 $REPORT**，不要只在
stdout 上回答。

这套系统在管真钱。宁可多花时间把证据核准，也不要给一个看起来漂亮但没核对过的结论。
写完在 stdout 上把 TL;DR 重复一遍。
EOF

log_ops "start (opus-5/xhigh, session $SID)"
cd "$PROJ"

STDOUT_LOG="$OUT_DIR/.opus_stdout_${STAMP}.log"

# 报告算不算"出来了"：文件在 + 有最后一节（review_prompt.md 规定 §5 是末节）。
# 只判 -f 不够 —— 连接断在写文件中途会留下半份报告，那比没有更糟：看起来
# 有产出，实际缺的正是"今天要动的事"。
report_ok() {
  [[ -f "$REPORT" ]] && grep -qE '^##[[:space:]]*5\.' "$REPORT"
}

run_review() {
  # $1 = "" 表示首轮（新 session）；否则续跑同一 session
  local extra_prompt="$1"
  if [[ -z "$extra_prompt" ]]; then
    "$CLAUDE_BIN" -p "$PROMPT" \
      --model claude-opus-5 \
      --settings '{"effortLevel":"xhigh"}' \
      --session-id "$SID" \
      --add-dir "$OUT_DIR" \
      --permission-mode acceptEdits \
      --allowedTools "Read" "Grep" "Glob" "Write" \
      >> "$STDOUT_LOG" 2>&1
  else
    "$CLAUDE_BIN" -p "$extra_prompt" \
      --model claude-opus-5 \
      --settings '{"effortLevel":"xhigh"}' \
      --resume "$SID" \
      --add-dir "$OUT_DIR" \
      --permission-mode acceptEdits \
      --allowedTools "Read" "Grep" "Glob" "Write" \
      >> "$STDOUT_LOG" 2>&1
  fi
}

: > "$STDOUT_LOG"
run_review ""
RC=$?
log_ops "attempt 1 exit=$RC, report_ok=$(report_ok && echo yes || echo no)"

# ---- 断点续跑 ----
# [8/13 + 8/14] 连续两晚同一个死法：跑满 44-61 分钟、证据都核完了，最后
# `API Error: Connection closed mid-response` 断在写文件之前，exit=1，
# 一个字没留 —— 两晚各烧掉 38 万 / 75 万 token 换来零产出。
# 单次长跑没有任何中断保护，而 --resume 能带着已有上下文接着跑（实测
# `--resume <sid> -p` 保留全部对话），续一轮只要再吃一遍缓存读，很便宜。
# 最多重试 2 次：还不行就是真出事了，别把额度耗在死循环上。
ATTEMPT=1
while ! report_ok && (( ATTEMPT < 3 )); do
  ATTEMPT=$(( ATTEMPT + 1 ))
  log_ops "报告缺失/不完整，第 $ATTEMPT 次尝试（--resume $SID）"
  sleep 10
  run_review "上一轮在写报告前中断了（连接断开），证据分析已经做完，不用重来。\
直接把复盘按 ops/review_prompt.md 的结构写进 $REPORT（§1 到 §5 一节都不能少），\
然后在 stdout 上重复一遍 TL;DR。如果文件已经写了一部分，补齐缺的小节即可。"
  RC=$?
  log_ops "attempt $ATTEMPT exit=$RC, report_ok=$(report_ok && echo yes || echo no)"
done

if report_ok; then
  open "$REPORT"
  echo "Opus 5 复盘已生成并打开：$REPORT"
else
  echo "Opus 复盘没出来（exit=$RC），看 $OUT_DIR/.opus_stdout_${STAMP}.log"
fi
