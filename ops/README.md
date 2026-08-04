# 夜间自动运行 + 早间复盘

launchd 用本机时区。作者机器是 Australia/Brisbane（UTC+10，无夏令时），
23:15 → 07:00 正好盖住完整美股时段（US 9:30–16:00 ET = Brisbane 23:30–06:00）。
换到别的时区要重新想这两个点位，改 `install.sh` 里 `emit_plist` 的时分参数。

## 安装

```bash
zsh ops/install.sh
```

幂等，可重复跑。卸载 `zsh ops/install.sh --uninstall`。

装完还差一步（要密码，脚本不代跑）：

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 23:10:00
```

## 结构

```
launchd
  └─ ~/Library/Application Support/autotrade-ops/launch_in_terminal.sh   ← 垫片
       └─ open -a Terminal
            └─ <项目>/ops/night_run.sh  或  morning_review.sh            ← 真正干活
```

**为什么要垫片这一层：** macOS 的 TCC 保护 `~/Desktop`、`~/Documents`、`~/Downloads`。
launchd 拉起的进程默认没有这些目录的访问权。项目在 Desktop 下时，
让 launchd 直接跑项目里的脚本会失败：

```
/bin/zsh: can't open input file: /Users/xxx/Desktop/.../night_run.sh
```

退出码 127，而且**从终端手动跑一切正常**，因为终端有权限 —— 很容易误判成脚本没问题。
所以 launchd 只碰 `~/Library/Application Support/`（含它自己的 stdout/stderr 日志），
项目目录的读写全部交给 Terminal.app 那边发生。

## 每天发生什么

| 时间 | 做什么 |
|---|---|
| 23:15 | 开新 Terminal 窗口跑 `caffeinate -i make run`，输出 tee 到 `logs/session_YYYY-MM-DD.log` |
| 07:00 | SIGTERM 停 listener → 存日志 → 抽 DB 摘要 → Opus 5 复盘出报告 → 就地进入交互会话 |

先停机再存日志，不是反过来：`app/main.py` 的 SIGTERM handler 会走 `shutdown()`，
收尾日志也该进文件。

复盘的第一优先级是**查漏单** —— 拿 `raw_signals`（收到的全部原始消息）
对照 `positions`/`orders`，找哪些消息其实是有效信号但 `signal_parser.py` 没认出来。
复盘用 `--session-id` 跑，同一窗口再 `--resume` 接上，早上打开就能直接追问。

改复盘的关注点就改 `review_prompt.md`，不用动脚本。

## 产出物

默认在 `~/Desktop/autotrade-logs/`（`AUTOTRADE_OUT_DIR` 可覆盖）：

- `autotrade_YYYY-MM-DD_overnight.txt` — 整晚终端日志
- `review_YYYY-MM-DD.md` — Opus 5 复盘报告（早上自动打开）
- `.digest_YYYY-MM-DD.txt` — 喂给模型的 DB 摘要
- `.review_stdout_YYYY-MM-DD.log` — headless 那次的 stdout，复盘没出来时查这个

运维流水账 `logs/ops.log`；launchd 自己的 stdout/stderr 在
`~/Library/Application Support/autotrade-ops/launchd.*.log`（**不在项目里**，见上面 TCC 那段）。

## 手动操作

演练早间流程，不烧 token、不进交互：

```bash
zsh ops/morning_review.sh --no-claude
```

立刻触发（不等到点）：

```bash
launchctl kickstart -k gui/$(id -u)/com.chengqiu.autotrade.night
```

看下次触发时间 / 上次退出码：

```bash
launchctl print gui/$(id -u)/com.chengqiu.autotrade.morning
```

改了 `ops/*.sh` 直接生效；改了 `install.sh` 或 `launch_in_terminal.sh` 要重跑 `install.sh`。

## 踩过的坑

**1. launchd 读不了 Desktop（TCC）。** 见上面「结构」。症状是退出码 127
`can't open input file`，而手动跑一切正常。

**2. 别用 `pgrep -f "autotrade.app.main"` 当运行守卫。**
这个串太松，任何命令行里含它的进程都会匹配 —— 包括你自己敲的
`pgrep -f autotrade.app.main` 或 `grep autotrade.app.main`。后果是当晚启动被静默跳过
（ops.log 里留下一条 `listener already running, skip`，但其实什么都没跑）。
现在两个脚本统一用 `LISTENER_PAT='bin/python -m autotrade\.app\.main'`，
匹配 Makefile 实际起的完整命令行。

**3. 不要用 osascript 开终端窗口。**
`tell application "Terminal" to do script` 需要 Automation (Apple Events) 的 TCC 授权，
在 launchd 这种非交互上下文里会卡死等一个没人点得到的弹窗（实测挂满 2 分钟且什么都没执行）。
现在走 `.command` 文件 + `open -a Terminal`（LaunchServices），无需授权。

**4. 机器睡着了就不会准点跑。**
launchd 会推迟到唤醒后才执行。`caffeinate -i` 只挡"空闲睡眠"，挡不住合盖。
所以上面那条 `pmset repeat` 要排，且**夜里别合盖**。

**5. moomoo OpenD 要自己保持开着。**
自动化不会帮你拉起它。没开的话 `app/preflight.py` 会 `sys.exit(1)` 快速失败 ——
不会静默出错，窗口会报错退出，第二天 `logs/ops.log` 里是 `no listener running`。

## 两个实现细节

`tee` 前设 `PYTHONUNBUFFERED=1`：接了管道后 python 默认块缓冲，不设的话
早上 7 点收尾会丢掉最后一段日志。

DB 摘要用 sqlite 的 `.mode list` 而非 `markdown`/`box`：后两者会按终端宽度折行，
把长消息切成续行，喂给模型会串行。时间用 `datetime(x,'localtime')` 而非写死
`'+10 hours'`，跟着系统时区走。
