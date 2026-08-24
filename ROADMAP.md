# ROADMAP — 二期重构与切换计划

一期(本目录)只做"搬家 + 接线收敛",行为逐字保留。以下各项**有真实收益但需要单独决策
或盘中验证**,按建议顺序排列。每项动手前先补"当前行为"的表征测试,改完全绿才算完。

## P0 — 上线前必须

### 0.0 实盘前回滚:模拟盘期间人为放大的额度(2026-08-11 起)

模拟盘积累数据阶段,为了让 TP 阶梯在数学上成立(qty=1 时"卖剩余 50%"要么全平要么不卖,
见 `review_2026-08-11.md` 第 ③ 项与 XOM 的 ~$185/张机会成本),把下单张数与配套额度整体
翻倍。**两个配置文件都在 .gitignore 里,这份清单是唯一入库的记录。**

切 `MOOMOO_TRD_ENV=REAL` 之前逐项减半:

| 文件 | 键 | 模拟盘现值 | 实盘改回 |
|---|---|---|---|
| `config/channels.json` | `default_qty`(两个频道) | 2 | 1 |
| `config/channels.json` | `max_price`(两个频道) | 2000.0 | 1000.0 |
| `config/.env` | `MAX_PRICE_PER_CONTRACT` | 10.0 | 5.0 |
| `config/.env` | `MAX_DAILY_COST` | 4000 | 2000 |
| `config/.env` | `DEFAULT_QTY`(仅兜底) | 2 | 1 |

`MAX_DAILY_ORDERS=10` 刻意**没**翻倍:翻倍的是每单张数,不是每日单数,当晚实际只下 1 单。
`MAX_COST_PER_ORDER` 同样不动(SIMULATE 下写大方便测试,REAL 另有 `risk.py` 的 $1000 硬顶)。

⚠️ 顺带发现:Layer 1 的价格上限实际由 `channels.json` 的 `max_price` 覆盖
(`risk.py:250`),`MAX_PRICE_PER_CONTRACT` 对这两个频道**从来没生效过**。
`max_price=2000` 等于没有单张价格上限。要真正启用 Layer 1,应把 `max_price`
降到 10.0 量级——这是独立决策,没有随本次翻倍一起做。

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

### 12. ADD(加仓)信号:决策后要么支持要么显式放弃(8/3 复盘)
8/3 14:28 ET,KC `just added a few SPY @ average is 1.76` 双语双发,两条都 parse-fail。
当晚 SPY 我们 1.93 进、16:06 平在 1.63;跟了这笔加仓均价会到 ~1.85,同一个出场
就是 -$22 而不是 -$30。

这不是 bug——`_looks_like_addon_attempt` 是**故意**只告警不下单的(无 strike/side
的 add-on 要"关联已有仓位"上下文,错配风险同 follow-up close,见 close_parser 顶部
注释)。但"故意漏"和"忘了做"在日志里长得一模一样,每次复盘都要重新推一遍。

要的是一次拍板,不是慢慢想:
- **方案 A(支持)**:限定在"已持仓 + 同频道 + 同 side + 有喊价"时按原合约加 1 张,
  沿用 OPEN 的风控闸门;strike 从现有仓位取,不从文本猜——这样绕开了错配风险的来源;
- **方案 B(放弃)**:保持现状,但把它写进 README 的"已知不做"清单,并让 TG 文案说清
  "检测到加仓信号,不自动执行"——现在的 parse-fail 告警读起来像故障。

任一方案都要先补当晚原文的表征测试。**决策点**:Zoe 拍板 A 还是 B。

### 13. `[zh_unrecognized]` 归因错误,会污染"要不要建中文名映射"的判断(8/3 复盘)
8/3 当晚该 warning 报了 5 次,全部是 SOFI——ticker 就是明文 ASCII,
`likely Chinese company name` 完全不成立。真实原因是 SOFI 不在 `open_symbols`
(我们没这个持仓),被裸 ticker 白名单正常挡掉。

代价不在这一晚,在**下一次判断**:这条 warning 的设计用途就是攒够样本后决定
要不要做数据驱动的中文名→ticker 学习(见 close_parser 顶部 TODO)。计数里混进
"未持仓"噪音,样本就没法用了。

修法很小:`_parse_close_zh` 里分两种 return——文本含裸 ticker 但不在白名单 →
`[zh_no_position]` (info 级,不是漏检);确实抽不出任何 symbol → 保留
`[zh_unrecognized]` (warning)。

### 14. 幻影仓的入口还开着:买单"成交与否"没有确定性复核(8/13 复盘,lesson #23)

8/13 的 1918 次拒单是**症状**。熔断(`position/retry_guard`)只保证不再刷屏,
产生幻影仓的那条路径**大部分仍未改**。逐条状态:

