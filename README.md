# discord-autotrade

监听 Discord 频道里的期权喊单(中英双语),实时解析为结构化交易信号,经四层风控后通过
moomoo OpenD 自动下单;持仓由止损 / 分批止盈 / 到期强平三个 watcher 全程看护,
所有关键事件推送 Telegram。单机、单账户、面向个人操作者设计。

> ⚠️ 默认 `DRY_RUN=true`(只解析不下单)。任何配置改动后,先 DRY_RUN 观察至少一个完整
> 交易日再切真单;真盘单笔成本被风控硬卡 $1000,不可通过配置绕开。

## 架构总览

```
                          ┌──────────────────────────────────────────────┐
                          │              app/main.py(唯一入口)           │
                          │  .env 校验 → preflight 探测 → 过期仓位清扫    │
                          │  → 启动 watchers → Discord client.start      │
                          └──────────────────┬───────────────────────────┘
                                             │ on_message
 Discord 多频道 ──────────────────────────►  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ listener/router    频道/触发人/自消息过滤 → 原文落库 → 30s 原文去重       │
│                    → detect_action 路由                                 │
│   ├── OPEN  → open_flow                                                 │
│   │     parse_signal(EN/ZH 三族模式)→ 解析失败三级告警分诊              │
│   │     → 信号指纹去重(5min)→ 短线标签×长 DTE 笔误防护                │
│   │     → [串行锁] 四层风控 → broker 下单 → 配额记录 → 持仓登记          │
│   │     → 成交确认(fill_checker)→ Telegram                            │
│   └── CLOSE → close_flow                                                │
│         parse_close(持仓白名单消歧)→ 去重(60s,broker 失败回滚)      │
│         → 开仓翻译孪生防护 → 频道来源隔离 → strike 精确过滤              │
│         → runner-preserve → [每仓位卖出锁] 卖出 → 持仓扣减 → Telegram    │
└─────────────────────────────────────────────────────────────────────────┘
        │                        │                          │
        ▼                        ▼                          ▼
┌───────────────┐   ┌──────────────────────┐   ┌─────────────────────────┐
│ policy/(纯)  │   │ broker/              │   │ position/ watchers      │
│ pricing 定价  │   │ trade  下单/撤单/查单 │   │ sl_watcher   止损轮询   │
│ guards  防护  │   │ quote  批量快照报价   │   │ tp_watcher   分批止盈   │
│ positions策略 │   │ errors SDK 错误分类  │   │ eod_watcher  到期强平   │
└───────────────┘   └──────────────────────┘   └─────────────────────────┘
        │                        │                          │
        ▼                        ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ storage/ (sqlite: 持仓+事件账本+原始信号+订单)   risk.py (配额+熔断)      │
│ notify/  (Telegram 传输加固 + 纯格式化)   app/connection (断线遥测+回补) │
└─────────────────────────────────────────────────────────────────────────┘
```

## 目录结构

