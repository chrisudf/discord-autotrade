# discord-autotrade 重构契约(实现 agent 必读)

OLD repo: /Users/zoez/Desktop/discord-copytrade  (branch chore/refactor, 282 tests green on py3.11)
NEW repo: /Users/zoez/Desktop/discord-autotrade  (package name: `autotrade`)

## 最高原则

1. **Move, don't rewrite**。每个函数/正则/窗口常量/哨兵值都是付过学费的 postmortem 修复,
   函数体逐字搬运,只允许改 import 语句和模块归属。注释(包括中文 postmortem 注释)必须全部保留。
2. 语句顺序即行为:raw-dedup 在 log_raw_signal 之后、fingerprint 在 parse 之后风控之前、
   addon 检查在 open-attempt 之前、_record_recent_exec 在锁内 broker 成功之后。不许"顺手整理"。
3. 已知怪异行为(B2/B3 pattern shadowing、TP round() vs manager ceil()、EN/ZH close pipeline
   不对称)**原样保留**,不修。
4. 唯一允许的行为变化(共 6 项,都要在代码注释里标 `[refactor-change]`):
   a. trade ctx 生命周期加 threading.Lock(镜像 quote 侧);
   b. stale-session retry helper 统一为一个函数,并让 query_order_status 也走它(retry 恰好一次);
   c. fill_checker.spawn / watcher task 持强引用(set + done_callback discard);
   d. _addon_alerted / _sized_entry_alerted 两个告警节流 dict 获得与其它 registry 相同的惰性 GC(有界);
   e. self-message 过滤因 client 注入而从死代码变活(bind_client);
   f. storage/risk 的 import-time _init_db()/load_dotenv() 移除,改为显式 init()(入口和 conftest 调用)。
5. 生成的代码禁止出现 sys.path.insert;禁止 import 时读 .env(load_dotenv 只在 app/main.py、
   ops/diag 脚本的 main() 里);storage 禁止 import 时建表/mkdir。
6. 不做 git 操作(不 init、不 commit)。

## 目录结构与内容映射

