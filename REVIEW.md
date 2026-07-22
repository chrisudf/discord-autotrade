# discord-copytrade (chore/refactor) 架构评审与重构说明

> 评审对象:`~/Desktop/discord-copytrade`,branch `chore/refactor`(commit a4ca755,2026-07-22 clone)。
> 评审方式:8 个子系统并行深读 + git 历史 postmortem 分析 + 3 份独立架构提案对比综合。
> 基线:老仓库测试在 Python 3.11 下 **282 passed / 1 skipped** 全绿(Python 3.14 会因
> `audioop` 移除和 `asyncio.get_event_loop()` 行为变化挂 4 项,均为环境兼容问题,非代码 bug)。
> 本目录(discord-autotrade)是按本评审结论落地的重构版,见文末与 ROADMAP.md。

## 一、总体判断

这个项目的**行为层是健康的**:约 30 个 postmortem 修复(dedup 窗口、孪生防护、strike 过滤、
频道隔离、runner-preserve、配额退避……)都是真金白银换来的正确决策,测试套件就是这些事故的
行为规格书。真正的问题全部在**结构层**:代码住错了地方、状态散落在模块全局、同一职责有两份
漂移的实现。用一句话概括:**缺的不是抽象,是"每个关注点只有一个家"。**

git 历史给出了最有力的证据:近 50 个 commit 里 32 个改的是 `discord_client.py`——
churn 集中在上帝模块,不是偶然,是结构性的。

## 二、主要发现(按严重程度)

### HIGH

1. **双入口漂移,已造成真实事故**
   `scripts/run_listener.py`(514 行,生产入口)和 `src/listener/discord_client.py` +
   `src/main.py` 各自创建 `discord.Client`、各自注册一套 `on_ready/on_message/on_message_edit`、
   各自启动 watcher。commit 207664f 的事故:生产入口一直没启动 SL/TP/EOD watcher,而唯一会
   启动它们的入口又被硬卡禁止在 REAL 跑——真盘持仓裸奔无保护。这是整个历史里最吓人的一次,
   根因纯粹是"两套接线"。
   连带后果:`discord_client.py:337` 的自消息过滤检查的是**从未登录**的那个模块级 client
   (`client.user` 恒为 None),在生产路径上是死代码。

2. **listener 上帝模块(1258 行,9+ 职责)**
   事件路由、6 个内存 dedup/节流 registry(msg-id、raw 文本 30s、OPEN 指纹 5min、CLOSE 指纹
   60s 含回滚、twin 快照 60s、2 个告警节流 dict)、报警启发式正则组、DTE 笔误防护、卖出限价
   策略(SELL_SLIP)、OPEN 编排、CLOSE 编排(260 行函数 + 8 处手工 `any_executed = True` 记账)、
   TG 兜底、进程引导,全部同居一个文件。三份复制的惰性 GC 实现;两个告警节流 dict 从不清理
   (无界增长)。conftest 需要手工清 ~12 个模块全局才能隔离测试。

3. **broker 上帝模块 + 真实并发隐患**
   `moomoo_client.py`(1016 行)七种职责混住。quote ctx 有 `threading.Lock`,**trade ctx 没有**
   ——而所有调用都通过 `asyncio.to_thread` 跑在真线程上,两个几乎同时的下单/平仓可以双双创建
   ctx、或 reset 正在使用的 ctx,今天没炸只是因为信号频率低。stale-session"reset + 重试一次"
   逻辑复制 3 份,且 `query_order_status` 完全没有(fill 轮询遇到过期会话直接失败)。
   错误分类靠 4 组散落的子串关键字表,moomoo 每换一种措辞就是一次夜间事故(5811b6a)。

4. **配置面失控**
   83 处 `os.getenv`、29 个变量、3 种生命周期(import 时冻结 / 每次调用重读 / 依赖 dotenv
   加载顺序),`load_dotenv(override=True)` 在 3 个库模块 import 时各跑一次。哪个值生效取决于
   import 顺序。`.env.example` 已过时且不全。6/18 IWM 事故(ACC_ID 没配,接到信号才炸)就是
   "启动时不校验配置"这一类的代表。

5. **存储层夹带交易策略 + import 副作用**
   `categorize()`(DTE×tags 的策略矩阵)和 TP tier-bit 编码住在 `positions_db.py`;三个模块
   import 时各自建表/mkdir;`logger_db` 和 `positions_db` 两个模块共写同一个 `trades.db`。
   曾导致测试往生产 DB 写入 130 个假仓位(conftest docstring 里的 7/3 事故)。

6. **测试资产的两个安全隐患**
   `scripts/` 里 15 个 `test_*.py` 对 pytest 是可收集的,其中 `test_handle_message_real.py`
   在 **import 时把 DRY_RUN 改成 false**——`pytest scripts/` 一次误跑就是实弹;另有 7 个带真
   断言的回归测试(holidays、fingerprint、qcom_replay、parser_rules、risk_manager、full_flow、
   skip 契约)从不在 CI 里跑,一直在烂。`tp_watcher`(会真卖仓位的路径)零测试。

### MEDIUM(择要)

7. **四条卖出路径四份复制**:kc_close / SL / TP / EOD 各自实现"锁 → 锁内重读 → 报价 → 卖出 →
   记账 → 通知"骨架,彼此有意的差异(SL 记账失败冻结、TP 先标 tier 再记账、EOD 锁内取价、
   各自 slippage 8/5/10/5%)和无意的漂移(TP 用 round() 而 manager 用 ceil())混在一起分不清。