```
autotrade/
├── app/                  进程壳:唯一的组合根与生命周期
│   ├── main.py           入口(python -m autotrade.app.main):装配一切、启动顺序、优雅退出
│   ├── preflight.py      启动硬门:token/账户/broker 探测/OPRA 行情权限探测
│   └── connection.py     断线防抖、重连风暴/慢性掉线告警、重登录后信号回补
├── settings.py           config/.env 一次性加载与校验(列完所有错误再退出)
├── config/
│   └── channel_loader.py channels.json 注册表(频道→触发人/张数/价格上限)+ REST 校验
├── parsing/              文本 → 结构化信号(纯文本处理,不碰 I/O)
│   ├── signal_parser.py  开仓解析:三族模式(裸 ticker / $ticker / 中文),动作路由
│   ├── close_parser.py   平仓解析:EN/ZH 双管线,比例/strike hint/豁免名单提取
│   └── holidays.py       美股节假日与到期日调整
├── policy/               交易策略(纯函数,无 I/O)
│   ├── pricing.py        定价内核:买入分档滑点、卖出限价、保本价、期权代码构造
│   ├── guards.py         短线标签 × 长 DTE 笔误防护
│   └── positions.py      持仓分类矩阵(DTE×标签)、卖出张数策略、TP 阶梯定义
├── listener/             消息处理管线
│   ├── router.py         过滤/落库/原文去重/OPEN-CLOSE 分发
│   ├── open_flow.py      开仓编排(风控-下单-记录整段串行)
│   ├── close_flow.py     平仓编排(多重防误卖防护)
│   ├── dedup.py          全部查重/节流 registry:msg-id、原文、开/平仓指纹、告警节流
│   └── heuristics.py     "疑似信号"告警启发式:加仓/无方向入场/翻译孪生识别
├── broker/               moomoo OpenD 适配
│   ├── common.py         连接配置、SDK 加载、DRY_RUN 判定
│   ├── trade.py          下单/卖出/查单(ctx 线程锁 + 过期会话统一重试)
│   ├── quote.py          批量快照、报价新鲜度校验、限频退避、期权代码验证
│   └── errors.py         SDK 报错关键字分类的唯一来源
├── position/             持仓看护
│   ├── manager.py        持仓门面 + 每合约卖出锁
│   ├── sl_watcher.py     止损轮询(记账失败即冻结该合约止损并告警)
│   ├── tp_watcher.py     分批止盈阶梯
│   ├── eod_watcher.py    到期日收盘强平(锁内取实时价,无价拒卖)
│   └── fill_checker.py   买/卖成交确认(强引用任务,超时告警对账)
├── storage/              sqlite 持久化(显式 init,import 零副作用)
│   ├── positions_db.py   持仓状态 + 不可变事件账本 + 过期清扫
│   └── logger_db.py      原始信号与订单留底(复盘数据源)
├── risk.py               四层风控:单张价上限→单笔成本→当日累计→当日单数;熔断器
├── notify/
│   ├── transport.py      Telegram 传输:串行锁、429 重试、Markdown 降级、永不抛
│   └── messages.py       全部消息模板(纯函数)
├── ops/                  只读运维:python -m autotrade.ops.show_today 等
└── diag/                 活体诊断(会碰真服务,永不被 pytest 收集)
tests/                    425 个回归测试,几乎每条对应一次真实事故的行为规格
```

## 快速开始

```bash
make venv                                 # Python 3.11 venv + 依赖
cp config/.env.example config/.env        # 填 Discord token / moomoo / Telegram
cp config/channels.json.example config/channels.json   # 配监听频道与触发人
make test                                 # 全套回归测试
make run                                  # 启动(默认 DRY_RUN,只解析不下单)
```

前置:moomoo OpenD 网关本机运行中;Discord 账号已加入目标服务器;
Telegram bot token + chat_id。详细步骤见 [docs/SETUP.md](docs/SETUP.md)。

## 安全设计要点

- **风控四层**,按挂单价(含滑点)而非信号价计成本;真盘单笔 $1000 硬顶写死在代码里,
  熔断后当日拒单直到人工复位(`python -m autotrade.ops.reset_daily_limit`)。
- **多层去重**:msg-id、同频道原文 30s、开仓指纹 5min、平仓指纹 60s(查即登记原子防
  双语双发竞态;broker 失败自动回滚,让 1-3s 后的另一语言版本充当天然重试)。
- **宁错过不错杀**:平仓无可靠价格参照(信号未喊价且 OPRA 不可用)一律拒卖并 TG 人工接管;
  平仓信号只作用于同频道开的仓;文本给了 strike 就只平精确匹配的合约。
- **翻译孪生防护**:同频道 60s 内刚成交的合约,机翻出平仓动词的孪生消息不会触发卖出。
- **断线自愈**:重连风暴/慢性掉线分级告警;完整重登录后自动拉取频道历史回补漏掉的信号
  (msg-id 去重保证幂等,不会重复下单)。
- **进程卫生**:库代码 import 零副作用(不读 .env、不建表、不改 sys.path);
  配置在启动时一次性校验并列出全部错误;诊断脚本与测试物理隔离,不存在误触实弹路径。

## 测试

```bash
make test                                  # 默认排除 integration 标记
.venv311/bin/python -m pytest tests/ -q -m integration   # 需要真实服务的交互测试
```

当前套件:412 passed / 1 skipped / 1 xfailed(xfail 为显式锁定的已知解析缺口)。
测试即规格:dedup 窗口、频道隔离、strike 过滤、runner-preserve、配额退避等
每条防线都有对应的回归测试钉死。

## 演进计划

见 [ROADMAP.md](ROADMAP.md):金语料回放回归门、统一 SellExecutor、平仓 Outcome 枚举、
定时 broker 对账、通知策略层、实时报价参照的滑点优化等。
