# Setup Guide

discord-autotrade：Discord 信号 → 解析 → 风控 → moomoo 下单 → Telegram 通知。
包名 `autotrade`，所有入口都用 `python -m autotrade.<...>` 跑，**cwd 必须是
repo 根**（ops/diag 脚本里 `config/.env`、`data/`、`logs/` 按 cwd 或 repo
根解析）。

## 1. Environment

Python **3.11**（282+ 项测试在 3.11 上验证；Makefile 固定用
`/opt/homebrew/bin/python3.11`）。

```bash
make venv
# 等价于：
#   /opt/homebrew/bin/python3.11 -m venv .venv311
#   .venv311/bin/python -m pip install -r requirements.txt
source .venv311/bin/activate
```

注意 pip 包名 vs import 名：`pip install moomoo-api`，但 `import moomoo`
（requirements.txt 已含）。

## 2. Config

配置都在 `config/`：

```bash
cp config/.env.example config/.env          # 编辑填 token
cp config/channels.json.example config/channels.json   # 编辑填频道
```

- `.env` 只在入口（`autotrade.app.main` / ops / diag 脚本的 `main()`）加载
  一次，`override=True`；库代码 import 时不读 `.env`。改了 `.env` 必须重启
  listener——没有热加载（parser/risk 代码改动同理）。
- 监听哪些频道、触发用户、每频道张数上限等全部配置在
  `config/channels.json`（`.env` 里的 `DISCORD_CHANNEL_ID` /
  `DISCORD_TRIGGER_USER_IDS` 是历史遗留，现行代码不读）。
- 全部 29 个变量的含义、缺省值、被哪个模块读取，见
  [config/.env.example](../config/.env.example) 的分组注释。

### Discord User Token
1. Browser login to Discord
2. F12 -> Network -> any request -> Headers -> Authorization
3. **WARNING: token equals your password. Leaking = account stolen.**

token 贴进 `.env` 后可用 `python -m autotrade.diag.diag_token` 体检
（长度/引号/空格/分段数，排查复制粘贴事故）。

### Telegram Bot
1. Chat @BotFather -> /newbot
2. Get BOT_TOKEN
3. Send a message to your bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   to find your chat_id
4. 验证：`python -m autotrade.diag.diag_telegram`

### moomoo OpenD
1. Download from https://www.moomoo.com/download/OpenAPI
2. Launch and login. Confirm 127.0.0.1:11111
3. `MOOMOO_TRD_ENV=SIMULATE` 起步；`MOOMOO_ACC_ID` 在 `DRY_RUN=false`
   时必填且非零（preflight 会强制 exit，空着会在接到信号时才暴露——
   6/18 IWM 卖单失败教训）
4. 交易密码填 `MOOMOO_TRD_PWD`（旧名 `MOOMOO_TRADE_PWD` 仍兼容但会 warn）。
   SIMULATE 下不会调 unlock_trade（lessons.md #1）
5. 验证：`python -m autotrade.diag.diag_quote_permission`（行情权限）、
   `python -m autotrade.diag.diag_quote US.SPY...`（单合约报价）

## 3. ⚠️ DRY_RUN first — 默认不下真单，请保持一段时间

`.env` 缺省 `DRY_RUN=true`：全链路照跑（解析/风控/落库/TG 通知），但
**不向 broker 提交订单**。上线顺序：

1. `DRY_RUN=true` 跑几天，看 TG 通知与 `data/trades.db` 落库是否符合预期；
2. `DRY_RUN=false` + `MOOMOO_TRD_ENV=SIMULATE` 跑 **1-2 周模拟盘**；
3. 确认统计口径无误后才切 `MOOMOO_TRD_ENV=REAL`。REAL 下单笔成本上限被
   代码强制 ≤ $1000（`MAX_COST_PER_ORDER` 只能调低，不能调高）。

真单开关就是这两个 env 变量——**不需要改任何代码**（旧版 "uncomment the
Real order block" 的说法已作废）。

另注意：`autotrade/diag/` 里的 `diag_moomoo_real` 和
`diag_handle_message_real` 会在自己的 `main()` 里**强制
`DRY_RUN=false`**（这是它们的用途：验证真实下单分支）。只在 SIMULATE
环境跑它们。

## 4. Test

```bash
make test
# 等价于：.venv311/bin/python -m pytest tests/ -q
```

integration 标记的用例（需要真实 OpenD/Discord/Telegram）默认被
pytest.ini 的 `-m "not integration"` 排除。