- ~~**`fill_checker.confirm_buy_fill` 有一条完全静默的分支。**~~ **(a) 已做**
  (2026-08-24):filled 分支现在无论走哪条都留一行
  `[fill] buy <code> filled dealt_avg=… (limit …) order=…`;
  `adjust_entry_price` 返回 False 时也从静默 return 改成 `logger.warning`。
  8/20 CRWV 88P 又踩过同一处——复盘要靠"四笔有 FILL_ADJUST、一笔没有"
  才能反推它是正常成交而不是任务没跑。

- **(b) filled 之后用 `_get_long_qty(option_code)` 复核真实持仓 —— 仍未做。**
  `order_status` 说成交不等于真有仓,当晚 broker 的 `position_list_query`
  从头到尾都是 0 长仓。对不上就告警并**不入账**。这是唯一能在 3 分钟内
  发现幻影仓的手段。**属钱路(影响是否入账),需单独拍板。**
  **待确认**(需查 moomoo 订单 2102868 的终态与 `filled_avg_price`):SIMULATE
  是否真的会对一张永远不可能成交的限价单回 `FILLED_ALL`。若是,则
  `order_status` 在 SIMULATE 下不可单独作为入账依据,(b) 就不是加固而是必须。

- **限价与市价的偏离没有任何闸门 —— 仍未做(下单前那一侧)。** 当晚用 $2.54
  去买一张市价 $14.80 的合约(信号说 weekly,parser 按规则解析成本周五 8/14,
  而喊单员指的是当天到期的 8/12;他 8 分钟后自己发的更正
  `$MU $945 8/12 calls $2.00 avg` 又 Parse failed)。现有闸门只看**成本**
  (`max_price` / `MAX_PRICE_PER_CONTRACT` / 日限额),没有一层比对
  "限价 vs 该合约当前报价"——而 TP 在 5 秒后就取到了 $14.80,说明报价当时拿得到。
  修法:下单前若能取到报价,`limit` 与 `last` 偏离超阈值(如 ±40%)拒单 + TG,
  文案里把两个价都写出来。这层同时是**到期日语义歧义的兜底**。
  **属钱路(影响是否下单),需单独拍板。**

  > 相关但不等价:2026-08-24 已加**成交后**的合理性闸门
  > (`fill_checker._DEALT_MIN_RATIO/_DEALT_MAX_RATIO`,见 lesson #26)——
  > 它拦的是 broker 回报的垃圾成交价污染成本基准(8/20 TSLA 限价 2.97 /
  > 回报 0.13),**拦不住"用错价格买错合约"**。下单前那道闸门仍然是空的。

### 15. 复盘素材与流水线的已知缺口(8/14 复盘)

已修的不在此列(digest 的 PARTIAL 漏报、SQL 错误被吞、断点续跑、日志摘要,
见 commit `7bd415d` 与后续)。剩下的:

- ~~**`send_telegram` 裸调用绕开 `notify()` 包装,日志里查不到发没发。**~~
  **已做**(2026-08-24):`transport.py::send_telegram` 的成功路径从 DEBUG 提到
  **INFO**(纯文本 fallback 成功同)。选这条而不是"错误路径统一走 notify()",
  是因为它一次覆盖全部裸调用点,且不改任何调用方的语义。
  8/5 的睡眠告警踩过一次并写进了 `transport.py` 的注释,8/13/8/14 的
  `tp_watcher._sell_rejected` 又踩了——1918 次告警在日志里零痕迹,导致 8/14
  的复盘据此得出"操作者手机上零告警"的**反向结论**(实际很可能收到了 ~1918 条)。
  **仍待确认**:reconciler 首轮漂移 TG 到底发没发(验证法见 review_2026-08-14 §4.3);
  这条要等下一晚的日志才能回答。
- **`effortLevel: xhigh` 是复盘账单里最大的单项。** 输出 token 按 5 倍计价,
  完整复盘的输出在 22k-45k,折合 110k-226k 有效 token,占总量三分之一。
  日志摘要把墙上时间从 61 分钟压到 11 分钟,但 token 没降(660k,仍在历史
  410k-680k 区间)。要压成本得动 effort 或报告篇幅,不是动素材。值得试一次
  `high` 做 A/B。
- ~~**listener 没有部署自检。**~~ **已做**(2026-08-14):`app/preflight.py::build_identity`
  在启动横幅第一行打 `构建 = <sha> @<branch> (<commit 时间>) 工作区干净/有未提交改动`。
  起因是 8/14 那晚跑的是 `d6fe50c` 之前的旧进程、两个修复一个都没生效,而日志里
  完全看不出来——当时是靠比对 commit 时间戳和 session start 才推断出来的。
  git 不可用时返回占位串、绝不抛异常(它在启动路径上,回归见
  `test_overnight_0814.py::test_build_identity_*`)。

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
