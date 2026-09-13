#!/bin/zsh
# 安装 / 卸载夜间运行 + 早间复盘的 launchd 定时任务。
#
#   zsh ops/install.sh             装上（可重复跑，幂等）
#   zsh ops/install.sh --uninstall 卸掉
#
# 换机器就跑这一条，路径按当前 clone 的位置自动生成。
#
# 结构（为什么绕这一圈，见 launch_in_terminal.sh 的注释）：
#   launchd → ~/Library/Application Support/autotrade-ops/launch_in_terminal.sh
#           → open -a Terminal → 项目里的 night_run.sh / morning_collect.sh
# launchd 读不了 ~/Desktop（TCC），所以它只碰 Application Support；
# 项目目录的读写全部发生在 Terminal.app 那边。
set -eu

PROJ="${0:A:h:h}"
APPSUP="$HOME/Library/Application Support/autotrade-ops"
LA_DIR="$HOME/Library/LaunchAgents"
NIGHT_LABEL="com.chengqiu.autotrade.night"
MORNING_LABEL="com.chengqiu.autotrade.morning"
GUI="gui/$(id -u)"

unload() { launchctl bootout "$GUI/$1" 2>/dev/null || true; }

if [[ "${1:-}" == "--uninstall" ]]; then
  for L in $NIGHT_LABEL $MORNING_LABEL; do
    unload "$L"
    rm -f "$LA_DIR/$L.plist"
    echo "已卸载 $L"
  done
  rm -rf "$APPSUP"
  echo "已删除 $APPSUP"
  echo
  echo "定时唤醒如果不再需要，另外跑： sudo pmset repeat cancel"
  exit 0
fi

mkdir -p "$LA_DIR" "$APPSUP" "$PROJ/logs"
chmod +x "$PROJ"/ops/*.sh

# 垫片必须住在非 TCC 保护目录，launchd 才读得到
cp "$PROJ/ops/launch_in_terminal.sh" "$APPSUP/launch_in_terminal.sh"
chmod +x "$APPSUP/launch_in_terminal.sh"

# launchd 不继承登录 shell 的 PATH。这个 PATH 只给垫片用，真正的活儿在
# Terminal 里跑，那边是登录 shell 的完整环境。
LAUNCHD_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# [9/10] 本函数以前只吃一个 hour/minute，而线上 night.plist 早在 8/29 就被手改成
# 4 个触发点了（lesson #28 的唤醒窗口冗余）。也就是说：**重跑一次 install.sh 会把
# 那 3 个兜底触发点静默抹掉**，而且抹掉之后一切看起来都正常，直到某晚 Mac 在 23:15
# 恰好没醒。现在触发点改成可变参列表，线上什么样这里就写什么样，漂移消失。
emit_plist() {
  local label=$1 script=$2 extra_env=$3 short=${1##*.}
  shift 3
  local intervals="" t
  for t in "$@"; do
    intervals+="		<dict><key>Hour</key><integer>$((10#${t%%:*}))</integer><key>Minute</key><integer>$((10#${t##*:}))</integer></dict>\n"
  done
  cat > "$LA_DIR/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>

	<key>ProgramArguments</key>
	<array>
		<string>/bin/zsh</string>
		<string>$APPSUP/launch_in_terminal.sh</string>
		<string>$PROJ/ops/$script</string>
	</array>

	<key>StartCalendarInterval</key>
	<array>
$(printf "$intervals")	</array>

	<key>RunAtLoad</key>
	<false/>

	<key>EnvironmentVariables</key>
	<dict>
		<key>PATH</key>
		<string>$LAUNCHD_PATH</string>
$extra_env	</dict>

	<!-- 注意：日志路径也必须在非 TCC 保护目录，否则 launchd 写不进去 -->
	<key>StandardOutPath</key>
	<string>$APPSUP/launchd.$short.out.log</string>
	<key>StandardErrorPath</key>
	<string>$APPSUP/launchd.$short.err.log</string>
</dict>
</plist>
EOF
  plutil -lint "$LA_DIR/$label.plist" > /dev/null
  unload "$label"
  launchctl bootstrap "$GUI" "$LA_DIR/$label.plist"
  launchctl enable "$GUI/$label"
  echo "已安装 $label  →  每天 $*  ops/$script"
}

# 守卫前移到垫片：4 个触发点里只有第一个真开窗口，其余 3 个 pgrep 命中就直接退出
# （见 launch_in_terminal.sh 的注释）。模式与 night_run.sh 里那道守卫必须逐字一致。
NIGHT_ENV=$'\t\t<key>SKIP_IF_RUNNING</key>\n\t\t<string>bin/python -m autotrade\\.app\\.main</string>\n'

emit_plist "$NIGHT_LABEL"   night_run.sh       "$NIGHT_ENV" 23:11 23:15 23:20 23:25
emit_plist "$MORNING_LABEL" morning_collect.sh ""            7:00

cat <<TIP

垫片装在: $APPSUP
launchd 日志: $APPSUP/launchd.*.log

还差一步（需要你的密码，脚本不代跑）：
排一个定时唤醒，否则机器睡着时 launchd 会推迟到唤醒后才执行。

    sudo pmset repeat wakeorpoweron MTWRFSU 23:10:00

另：caffeinate -i 只挡空闲睡眠，挡不住合盖。夜里别合盖。
TIP
