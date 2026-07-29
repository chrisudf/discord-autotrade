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
同款的后台循环：启动先跑一次，之后每 RECONCILE_INTERVAL_MIN 分钟一轮，
发现漂移只发 TG —— **report-only，绝不写 DB**。

v1 只做可见性。升级路径（显式推迟的设计决策，需单独拍板后另开 WP）：
  1. 对 broker 的**确定性响应**自动落账（broker 明确回答"没有该仓" →
     record_close(fill_price=0, trigger_source="broker_sync")，即把
     ops/sync_positions 的写库分支搬进来）；
  2. qty 不一致时自动收敛 qty_remaining。
  两者都会改变 close 白名单和 watcher 选仓 —— 影响"是否下单"，按契约
  铁律 2 属钱路行为，v1 一概不做，只报告。

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
from autotrade.storage import positions_db
from autotrade.utils.envcfg import env_int
from autotrade.utils.logger import logger

# diff 类别（契约 WP-G 钦定两类 + qty 不一致一类，见 diff_positions docstring）
KIND_DB_ONLY = "db_only"            # DB 有 / broker 无：疑似已行权或场外平仓
KIND_BROKER_ONLY = "broker_only"    # broker 有 / DB 无：幽灵仓，bot 不会保护
KIND_QTY_MISMATCH = "qty_mismatch"  # 两边都有但张数不同：部分场外平仓/记账失败

# 上一轮的漂移签名（进程级）。同一份漂移每轮重发 TG 就是 7/23 夜
# runner-preserve 6 连发的重演（0002 的教训：动作照常执行并留 log，
# TG 才需要节流）——这里同款处理：diff 每轮照算照 log，TG 只在漂移
# **内容变化**时重发；漂移清零会重置签名，下次再出现同样漂移会重新告警。
# 已知边界（与 runner-preserve 节流同语义）：签名在发送前登记，若那一条
# TG 恰好发送失败，同一份漂移不会重试——log 里每轮都有完整记录兜底。
_last_signature: "tuple | None" = None


def _interval_min() -> int:
    """每次调用重读 os.environ（同 watcher 风格；非 .env 热更新，改配置需重启）。
    走 envcfg：写坏了告警一次退 0（关），不崩后台 task。0=关，故 minimum=0。"""
    return env_int("RECONCILE_INTERVAL_MIN", 0, minimum=0)


def diff_positions(db_rows: list[dict], broker_rows: dict[str, int]) -> list[dict]:
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
        diffs.append({"kind": KIND_BROKER_ONLY, "option_code": code,
                      "db_qty": 0, "broker_qty": qty})

    return diffs


def _format_report(diffs: list[dict]) -> str:
    """漂移列表 → TG 纯文本（parse_mode=None，同 storm/churn/睡眠告警惯例，
    option_code 里的下划线/点号不用管转义）。纯函数。"""
    by_kind: dict[str, list[dict]] = {}
    for d in diffs:
        by_kind.setdefault(d["kind"], []).append(d)

    lines = [f"🔍 持仓对账：发现 {len(diffs)} 处漂移（report-only，DB 未改）"]
    if KIND_DB_ONLY in by_kind:
        lines.append("• DB 有 / broker 无（疑似已行权或场外平仓，本地记账已陈旧）:")
        for d in by_kind[KIND_DB_ONLY]:
            lines.append(f"  - {d['option_code']} DB 剩 {d['db_qty']} 张")
    if KIND_BROKER_ONLY in by_kind:
        lines.append("• broker 有 / DB 无（幽灵仓，SL/TP/EOD 不会保护它）:")
        for d in by_kind[KIND_BROKER_ONLY]:
            lines.append(f"  - {d['option_code']} broker {d['broker_qty']} 张")
    if KIND_QTY_MISMATCH in by_kind:
        lines.append("• 张数不一致（部分场外平仓 / 记账失败）:")
        for d in by_kind[KIND_QTY_MISMATCH]:
            lines.append(
                f"  - {d['option_code']} DB {d['db_qty']} vs broker {d['broker_qty']}")
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

    diffs = diff_positions(db_rows, broker_rows)
    if not diffs:
        # 漂移清零 → 重置签名：同样的漂移将来再出现，属于新事件要重新告警
        _last_signature = None
        logger.info(
            f"[reconcile] OK: broker {len(broker_rows)} / DB {len(db_rows)}，无漂移")
        return []

    # log 每轮完整记录（半夜 TG 被节流时，复盘还有日志可查）
    for d in diffs:
        logger.warning(
            f"[reconcile] drift {d['kind']}: {d['option_code']} "
            f"db={d['db_qty']} broker={d['broker_qty']}")

    signature = tuple(sorted(
        (d["kind"], d["option_code"], d["db_qty"], d["broker_qty"]) for d in diffs))
    if signature == _last_signature:
        logger.info(f"[reconcile] {len(diffs)} 处漂移与上一轮相同，TG 不重发（log 照记）")
        return diffs
    _last_signature = signature

    await send_telegram(_format_report(diffs), parse_mode=None)
    return diffs


async def run_reconciler():
    """后台主循环：启动先跑一次，之后每 RECONCILE_INTERVAL_MIN 分钟一轮。

    由 app.main 经 start_reconciler() 启动（env<=0 时根本不建 task）。
    """
    logger.info(
        f"[reconcile] started: interval={_interval_min()}min（report-only，不写 DB）")
    while True:
        try:
            await _reconcile_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[reconcile] tick error (continuing)")
        # 实时重读 interval（同 sl_watcher 风格）；防御 max(_,1)：运行中被改成
        # <=0 也不 busy-loop（正常路径 env<=0 时 task 压根不存在）
        await asyncio.sleep(max(_interval_min(), 1) * 60)


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
