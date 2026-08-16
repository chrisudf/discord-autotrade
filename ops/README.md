# 夜间自动运行 + 早间复盘

launchd 用本机时区。作者机器是 Australia/Brisbane（UTC+10，无夏令时），
23:15 → 07:00 正好盖住完整美股时段（US 9:30–16:00 ET = Brisbane 23:30–06:00）。
换到别的时区要重新想这两个点位，改 `install.sh` 里 `emit_plist` 的时分参数。

## 每天发生什么

| 时间 | 谁 | 做什么 |
|---|---|---|
| 23:15 | launchd → `night_run.sh` | 开新 Terminal 窗口跑 `caffeinate -i make run`，输出 tee 到 `logs/session_YYYY-MM-DD.log` |
| 07:00 | launchd → `morning_collect.sh` | SIGTERM 停 listener → 整晚日志存到桌面 → 从 `trades.db` 抽摘要 |
| 07:00 起约 8-9 分钟 | 同上 → `opus_review.sh` | **Opus 5 + xhigh** 出权威复盘 markdown，写完自动弹开 |
| 07:10 | Claude 定时任务 `autotrade-nightly-review` | 读同样两份产物，**在对话里回答**一份可追问的复盘（Sonnet 5） |

**为什么取证和复盘分开**：停机和存日志不该依赖 Claude app 开着。Claude 的定时任务
只在 app 开着时跑（关着就推迟到下次打开），但那时取证文件已经稳稳躺在桌面上了 ——
复盘晚点看没关系，日志丢了就没了。

**为什么有两份复盘**：这个 app 版本把定时任务的模型**写死成 Sonnet 5**，没法改
（证据和排查见下面「模型」一节）。权威版因此走命令行 `claude -p --model claude-opus-5
--settings '{"effortLevel":"xhigh"}'` —— 命令行参数是唯一能硬控模型和 effort 的地方。
对话版保留是因为它能追问。两条路读同一份素材、同一份 `review_prompt.md`，只是模型
和载体不同。不想要对话版就在侧边栏 Scheduled 里禁用它；不想要权威版就
`zsh ops/morning_collect.sh --no-review`。

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

`~/.claude/settings.json` 只需要这一段：

```json
{
  "permissions": {
    "additionalDirectories": ["<项目路径>", "<输出目录路径>"]
  }
}
```

**是必须项，不是优化**：定时任务会话的 cwd 继承自创建它的会话，多半不是本项目，
读 cwd 之外的文件会触发权限确认 —— 无人值守时没人去点，整个任务静默挂死。
**不要**改成给 Bash 开全局白名单，权限面大得多且没必要。

### 模型：定时任务锁死 Sonnet 5

试过全部三条路，都无效：

| 试法 | 结果 |
|---|---|
| `~/.claude/settings.json` 的 `model` / `effortLevel` | 无效（对 app 创建的会话不生效） |
| `SKILL.md` frontmatter 的 `model:` | 无效 |
| 删任务重建，让它在新配置下建全新会话 | 无效，新会话仍是 sonnet-5 |

决定性证据：翻了 20 个会话记录，**唯二的两个 `claude-sonnet-5` 恰好就是那两次定时
任务运行**，其余普通会话全是 `claude-opus-5`。所以不是配置漏了，是这个 app 版本对
定时任务写死了模型。

另外 app 有自己一份偏好覆盖 `settings.json`：
`~/Library/Application Support/Claude/claude_desktop_config.json` 里的
`preferences.epitaxyPrefs.ccd-effort-level`（作者机器上是 `"low"`）。这是 app UI
的设置，影响所有 app 会话，要改在 UI 里改，别手改这个文件。

所以权威复盘走 `opus_review.sh` 的命令行，`--model` / `--settings` 不受这些影响。
排查过程中还摸到两件事：**"Run now" 会复用任务已绑定的会话**（所以光改配置不重建
永远看不到变化），以及 **sessionId 与 transcript 文件名（cliSessionId）是两套 id**。

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
- `review_YYYY-MM-DD.md` — Opus 5 + xhigh 权威复盘，写完自动弹开
- `.opus_stdout_YYYY-MM-DD.log` — 那次 headless 运行的 stdout，报告没出来时查这个

对话版复盘不落盘，就是 Claude 那条回复；想存档就在对话里让它写。

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