8. **解析器双语双管线**:close_parser 里 EN/ZH 各一套 `_extract_symbols/_extract_pct/_parse_close`,
   ~200 行近似重复且已有行为漂移;动作词表在 router(detect_action)和 close_parser 两处手工
   同步,历史上失同步 3 次;signal_parser 的 B2 模式遮蔽 B3(B3 是死代码)——已知怪异,本次
   重构**原样保留**,列入二期决策。
9. **死代码**:根目录 `config/channel_loader.py`(旧版 API,无人 import)、
   `src/notifier/telegram_bot.py`(无加固的旧 TG 实现)、`src/main.py`(已弱化的入口)。
10. **fire-and-forget 弱引用**:`fill_checker.spawn` 的 task 只有弱引用,GC 可以吃掉
    "仓位未确认成交"这个最需要活着的告警任务。
11. **通知层**:传输(429 重试、MarkdownV2 400 降级、锁串行)和 8 个 format_* 领域格式化
    混在一个模块;5 次 postmortem 各自长出一个本地节流 dict。

### 反复发作的故障类别(来自 git 历史分析)

| 故障类别 | 代表事故 | 结构性根因 | 杀死它的重构 |
|---|---|---|---|
| 解析漏检/误检 | 9/10 个 postmortem | 无语料回归门 | 金语料回放套件(二期) |
| dedup 竞态 | 0.9s 双语孪生双执行 | 4 套 dedup 各自为政 | dedup 集中一个模块(已做) |
| 卖出竞态 | 双卖、SL/CLOSE 互踩 | 四份卖出骨架 | SellExecutor(二期) |
| SDK 措辞变化 | quota 关键字没接住 | 子串匹配散落 4 处 | errors.py 集中 + 逐串 pin 测试(已收拢) |
| 接线漂移 | 207664f watcher 裸奔 | 双入口 | 单一组合根(已做) |
| DB 漂移 | OCC 自动行权无感知 | 对账靠手工脚本 | 定时 reconciler(二期) |
| TG 噪音 | 一夜 12+ 条 runner 提醒 | 节流逐处补丁 | 通知策略层(二期) |

## 三、重构方案(本目录已落地部分)

三份独立架构提案(渐进搬运派 / 务实六边形派 / 回放优先派)在 P0 层完全收敛,分歧只在二期
走多远。落地原则:**Move, don't rewrite**——函数体逐字搬运,只改归属和 import;
已知怪异行为原样保留;唯一的 6 项行为变化全部显式标注 `[refactor-change]`:

1. trade ctx 生命周期加 `threading.Lock`(修复潜在竞态,镜像 quote 侧);
2. stale-session 重试统一为一个 helper,`query_order_status` 也纳入(恰好重试一次,白名单不变);
3. `fill_checker.spawn` / watcher task 持强引用;
4. 两个告警节流 dict 获得与其它 registry 相同的惰性 GC(有界);
5. 自消息过滤因 client 注入而从死代码变活;
6. storage/risk 的 import-time 建表/dotenv 移除,改为入口显式 `init()`。

新结构(包名 `autotrade`):

```
autotrade/
├── app/          main.py(唯一组合根)/ preflight.py / connection.py(断线遥测+回补)
├── settings.py   启动时一次性校验、列出全部错误
├── config/       channel_loader.py(唯一版本)
├── parsing/      signal_parser / close_parser / holidays(逐字)
├── policy/       pricing.py(买卖定价内核统一)/ guards.py(DTE 防护)/ positions.py(categorize、TP 阶梯)
├── listener/     router / open_flow / close_flow / dedup(6 registry 一个家)/ heuristics
├── broker/       common / errors(4 组关键字集中)/ trade(加锁+统一重试)/ quote
├── position/     manager + 3 watcher + fill_checker(逐字;强引用)
├── storage/      positions_db / logger_db(显式 init)
├── notify/       transport(含 _safe_notify)/ messages(纯格式化)
├── ops/          只读运维脚本(python -m autotrade.ops.*)
└── diag/         活体诊断改名 diag_*,DRY_RUN 改动只许在 main() 内
tests/            13 个文件逐字移植 + scripts 里搁浅的 7 个回归测试折叠进 pytest
```

删除不搬:根目录旧版 `channel_loader.py`、`telegram_bot.py`、`src/main.py`、
`discord_client.py` 的模块级 client 与重复事件栈。

**有意不做**(风险>收益或需要盘中验证,全部进 ROADMAP.md):SellExecutor 四路合一、
close_flow Outcome 枚举、EN/ZH LanguageProfile 合并、B2/B3 遮蔽修复、TP round/ceil 统一、
DB 合并 + tape/journal 回放架构、Settings 吞掉 broker/watcher 的 per-call env 读取。

## 四、验收

见 README.md"验证"一节:新仓库 `make test` 需全绿(≥282 项 + 折叠新增),且
`import autotrade.app.main` 无副作用;上线前必须按 ROADMAP.md 的"切换检查单"做 DRY_RUN 并跑。

## 五、给老仓库的顺手修复建议(不动结构也该做)

- `scripts/test_handle_message_real.py` 立即改名/挪出 pytest 可收集范围(实弹风险);
- `tests/test_backfill.py` 的 `asyncio.get_event_loop()` 换 `asyncio.run`(3.12+ 直接挂);
- 删三个死文件;`docs/lessons.md` #4 时区条目与已上线实现矛盾,先修文档再谈重构。
