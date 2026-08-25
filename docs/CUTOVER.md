# CUTOVER — 新仓库切换检查单 [0018]

ROADMAP「P0-0 切换检查单」的落地版，吸收 7/22-7/28 四个 SIMULATE 实战夜
（REVIEW-2026-07-23/24/25/28）的教训。目标状态：本仓库（chore/refactor 重构版）
接管生产，老仓库退役为只读回滚备份。

原则：**每一步都可回退、每一步都有验证动作**。检查项按顺序做，
不确定就停在 SIMULATE——宁可多陪跑一周，不要带着疑问切 REAL。

---

## 0. 前置（任意时间可做，不影响老 bot 运行）

- [ ] **代码水位核对**。切换用的工作区必须打齐全部 patch：
  ```bash
  git log --oneline -5        # 确认在 chore/refactor 且包含最新 patch commit
  .venv311/bin/python -m pytest tests/ -q
  ```
  尾行 passed 数必须与**最新交付说明**里的期望数逐字一致
  （历史水位：0001-0005=360 → 0006=367 → 0007=374 → 0008=382 →
  0010-0018 批次见其交付说明）。数字对不上 = 有 patch 没打上/打串了，
  **先解决再往下走**。教训：7/24 第二夜整晚跑的是未打 patch 的代码
  （commit 时间比停机还晚 7 分钟），当夜行为完全不能用来评估 patch 效果。
- [ ] patch 生效抽查（来自 7/24 复盘的"验证三件套"思路，按最新批次更新）：
  - `grep -c notify_bg autotrade/listener/open_flow.py` ≥ 1（0003）；
  - 启动探测样本是 ≈ATM 档（不再是链首 C500000，0004）；
  - 首条信号 📩 → `[broker] Order` 间隔 < 0.3s（7/28 实测 20ms）。
- [ ] `cp` 生产配置进新仓库：`config/.env`、`config/channels.json`。
  跑 `python -m autotrade.app.main` 前先看 preflight 全绿
  （token 体检 / 风控 / broker 探测 / 行情权限）。
- [ ] `.env` 新增变量对照 `config/.env.example` 补齐（老 .env 没有这些）：
  `STARTUP_BACKFILL_MIN=60`、`OPEN_SIGNAL_MAX_AGE_SEC=300`（0008）、
  `LOTTO_STOP_LOSS_PCT=80`（0013）、`CLOSE_QUOTE_FALLBACK=true`（0010）、
  `RECONCILE_INTERVAL_MIN=60`（0016）；`STRATEGY_B` 保持 false（ship-dark）。
- [ ] **自消息过滤确认**：selfbot 账号自己的 ID 不在任何频道的
  `trigger_user_ids` 里（[refactor-change] e 激活后在的话会丢信号）。

## 1. 数据迁移（盘后做）

- [ ] 老仓库 `data/trades.db` **盘后**拷入新仓库（schema 未变，直接复用）。
- [ ] 拷完立即对账，把行权/过期造成的陈旧 OPEN 清干净再开跑：
  ```bash
  python -m autotrade.ops.sync_positions --dry-run   # 先看 diff
  python -m autotrade.ops.sync_positions             # 确认后落库
  ```
  背景：ITM 到期自动行权本地无感知（lessons #14/#15）；过期仓位会毒化
  watcher 快照配额（7/13 整夜 backoff 循环）。

## 2. 机器常驻（切换前必须解决，代码兜不了）

- [ ] **caffeinate 常驻**。四夜"16-20 分钟节拍器式掉线"的真相是 macOS
  系统睡眠（7/28 破案）：睡眠期间 gateway 死、SL/TP/EOD watcher 停摆、
  整段睡眠期消息静默丢失，7/25 AVGO 强平窗口被吃掉 5 分钟同一根因。跑法：
  ```bash
  caffeinate -is make run
  ```
  或系统设置 → 电源 → 防止自动睡眠。0008 的睡眠检测/回补只是兜底，
  **不是替代**。
- [ ] 启动日志确认：`watchers started: sl / eod / tp / alive-heartbeat`、
  （配置了的话）`[backfill] 启动回补` 与 `[reconcile] started` 各一行。
- [ ] 网络：路由器 NAT 空闲超时调大（常见默认 900-1200s，正是 16-20min
  churn 周期的另一半贡献），有条件走网线。
- [ ] launchd 化（可选，建议先前台 caffeinate 跑稳再说）：见 SETUP.md
  「Running unattended」——WorkingDirectory=repo 根、ThrottleInterval≥60s
  （防 IDENTIFY 限流 crash-loop，lessons #12）。

## 3. SIMULATE 陪跑（判据：稳定一周）

新 bot 以 `DRY_RUN=false + MOOMOO_TRD_ENV=SIMULATE` 跑，**老 bot 同时停掉**
（双跑 = 双下单；若想并行对比，新 bot 必须 `DRY_RUN=true` 只观察）。

每晚盘后复盘（语料 = 当夜 discord 导出 + `logs/app_*.log` +
`python -m autotrade.ops.show_today`），一周内同时满足才算过：

