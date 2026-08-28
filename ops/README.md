# 夜间自动运行 + 早间复盘

launchd 用本机时区。作者机器是 Australia/Brisbane（UTC+10，无夏令时），
23:15 → 07:00 正好盖住完整美股时段（US 9:30–16:00 ET = Brisbane 23:30–06:00）。
换到别的时区要重新想这两个点位，改 `install.sh` 里 `emit_plist` 的时分参数。

## 每天发生什么

| 时间 | 谁 | 做什么 |
|---|---|---|
| 23:15 | launchd → `night_run.sh` | 开新 Terminal 窗口跑 `caffeinate -i make run`，输出 tee 到 `logs/session_YYYY-MM-DD.log` |
| 07:00 | launchd → `morning_collect.sh` | SIGTERM 停 listener → 整晚日志存到桌面 → 从 `trades.db` 抽摘要 |

**早上只取证，不自动复盘**（2026-08-17 起）：`morning_collect.sh` 全是纯 shell，
几秒钟结束，只产出 txt。复盘按需手动跑 `zsh ops/opus_review.sh $(date +%F)`。
停机和存日志不该依赖 Claude 跑得完 —— 复盘晚点补没关系，日志丢了就没了。

**只有一份复盘**。曾经并行跑过 Claude app 的定时任务 `autotrade-nightly-review`
（07:10，读同样两份产物，在对话里给一份可追问的版本），理由是那时 app 把定时任务
的模型写死成 Sonnet 5、拿不到 Opus。**2026-08-06 停用**：复查发现该限制已经没了
（当天那次定时任务 29 次 API 调用全是 `claude-opus-5`），两条路于是变成同一件事
跑两遍 Opus，一个早上吃掉 5 小时额度的一半多。留命令行这条是因为它不依赖 app 开着、
`--model` / `--settings` 能硬控模型和 effort、产物直接落盘。
现在它已不由早上的任务自动触发，要复盘就手动跑 `zsh ops/opus_review.sh $(date +%F)`。

先停机再存日志，不是反过来：`app/main.py` 的 SIGTERM handler 会走 `shutdown()`，
收尾日志也该进文件。

## 改复盘的关注点

改 `review_prompt.md` 就行，**不用碰 `opus_review.sh`**。脚本里的 prompt 只负责指路
（素材在哪、报告写到哪），具体看什么、怎么排序、怎么写全部指向 `review_prompt.md`，
那是唯一事实来源。

复盘的第一优先级是**查漏单** —— 拿 `raw_signals`（收到的全部原始消息）对照
`positions`/`orders`，找哪些消息其实是有效信号但 `signal_parser.py` 没认出来。

想要一份能追问的对话版：停用的定时任务还在
`~/.claude/scheduled-tasks/autotrade-nightly-review/SKILL.md`，在 Claude 侧边栏
"Scheduled" 里手动 Run now 即可（它产生的会话归在那儿，**不在普通聊天列表里**）。
但**不要在跑完复盘的那个会话里继续追问** —— 复盘结束时 context 已经十几万 token，
每追问一轮都要全量重读一遍，比整次复盘还贵。新开一个会话 Read
`review_YYYY-MM-DD.md` 再问。

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

### 模型：定时任务曾经锁死 Sonnet 5（已不成立）

2026-08-04 前后实测，定时任务无论怎么配都跑 Sonnet 5：`settings.json` 的 `model` /
`effortLevel`、`SKILL.md` frontmatter 的 `model:`、删任务重建，三条路全无效。
当时决定性证据是翻了 20 个会话记录，唯二两个 `claude-sonnet-5` 恰好是那两次定时任务。

**2026-08-06 复查：限制没了**，那天早上定时任务 29 次 API 调用全是 `claude-opus-5`。
换句话说 app 已经认 frontmatter 的 `model:` 了。这也正是要停掉它的原因 ——
它不再是"便宜的对话版"，而是第二份全价 Opus。

app 有自己一份偏好覆盖 `settings.json`：
`~/Library/Application Support/Claude/claude_desktop_config.json` 里的
`preferences.epitaxyPrefs.ccd-effort-level`（作者机器上是 `"low"`）。这是 app UI
的设置，影响所有 app 会话，要改在 UI 里改，别手改这个文件。命令行的
`--model` / `--settings` 不受它影响，这仍是 `opus_review.sh` 走命令行的理由之一。

排查过程中摸到的两件事仍然有效：**"Run now" 会复用任务已绑定的会话**（所以光改配置
不重建永远看不到变化），以及 **sessionId 与 transcript 文件名（cliSessionId）是两套 id**。

## 安装

```bash
zsh ops/install.sh
```

幂等，可重复跑。卸载 `zsh ops/install.sh --uninstall`（只卸 launchd 那两个；
Claude 定时任务已停用，彻底不要就在侧边栏 "Scheduled" 里删）。

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
- `review_YYYY-MM-DD.md` — Opus 5 + xhigh 复盘，写完自动弹开
- `.opus_stdout_YYYY-MM-DD.log` — 那次 headless 运行的 stdout，报告没出来时查这个
- `.last_opus_session_id` — 最近一次 headless 运行的 session id，要复查那次跑了什么用得上

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

**4. 机器睡着了就不会准点跑，跑起来了也可能中途睡过去。**
两件不同的事，要分开治：

- **起跑前**：launchd 会把错过的时点推迟到唤醒后才执行。ops.log 实测 8/4 是
  23:25:42、8/5 是 23:28:52 才起来，晚了 10-14 分钟，距 23:30 开盘只剩一分多钟。
  这个只能靠上面那条 `pmset repeat wakeorpoweron` 定时唤醒。
- **跑起来之后**：`caffeinate -i` 只挡"空闲睡眠"。8/5 夜实测进程照样被睡进去
  8 分 44 秒（alive 心跳报「挂钟跳变 524s」），**正好横跨 09:30 ET 开盘钟**，
  那段时间 SL/TP/EOD watcher 全停、Discord 消息不收（靠重连回补捞回来）。
  `night_run.sh` 已改成 **`caffeinate -is`**（`-s` 挡系统睡眠，接电源时生效；
  纯电池下 macOS 会忽略它）。

两条都挡不住**合盖**，夜里别合盖。

**5. moomoo OpenD 要自己保持开着。**
自动化不会帮你拉起它。没开的话 `app/preflight.py` 会 `sys.exit(1)` 快速失败 ——
不会静默出错，窗口会报错退出，第二天 `logs/ops.log` 里是 `no listener running`。

**6. 复盘很贵，一个早上能吃掉半个额度。** 2026-08-06 早上 07:15 起的 5 小时窗口用掉
50%+，拆开是：定时任务追问 ~60 万、`opus_review.sh` ~37 万、定时任务自动跑 ~27 万
（输入等价，cache_read 按 0.1× / output 按 5× 折算）。素材本身才 16KB —— 贵的是
xhigh 的 thinking、一次吐 15KB 报告（单轮 12.8K output ×5），以及**在长会话里继续
追问**：context 涨到 16 万后每轮都要全量重读。所以：同一件事只跑一遍、追问另开会话、
日志平静的晚上可以把 `--settings` 的 `xhigh` 降成 `high`。

## 两个实现细节

`tee` 前设 `PYTHONUNBUFFERED=1`：接了管道后 python 默认块缓冲，不设的话
早上 7 点收尾会丢掉最后一段日志。

DB 摘要用 sqlite 的 `.mode list` 而非 `markdown`/`box`：后两者会按终端宽度折行，
把长消息切成续行，喂给模型会串行。时间用 `datetime(x,'localtime')` 而非写死
`'+10 hours'`，跟着系统时区走。
