#!/bin/zsh
# 从整晚终端日志生成一份「日志摘要」：统计 + ERROR 直方图 + 折叠掉重复刷屏
# 之后的整晚日志全文。
#
# 为什么需要它（两个独立的理由，缺一条都还不够）：
#
#   1. **DB 摘要看不见卖出侧。** digest 的数据全部来自 trades.db，而 orders 表
#      只记买单 —— 8/13 那晚它 success=0 是 0 行，1918 次 naked-short 拒单和
#      EOD 卖单在整个 trades.db 里查无此事。只读 digest 会得出"3 单全成、
#      一夜平静"的结论。那场风暴只存在于终端日志里。
#   2. **复盘自己压不了日志。** opus_review.sh 给的工具集是 Read/Grep/Glob/Write，
#      没有 Bash。8183 行的日志要么整读（贵），要么盲抽（漏）。压缩得在 shell 里做。
#
# 折叠只动"同一条记录重复 > THRESH 次"的部分，安静的夜晚输出与输入逐字节相同
# （8/12 的 363 行实测 diff 无差异）。
#
# 用法：
#   zsh ops/build_logdigest.sh <整晚日志> <输出文件> [THRESH]
set -u

SRC="${1:?用法: build_logdigest.sh <overnight.txt> <out.txt> [thresh]}"
OUT="${2:?用法: build_logdigest.sh <overnight.txt> <out.txt> [thresh]}"
THRESH="${3:-20}"
HERE="${0:A:h}"

[[ -f "$SRC" ]] || { echo "找不到日志: $SRC" >&2; exit 1 }

{
  echo "## 概览"
  echo "原始日志      : $SRC"
  echo "行数          : $(wc -l < "$SRC" | tr -d ' ')"
  echo "ERROR 行数    : $(grep -c '| ERROR |' "$SRC" || true)"
  echo "WARNING 行数  : $(grep -c '| WARNING |' "$SRC" || true)"
  # 恢复有两条路径，只数 RESUMED 会少算：
  #   a. `session resumed`   —— gateway RESUME（秒级，session id 不变）
  #   b. `logged back in`    —— 完整重登录（gateway session 已失效，on_ready
  #                             会走 _backfill_missed 补回窗口内的消息）
  # [8/20 实测] 那晚 6 次掉线里有一次是 CloudFlare 重启导致的完整重登录，
  # 摘要报成"恢复 4 次"，看上去像有两次没恢复 —— 实际是 5 次恢复 + 1 次
  # 我们自己 SIGTERM 关的（code=1000）。复盘每次都要被这行误导一遍。
  _dc=$(grep -c 'on_disconnect fired' "$SRC" || true)
  _resumed=$(grep -c 'session resumed' "$SRC" || true)
  _relogin=$(grep -c 'logged back in' "$SRC" || true)
  _shutdown=$(grep -c 'on_disconnect fired.*code=1000' "$SRC" || true)
  echo "Discord 掉线  : ${_dc} 次（RESUME ${_resumed} + 重登录 ${_relogin}，主动关闭 ${_shutdown}）"
  echo "Parse failed  : $(grep -c 'Parse failed' "$SRC" || true)"
  echo "收到消息      : $(grep -c '📩' "$SRC" || true)"
  echo
  echo "## ERROR 直方图（去掉时间戳与数字后归并）"
  grep '| ERROR |' "$SRC" 2>/dev/null \
    | sed -E 's/^[0-9:.]+ \| ERROR \| //; s/[0-9]+/#/g' | cut -c1-100 \
    | sort | uniq -c | sort -rn | head -20
  echo
  echo "## 重复最多的记录 top 10（同上归并）"
  sed -E 's/^[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3} \| //; s/[0-9]+/#/g' "$SRC" \
    | cut -c1-100 | sort | uniq -c | sort -rn | head -10
  echo
  echo "## 折叠后的整晚日志"
  echo "（这是**全文**不是节选：同一条记录重复超过 $THRESH 次时只保留首末各一条，"
  echo "  折叠处有 ⋮⋮⋮ 标注总次数。首末都留是为了能判断循环何时开始、何时停下。）"
  echo "------------------------------------------------------------"
  awk -v THRESH="$THRESH" -f "$HERE/collapse_log.awk" "$SRC" "$SRC"
} > "$OUT" 2>&1

echo "日志摘要: $OUT ($(wc -l < "$OUT" | tr -d ' ') 行，原始 $(wc -l < "$SRC" | tr -d ' ') 行)"
