#!/bin/zsh
# 夜间看门狗：launchd 直接执行（住在 Application Support，不碰 TCC 保护目录）。
#
#   watchdog.sh power   —— 20:30/21:30/22:30 查电源/合盖，不对就 TG 叫人去插电（带电量和预计没电时刻）
#   watchdog.sh alive   —— 23:28 起每小时查 listener 进程，没在跑就 TG
#
# [9/23-24] 两晚晚启动都在用电池；9/23 那晚 launchd 四个触发点全"成功"，进程却没起来。
# 启动器只知道自己发没发请求（lesson #55），得有个独立的观察者事后去看。
# 局限：机器睡着时本脚本同样不跑，要等下次唤醒 —— 22:30 那道就是为了在睡着之前叫人。
set -u

HERE="${0:A:h}"
MODE="${1:?用法: watchdog.sh power|alive}"
CURL="${WATCHDOG_CURL:-/usr/bin/curl}"
NOW="${WATCHDOG_NOW:-$(date +%s)}"   # 测试注入用
# 与 night_run.sh / launch_in_terminal 的守卫模式逐字一致
LISTENER_PAT='bin/python -m autotrade\.app\.main'

log() { echo "$(date '+%F %T') watchdog[$MODE]: $*"; }

send_tg() {
  # tg.env 由 install.sh 从 config/.env 抄过来（600）：launchd 读不了 Desktop 下的原件
  [[ -f "$HERE/tg.env" ]] || { printf '未发（缺 tg.env）'; return 1; }
  source "$HERE/tg.env"
  "$CURL" -sS -m 20 -o /dev/null -w '%{http_code}' \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1"
}

# [10/22] 只说「在用电池」没人当回事，22:46 就没电休眠了：写上电量、按现在的耗电速度几点没电。
# pmset 一行形如 ` -InternalBattery-0 (id=…)	74%; discharging; 2:17 remaining present: true`
battery_note() {
  local b parts=() mins eta start end today
  b=$(pmset -g batt | grep -m1 'InternalBattery')
  [[ $b =~ '([0-9]+)%' ]] && parts+=("$match[1]%")
  if [[ $b =~ '([0-9]+):([0-9]+) remaining' ]]; then
    mins=$(( 10#$match[1] * 60 + 10#$match[2] ))
    eta=$(( NOW + mins * 60 ))
    today=$(date -r $NOW +%Y-%m-%d)
    start=$(date -j -f '%Y-%m-%d %H:%M' "$today 23:11" +%s)
    end=$(date -j -v+1d -f '%Y-%m-%d %H:%M' "$today 07:00" +%s)
    parts+=("约 $(date -r $eta +%H:%M) 没电")
    if (( NOW < start && eta < start )); then parts+=("撑不到 23:11 的启动")
    elif (( eta < end )); then parts+=("撑不到 07:00 收工")
    fi
  fi
  echo "在用电池${parts:+（${(j:，:)parts}）}"
}

case "$MODE" in
  power)
    problems=()
    pmset -g ps | head -1 | grep -q "'AC Power'" || problems+=("$(battery_note)")
    ioreg -r -k AppleClamshellState -d 1 | grep -q '"AppleClamshellState" = Yes' && problems+=("合着盖")
    if (( ${#problems} )); then
      [[ "${(j::)problems}" == *撑不到* ]] && lead="⛔ 现在就插电" || lead="⚠️ 今晚 23:11 listener 可能起不来"
      log "${(j:、:)problems} → TG http=$(send_tg "$lead：${(j:、:)problems}。电池 + 合盖时定时唤醒只给得到几秒 DarkWake（9/24 实锤）。请插电、别合盖。")"
    else
      log "AC 供电、未合盖，OK"
    fi
    ;;
  alive)
    if pgrep -f "$LISTENER_PAT" > /dev/null; then
      log "listener 在跑，OK"
    else
      log "listener 没起来 → TG http=$(send_tg "❌ $(date '+%H:%M') listener 没在跑（night 任务 23:11 起、之后每半小时补一次都没把它拉起来）。手动启动：launchctl kickstart -k gui/$(id -u)/com.chengqiu.autotrade.night")"
    fi
    ;;
  *)
    log "未知模式: $MODE"; exit 2 ;;
esac