```
discord-autotrade/
├── pyproject.toml / pytest.ini / requirements.txt / Makefile / .gitignore / README.md  (脚手架,已建)
├── config/
│   ├── .env.example          ← 老 .env.example 补全(29 个变量全列,按模块分组注释)
│   └── channels.json.example ← 原样拷贝
├── autotrade/
│   ├── __init__.py
│   ├── settings.py           ← 新:frozen dataclass Settings + load_settings(env_path)->Settings
│   │                            字段:discord_token, dry_run, trd_env, acc_id, moomoo_host/port,
│   │                            trade_pwd(含 MOOMOO_TRADE_PWD 旧名兼容+warn), tg_token/chat_id,
│   │                            log_dir, data_dir。校验:一次性列出所有错误再退出。
│   │                            注意:watcher/risk 的 per-call os.getenv 语义本期保留,Settings 只服务 app 层。
│   ├── risk.py                ← src/risk/risk_manager.py 整体搬运。删 import-time load_dotenv;
│   │                            _init_db() 不在 import 时调用,新增显式 init()(建表)。
│   │                            模块级 MAX_* 常量、DB_PATH、所有函数名原样保留(tests patch rm.MAX_PRICE_PER_CONTRACT/rm.DB_PATH)。
│   ├── utils/
│   │   ├── logger.py          ← 暴露 loguru `logger`(import 无副作用)+ setup_logging(log_dir: Path)
│   │   │                        把老 src/utils/logger.py 的 sink 配置搬进 setup_logging,由 main/脚本调用。
│   │   └── timeutil.py        ← ET_TZ = ZoneInfo("America/New_York") + today_et()。
│   │                            各 storage 模块自己的 _utc_iso 原样保留在原模块(行为有分歧,本期不统一)。
│   ├── config/
│   │   └── channel_loader.py  ← src/config/channel_loader.py 整体搬运(registry 单例 + validate_channels 保留)。
│   │                            老 repo 根下 config/channel_loader.py 是死代码,不搬。
│   ├── parsing/
│   │   ├── signal_parser.py   ← src/parser/signal_parser.py 逐字搬运
│   │   ├── close_parser.py    ← src/parser/close_parser.py 逐字搬运
│   │   └── holidays.py        ← src/parser/holidays.py 逐字搬运
│   ├── policy/                (纯函数层:不 import broker/storage/listener/notify)
│   │   ├── pricing.py         ← 统一定价内核:broker 的 _get_slippage_pct/calc_limit_price/
│   │   │                        breakeven_exit_price/build_option_code + listener 的 SELL_SLIP/_calc_sell_limit
│   │   │                        (改名 calc_sell_limit,close_flow 引用新名)。函数体逐字。
│   │   ├── guards.py          ← listener 的 _SHORT_TAGS/_SHORT_TAG_MAX_DTE/_suspicious_long_dte
│   │   └── positions.py       ← positions_db.categorize() + manager.calc_qty_to_sell() +
│   │                            TP LADDER 与 tier-bit 常量(从 tp_watcher/positions_db 集中过来)。
│   │                            原模块通过 re-export 保持旧调用点可用(manager.calc_qty_to_sell 等)。
│   ├── listener/
│   │   ├── dedup.py           ← discord_client 六个 registry 集中:_processed_msg_ids/_processed_set/_seen、
│   │   │                        _signal_fps/_signal_fingerprint/_is_duplicate_signal/FINGERPRINT_WINDOW/_FP_MAX、
│   │   │                        _close_fps/_close_fingerprint/_is_duplicate_close/_unregister_close_fp/CLOSE_FP_WINDOW/_CLOSE_FP_MAX、
│   │   │                        _recent_raw/_is_duplicate_raw/_RAW_DEDUP_WINDOW、
│   │   │                        告警节流 _addon_alerted/_sized_entry_alerted/_ADDON_ALERT_WINDOW(变化 d:加惰性 GC)。
│   │   │                        提取一个共享 helper `_sweep_expired(reg: dict, now, window, cap=None)` 供各查重函数用,
│   │   │                        查重语义(查即登记原子性、回滚)逐字保留。所有名字保留(tests 直接引用)。
│   │   ├── heuristics.py      ← _re 正则组、_strip_bot_noise、_OPEN_*_RE、_BOT_NOISE_RE、
│   │   │                        _looks_like_open_attempt/_looks_like_sized_entry/_SIZED_ENTRY_*、
│   │   │                        _looks_like_close_attempt/_ZH_TICKER_HINTS、
│   │   │                        _looks_like_addon_attempt/_ADDON_*、
│   │   │                        twin 侦测:_recent_exec/_TWIN_SUPPRESS_WINDOW/_record_recent_exec/
│   │   │                        _twin_of_recent_exec/_close_is_open_twin。
│   │   ├── router.py          ← handle_message(crash barrier)+ _handle_message_inner 的
│   │   │                        过滤/落库/raw-dedup/detect_action 路由部分 + _extract_et_date。
│   │   │                        模块级 `client = None` + bind_client(c),self-filter 用它(变化 e)。
│   │   │                        CLOSE → close_flow.handle_close_signal;否则 open_flow.process_open。
│   │   ├── open_flow.py       ← OPEN 编排逐字:parse → parse-fail 三级 triage(addon/twin/open-attempt/
│   │   │                        sized-entry,含节流)→ skip → multi-signal 防御 → fp dedup → DTE guard →
│   │   │                        signal alert → _order_flow_lock 内 check_order/place_order/log_order/
│   │   │                        record_order → _record_recent_exec → position_mgr.on_order_filled →
│   │   │                        fill_checker.spawn → 成交通知+延迟统计。_order_flow_lock 归本模块。
│   │   └── close_flow.py      ← _handle_close_signal 逐字(660-924)。定价引用 policy.pricing.calc_sell_limit。
│   ├── broker/
│   │   ├── common.py          ← env 常量(DEFAULT_QTY/TRD_ENV_STR/OPEND_HOST/OPEND_PORT/TRADE_PWD 含旧名
│   │   │                        兼容/ACC_ID)、moomoo SDK import + SDK_AVAILABLE、_is_dry_run、_get_trd_env、
│   │   │                        _is_stale_session、QUOTE_TZ/_quote_epoch。
│   │   ├── errors.py          ← 四组关键字元组集中(byte-identical):stale-session 关键字、
│   │   │                        _is_definitely_missing 关键字、no-permission 提示词、限频提示词。
│   │   ├── trade.py           ← _ctx/_reset_ctx/_get_ctx/_ensure_account/_ensure_unlocked/place_order/
│   │   │                        _get_long_qty/place_sell_order/query_order_status/close_ctx/probe_broker。
│   │   │                        变化 a:ctx 生命周期加 threading.Lock;变化 b:提取 _call_with_session_retry
│   │   │                        并用于三处复制块 + query_order_status。calc_limit_price 从 policy.pricing import。
│   │   └── quote.py           ← _quote_ctx/_quote_lock/_get_quote_ctx/_reset_quote_ctx/_quote_backoff_until/
│   │                            _snapshot/get_last_prices/get_last_price/_no_perm_last_warn(名字保留,
│   │                            test_quote_snapshot patch 这些)/validate_option_codes/_validate_one/
│   │                            probe_quote_access + QUOTE_OK/QUOTE_DELAYED/QUOTE_NO_PERMISSION/QUOTE_ERROR/
│   │                            QUOTE_FRESHNESS_SEC。从 common import 的名字必须以裸名调用(可被 monkeypatch)。
│   ├── position/
│   │   ├── manager.py         ← 逐字;calc_qty_to_sell 改为 re-export policy.positions。
│   │   ├── sl_watcher.py      ← 逐字(per-call os.getenv 保留)
│   │   ├── tp_watcher.py      ← 逐字;LADDER 从 policy.positions import(值不变)
│   │   ├── eod_watcher.py     ← 逐字
│   │   └── fill_checker.py    ← 逐字 + 变化 c(强引用 task set)
│   ├── storage/
│   │   ├── positions_db.py    ← 逐字;去 import-time _init_db/mkdir(变化 f),DB_PATH 常量保留,
│   │   │                        categorize 移去 policy(此处 re-export 兼容)。
│   │   └── logger_db.py       ← 逐字;同上显式 init。
│   ├── notify/
│   │   ├── transport.py       ← telegram_client 的传输层:send_telegram(async+sync)、_send_lock、
│   │   │                        429 retry、400→plain fallback、escape_md、token 泄漏防护 + listener 的 _safe_notify。
│   │   └── messages.py        ← 八个 format_* 纯函数。telegram_bot.py 死代码不搬。
│   ├── app/
│   │   ├── preflight.py       ← run_listener.preflight() 逐字(返回 token)。
│   │   ├── connection.py      ← run_listener 229-429 逐字:断线防抖/storm/churn/close-code 捕获/
│   │   │                        _backfill_missed/_last_disconnect_wall/_log_reconnect_time/on_resumed 逻辑。
│   │   │                        模块级 client=None + bind(client);_install_gateway_log_capture 不在 import
│   │   │                        时调用,由 main 调(注释标注)。tests 以 `import autotrade.app.connection as rl` 使用。
│   │   └── main.py            ← 唯一组合根:load_dotenv(config/.env)一次 → setup_logging → storage/risk init →
│   │                            preflight → 建唯一 discord.Client → router.bind_client + connection.bind →
│   │                            注册 on_ready(首连:validate_channels+TG 启动通知;重连:_log_reconnect_time+
│   │                            _backfill_missed)/on_message→router.handle_message/on_message_edit/
│   │                            on_disconnect/on_resumed/on_error → shutdown 幂等(close_ctx 先于 client.close)+
│   │                            signal handlers → sweep_expired_and_notify → 三个 watcher(强引用)→ client.start。
│   │                            run_listener 的 handlers 为准(backfill/storm/churn 版本)。老 src/main.py 与
│   │                            discord_client 模块级 client/事件注册不搬。
│   ├── ops/                   ← scripts/ 里的只读运维脚本:show_today, generate_report, analyze_trades,
│   │                            sync_positions, backfill_history, backtest_parser, reset_daily_limit。
│   │                            `python -m autotrade.ops.show_today` 运行;去 sys.path hack,显式调 storage init。
│   └── diag/                  ← 活体诊断脚本改名 diag_*:moomoo_real, discord_connect, telegram, quote,
│                                option_chain, listener_flow, handle_message_real, token, quote_permission,
│                                verify_channels, find_option_code。DRY_RUN/env 改动只许在 main() 内。
├── tests/                     ← 13 个文件 + conftest 移植(import 换路径;断言/样本消息一字不改)+
│                                scripts/ 里搁浅的 assert 测试折叠为 pytest:
│                                test_holidays, test_fingerprint, test_qcom_replay, test_skip_returns_dict,
│                                test_parser_rules, test_risk_manager, test_full_flow, test_channel_loader。
│                                conftest:monkeypatch DB_PATH 后调 _init_db()(与现状一致);channels.json
│                                兜底逻辑保留(路径改新 repo)。test_backfill 的 get_event_loop() 现代化为
│                                asyncio.run(仅测试侧,标注)。tests/test_option_chain.py 交互测试加
│                                @pytest.mark.integration,pytest.ini 默认 `-m "not integration"`。
└── docs/
    ├── lessons.md             ← 移植 + 修 #4 时区条目与实际实现的矛盾(标注 [docs-fix])+
    │                            附"lesson → 回归测试"映射表。src/listener/LESSONS.md 并入。
    └── SETUP.md               ← 重写:新结构、python -m autotrade.app.main、launchd 注意事项。
```

