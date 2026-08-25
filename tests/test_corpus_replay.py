"""[0012] 金语料回放门（golden corpus replay gate）。

背景：四夜 SIMULATE（7/22-7/28）的每一次 parser 修复都是"改一处、怕三处"——
7/24 给 STRONG_CLOSE_RE 加裸名词 close 边界时，全靠 test_overnight_* 里散落的
逐字语料才敢动刀。本文件把这些语料统一成数据驱动的回放门：

  tests/corpus/*.jsonl 每行一条**逐字**真实消息 + 已验证的期望行为，
  按夜份分文件（2026-07-22.jsonl = 7/22-23 夜，取自 test_overnight_0723 等），
  历史实锤（6/14-7/17，来自 test_parser / test_close_parser）进 lessons.jsonl。

铁律（docs/CORPUS.md 有完整工作流）：
  1. 期望值只能来自"跑过 parser 确认"或"postmortem 定案的正确行为"，禁止手猜。
  2. postmortem 修复 = 先加语料行（此时红），再改 parser（变绿）。
  3. 合法改变 parser 行为的 patch 必须在同一 patch 里更新受影响语料行——
     语料是行为契约，不是摆设。

行 schema（见 docs/CORPUS.md）：
  {"name", "text", "open_symbols": [...], "msg_date"?: "YYYY-MM-DD",
   "expect": {"detect": "OPEN|CLOSE",
              "open":  {...} | "skip" | "none" | null,
              "close": {...} | "none" | null},
   "note"?: "..."}

  - detect 永远断言（detect_action 是钱路由的第一道闸，7/24 META 事故的层级）。
  - open  仅当 detect=OPEN 断言（生产路由只在 OPEN 时调 parse_signal）；
    dict = 逐字段断言解析结果，"skip" = 主动 skip，"none" = 解析失败返回 None，
    null = 不断言（过渡态：仅用于已知会被排期内 patch 合法改变 parse 结果的行，
    路由仍锁死；该 patch 落地后必须回填完整期望——META ZH 孪生行在 0014
    同批落地后已回填，见 docs/CORPUS.md 工作流 3）。
  - close 仅当 detect=CLOSE 断言，取值语义同上。
  - msg_date：parse_signal 的 msg_ts 固定注入，缺省用 DEFAULT_MSG_DATE——
    绝不能用 date.today()，否则 weekly/NDTE/2-29 这类相对日期会随跑测日漂移
    （2/29 在闰年可解析，语料在 2028 年就会假红/假绿）。
"""
import json
from datetime import date
from pathlib import Path

import pytest

from autotrade.parsing.close_parser import parse_close
from autotrade.parsing.signal_parser import detect_action, parse_signal

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

# 周二非假日；与 test_parser.FIXED_TODAY 同思路的固定锚，行内 msg_date 优先
DEFAULT_MSG_DATE = date(2026, 7, 21)

# 语料门的最低水位——低于它说明语料被误删/加载失败，门就形同虚设
MIN_ROWS = 35

REQUIRED_KEYS = {"name", "text", "open_symbols", "expect"}


