# 夜间自动运行 + 早间复盘

launchd 用本机时区。作者机器是 Australia/Brisbane（UTC+10，无夏令时），
23:15 → 07:00 正好盖住完整美股时段（US 9:30–16:00 ET = Brisbane 23:30–06:00）。
换到别的时区要重新想这两个点位，改 `install.sh` 里 `emit_plist` 的时分参数。

## 安装

```bash
zsh ops/install.sh
```

幂等，可重复跑。它会按当前 clone 的路径现生成两个 plist 写进 `~/Library/LaunchAgents/`
并 bootstrap。**plist 里不能用变量**，所以生成物不入库 —— 换机器就重跑这条。

卸载：

```bash
zsh ops/install.sh --uninstall
```

装完还差一步（要密码，脚本不代跑）：

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 23:10:00
```

## 每天发生什么

| 时间 | 谁 | 做什么 |
|---|---|---|
| 23:15 | `com.chengqiu.autotrade.night` → `night_run.sh` | 开一个新 Terminal 窗口跑 `caffeinate -i make run`，输出 tee 到 `logs/session_YYYY-MM-DD.log` |
| 07:00 | `com.chengqiu.autotrade.morning` → `morning_review.sh` | SIGTERM 停 listener → 存日志 → 抽 DB 摘要 → Opus 5 复盘出报告 → 打开报告 + 开一个接续会话的 Terminal |

先停机再存日志，不是反过来：`app/main.py` 的 SIGTERM handler 会走 `shutdown()`，
收尾日志也该进文件。

复盘的第一优先级是**查漏单** —— 拿 `raw_signals`（收到的全部原始消息）
对照 `positions`/`orders`，找哪些消息其实是有效信号但 `signal_parser.py` 没认出来。
复盘用 `--session-id` 跑，交互窗口 `--resume` 同一个会话，早上打开就能直接追问。

改复盘的关注点就改 `review_prompt.md`，不用动脚本。

## 产出物

默认在 `~/Desktop/autotrade-logs/`（`AUTOTRADE_OUT_DIR` 环境变量可覆盖）：

- `autotrade_YYYY-MM-DD_overnight.txt` — 整晚终端日志
- `review_YYYY-MM-DD.md` — Opus 5 复盘报告（早上自动打开）
- `.digest_YYYY-MM-DD.txt` — 喂给模型的 DB 摘要，自己想看也能看
- `.review_stdout_YYYY-MM-DD.log` — headless 那次运行的 stdout，复盘没出来时查这个

运维流水账在 `logs/ops.log`。

## 手动操作

演练早间流程，不烧 token、不开窗口：

```bash
zsh ops/morning_review.sh --no-claude
```

立刻触发某个任务（不等到点）：

```bash
launchctl kickstart -k gui/$(id -u)/com.chengqiu.autotrade.night
```

看下次触发时间 / 上次退出码：

```bash
launchctl print gui/$(id -u)/com.chengqiu.autotrade.morning
```

暂停一晚：

```bash
launchctl bootout gui/$(id -u)/com.chengqiu.autotrade.night
```

改了 `ops/*.sh` 直接生效；改了 `install.sh` 里的时间点要重跑 `install.sh`。

## 三个已知的坑

**1. 机器睡着了就不会准点跑。**
launchd 遇到睡眠中的机器会推迟到唤醒后才执行。`caffeinate -i` 只挡"空闲睡眠"，
挡不住合盖。所以上面那条 `pmset repeat` 要排，且**夜里别合盖**。

**2. 不要用 osascript 开终端窗口。**
`tell application "Terminal" to do script` 需要 Automation (Apple Events) 的 TCC 授权，
在 launchd 这种非交互上下文里会卡死等一个没人点得到的弹窗（实测挂满 2 分钟且什么都没执行）。
现在两个脚本都是写一个 `.command` 文件再 `open -a Terminal`，走 LaunchServices，无需授权。
以后改这块留意别改回去。（`ops/.*.command` 就是这些每次现生成的一次性启动器，已 gitignore。）

**3. moomoo OpenD 要自己保持开着。**
自动化不会帮你拉起它。它没开的话 `app/preflight.py` 会 `sys.exit(1)` 快速失败 ——
不会静默出错，窗口会报错退出，第二天 `logs/ops.log` 里是 `no listener running`。

## 一个实现细节

DB 摘要用 sqlite 的 `.mode list` 而不是 `markdown`/`box`：后两者会按终端宽度折行，
把长消息切成续行，喂给模型会串行。时间用 `datetime(x,'localtime')` 而不是写死
`'+10 hours'`，跟着系统时区走。
