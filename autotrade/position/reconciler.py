"""[0016] 定时对账 reconciler（report-only）

杀"DB 漂移"类故障（ROADMAP P1-4）：本地 trades.db 的 OPEN/PARTIAL 仓位与
broker 真实持仓会静默脱钩，实际见过的来源：

  - OCC ITM 到期自动行权：期权仓被清、换来正股，本地一直挂 OPEN
    （lessons #14/#15，7/2 实锤 —— ops/sync_positions 就是为此而生）；
  - 强平失败后过期：7/25 夜 AVGO 415C 到期日 no-quote 强平失败，过期归零后
    本地 DB 仍 OPEN，直到下次启动 sweep 才被动发现 —— 夜里零可见性；
  - 场外手动平仓：人在 moomoo App 手平，bot 全然不知，后续 close 信号会对
    不存在的仓位挂卖单（naked-short check 拒单 + 刷 TG）；
  - 记账失败：卖单已成交但 on_close_filled 落库失败（SL 冻结那条链），
    DB qty 虚高；
  - 幽灵仓：broker 有、DB 无（手动开仓/历史残留），SL/TP/EOD 全都不保护它。

之前对账只发生在"有人想起来手动跑 ops/sync_positions"的时刻。本模块把
sync_positions 的 diff 逻辑抽成纯函数（diff_positions），套上与 watcher
同款的后台循环：启动先跑一次，之后每 RECONCILE_INTERVAL_MIN 分钟一轮。

v1（0016）只做可见性，report-only。**v2（0018，2026-08-13）落地了升级路径 1**：
对 broker 的确定性响应自动落账。触发它的是 8/13 夜的实证 —— 00:11 TP 开始
对着一张 broker 侧不存在的 MU 945C 硬打，00:27 本模块**看见并报了**
（db_only db=2 broker=0），然后按签名节流沉默，那条陈旧 OPEN 一直挂到 06:00，
1918 次拒单。可见性到位而手没伸出去，等于把止血完全押在"半夜有人看 TG"上。

现在的分工：
  - db_only（broker 明确说没有）→ **自动 record_close(fill_price=0,
    "broker_sync")**，三道闸门见 _auto_close_veto；RECONCILE_AUTO_CLOSE=0 可关。
  - qty_mismatch（升级路径 2）→ 仍然只报告。收敛 qty 会改变 SL/TP 的卖出
    张数，按契约铁律 2 属钱路行为，需单独拍板。
  - broker_only（幽灵仓）→ 仍然只报告。凭空造记录要猜入场价/category/频道。

生产机验证（本模块自身不需要 diag 脚本 —— broker 查询与人工对账脚本
共用同一 SDK 调用）：
  1. `python -m autotrade.ops.sync_positions --dry-run` 打印同一份 broker
     持仓与 diff（独立建 ctx，验证 OpenD 链路）；
  2. `.env` 设 RECONCILE_INTERVAL_MIN=1 起 listener，观察日志
     `[reconcile] OK: broker N / DB M` 与（有漂移时）TG 报告，验完调回。
"""
import asyncio
import os

from autotrade.broker.common import _is_dry_run
from autotrade.broker.trade import list_open_option_positions
from autotrade.notify.transport import send_telegram
from autotrade.notify.watchdog import notify_tick_error, notify_tick_ok
from autotrade.storage import positions_db
from autotrade.utils import logdedup
from autotrade.utils.envcfg import env_int
from autotrade.utils.logger import logger

