# 0007 / 0008 patch 复核(2026-07-28)

> 复核对象:`0007-eod-retry-scalp-alert-out-pct.patch`(7/25 第三夜)与
> `0008-sleep-detection-backfill-age-guard.patch`(7/28 第四夜),
> 基线 `chore/refactor` @ 3b1601a(0001-0006)。
> 方法:两个 patch 打到干净基线跑通(382 passed)后**逐条驱动新代码**,
> 每个结论都在交付版快照上复现过——不是读代码读出来的怀疑。

## 一、结论

0007 三处修复经得起推敲,原样保留(只补了一个词形漏洞,见 ⑤)。
**0008 的三个新机制各带一个洞**,其中 ① 让 patch 在它自己的目标场景里失效。
全部已修,套件 392 passed(377 = 只打 0007)。

已核对无误的部分:
- eod 的 `_skip_until` 只在 `_eod_tick` 选仓位时消费,no-quote 脱钩后确实每 tick 重试;
- `send_telegram` 确实返回 bool,`ok = await send_telegram(...)` 成立;
- `startup_backfill` 挂在 `on_ready` 的首次分支之后,重连路径提前 return,不会每次重登录重放;
- scalp 启发式只在 `signal is None` 且非 addon/open-attempt 时触发,CLOSE 已被 router 先行路由走。

## 二、逐条发现

### ① 回补锚点被吞 —— 0008 的头号目标场景直接失效

`_backfill_missed` 一进函数就把 `_last_disconnect_wall` 置 None。而"刚睡醒"
正是 gateway 最可能还没活过来的时刻(`is_ready()` 为真只说明 ready 事件设过,
不代表连接还在),`history()` 逐频道抛异常被 `except` 吞掉 → 函数正常返回,
**锚点却已经没了** → 随后重登录的 `on_ready` 回补拿到 `since=None` 直接 return。

交付版复现:

```
洞1 抓取失败后 anchor = None   (应保留 23:37:04)
   → 随后重登录 on_ready 的回补: replayed=[]  ← 整段睡眠期静默丢失
```

`_on_alive_gap` 注释里写的"未就绪则保留锚,重登录后仍会重试"在交付版里不成立:
异常在 `_backfill_missed` 内部就被吞了,外层那个 `except` 根本不会触发。

**修复**:抓取失败/频道未就绪/命中条数上限 → 标记 `incomplete`,锚点不消费,
留给下一次 `on_ready`/心跳重试。成功时也只清"我们刚回补的那个锚"
(`_last_disconnect_wall == since`)——回补途中若心跳又把锚回拨得更早
(又睡了一觉),那段窗口还没回补过,不能顺手清掉。

### ② 截断丢的是最新的几条

`history(limit=50, after=X, oldest_first=True)` 是从锚点往后取**最老**的 50 条
(discord.py `abc.py`:`reverse=oldest_first` → `_after_strategy`)。
60 条窗口的复现:重放 1..50,丢掉 51-60 —— 全是最近、最该跟的。
0008 只加了告警,消息照丢。

**修复**:改成新→旧拉取再倒序重放(discord.py 的 after 谓词一过界就 break,
不会翻整个频道历史),截断掉的变成最老的;上限 50 → 200,
可配 `BACKFILL_HISTORY_LIMIT`。

### ③ `STARTUP_BACKFILL_MIN=60` 会重放上次运行已执行的 trim

`_seen` 只活在进程内存,重启即清零;CLOSE 按设计没有年龄闸门(0008 的原话:
"还持着就该平")。于是重启后 60 分钟窗口里那条**上次运行已经执行过**的
"Out 25%" 会被当新信号再 trim 一次 —— 正是 `dedup.py` 里 7/8 教训写的
"KC bot 双发穿过 dup 检查双重处理,qty≥2 时会 trim 两次"。

```
上次运行已落库 raw_signals: msg_id=1531308200920481862  action=CLOSE
重启后 _seen(1531308200920481862) = False   ← False = 当成新消息放行
```

**修复**:用已有的持久化当水位线。`raw_signals.msg_id` 是 PRIMARY KEY 且在
handler 早期(过滤之后、路由之前)就写,新增 `logger_db.processed_msg_ids_since()`,
回补时跳过上次运行已落库的 msg_id。注意 msg_id 列是 TEXT(写入侧 `str(msg_id)`),
比较必须用字符串。

代价:若上次运行"落库后、执行前"崩溃,这条会被跳过。安全方向优先——
少跟一次好过多平一次,且日志留痕。

