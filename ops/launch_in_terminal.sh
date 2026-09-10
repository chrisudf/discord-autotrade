#!/bin/zsh
# launchd 与 TCC 之间的垫片。install.sh 会把本脚本复制到
# ~/Library/Application Support/autotrade-ops/ 下，由 launchd 直接调用。
#
# 为什么需要这层：macOS 的 TCC 保护 ~/Desktop、~/Documents、~/Downloads。
# launchd 拉起的进程默认没有这些目录的访问权，项目在 Desktop 下时
# `/bin/zsh <项目内脚本>` 会直接失败：
#     /bin/zsh: can't open input file: /Users/xxx/Desktop/.../night_run.sh
# （对照实验：同一脚本放 Application Support 下由 launchd 跑就正常。）
#
# 所以 launchd 只碰非保护目录：本脚本写一个 .command 到自己所在目录，
# 再 open -a Terminal 交给 Terminal.app —— Terminal 有 Desktop 权限
# （首次可能弹一次授权），项目目录里的读写全在它那边发生。
set -u

TARGET="${1:?用法: launch_in_terminal.sh <要在 Terminal 里跑的脚本> [参数...]}"
shift

# [9/10] 夜间有 4 个触发点（23:11/15/20/25，唤醒窗口冗余见 lesson #28），可守卫
# 一直在 night_run.sh 里 —— 窗口已经开出来才发现"已在跑"，每晚白留 3 个只打印
# 一行就退出的 Terminal 窗口（shellExitAction 又不关它们，一周攒 20 多个）。
# 守卫前移到这一层：pgrep 不碰 TCC 保护目录，launchd 直接跑得了，命中就连
# Terminal 都不开。**night_run.sh 里那道守卫要保留** —— 手动启动不经过本文件。
# 模式由调用方经 env 给（night.plist 的 EnvironmentVariables），本文件不写死。
if [[ -n "${SKIP_IF_RUNNING:-}" ]] && pgrep -f "$SKIP_IF_RUNNING" > /dev/null; then
  echo "$(date '+%F %T') launch_in_terminal: 已有进程匹配 /$SKIP_IF_RUNNING/，不开窗口"
  exit 0
fi

CMD_DIR="${0:A:h}"
CMD="$CMD_DIR/$(basename "${TARGET%.sh}").command"

# %q 转义，路径带空格也不会散架
{
  echo '#!/bin/zsh'
  printf 'exec /bin/zsh %q' "$TARGET"
  for a in "$@"; do printf ' %q' "$a"; done
  echo
} > "$CMD"
chmod +x "$CMD"

exec /usr/bin/open -a Terminal "$CMD"
