# ROADMAP — 二期重构与切换计划

一期(本目录)只做"搬家 + 接线收敛",行为逐字保留。以下各项**有真实收益但需要单独决策
或盘中验证**,按建议顺序排列。每项动手前先补"当前行为"的表征测试,改完全绿才算完。

## P0 — 上线前必须

### 0. 切换检查单(cutover)
- [ ] 生产 `config/.env` / `channels.json` 拷入新仓库并跑 preflight 全绿;
- [ ] `data/trades.db` 由老仓库拷贝(schema 未变,直接复用);盘后进行,拷完跑
      `python -m autotrade.ops.sync_positions` 与 broker 对账;
- [ ] DRY_RUN=true 与老 bot 并行观察 3-5 个交易夜:逐 msg_id 对比两边的
      raw_signals / 决策日志 / TG 输出,任何分歧先写成测试再改代码;
- [ ] 自消息过滤已激活([refactor-change] e):确认 selfbot 账号不在任何频道的
      trigger_user_ids 里,否则会丢信号;
- [ ] 切换当天:老仓库 launchd/进程彻底移除(双跑 = 双下单),首个交易日用 SIMULATE。

## P1 — 高价值,建议尽快

### 1. 金语料回放套件(杀"解析漏检"故障类,9/10 postmortem 属于此类)
把老库 `data/trades.db` 的 raw_signals 全量导出为 `tests/corpus/*.jsonl`
(msg_id、channel、原文、当时的决策),加上测试里所有事故原文字符串。
tier-1:纯 parser/policy 回放,断言决策不变;进 CI。以后每个 postmortem 修复 =
追加一条语料 + 期望决策,5 行测试。
注意:期望值必须是**当前行为**的快照(包括已知怪异),否则安全网变成改写向量。

### 2. SellExecutor 四路合一(杀"卖出竞态"类)
kc_close/SL/TP/EOD 共享"锁→锁内重读→OPEN/PARTIAL 防护→限价→下单→记账→锁外通知"骨架,
差异显式参数化:slippage(SL 8% / TP 5% / EOD 10% / CLOSE 5%)、SL 记账失败冻结、
TP 先标 tier 再记账、EOD 锁内取价 + 无价拒卖 + backoff。
**决策点**:TP 用 round() 而 manager 用 ceil()——统一会改变真实 trim 张数,需要 Zoe 拍板;
不拍板就逐字保留两种取整。

### 3. close_flow Outcome 枚举
用 SOLD / SKIPPED_NOTIFIED / SKIPPED_SILENT / RUNNER_PRESERVED / BROKER_FAILED / NOT_FOUND
替换 8 处手工 `any_executed = True`;指纹回滚条件(零成交且有 broker 失败)和 runner-preserve
合并 TG 从 outcomes 推导。先把现有 TG 抑制矩阵写成表驱动测试。

### 4. 定时对账 reconciler(杀"DB 漂移"类)
把 `ops/sync_positions` 的 diff 逻辑变成启动时 + 定时任务,只对 broker 的确定性响应行动。
背景:OCC 自动行权无感知(lessons #14/#15)、过期仓位毒化 watcher 配额循环(f9e6df1)。

### 10. 磁盘闸门 + 日志尺寸上限(7/31 postmortem 未完成项,杀"宿主资源耗尽"类)
7/31 夜磁盘写满,sl/tp/eod 三路同时抛 `disk I/O error`,而**日志 sink 与 sqlite
同盘**,故障把自己的证据一起擦掉(app log 08:34 直接跳到 11:10、errors/ 当天 0 字节)。
告警侧已修(`notify/watchdog.py`,lessons #19),剩下两件事没做:

- **preflight 加可用磁盘闸门**:低于阈值(建议 2GB)拒绝以 `DRY_RUN=False` 启动,
  并 TG 告警。现在 `app/preflight.py` 查 broker / OPRA / 风控额度,唯独不查磁盘;
- **loguru 加 `rotation="50 MB"`**:目前只有 `rotation="1 day"` + retention,
  单日暴量(SDK 刷屏、traceback 风暴)没有上限;
- 可选:日志沟与 `data/*.db` 分盘,拆掉共享故障域——两个看起来独立的防御死于同一个原因。

注意:当晚根因在宿主机不在本仓库(checkout 才 ~4MB),所以闸门的价值是**把静默
盲区变成启动期硬失败**,不是省空间。

### 11. `RECONCILE_INTERVAL_MIN` 生产值定档(运维配置,非代码)
7/31 运行时该值为 0 → 定时对账全程关闭(见当晚启动日志 `[reconcile] ... 定时对账关闭`)。
`config/.env.example` 已建议 60,但生产 `.env` 没跟上。定时对账正好能兜住这类
"DB 写不进去/漂移"的静默失败——属于低成本高价值,切换检查单里应显式确认一次。

## P2 — 值得做,不急

### 5. 解析器收敛
- 动作词表单一来源(router 与 close_parser 共用),先写失同步回归测试;
- EN/ZH close 管线合并为一个引擎 × LanguageProfile,故意的不对称变成 profile 显式字段;
- B2/B3 遮蔽:显式决策——要么修(B3 复活)要么删(B3 就是死代码),二选一都要有测试;
- holidays 表加"年份耗尽"运行时告警。

### 6. 通知策略层(杀"TG 噪音"类)
每种告警带事件类型 + 显式策略(dedup key、节流窗口、合并规则)+ 安全级别;
安全关键事件(SL 冻结、fill 超时、裸卖空拒单、crash barrier)结构性豁免节流。

### 7. 配置统一收口
Settings 吞掉 broker/watcher 的 per-call `os.getenv`(改为显式 Tunables 视图)。
注意:每调用重读是现在"改 .env 热调 SL 参数"的依赖——收口后语义变为"重启生效",
必须写进 SETUP.md,否则半夜改 .env 以为生效了。

### 8. tape + journal(回放优先架构的完整形态)
raw_signals 扩展为完整 MessageEvent tape(过滤前落盘);每个分支决策写结构化 journal
(msg_id, stage, decision, reason)——postmortem 从 grep 变 SELECT。DB 合并为单一 state.db
(一次性迁移,盘后做,老 DB 留作回滚)。

### 9. 杂项
- watcher 三个轮询循环合并为单 scheduler tick(每 tick 一次批量报价,配额结构性达标);
- ConnectionSupervisor 类化 + 注入时钟(storm/churn/回补逻辑首次可单测);
- gateway close-code 靠抓 discord.py-self 日志文本,锁死库版本 + 加金丝雀测试;
- 三个 `_utc_iso` 行为不一致,统一前先写差异测试。

## 永远不做(除非能报出它杀死哪类 postmortem)

DI 框架、消息总线、微服务化、Stage/middleware 管道框架、超过 3 个 port 的抽象。
单人维护的钱路代码,每个抽象都要交房租。
