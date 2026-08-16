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