## Import 路径映射(测试与代码统一用)

| 老 | 新 |
|---|---|
| src.parser.signal_parser | autotrade.parsing.signal_parser |
| src.parser.close_parser | autotrade.parsing.close_parser |
| src.parser.holidays | autotrade.parsing.holidays |
| src.broker.moomoo_client (quote 侧: _get_quote_ctx/_snapshot/get_last_price(s)/validate/_quote_backoff_until/SDK_AVAILABLE/_no_perm_last_warn/_is_dry_run/probe_quote_access/QUOTE_*) | autotrade.broker.quote |
| src.broker.moomoo_client (trade 侧: place_order/place_sell_order/query_order_status/close_ctx/probe_broker) | autotrade.broker.trade |
| src.broker.moomoo_client (calc_limit_price/breakeven_exit_price/build_option_code) | autotrade.policy.pricing |
| src.listener.discord_client (handle_message) | autotrade.listener.router |
| src.listener.discord_client (dedup 名) | autotrade.listener.dedup |
| src.listener.discord_client (启发式/twin 名) | autotrade.listener.heuristics |
| src.listener.discord_client (_suspicious_long_dte) | autotrade.policy.guards |
| src.listener.discord_client (_calc_sell_limit/SELL_SLIP) | autotrade.policy.pricing (calc_sell_limit) |
| scripts.run_listener (rl.*) | autotrade.app.connection(_backfill_missed/_last_disconnect_wall/client/registry)与 autotrade.app.main |
| src.config.channel_loader | autotrade.config.channel_loader |
| src.risk.risk_manager | autotrade.risk |
| src.storage.positions_db / logger_db | autotrade.storage.positions_db / logger_db |
| src.position.* | autotrade.position.* |
| src.notifier.telegram_client (send_telegram) | autotrade.notify.transport |
| src.notifier.telegram_client (format_*) | autotrade.notify.messages |
| src.utils.logger | autotrade.utils.logger |

## 验收

在 NEW repo 根:`.venv311/bin/python -m pytest tests/ -q` 全绿(≥282 项 + 折叠新增),
无 integration 标记项运行;`python -c "import autotrade.app.main"` 无副作用报错;
grep 检查:无 sys.path.insert、无 import-time load_dotenv(app/main、ops、diag 的 main() 除外)。