# diff 类别（契约 WP-G 钦定两类 + qty 不一致一类，见 diff_positions docstring）
KIND_DB_ONLY = "db_only"            # DB 有 / broker 无：疑似已行权或场外平仓
KIND_BROKER_ONLY = "broker_only"    # broker 有 / DB 无：幽灵仓，bot 不会保护
KIND_QTY_MISMATCH = "qty_mismatch"  # 两边都有但张数不同：部分场外平仓/记账失败
# [9/1] broker 有 / DB **从来没有过**：手动开的、或本仓库之前就存在的外部持仓。
# 与 broker_only 的区别不是严重程度，是**成因与可行动作**：
#   broker_only（幽灵）= 我们开过它、DB 说没了、broker 说还在 → 记账脱钩，要吼；
#   foreign（外仓）    = 我们从没开过它 → 不是漂移，bot 也永远不会自动处理它
#                        （见 docstring "凭空造记录要猜入场价" 那段）。
#
# 9/1 夜的实证：NVDA 250C 9/18、SOFI 20C 2027-01、UPS 130C 2027-01 三条
# 每轮一条 WARNING 连报 49 轮 = 147 行，签名不变所以 TG 只响过一次。
# 它们全都从没进过 trades.db（LEAPS，本 bot 只打 weekly/swing），也就是说
# **这 147 行里没有一行是可行动的**，而下一次真漂移出现时长得一模一样。
# 这类噪音的代价不是磁盘，是把 broker_only 这个信号训练成了背景色。
KIND_FOREIGN = "foreign"

# 上一轮的漂移签名（进程级）。同一份漂移每轮重发 TG 就是 7/23 夜
# runner-preserve 6 连发的重演（0002 的教训：动作照常执行并留 log，
# TG 才需要节流）——这里同款处理：diff 每轮照算照 log，TG 只在漂移
# **内容变化**时重发；漂移清零会重置签名，下次再出现同样漂移会重新告警。
# 已知边界（与 runner-preserve 节流同语义）：签名在发送前登记，若那一条
# TG 恰好发送失败，同一份漂移不会重试——log 里每轮都有完整记录兜底。
_last_signature: "tuple | None" = None

# [9/11] 第四道闸门的记忆：上一轮被判为 db_only 的 code。
# 只有连续两轮都说"broker 没有"才落账，见 _split_by_confirmation。
_db_only_seen: "set[str]" = set()


# ---- 自动落账（0018，2026-08-13）----------------------------------------
# 8/13 夜实证：reconciler **看见了**（00:27 一条 db_only: MU 945C db=2 broker=0，
# storm 起于 00:11），报了一次 TG 之后按签名节流沉默，DB 里那条陈旧 OPEN 一直
# 挂到 06:00 —— TP 对着一张 broker 侧不存在的仓打了 1918 次。可见性到位、
# 手却没伸出去，这正是模块 docstring 里"升级路径 1"要解决的事。
#
# 落地范围严格限定在**确定性响应**这一类：broker 明确回答"这个 code 我没有"
# （db_only，broker_qty==0）→ record_close(fill_price=0, "broker_sync")，与
# ops/sync_positions 的写法逐字同款。
#   - qty_mismatch **不动**（升级路径 2，仍未拍板）：张数对不上可能是部分成交
#     在途，收敛 qty 会改变 SL/TP 的卖出张数，是另一个量级的决定。
#   - broker_only（幽灵仓）**不动**：DB 里没有的仓凭空造一条记录，入场价、
#     category、频道全是猜的，猜错比不写更糟。
#
# 三道闸门（任一不满足 → 退回 report-only，本轮不写库）：
#   1. RECONCILE_AUTO_CLOSE=1（默认开；出事时可以一个 env 关掉）
#   2. broker 侧期权持仓数为 0 时**绝不落账**：分不清"确实全平了"和
#      "position_list_query 返回了个空 df"，而后者会把全部活仓一次清空。
#   3. 单轮最多落账 RECONCILE_AUTO_CLOSE_MAX 条（默认 3）：超过这个数更像
#      查询侧出了问题，不像真有那么多仓同时消失。
#   4. [9/11] **同一个 code 连续两轮都是 db_only 才落账**（_split_by_confirmation）。
#      上面三道防的都是"**整个查询**坏掉"——返回空 df、一次消失一大片。9/10 夜
#      栽在它们都不覆盖的那一种：一次**良好**响应里少了一行。01:47:39 报
#      AMZN 250C db=2 broker=0 当场落账，03:47/04:47/05:47/06:47 连续四轮又报
#      它 broker_only ——仓位一直都在。代价是双份的：那 2 张当天到期却掉出了
#      SL/TP/EOD 的选仓（幽灵仓，没有任何东西再看它），同时 fill_price=0 把一笔
#      -100% 的假账写进了 position_events。
#      一次读数不足以宣告一个仓位消失。默认 60min 间隔下这条闸门要多等一轮，
#      而自动落账是记账便利、不是时效动作——一小时很便宜。
_DEFAULT_AUTO_CLOSE_MAX = 3