def _load_rows() -> list:
    """加载全部语料行。schema 错误在这里就近报文件+行号，
    不要等参数化阶段的 KeyError（那时定位不到是哪个 jsonl 坏了）。"""
    params = []
    seen_names = set()
    for path in sorted(CORPUS_DIR.glob("*.jsonl")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = line.strip()
            if not line:
                continue
            where = f"{path.name}:{lineno}"
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise AssertionError(f"[corpus] {where} 不是合法 JSON: {e}") from e
            # 顶层形状先判（PR#2 review）：合法 JSON 但不是 object 时，
            # 下面的 set(row) 会抛 TypeError（int/float）或把字符串拆成字符集
            # （"abc" → {'a','b','c'}），报出来的错和真实原因对不上，
            # 而且 int 那条直接逃出 assert，丢掉 [corpus] file:line 归因——
            # 本函数存在的唯一理由就是保住这个归因。
            assert isinstance(row, dict), (
                f"[corpus] {where} 顶层必须是 JSON object，实际是 "
                f"{type(row).__name__}"
            )
            missing = REQUIRED_KEYS - set(row)
            assert not missing, f"[corpus] {where} 缺字段 {missing}"
            # 喂给 parser 的两个字段也当场验型：类型错在这里报比在 40 个
            # 参数化用例里报 TypeError 好定位
            assert isinstance(row["text"], str) and row["text"].strip(), (
                f"[corpus] {where} text 必须是非空字符串"
            )
            assert isinstance(row["open_symbols"], list), (
                f"[corpus] {where} open_symbols 必须是数组，实际是 "
                f"{type(row['open_symbols']).__name__}"
            )
            # skeleton（export_corpus 产物）不许直接进门：expect 必须已人工定案
            assert isinstance(row["expect"], dict), (
                f"[corpus] {where} expect 未标注（skeleton 不能直接放进 tests/corpus/）"
            )
            assert row["expect"].get("detect") in ("OPEN", "CLOSE"), (
                f"[corpus] {where} expect.detect 必须是 OPEN|CLOSE"
            )
            assert row["name"] not in seen_names, (
                f"[corpus] {where} name 重复: {row['name']}"
            )
            seen_names.add(row["name"])
            params.append(
                pytest.param(row, id=f"{path.stem}:{row['name']}")
            )
    return params


_ROWS = _load_rows()


def test_corpus_minimum_size():
    """语料水位门：行数掉到 35 以下（误删/glob 落空）比单行失败更危险——
    整个门静默变没。"""
    assert len(_ROWS) >= MIN_ROWS, (
        f"corpus 只加载到 {len(_ROWS)} 行 (< {MIN_ROWS})，检查 tests/corpus/*.jsonl"
    )


def _msg_ts(row) -> date:
    if row.get("msg_date"):
        return date.fromisoformat(row["msg_date"])
    return DEFAULT_MSG_DATE


def _assert_open(row, expect_open):
    sig = parse_signal(row["text"], msg_ts=_msg_ts(row))
    if expect_open == "none":
        # 解析失败（listener 会走 looks-like-signal 告警路径，但绝不下单）
        assert sig is None, f"期望解析失败(None)，实际: {sig}"
        return
    if expect_open == "skip":
        # 主动 skip（holding/price-range/no-price）——非错误，不该 TG 告警
        assert isinstance(sig, dict) and sig.get("skip"), (
            f"期望主动 skip，实际: {sig}"
        )
        return
    assert isinstance(sig, dict) and not sig.get("skip"), (
        f"期望解析成功，实际: {sig}"
    )
    for key, want in expect_open.items():
        if key == "expiry_date":
            got = sig["expiry_date"].isoformat() if sig.get("expiry_date") else None
        elif key == "tags":
            # tags 抽取无顺序语义，排序后比（ZH/EN 词表命中顺序不同）
            got = sorted(sig.get("tags", []))
        else:
            got = sig.get(key)
        assert got == want, f"open.{key}: 期望 {want!r}, 实际 {got!r}"


def _assert_close(row, expect_close):
    parsed = parse_close(row["text"], set(row["open_symbols"]))
    if expect_close == "none":
        # None = 噪音/recap/白名单不命中——CLOSE 误平的代价 > 漏平，
        # 这些"必须拒绝"的行为和"必须执行"同等重要（7/10 SPY put 错平教训）
        assert parsed is None, f"期望 parse_close=None，实际: {parsed}"
        return
    assert parsed is not None, "期望 parse_close 命中，实际 None"
    for key, want in expect_close.items():
        got = parsed.get(key)
        assert got == want, f"close.{key}: 期望 {want!r}, 实际 {got!r}"


@pytest.mark.parametrize("row", _ROWS)
def test_corpus_replay(row):
    expect = row["expect"]

    # 1. 路由层：OPEN/CLOSE 判定（7/24 META "into the close" 事故 = 这一层判错，
    #    一个完全可解析的开仓信号整条丢失）
    got_detect = detect_action(row["text"])
    assert got_detect == expect["detect"], (
        f"detect: 期望 {expect['detect']}, 实际 {got_detect} | {row['text'][:80]}"
    )

    # 2. 解析层：只断言与生产路由一致的那一侧
    #    （detect=OPEN 时生产不会调 parse_close，反之亦然；
    #    corpus 不锁"永远不会被调用的路径"的行为）
    if expect["detect"] == "OPEN":
        if expect.get("open") is not None:
            _assert_open(row, expect["open"])
    else:
        if expect.get("close") is not None:
            _assert_close(row, expect["close"])


# ============================================================
# 采集端：export_corpus skeleton（生产机导出 → 人工标注 → 进门）
# ============================================================

from autotrade.ops.export_corpus import build_skeletons  # noqa: E402


def test_export_skeleton_shape_and_window():
    """skeleton 形状 + ET 日期窗口过滤。
    2026-07-28T01:30Z = ET 7/27 晚 21:30——按 ET 归夜，与 parser 回放的
    msg_ts 口径一致（backfill_history 同款换算）。"""
    rows = [
        {"msg_id": "111", "author": "KC", "content": "trimmed AMZN",
         "received_at": "2026-07-28T01:30:00Z"},
        {"msg_id": "222", "author": "KC", "content": "",
         "received_at": "2026-07-28T01:31:00Z"},          # 空文本剔除
        {"msg_id": "333", "author": "KC", "content": "SPY 745c 4DTE @ 3.15",
         "received_at": "2026-07-26T12:00:00Z"},          # 窗口外
    ]
    sk = build_skeletons(rows, date(2026, 7, 27), date(2026, 7, 27))
    assert len(sk) == 1
    row = sk[0]
    assert row["name"] == "raw_2026-07-27_111"
    assert row["text"] == "trimmed AMZN"
    assert row["open_symbols"] == []
    assert row["msg_date"] == "2026-07-27"
    assert row["expect"] is None                # 未标注——进门会被 schema 拦
    assert row["suggest"] == {"detect": "CLOSE"}


def test_export_naive_and_dirty_timestamps():
    """naive received_at 视作 UTC（修 bug 前的早期行没带 Z）；
    脏时间戳跳过该行而不是炸掉整次导出。"""
    rows = [
        {"msg_id": "1", "author": "KC", "content": "out half MSFT",
         "received_at": "2026-07-28T01:30:00"},
        {"msg_id": "2", "author": "KC", "content": "hello world",
         "received_at": "not-a-date"},
    ]
    sk = build_skeletons(rows, date(2026, 7, 27), date(2026, 7, 28))
    assert [r["name"] for r in sk] == ["raw_2026-07-27_1"]


def test_skeleton_expect_null_rejected_by_gate(tmp_path, monkeypatch):
    """防呆闭环：没标注的 skeleton 直接丢进 tests/corpus/ 必须大声失败，
    绝不能静默变成"永远通过的空断言"（那样语料门就是假的）。"""
    import tests.test_corpus_replay as mod

    (tmp_path / "skeleton.jsonl").write_text(
        json.dumps({"name": "n1", "text": "trimmed AMZN",
                    "open_symbols": [], "expect": None,
                    "suggest": {"detect": "CLOSE"}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "CORPUS_DIR", tmp_path)
    with pytest.raises(AssertionError, match="skeleton"):
        mod._load_rows()


@pytest.mark.parametrize("bad_line,kind", [
    ("42", "int"),                       # 老代码：set(42) → TypeError，逃出 assert
    ('"trimmed AMZN"', "str"),           # 老代码：set("...") 拆成字符集，报错文不对题
    ('["name", "text"]', "list"),
    ("null", "NoneType"),
])
def test_non_object_row_keeps_file_line_attribution(tmp_path, monkeypatch,
                                                    bad_line, kind):
    """合法 JSON 但不是 object → 必须仍带 [corpus] file:line 归因（PR#2 review）。

    _load_rows 存在的唯一理由就是"schema 错就近报文件+行号"；
    裸 TypeError 逃出去等于这个函数白写。
    """
    import tests.test_corpus_replay as mod

    (tmp_path / "bad.jsonl").write_text(bad_line + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CORPUS_DIR", tmp_path)
    with pytest.raises(AssertionError, match=r"bad\.jsonl:1.*JSON object"):
        mod._load_rows()


@pytest.mark.parametrize("row,pattern", [
    ({"name": "n", "text": "", "open_symbols": [],
      "expect": {"detect": "CLOSE"}}, "text"),
    ({"name": "n", "text": 123, "open_symbols": [],
      "expect": {"detect": "CLOSE"}}, "text"),
    ({"name": "n", "text": "trimmed AMZN", "open_symbols": "AMZN",
      "expect": {"detect": "CLOSE"}}, "open_symbols"),
])
def test_parser_input_fields_are_type_checked(tmp_path, monkeypatch, row, pattern):
    """喂 parser 的两个字段当场验型，别等 40 个参数化用例里抛 TypeError。"""
    import tests.test_corpus_replay as mod

    (tmp_path / "bad.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CORPUS_DIR", tmp_path)
    with pytest.raises(AssertionError, match=pattern):
        mod._load_rows()


def test_duplicate_names_rejected(tmp_path, monkeypatch):
    """name 是回放报告里的定位键，跨文件重复会让失败定位歧义。"""
    import tests.test_corpus_replay as mod

    good = {"name": "dup", "text": "trimmed AMZN", "open_symbols": ["AMZN"],
            "expect": {"detect": "CLOSE", "open": None, "close": None}}
    (tmp_path / "a.jsonl").write_text(
        json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(
        json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CORPUS_DIR", tmp_path)
    with pytest.raises(AssertionError, match="重复"):
        mod._load_rows()