### ④ 年龄闸门排在指纹去重之后,会静音真实信号

`_is_duplicate_signal` 是"查即登记"。一条 17 分钟前的重放先把指纹登记上,
等于给这个合约上了 5 分钟静音闸;KC 随后**实时**重喊同一张
(`dedup.py` 记的 NNE 实测 8s 重发)就被当孪生静默丢掉——没下单、没告警。

```
回补重放(17min 前): placed=[]  alerts=1  指纹表=['SPY|CALL|745.0|2026-07-31']
KC 实时重喊同一张:   placed=[]  ← 期望 ['SPY']
```

**修复**:闸门移到指纹去重**之前**,陈旧信号直接告警返回、不碰指纹表。
双语孪生的重复告警改用新增的 `dedup.stale_open_should_alert()` 按 symbol
5min 节流(与 `_sized_entry_alerted` 同语义,独立 registry,
免得"没下单的告警"顶掉真实入场告警的节流位)。

### ⑤ 0007 的解说排除漏了词形

`(?<!knocked\s)(?<!knock\s)` 只排了两个词形。
`"IV crush knocking out 25% of the premium on $LLY"` 照样路由成 CLOSE
且解析出 pct=25 —— 持着 LLY 时就是一次凭空 trim。同形状的还有
`"shaking out 20% of weak hands"`(0007 之前不命中,之后命中)。

**修复**:补 `knocking/knocks/shaking/shook/shaken/shakes`。
刻意**不**做 `(?<![a-z]ing\s)` 一刀切——`scaling out 50%` / `taking out 30%`
是真 trim,会被误砍。

顺带把 `out N%` 收成 `close_parser._OUT_PCT_PATTERN` 单一常量、
signal_parser import 同一份:原版在两个文件各写一份字面量,
漂移就是"路由成 CLOSE 但 parse_close 返 None"的漏单裂缝
(`test_out_pct_pattern_is_single_sourced` 钉住)。

## 三、看过但**没改**的

- **scalp 启发式对 recap 的误报**:`"great scalp on $SPY, banked $500"`
  会触发一条节流 TG。0007 已明确权衡过"一条节流 TG 的代价换不漏",
  不覆盖这个判断,进语料库观察。
- **`"$SPY up 25% out of nowhere"` 路由成 CLOSE**:0007 之前就是这样
  (`out of` 在老词表里),非本次回归,无持仓时 `parse_close` 返 None 无害。
- **eod broker 异常路径每次失败都发 TG**:60s `_skip_until` 下最多 10 条/窗口,
  属既有行为,未动。

## 四、回归覆盖

新增 15 条(`test_overnight_0725.py` +4,`test_overnight_0728.py` +7,
其余为既有文件内的边界补充):

| 发现 | 回归测试 |
|---|---|
| ① 锚点被吞 | `test_overnight_0728.py::test_backfill_keeps_anchor_when_fetch_fails`、`::test_backfill_consumes_anchor_on_success`、`::test_backfill_keeps_anchor_moved_by_a_second_sleep` |
| ② 截断方向 | `::test_backfill_truncation_drops_oldest_not_newest`;`test_backfill.py::_FakeChannel` 改为如实模拟 `oldest_first`/`limit`(老 fake 无视这两个参数,正好掩盖了这个缺陷) |
| ③ 跨进程重放 | `::test_backfill_skips_messages_already_processed_last_run` |
| ④ 指纹污染 | `::test_stale_open_does_not_mute_live_resend`、`::test_stale_open_bilingual_twins_alert_once` |
| ⑤ 词形漏洞 | `test_overnight_0725.py::test_out_pct_commentary_all_verb_forms`、`::test_out_pct_real_trims_survive_the_commentary_guard`、`::test_out_pct_pattern_is_single_sourced` |

**392 passed / 1 skipped / 12 deselected / 1 xfailed**(377 = 只打 0007)。
两阶段各自独立验证,并在 HEAD 的干净 clone 上按顺序 apply → 结果树逐字节一致 → 跑绿。

## 五、治本那条没变

以上全是代码侧兜底。四夜"节拍器掉线"的根因是 macOS 系统睡眠,
物理层的洞只能物理层补:

```bash
caffeinate -is make run
```

`.env` 需要的三个键:

```
STARTUP_BACKFILL_MIN=60
OPEN_SIGNAL_MAX_AGE_SEC=300
BACKFILL_HISTORY_LIMIT=200
```