# 外仓日志收敛窗口：6 小时。取值只需满足"整夜出现一两次"——比任何合理的
# RECONCILE_INTERVAL_MIN 都长，又短到一整夜必留痕。不走 env：这不是策略参数。
_FOREIGN_LOG_WINDOW_SEC = 6 * 3600


def _auto_close_enabled() -> bool:
    return env_int("RECONCILE_AUTO_CLOSE", 1, minimum=0) > 0


def _auto_close_max() -> int:
    return env_int("RECONCILE_AUTO_CLOSE_MAX", _DEFAULT_AUTO_CLOSE_MAX, minimum=1)


def _auto_close_veto(diffs: list[dict], broker_rows: dict[str, int]) -> "str | None":
    """返回本轮拒绝落账的理由；None = 可以落账。纯函数，便于单测穷举闸门。"""
    if not _auto_close_enabled():
        return "RECONCILE_AUTO_CLOSE=0（已关闭自动落账）"
    if not any(int(q or 0) > 0 for q in broker_rows.values()):
        return ("broker 侧期权持仓为 0 —— 分不清真全平还是查询返回空，"
                "本轮不写库")
    stale = [d for d in diffs if d["kind"] == KIND_DB_ONLY]
    if len(stale) > _auto_close_max():
        return (f"本轮 db_only 有 {len(stale)} 条，超过上限 {_auto_close_max()} "
                f"—— 更像查询侧异常，本轮不写库")
    return None


def _split_by_confirmation(
    stale_codes: "set[str]", seen: "set[str]"
) -> "tuple[set[str], set[str]]":
    """第四道闸门：(本轮可落账的, 仅初次观测到的)。纯函数，便于单测穷举。"""
    return stale_codes & seen, stale_codes - seen


def _auto_close(diffs: list[dict], broker_rows: dict[str, int]) -> tuple[list[str], str]:
    """把确定性 db_only 落账为 CLOSED。返回 (已落账的 code 列表, 说明串)。

    单条失败不影响其余（逐条 try）：对账是止血路径，一条写不进去不该让
    另外几条也留在陈旧状态。
    """
    global _db_only_seen

    stale = [d for d in diffs if d["kind"] == KIND_DB_ONLY]
    if not stale:
        _db_only_seen = set()      # 漂移消失 → 证据清零
        return [], ""

    veto = _auto_close_veto(diffs, broker_rows)
    if veto is not None:
        # 读数本身就不可信，不能拿它当"第一次观测"存下来 —— 否则下一轮
        # 一旦放行，闸门 4 会被这次坏读数直接满足。
        _db_only_seen = set()
        logger.warning(f"[reconcile] 自动落账跳过：{veto}")
        return [], f"⚠️ 自动落账跳过：{veto}"

    codes = {d["option_code"] for d in stale}
    confirmed, first_seen = _split_by_confirmation(codes, _db_only_seen)
    _db_only_seen = codes
    if first_seen:
        logger.warning(
            f"[reconcile] db_only 首次观测，本轮不落账、等下一轮确认: "
            f"{sorted(first_seen)}")
    if not confirmed:
        return [], (
            "⏳ db_only 首次观测，等下一轮对账确认后再落账（一次读数不足以宣告"
            "仓位消失，9/10 夜 AMZN 实锤）：\n"
            + "\n".join(f"  - {c}" for c in sorted(first_seen))
        )

    closed: list[str] = []
    for d in [d for d in stale if d["option_code"] in confirmed]:
        code = d["option_code"]
        try:
            positions_db.record_close(
                # fill_price=None 而不是 0：这个仓位是"消失"了不是"卖了"，
                # 0 会被当成成交价，一笔 -100% 的假账就此进了 position_events。
                # NULL 的语义是"没有成交价"，PnL 统计据此跳过它。
                option_code=code, qty_sold=d["db_qty"], fill_price=None,
                trigger_source="broker_sync",
                note="reconcile auto-close: broker no longer has this position "
                     "(auto-exercise / expired / manual close)",
            )
            closed.append(code)
            logger.warning(
                f"[reconcile] ✅ 自动落账 CLOSED: {code} qty={d['db_qty']} "
                f"（无成交价：仓位是消失不是卖出；PnL 需人工核）")
        except Exception as e:
            logger.error(f"[reconcile] ❌ 自动落账失败 {code}: {type(e).__name__}: {e}")

    if not closed:
        return [], ""
    return closed, (
        f"✅ 已自动落账 {len(closed)} 条陈旧 OPEN 为 CLOSED（连续两轮确认，无成交价）：\n"
        + "\n".join(f"  - {c}" for c in closed)
        + "\n这些仓位从此掉出 SL/TP/EOD 选仓，止盈不会再对着空仓硬打。PnL 需人工核。"
    )


