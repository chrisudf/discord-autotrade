# discord-autotrade

`discord-copytrade`(branch `chore/refactor`)的架构重构版。行为**逐字保留**
(Move-Don't-Rewrite),只重排结构:每个关注点一个家、一个入口、无 import 副作用。
评审报告见 [REVIEW.md](REVIEW.md),二期计划与上线切换检查单见 [ROADMAP.md](ROADMAP.md)。

> ⚠️ 尚未切换生产。老仓库(~/Desktop/discord-copytrade)仍是在跑的 bot;
> 切换前必须走完 ROADMAP.md 的 cutover 检查单(DRY_RUN 并行 3-5 夜 + 拷 DB + 移除老进程)。
> 两个仓库都不从这台机器 commit/push。

## 结构

```
autotrade/
├── app/          main.py(唯一入口/组合根)· preflight.py · connection.py(断线遥测+重登录回补)
├── settings.py   启动时一次性校验全部配置,列完所有错误再退出
├── config/       channel_loader.py(channels.json 注册表 + REST 校验)
├── parsing/      signal_parser · close_parser · holidays        —— 逐字移植
├── policy/       pricing(买+卖定价内核)· guards(DTE 笔误防护)· positions(categorize/TP 阶梯)
├── listener/     router → open_flow / close_flow · dedup(6 个查重/节流 registry)· heuristics
├── broker/       common · errors(SDK 报错关键字唯一来源)· trade(ctx 加锁+统一重试)· quote
├── position/     manager · sl/tp/eod watcher · fill_checker(强引用)
├── storage/      positions_db · logger_db(显式 init,无 import 副作用)
├── notify/       transport(TG 传输+加固)· messages(纯格式化)
├── ops/          运维脚本:python -m autotrade.ops.show_today 等
└── diag/         活体诊断(会碰真服务):python -m autotrade.diag.diag_telegram 等
tests/            288 个原测试函数逐字移植 + 39 个原本搁浅在 scripts/ 的回归测试折叠进来
```

## 快速开始

```bash
make venv                                # python3.11 venv + 依赖
cp config/.env.example config/.env       # 填 token/账号;DRY_RUN=true 起步
cp config/channels.json.example config/channels.json
make test                                # 全套测试
make run                                 # = .venv311/bin/python -m autotrade.app.main
```

## 验证(2026-07-22 移植完成时)

- 老仓库基线:Python 3.11,**282 passed / 1 skipped**;
- 本仓库:**329 passed / 1 skipped / 12 deselected(integration 标记)/ 1 xfailed**,
  老套件 288 个测试函数全部在场,断言与样本消息未改;
- 关键函数 AST 抽验:50 个高危函数(下单/平仓/查重/风控/解析)中 40 个与老库逐字一致,
  10 个差异逐一核对均为下述允许变化或纯重命名;
- `import autotrade.app.main` 无副作用(不建目录、不读 .env);包内无 sys.path hack。

## 与老仓库的全部行为差异(6 项,代码内标 `[refactor-change]`)

1. broker trade ctx 生命周期加 `threading.Lock`(修复 to_thread 并发下的潜在双 ctx 竞态);
2. stale-session"重连重试一次"统一为一个 helper,`query_order_status` 也纳入;
3. `fill_checker.spawn` / watcher 任务持强引用(防 GC 吃掉成交确认任务);
4. `_addon_alerted` / `_sized_entry_alerted` 告警节流获得与其它 registry 相同的惰性 GC(有界);
5. 自消息过滤激活(老代码在生产入口下 `client.user` 恒为 None,是死代码)——
   **切换前确认 selfbot 账号不在任何 trigger_user_ids**;
6. storage/risk 不再在 import 时建表/加载 .env,由入口显式 `init()`(conftest 同步更新)。

另:测试侧 `test_backfill` 的 `asyncio.get_event_loop()` 现代化为 `asyncio.run`(3.12+ 兼容);
`diag_handle_message_real` 等活体脚本的 DRY_RUN 改动移入 `main()`,pytest 永远收集不到实弹代码。