- [ ] **零漏信号**：KC/enrich 每条开仓、平仓喊单都有对应的
  raw_signals 记录 + 决策日志（下单 / 拒单 / 告警三者其一，静默=事故）。
  对照案例：7/24 META "into the close" 误路由丢单、7/25 "Out 25% more"
  双语双漏——发现漏检先加语料行（docs/CORPUS.md 工作流）再改 parser。
- [ ] **零误卖**：每笔卖出都能指回一条 KC 指令或 watcher 阈值
  （SL/TP/EOD/lotto 硬底）。对照案例：7/23 "止损设在现价" 被解析成
  CLOSE pct=33，距离误卖只差两道薄防线。
- [ ] **告警噪音可忍**：TG 一夜的条数人愿意看完；重复告警有节流
  （runner-preserve 0002、EOD no-quote 0007、reconciler 0016 均已带）。
  噪音超标就修节流，不许直接关告警。
- [ ] 每晚 `pytest -q` 保持全绿（周末跑也应全绿——挂钟依赖的假红 0007 已根治）。
- [ ] 中途改了任何代码/配置 → 一周计时**重新开始**。

## 4. 切 REAL：.env 双钥匙，一次只拧一把

真单开关就是两个变量（不需要改任何代码）：

| 步骤 | DRY_RUN | MOOMOO_TRD_ENV | 含义 |
|---|---|---|---|
| A（已完成） | true | 任意 | 全链路干跑，不碰 broker |
| B（陪跑中） | false | SIMULATE | 模拟盘真下单 |
| C（目标） | false | REAL | 真金白银 |

- [ ] **切换顺序**：只把 `MOOMOO_TRD_ENV` 改成 `REAL`，其余一律不动。
  **绝不同一天既改 env 又改代码/patch**——出问题分不清归因。
- [ ] REAL 前置检查：`MOOMOO_ACC_ID` 是真实账户 ID（preflight 会校验
  env 匹配）、`MOOMOO_TRD_PWD` 已填（REAL 需要 unlock_trade；SIMULATE
  一直不需要所以这项从没被验证过，lessons #1）、
  单笔成本上限确认（REAL 下代码强制 `MAX_COST_PER_ORDER` ≤ $1000，只能调低）。
- [ ] 风控参数按真实资金重过一遍：`MAX_PRICE_PER_CONTRACT` /
  `MAX_DAILY_COST` / `MAX_DAILY_ORDERS`（模拟盘的宽松值不适用）。
- [ ] 改完 `.env` **必须重启进程**——没有热加载（SETUP.md §2）。

## 5. REAL 首日（陪跑日）

- [ ] 首个交易日**人盯盘**：确认首笔真单端到端（信号 → 风控 → 下单 →
  fill 确认 → 落库 → TG）后才离开。
- [ ] 首日前先在 SIMULATE 再跑一夜作对照（ROADMAP 原文：切换当天首个
  交易日用 SIMULATE——即 REAL 首日的前一晚必须是干净的 SIMULATE 夜）。
- [ ] **老仓库进程彻底清理**（双跑 = 双下单，REAL 下是真双倍仓位）：
  ```bash
  launchctl list | grep -i -e discord -e autotrade -e copytrade   # 应为空
  ps aux | grep -i -e run_listener -e autotrade | grep -v grep    # 应为空
  ```
  有 launchd plist 的 `launchctl unload` 并移走文件；确认老仓库目录里
  没有 nohup 残留进程。
- [ ] 首日收盘后：`python -m autotrade.ops.sync_positions --dry-run` 对平、
  `ops/show_today` 过一遍当日订单、TG 记录与 moomoo 成交明细三方一致。

## 6. REAL 后日常运维

- [ ] **每日开盘前对账**：`python -m autotrade.ops.sync_positions --dry-run`，
  有 diff 人工确认后去掉 `--dry-run` 落库（行权/过期/手动平仓都会造成漂移）。
- [ ] 盘中漂移可见性：`RECONCILE_INTERVAL_MIN=60`（0016）让 bot 每小时
  自动 diff 一轮，有漂移发 TG（report-only，落账修复仍走上面那条人工命令）。
- [ ] 每次重启前：`pytest -q` 全绿 + 水位数对得上；重启后看
  watchers/heartbeat/backfill/reconcile 四行启动日志。
- [ ] postmortem 纪律：任何解析/执行分歧，先把原文写进
  `tests/corpus/`（期望 = 当前行为或修正后行为，见 docs/CORPUS.md），
  再动代码——安全网先行。

## 7. 回滚路径

- [ ] **老仓库只读保留**（连同它当时的 `.env`/`channels.json`/launchd plist），
  不删、不继续开发——它是唯一经过长期实盘验证的退路。
- [ ] 回滚操作 = 停新起老：
  1. 停新 bot（Ctrl-C 一次即可，shutdown 幂等）；
  2. 盘后把 `data/trades.db` 拷回老仓库（schema 相同双向可用；
     盘中紧急回滚就让老 bot 用自己的旧 DB + 立刻 `sync_positions` 对平）；
  3. 老仓库按原方式启动，TG 确认上线。
- [ ] 回滚不是终点：把触发回滚的事故写成 REVIEW-YYYY-MM-DD 复盘 +
  语料测试，修完再走一遍本检查单（从 §3 SIMULATE 陪跑重新计时）。