导入无副作用自检：`python -c "import autotrade.app.main"` 应静默通过
（不建表、不读 .env、不连任何服务）。

## 5. Run

```bash
make run
# 等价于：.venv311/bin/python -m autotrade.app.main
```

启动时序（组合根在 `autotrade/app/main.py`）：load .env → setup_logging →
storage/risk 显式 init（建表）→ preflight（token/风控/broker/行情探测）→
Discord client 连接 → 频道校验 + TG 启动通知 → 三个 watcher（SL/TP/EOD）。

开盘前建议先对账（ITM 到期自动行权会让本地 DB 变陈旧，lessons.md #14）：

```bash
python -m autotrade.ops.sync_positions --dry-run   # 先看 diff
python -m autotrade.ops.sync_positions             # 确认后落库
```

### Running unattended（Mac 长跑注意）

macOS WiFi 省电会静默掐死空闲 TCP，导致 Discord gateway 每 15-20 分钟
断一次（lessons.md #7/#16）。前台跑用：

```bash
caffeinate -i .venv311/bin/python -m autotrade.app.main
```

**launchd 注意事项**（做成 LaunchAgent 时）：

- `ProgramArguments` 用绝对路径的 venv python：
  `<repo>/.venv311/bin/python -m autotrade.app.main`；
- **`WorkingDirectory` 必须设为 repo 根**——`config/.env`、
  `config/channels.json` 按包位置解析没问题，但 ops/diag 与日志/数据目录
  依赖 cwd = repo 根；
- launchd 环境不继承 shell 的 env，全部配置要落在 `config/.env` 里；
- `KeepAlive=true` 可自动拉活，但注意 lessons.md #12 的 IDENTIFY 限流：
  crash-loop 式重启会触发 Discord 指数退避，建议加 `ThrottleInterval`
  （≥60s）；
- caffeinate 包装在 launchd 下同样适用（`ProgramArguments` 首项换成
  `/usr/bin/caffeinate`，`-i` 后接 python 命令），或单独跑
  `caffeinate -i -d`；
- stdout/stderr 重定向到 `logs/`（`StandardOutPath` /
  `StandardErrorPath`），应用自身日志在 `logs/app_YYYY-MM-DD.log`。

简单的后台替代（不走 launchd）：

```bash
nohup caffeinate -i .venv311/bin/python -m autotrade.app.main > logs/listener.out 2>&1 &
echo $! > logs/listener.pid
```

## 6. Ops 脚本（只读运维，`autotrade/ops/`）

```bash
python -m autotrade.ops.show_today [YYYY-MM-DD]  # 按 ET 看当日 raw_signals/orders/daily_orders
python -m autotrade.ops.generate_report          # 日报 CSV + 统计（cwd = repo 根）
python -m autotrade.ops.sync_positions           # broker ↔ 本地 DB 对账
python -m autotrade.ops.reset_daily_limit        # 清熔断（--hard 连订单记录一起清，慎用）
python -m autotrade.ops.backfill_history         # 拉历史消息离线重放 parser
python -m autotrade.ops.analyze_trades           # OPEN/CLOSE 配对回测报告
python -m autotrade.ops.backtest_parser          # 频道历史消息 parser 回测
```

## 7. Diag 脚本（活体诊断，`autotrade/diag/`，需真实外部服务）

```bash
python -m autotrade.diag.diag_token               # token 体检（本地，无网络）
python -m autotrade.diag.diag_discord_connect     # Discord 连接 + 频道配置
python -m autotrade.diag.diag_verify_channels     # 频道可见性 + 最近消息试解析
python -m autotrade.diag.diag_telegram            # TG 推送全格式
python -m autotrade.diag.diag_quote CODE...       # 单合约报价
python -m autotrade.diag.diag_quote_permission    # OPRA 权限探测
python -m autotrade.diag.diag_option_chain [SYM]  # 期权链
python -m autotrade.diag.diag_find_option_code    # 找可用合约
python -m autotrade.diag.diag_listener_flow       # 端到端干跑（不连 Discord，不 load .env）
python -m autotrade.diag.diag_moomoo_real         # ⚠️ 强制 DRY_RUN=false 真下单
python -m autotrade.diag.diag_handle_message_real # ⚠️ 强制 DRY_RUN=false 真下单
```

## 8. 更多

- 踩坑史与不变量：[docs/lessons.md](lessons.md)（改代码前先读「重要架构决策」）
- 每日数据位置：`data/trades.db`（raw_signals + orders + positions）、
  `data/risk.db`（daily_orders + circuit_breaker）