def _interval_min() -> int:
    """每次调用重读 os.environ（同 watcher 风格；非 .env 热更新，改配置需重启）。
    走 envcfg：写坏了告警一次退 0（关），不崩后台 task。0=关，故 minimum=0。"""
    return env_int("RECONCILE_INTERVAL_MIN", 0, minimum=0)


def diff_positions(db_rows: list[dict], broker_rows: dict[str, int],
                   known_codes: "set[str] | None" = None) -> list[dict]:
    """本地 DB open 仓位 vs broker 持仓的差异列表。纯函数，不做 I/O。

    diff 逻辑抽自 ops/sync_positions.sync()（那边的 stale 判定 =
    `code not in broker_positions`；脚本保持独立可跑，人工对账继续用它）。
    在其基础上补齐另一方向（broker 有 DB 无 = 幽灵仓）与 qty 不一致
    ——sync_positions 只看"在不在"，看不出"部分场外平仓/记账失败"这种
    张数级漂移，而 report-only 模式下多报一类只增加可见性、不碰钱路。

    Args:
        db_rows: positions_db.get_open_positions() 形状的 dict 列表
                 （只消费 option_code / qty_remaining 两个键）
        broker_rows: {option_code: qty}（broker 侧期权持仓；qty<=0 视为无仓）
        known_codes: 在 positions 表里**任何状态**出现过的 code 集合。
                 只用来把 broker_only 拆成"幽灵仓"与"外仓"两类
                 （见 KIND_FOREIGN）。**None = 不区分**，全部按
                 broker_only 给出——0016 以来的行为逐字不变。

    Returns:
        [{kind, option_code, db_qty, broker_qty}, ...]
        顺序：先按 db_rows 原序给出 db_only / qty_mismatch，
        再按 code 排序给出 broker_only —— 输出确定性，方便测试与 TG 稳定。
    """
    diffs: list[dict] = []
    seen_db: set[str] = set()

    for pos in db_rows:
        code = pos["option_code"]
        db_qty = int(pos.get("qty_remaining") or 0)
        if db_qty <= 0:
            # OPEN/PARTIAL 却 qty<=0 属 DB 自身矛盾（record_close 的状态机
            # 不该产生），对账层面视作"本地无仓"跳过，不掩盖也不误报。
            continue
        seen_db.add(code)
        broker_qty = int(broker_rows.get(code) or 0)
        if broker_qty <= 0:
            diffs.append({"kind": KIND_DB_ONLY, "option_code": code,
                          "db_qty": db_qty, "broker_qty": 0})
        elif broker_qty != db_qty:
            diffs.append({"kind": KIND_QTY_MISMATCH, "option_code": code,
                          "db_qty": db_qty, "broker_qty": broker_qty})

    for code in sorted(broker_rows):
        qty = int(broker_rows.get(code) or 0)
        if qty <= 0 or code in seen_db:
            continue
        kind = KIND_BROKER_ONLY
        if known_codes is not None and code not in known_codes:
            kind = KIND_FOREIGN
        diffs.append({"kind": kind, "option_code": code,
                      "db_qty": 0, "broker_qty": qty})

    return diffs


