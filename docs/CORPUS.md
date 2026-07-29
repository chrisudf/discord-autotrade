# 金语料回放门（Golden Corpus Replay Gate）[0012]

`tests/corpus/*.jsonl` + `tests/test_corpus_replay.py` 把四夜 SIMULATE
（7/22-7/28）和更早复盘里的**逐字**真实消息固化成数据驱动的回归门。
每次改 `signal_parser` / `close_parser` 前后，这 100+ 条真实语料整体回放一遍——
"改一处、怕三处"的日子到此为止。

## 为什么存在

- 7/24 夜：给 `STRONG_CLOSE_RE` 加裸名词 close 边界（META "into the close"
  误路由事故），改动一个正则要人肉核对散落在 5 个测试文件里的历史语料。
- 7/23 夜："出半" 两轮对抗评审，每一条边界（冲出半年新高/走出半V型反转/
  出半仓于2.45）都是一条真实消息。语料不集中,下次改词表就会漏检查。
- CLOSE 误平的代价 > 漏平（7/10 SPY put 被当 755 call 平掉）——
  "必须拒绝"的语料（recap/watchlist/hold-context/白名单不命中）
  与"必须执行"的语料同等重要，回放门两类都锁。

## 文件布局

```
tests/corpus/
  2026-07-22.jsonl   7/22-23 夜（SIMULATE 首夜：止损掩码、出半、AVGO lotto）
  2026-07-23.jsonl   7/23-24 夜（META into-the-close 事故 + ZH 孪生）
  2026-07-24.jsonl   7/24-25 夜（enrich scalp 形态、Out 25% more）
  2026-07-27.jsonl   7/27-28 夜（睡眠夜的 SPY 745C 主力单）
  lessons.jsonl      6/14-7/17 历史实锤（来自 test_parser / test_close_parser）
```

按"复盘夜"分文件（与 `tests/test_overnight_MMDD.py` 同一命名口径）；
不属于某个具体夜份的老教训进 `lessons.jsonl`。

## 行 schema

```json
{"name": "唯一定位键（snake_case）",
 "text": "逐字原文（含 @everyone/emoji/换行，一个字符都不许改）",
 "open_symbols": ["当时实际持仓 symbol", "..."],
 "msg_date": "YYYY-MM-DD（可选，parse_signal 的 msg_ts）",
 "expect": {"detect": "OPEN|CLOSE",
            "open":  {"symbol":..,"strike":..,"side":..,"price":..,
                      "expiry_date":"YYYY-MM-DD","tags":[..]} | "skip" | "none" | null,
            "close": {"kind":..,"symbols":[..],"pct":..,"signal_price":..,
                      "signal_pnl_pct":..,"hint_strike":..,"hint_side":..,
                      "lang":..,"exclude_symbols":[..]} | "none" | null},
 "note": "该行对应的实战教训（可选）"}
```

语义要点：

- **detect 永远断言**——`detect_action` 是钱路由第一道闸。
- **open 仅当 detect=OPEN 断言，close 仅当 detect=CLOSE 断言**——
  与生产路由一致；corpus 不锁"永远不会被调用的路径"。
- dict 取值 = 逐字段断言（写了哪个字段断哪个，可选字段如 `hint_strike`
  只在该行的教训涉及它时才写）。
- `"skip"` = parser 主动 skip（holding/price-range/no-price，不告警）。
- `"none"` = 解析失败/拒绝返回 None（OPEN 侧走 looks-like-signal 告警，
  CLOSE 侧静默——两类都绝不下单）。
- `null` = 不断言。**只**用于已知会被排期内 patch 合法改变解析结果的行，
  作为过渡态存在（路由 detect 仍然锁死）；该 patch 落地后必须回填成
  完整期望。实例：`meta_zh_twin_machine_translation` 的 open 在 0014
  （ZH 开仓模板）落地前是 null，同批落地后已按新行为锁定。
- `msg_date` 缺省时 harness 注入固定的 `DEFAULT_MSG_DATE`（2026-07-21），
  **绝不用 date.today()**——weekly/NDTE/2-29 这类相对日期会随跑测日漂移。
  所有 OPEN 侧断言 `expiry_date` 的行必须带 `msg_date`。

## 铁律工作流

### 1. postmortem 修复 = 先加语料行，再改 parser

```
夜盘出事 → 从日志/DB 拿到逐字原文
  → export_corpus 导 skeleton（或手写一行）
  → 把 expect 写成 postmortem 定案的【正确】行为 → 此时该行【红】
  → 改 parser → 该行变绿，且其余 100+ 行必须全绿（没误伤别的夜的行为）
```

### 2. 期望值从哪来（禁止手猜）

- 新增"锁现状"的行：先跑 parser 确认实际输出，再写进 expect
  （初版语料全部按此生成——expect = 2026-07-28 基线的实际行为）。
- 新增"修 bug"的行：expect = postmortem 定案的正确行为（见工作流 1）。

### 3. 合法改变行为的 patch 必须同 patch 更新语料

语料是行为契约。已完成的实例：0014（ZH 开仓模板）让 META ZH 孪生从
"解析失败"变成"解析成功"——`2026-07-23.jsonl:
meta_zh_twin_machine_translation` 的 open 期望已随 0014 同批更新为
解析成功（与 EN 孪生指纹对齐）。改 parser 不改语料 = 回放门红 =
patch 不完整。

### 4. 已知的现状快照（不是背书）

初版语料锁的是**当前**行为，包括几处已知的口径瑕疵，日后修复时按工作流 1 走：

- `Cutting $QCOM` 在 detect 层路由 OPEN（`cutting` 只在 close_parser 的
  ACTION_VERBS 里，不在 detect_action 词表——两层词表不同步的又一例）；
- `trimmed another at 7.25 on GOOGL ... pushing near 20%` 的 pct 抓到 20
  （"near 20%" 是行情评论不是 trim 比例）；
- `30% LEFT` 行顺带产出 `signal_pnl_pct=-30`（"- 30%" 被带符号 PnL 正则命中）。

## 采集端：export_corpus（生产机）

```bash
# 只读 data/trades.db 的 raw_signals，不连 broker/Discord
python -m autotrade.ops.export_corpus --since 2026-07-27
python -m autotrade.ops.export_corpus --since 2026-07-27 --until 2026-07-28 \
    --out data/corpus_2026-07-27.jsonl
```

产出 skeleton：`expect=null` + `suggest.detect`（当前 detect_action 的机读建议）。
人工要做的事：

1. 删掉与交易无关的闲聊行（或保留有边界价值的，如"冲出半年新高"类评论）；
2. 补 `open_symbols`（当晚实际持仓——DB 没有这个历史快照，必须人补，
   CLOSE 白名单语义完全依赖它）；
3. 把 `suggest` 确认/修正后搬进 `expect`，删掉 `suggest` 字段；
4. 改个可读的 `name`，写 `note`；
5. 放进 `tests/corpus/<夜份>.jsonl`，跑
   `pytest tests/test_corpus_replay.py -q` 全绿后随 patch 交付。

防呆：`expect=null` 的 skeleton 直接丢进 `tests/corpus/` 会被 harness 的
schema 校验当场拦下（大声失败，不是静默跳过）。

## 跑法

```bash
# 只跑回放门
python -m pytest tests/test_corpus_replay.py -q
# 全套件（回放门包含在内）
python -m pytest tests/ -q
```

失败输出形如 `[2026-07-23:meta_into_the_close_open] detect: 期望 OPEN, 实际 CLOSE`
——文件名即夜份，name 即定位键。
