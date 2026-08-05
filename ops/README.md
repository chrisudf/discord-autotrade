# 夜间自动运行 + 早间复盘

launchd 用本机时区。作者机器是 Australia/Brisbane（UTC+10，无夏令时），
23:15 → 07:00 正好盖住完整美股时段（US 9:30–16:00 ET = Brisbane 23:30–06:00）。
换到别的时区要重新想这两个点位，改 `install.sh` 里 `emit_plist` 的时分参数。

## 每天发生什么

| 时间 | 谁 | 做什么 |
|---|---|---|
| 23:15 | launchd → `night_run.sh` | 开新 Terminal 窗口跑 `caffeinate -i make run`，输出 tee 到 `logs/session_YYYY-MM-DD.log` |
| 07:00 | launchd → `morning_collect.sh` | SIGTERM 停 listener → 整晚日志存到桌面 → 从 `trades.db` 抽摘要 |
| 07:10 | Claude 定时任务 `autotrade-nightly-review` | 读那两份产物，**直接在对话里回答复盘**，不写文件 |

**为什么切成两段**：停机和存日志不该依赖 Claude app 开着。Claude 的定时任务只在
app 开着时跑（app 关着的话推迟到下次打开），但那时取证文件已经稳稳躺在桌面上了 ——
复盘晚点看没关系，日志丢了就没了。

先停机再存日志，不是反过来：`app/main.py` 的 SIGTERM handler 会走 `shutdown()`，
收尾日志也该进文件。

## 改复盘的关注点

改 `review_prompt.md` 就行，**不用碰定时任务**。定时任务的 prompt 只负责找文件、
交代背景，具体看什么、怎么排序、怎么写全部指向 `review_prompt.md`，那是唯一事实来源。

复盘的第一优先级是**查漏单** —— 拿 `raw_signals`（收到的全部原始消息）对照
`positions`/`orders`，找哪些消息其实是有效信号但 `signal_parser.py` 没认出来。

定时任务本身存在 `~/.claude/scheduled-tasks/autotrade-nightly-review/SKILL.md`，
在 Claude 侧边栏的 "Scheduled" 里管理（它产生的会话归在那儿，**不在普通聊天列表里**，
所以常规列表翻不到）。

### 仓库外的配置（换机器要手动重做）

`~/.claude/settings.json`：

```json
{
  "model": "claude-opus-5",
  "effortLevel": "xhigh",
  "permissions": {
    "additionalDirectories": [
      "<项目路径>",
      "<输出目录路径>"
    ]
  }
}
```

- `additionalDirectories` 是**必须的**：任务会话的 cwd 继承自创建它的会话，
  多半不是本项目，读 cwd 之外的文件会触发权限确认 —— 无人值守时没人去点，
  整个任务静默挂死。**不要**改成给 Bash 开全局白名单，权限面大得多且没必要。
- `model` / `effortLevel` 是**全局**的，因为定时任务没有 per-task 的模型设置
  （app 侧的 `scheduled-tasks.json` 记录里只有 cron / cwd / 权限，没有 model 字段）。
  这套系统在管真钱，复盘用 Opus 5 + xhigh；代价是别的会话也会跟着用，
  不想要就在那些会话里单独调。

**换模型必须删掉任务重建。** "Run now" 会复用任务已绑定的那个会话，而模型在会话
创建时就定死了 —— 光改 settings 不重建，它会一直挂在旧模型的会话上。
（`SKILL.md` frontmatter 里的 `model:` 是否生效未经证实，当双保险留着。）

## 安装

```bash
zsh ops/install.sh
```

幂等，可重复跑。卸载 `zsh ops/install.sh --uninstall`（只卸 launchd 那两个，
Claude 定时任务在侧边栏里删）。

装完还差一步（要密码，脚本不代跑）：

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 23:10:00
```

## 结构

```
launchd
  └─ ~/Library/Application Support/autotrade-ops/launch_in_terminal.sh   ← 垫片
       └─ open -a Terminal
            └─ <项目>/ops/night_run.sh  或  morning_collect.sh           ← 真正干活
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

## 产出物

在 `~/Desktop/autotrade-logs/`（`AUTOTRADE_OUT_DIR` 可覆盖）：

- `autotrade_YYYY-MM-DD_overnight.txt` — 整晚终端日志
- `digest_YYYY-MM-DD.txt` — DB 摘要（开仓/事件/下单/原始消息/未平持仓）

复盘结论不落盘，就是 Claude 那条回复。想存档就在对话里让它写。

运维流水账 `logs/ops.log`；launchd 自己的 stdout/stderr 在
`~/Library/Application Support/autotrade-ops/launchd.*.log`（**不在项目里**，见上面 TCC 那段）。

## 手动操作

立刻触发（不等到点）：

```bash
launchctl kickstart -k gui/$(id -u)/com.chengqiu.autotrade.night
```

看下次触发时间 / 上次退出码：

```bash
launchctl print gui/$(id -u)/com.chengqiu.autotrade.morning
```

改了 `ops/*.sh` 直接生效；改了 `install.sh` 或 `launch_in_terminal.sh` 要重跑 `install.sh`。

注意 `morning_collect.sh` 会停掉正在跑的 listener，别在盘中手贱。

## 踩过的坑

**1. launchd 读不了 Desktop（TCC）。** 见上面「结构」。症状是退出码 127
`can't open input file`，而手动跑一切正常。

**2. 别用 `pgrep -f "autotrade.app.main"` 当运行守卫。**
这个串太松，任何命令行里含它的进程都会匹配 —— 包括你自己敲的
`pgrep -f autotrade.app.main` 或 `grep autotrade.app.main`。后果是当晚启动被静默跳过
（ops.log 里留下一条 `listener already running, skip`，但其实什么都没跑）。
现在两个脚本统一用 `LISTENER_PAT='bin/python -m autotrade\.app\.main'`，
匹配 Makefile 实际起的完整命令行。早上的 kill 路径同理，否则会误杀一个只是在
grep 的 shell，而真正的 listener 反倒停不掉。

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