def _format_report(diffs: list[dict], auto_note: str = "") -> str:
    """漂移列表 → TG 纯文本（parse_mode=None，同 storm/churn/睡眠告警惯例，
    option_code 里的下划线/点号不用管转义）。纯函数。

    auto_note：本轮自动落账的结果（或跳过理由），由 _auto_close 给出。
    空串 = 这一轮没碰 DB。"""
    by_kind: dict[str, list[dict]] = {}
    for d in diffs:
        by_kind.setdefault(d["kind"], []).append(d)

    lines = [f"🔍 持仓对账：发现 {len(diffs)} 处漂移"]
    if KIND_DB_ONLY in by_kind:
        lines.append("• DB 有 / broker 无（疑似已行权或场外平仓，本地记账已陈旧）:")
        for d in by_kind[KIND_DB_ONLY]:
            lines.append(f"  - {d['option_code']} DB 剩 {d['db_qty']} 张")
    if KIND_BROKER_ONLY in by_kind:
        lines.append("• broker 有 / DB 无（幽灵仓，SL/TP/EOD 不会保护它）:")
        for d in by_kind[KIND_BROKER_ONLY]:
            lines.append(f"  - {d['option_code']} broker {d['broker_qty']} 张")
    if KIND_FOREIGN in by_kind:
        lines.append("• broker 有 / DB 从无此记录（外仓：非本 bot 开的，"
                     "SL/TP/EOD 不管它，对账也不会自动处理）:")
        for d in by_kind[KIND_FOREIGN]:
            lines.append(f"  - {d['option_code']} broker {d['broker_qty']} 张")
    if KIND_QTY_MISMATCH in by_kind:
        lines.append("• 张数不一致（部分场外平仓 / 记账失败）:")
        for d in by_kind[KIND_QTY_MISMATCH]:
            lines.append(
                f"  - {d['option_code']} DB {d['db_qty']} vs broker {d['broker_qty']}")
    if auto_note:
        lines.append("")
        lines.append(auto_note)
    lines.append(
        "处理：人工核对 moomoo 持仓 → "
        "python -m autotrade.ops.sync_positions（先 --dry-run）→ 重启 bot")
    return "\n".join(lines)


async def _reconcile_tick() -> list[dict]:
    """单轮对账。可独立测试。返回本轮 diff 列表（含被 TG 节流的）。"""
    global _last_signature

    if _is_dry_run():
        # DRY_RUN 的"成交"是 mock（MOCK_001），仓位只进本地 DB、永远不会
        # 出现在 broker —— 对账必然满屏假 db_only。跳过而不是刷噪音。
        logger.debug("[reconcile] DRY_RUN → skip（mock 仓位不会出现在 broker）")
        return []

    db_rows = positions_db.get_open_positions()
    # SDK 同步调用，与 watcher 取价同款 to_thread；查询失败抛异常，
    # 由 run_reconciler 主循环 catch 后下一轮再试（不 TG——OpenD 半夜抖一下
    # 每轮告警就是新的噪音源，连接类故障已有 probe/stale-session 通道兜着）。
    broker_rows = await asyncio.to_thread(list_open_option_positions)

    # broker 侧比 DB 多出来的 code 先过一遍历史表，分出"幽灵仓/外仓"（见 KIND_FOREIGN）。
    # 只查这几个 code，不是全表扫描；没有多余 code 时连 DB 都不碰。
    open_codes = {p["option_code"] for p in db_rows}
    extra = [c for c, q in broker_rows.items() if int(q or 0) > 0 and c not in open_codes]
    known = positions_db.known_option_codes(extra)

    diffs = diff_positions(db_rows, broker_rows, known_codes=known)
    if not diffs:
        # 漂移清零 → 重置签名：同样的漂移将来再出现，属于新事件要重新告警
        _last_signature = None
        # 漂移清零 → 闸门 4 的记忆同样清零（此路径不经过 _auto_close）
        globals()["_db_only_seen"] = set()
        logger.info(
            f"[reconcile] OK: broker {len(broker_rows)} / DB {len(db_rows)}，无漂移")
        return []

    # log 每轮完整记录（半夜 TG 被节流时，复盘还有日志可查）——**真漂移照旧
    # 每轮一条 WARNING，这是 0016 的有意设计，不动**。
    # 外仓是唯一的例外：它不是漂移、没有可行动作、内容永远不变（9/1 夜 147 行），
    # 逐轮 WARNING 只会把上面那条真信号淹掉。降到 INFO + 长窗口收敛，
    # 一晚仍会留痕，但不再是背景色。
    for d in diffs:
        line = (f"[reconcile] drift {d['kind']}: {d['option_code']} "
                f"db={d['db_qty']} broker={d['broker_qty']}")
        if d["kind"] == KIND_FOREIGN:
            logdedup.log_throttled(
                f"reconcile-foreign:{d['option_code']}:{d['broker_qty']}",
                line, level="INFO", window=_FOREIGN_LOG_WINDOW_SEC)
        else:
            logger.warning(line)

    # [0018] 确定性漂移自动落账。放在 TG 节流**之前**：这一轮真的动了 DB，
    # 那就是新事件，不能被"与上一轮签名相同"压掉（8/13 那一夜正是被压掉的）。
    closed, auto_note = _auto_close(diffs, broker_rows)

    signature = tuple(sorted(
        (d["kind"], d["option_code"], d["db_qty"], d["broker_qty"]) for d in diffs))
    if signature == _last_signature and not closed:
        logger.info(f"[reconcile] {len(diffs)} 处漂移与上一轮相同，TG 不重发（log 照记）")
        return diffs
    _last_signature = signature

    await send_telegram(_format_report(diffs, auto_note), parse_mode=None)
    return diffs


# interval<=0 时的空转轮询：只读 env、不碰 broker，用于探测被改回正数
_DISABLED_POLL_SEC = 60


async def run_reconciler():
    """后台主循环：启动先跑一次，之后每 RECONCILE_INTERVAL_MIN 分钟一轮。

    由 app.main 经 start_reconciler() 启动（env<=0 时根本不建 task）。
    interval 每轮重读（同 sl_watcher 风格），**0 = 暂停也算数**——见下方注释。
    """
    logger.info(
        f"[reconcile] started: interval={_interval_min()}min（report-only，不写 DB）")
    disabled_logged = False
    while True:
        interval = _interval_min()
        # 0 = 关，运行中改也生效（PR#2 review）。老写法只把 sleep clamp 到
        # max(_,1)，_reconcile_tick() 照跑——"关掉"实际变成每分钟一轮的**加速**
        # 轮询，而这一轮是要打 broker 的（get_open_option_positions）。
        # 语义上更别扭：interval 支持热重读、文档写着 0=关，偏偏 0 不生效。
        # 这里不结束 task，只空转——改回正数自动恢复，与热重读的初衷一致。
        if interval <= 0:
            if not disabled_logged:
                logger.info(
                    "[reconcile] RECONCILE_INTERVAL_MIN<=0，暂停对账"
                    "（不查 broker；改回正数自动恢复）")
                disabled_logged = True
            await asyncio.sleep(_DISABLED_POLL_SEC)
            continue
        if disabled_logged:
            logger.info(f"[reconcile] 恢复对账：interval={interval}min")
            disabled_logged = False

        try:
            await _reconcile_tick()
            notify_tick_ok("reconcile")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            notify_tick_error("reconcile", e)
        await asyncio.sleep(interval * 60)


def start_reconciler(task_set: set) -> "asyncio.Task | None":
    """组合根（app.main）调用：env>0 才建 task，并挂进调用方的强引用 set。

    与 alive_heartbeat/watcher 同款接线：asyncio 对 task 只持弱引用，
    裸 create_task 不存返回值可能被 GC 静默回收（[refactor-change] c）——
    强引用 set + done_callback discard。env<=0（缺省 "0"）= 关，不起 task，
    行为与 0016 之前逐字一致。

    Returns:
        创建的 Task；未启用返回 None。
    """
    minutes = _interval_min()
    if minutes <= 0:
        logger.info("[reconcile] RECONCILE_INTERVAL_MIN<=0 → 定时对账关闭"
                    "（缺省；生产建议 60，见 config/.env.example）")
        return None
    task = asyncio.create_task(run_reconciler(), name="reconciler")
    task_set.add(task)
    task.add_done_callback(task_set.discard)
    return task
