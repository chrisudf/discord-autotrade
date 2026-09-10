# Lessons learned

Hard-to-find gotchas discovered the painful way during this refactor.
Filed here so future-me (or anyone else) doesn't re-spend hours on the
same surprise. Each entry: **what was wrong**, **how it looked**, **why
the lesson is non-obvious**, **how we now defend against it**.

> Ported 2026-07-22 from discord-copytrade `docs/lessons.md` +
> `src/listener/LESSONS.md`（后者并入下方「中文 postmortem 记录」一节）。
> 文件引用已改为新 `autotrade.*` 布局；正文内容原样保留，唯一的实质性
> 修订是 #4（标 `[docs-fix]`）。文末附 lesson → 回归测试映射表。

---

## 1. moomoo SIMULATE does **not** accept `unlock_trade`

**Symptom**: All overnight signals fail at order submission with
`unlock_trade failed: ERROR. No one available account!` even though
`get_acc_list` shows the account as ACTIVE.

**Trigger condition**: `MOOMOO_TRD_ENV=SIMULATE` AND `MOOMOO_TRD_PWD`
is non-empty. Was latent for months because `MOOMOO_TRD_PWD` was unset
in .env, so the old `if not TRADE_PWD: skip unlock` branch saved us.
The bug appeared the moment a real-trade password got configured.

**Why non-obvious**:
- The error message says "No one available account" — pointing to
  account state, not to "you shouldn't be calling unlock_trade".
- get_acc_list shows the account fine. The accounts are there;
  unlock_trade just doesn't apply to them in SIMULATE.
- moomoo docs do not warn about this.

**Defense**: `_ensure_unlocked` now always skips unlock in SIMULATE,
regardless of password. REAL mode additionally fails fast if
TRADE_PWD is missing rather than calling unlock_trade with `""`.
See [autotrade/broker/trade.py](../autotrade/broker/trade.py)
`_ensure_unlocked()` + `probe_broker()` startup check.

---

## 2. moomoo SDK `get_market_snapshot` poisons whole batch on one bad ticker

**Symptom**: Calling `get_market_snapshot(["US.HOOD...", "US.TEM..."])`
returns `(-1, "Unknown stock. TEM260717C065000")` — the whole call
fails, and we never see HOOD's data even though HOOD is valid.

**Why non-obvious**:
- SDK docs imply batch is just "snapshot for multiple codes".
- The natural mental model is "valid codes get data, invalid ones
  return None or empty rows" (like most REST batch APIs).
- Reality: the SDK treats the input as a single conceptual query;
  any unknown code is fatal for the whole call.

**Defense**: `validate_option_codes` first tries batch; on failure it
falls back to per-code snapshot, isolating bad codes so good ones
still get validated. Adds N RTTs only when batch failed — fast path
stays cheap. See `validate_option_codes` in
[autotrade/broker/quote.py](../autotrade/broker/quote.py).

---

## 3. moomoo OPRA permission gates **more** APIs than just snapshot

**Symptom**: `probe_quote_access` step 4 (option snapshot) was
expected to fail without OPRA subscription, but **step 3
(get_option_chain) already fails** with the same "No permission"
error. The probe then misclassifies the failure as `QUOTE_ERROR`
instead of `QUOTE_NO_PERMISSION`.

**Why non-obvious**:
- `get_option_chain` reads listing metadata (which contracts exist),
  not real-time quotes. We assumed it would work for free.
- moomoo's permission system bundles "anything that touches options
  data" together, even discovery queries.

**Defense**: `probe_quote_access` now matches "no permission" on
**every** OPRA-adjacent API call (chain, snapshot, etc.) and routes
them all to `QUOTE_NO_PERMISSION` instead of `QUOTE_ERROR`. So the
operator gets the right "subscribe Lv1" message instead of a confusing
"chain fetch failed" error. See
[autotrade/broker/quote.py](../autotrade/broker/quote.py); the shared
no-permission keyword tuple lives in
[autotrade/broker/errors.py](../autotrade/broker/errors.py).

---

## 4. `[docs-fix]` moomoo `update_time` is **naive ET**, and pandas parses naive strings as UTC — the two combined silently mark every live quote stale

*(Rewritten 2026-07-22 to match shipped behavior. The original entry
ended with "for now we trust pandas' UTC assumption. If moomoo turns
out to send ET timestamps, that TODO becomes a real bug." — moomoo
**does** send ET timestamps; that TODO became a real bug and has since
been fixed in code. The old Defense paragraph no longer described what
ships.)*

**Symptom (original discovery)**: Stale-quote filter test failed by
~10h. Mock fixture constructed `update_time` from `pd.Timestamp.now()`
(local AEST), broker code parsed it back with
`pd.to_datetime(...).timestamp()` (assumes UTC) and got a value hours
off, so a "5min old" timestamp looked like many hours old.

**Symptom (the real bug the TODO warned about)**: moomoo snapshot's
`update_time` is a **timezone-naive US-Eastern** time string. Parsing
it with pandas' UTC assumption yields an epoch 4-5h *earlier* than
reality, so the 60s freshness check
(`QUOTE_FRESHNESS_SEC`) classifies **every** real-time quote as stale
and drops it — SL/TP/EOD watchers silently never get a price and
no-op forever.

**Why non-obvious**:
- Python's stdlib: `datetime.fromisoformat(...).timestamp()` on a
  naive datetime assumes local-zone.
- pandas: `pd.to_datetime(naive_str).timestamp()` assumes **UTC**.
- They look identical at the call site, behave 5-10 hours different.
- Nothing crashes: the quotes arrive, parse fine, and are quietly
  discarded by a sanity filter. The failure reads as "no data",
  not "wrong timezone".

**Shipped defense** (was a TODO in the original entry, now real code):
- `QUOTE_TZ = ZoneInfo(os.getenv("MOOMOO_QUOTE_TZ", "America/New_York"))`
  in [autotrade/broker/common.py](../autotrade/broker/common.py) — the
  quote timestamp zone is pinned to ET by default, overridable by env
  if a future OpenD build turns out to send something else.
- `_quote_epoch(update_time)` (same module) is the **only** path from
  `update_time` to epoch seconds: `pd.to_datetime(...)`, then
  `tz_localize(QUOTE_TZ)` if naive, then `.timestamp()`. The freshness
  filter in [autotrade/broker/quote.py](../autotrade/broker/quote.py)
  (`_snapshot`/`get_last_prices`) and `probe_quote_access`'s delayed-data
  detection both go through it.
- Test fixtures build `update_time` from
  `pd.Timestamp.now(tz=QUOTE_TZ).tz_localize(None)` — i.e. naive ET,
  exactly what production receives. Regression:
  `tests/test_quote_snapshot.py::test_get_last_prices_et_realtime_not_stale`
  (a live ET timestamp must NOT be filtered as stale).

**Related but separate — the message-date side is and was correct**:
Discord `message.created_at` is a **UTC-aware** datetime (discord.py
guarantees it). `_extract_et_date(message)` in
[autotrade/listener/router.py](../autotrade/listener/router.py)
converts it to an ET calendar date (with a defensive fallback to
`datetime.now(timezone.utc)` when a test FakeMessage has no
`created_at`, and a `replace(tzinfo=utc)` guard should a naive value
ever appear), and the result is passed as
`parse_signal(raw, msg_ts=msg_date_et)`. Inside the parser,
`today = msg_ts or date.today()` — the local-date fallback only fires
when no message timestamp is supplied. Don't "simplify" either side:
the quote path is naive-ET, the message path is aware-UTC→ET; they are
different problems that merely rhyme.

---

## 5. `MOOMOO_TRD_ENV` env-var matching is case-sensitive in Python

**Symptom**: User wrote `MOOMOO_TRD_ENV=simulate` (lowercase) in .env.
Code compared `TRD_ENV_STR == "SIMULATE"` (uppercase) → fell into the
REAL branch → tried `unlock_trade` → see lesson #1.

**Why non-obvious**: most config files / env vars in the wild are
case-insensitive by convention (e.g., shell flags). Python `==` isn't.

**Defense**: At module load, normalize: `TRD_ENV_STR =
os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()`
([autotrade/broker/common.py](../autotrade/broker/common.py)). Same
trick applied wherever the value is read at runtime
(`_effective_max_cost_per_order` in
[autotrade/risk.py](../autotrade/risk.py) etc.).

---

## 6. `discord.py-self` pairs `on_disconnect` calls (`WS + HTTP`)

**Symptom**: Overnight logs showed 14-17 `Discord on_disconnect fired`
warnings per night — much higher than expected for a stable network.
Closer look: they always came in pairs <1s apart, then one reconnect.

**Why non-obvious**: The pairs are not 14 *disconnect events* — they're
7 *underlying drops* each surfacing twice in the callback (once for
WebSocket close, once for HTTP session close). The pair structure isn't
mentioned in discord.py-self docs and looks alarming.

**Defense**: `on_disconnect` in
[autotrade/app/connection.py](../autotrade/app/connection.py)
now debounces with a 3s window — pair becomes one log line. Plus we
log `reconnect after Xms` from `on_resumed` / `on_ready` so the actual
recovery time is visible.

---

## 7. Mac WiFi power-saving silently drops idle TCP, breaks Discord gateway

**Symptom**: Even after fixing the pair-debounce, base rate was still
~5-17 disconnects per night. Discord library logged close reason as
`None` with "Connection closed unexpectedly by server (EOF)" — i.e.
no close frame, just TCP EOF. Pattern was strikingly regular
(~15-20 min intervals).

**Why non-obvious**:
- The disconnect *looks* like a Discord-side problem (the library
  receives EOF on the socket).
- Actually it's macOS putting the WiFi NIC to sleep when "idle",
  which kills the heartbeat keepalive Discord expects.
- No code-level fix on either Discord's or our side resolves this;
  it's an OS power-management decision.

**Defense**: Run the bot under `caffeinate -i python ...`. This single
shell wrapper reduced overnight reconnects from 17 → 4 (with the
remaining 4 being clean session-resume in <1s). Documented in
[docs/SETUP.md](SETUP.md) under "Running unattended".

---

## 8. `asyncio.run(coro)` cannot be called twice in one process if `coro` uses module-level async state

**Symptom**: `send_telegram_sync` was implemented as
`asyncio.run(send_telegram(...))`. First call worked; second call
raised `RuntimeError: ... bound to a different event loop` referring
to the module-level `asyncio.Lock` and `httpx.AsyncClient` inside
`send_telegram`.

**Why non-obvious**:
- `asyncio.run` creates a fresh event loop each call and closes it.
- Module-level async primitives are bound to the first loop they
  touch. Subsequent loops can't reuse them.
- The error message points at "different event loop" which sounds
  like a misuse of asyncio, not a "your sync wrapper is wrong" hint.

**Defense**: `send_telegram_sync` now uses synchronous `httpx.Client`
directly, no async primitives at all. Sync and async paths are fully
isolated — no cross-loop state sharing possible. See
[autotrade/notify/transport.py](../autotrade/notify/transport.py).

---

## 9. `sqlite3.Row` does not implement `.get()`

**Symptom**: After refactoring `positions_db.open_or_add` to detect
reopen vs add-on, code that read `existing.get("status", "")` blew up
with `AttributeError: 'sqlite3.Row' object has no attribute 'get'`.

**Why non-obvious**: `sqlite3.Row` exposes `__getitem__` (so
`row["col"]` works) and behaves like a dict in many places, but it's
not actually a dict and lacks `.get()`. Static type hints often say
`Mapping` and IDEs autocomplete `.get`.

**Defense**: Inside DB access functions, use `row["col"]` (raises
KeyError if column missing — that's actually what you want for a
schema-managed table). Or convert with `dict(row)` if you really want
`.get()` semantics. Lesson: don't trust autocomplete on `sqlite3.Row`.
See [autotrade/storage/positions_db.py](../autotrade/storage/positions_db.py).

---

## 10. `discord.py-self` channel-state cache is empty at `on_ready`

**Symptom**: Channel-ID validation in `on_ready` used
`client.get_channel(cid)` and logged "NOT visible" for valid IDs at
startup. Same IDs worked fine 30s later when messages arrived.

**Why non-obvious**: `get_channel` is a **cache lookup**, not a fetch.
On bot startup with discord.py-self, the cache is populated lazily as
guild events arrive — not at the moment `on_ready` fires. Returns
`None` for both "invalid ID" and "not yet cached", which the caller
cannot distinguish.

**Defense**: `validate_channels()` now uses `await
client.fetch_channel(cid)` (REST call) which returns the channel
object or raises `discord.NotFound` / `discord.Forbidden` —
unambiguous result, properly distinguishes the two failure modes.
See [autotrade/config/channel_loader.py](../autotrade/config/channel_loader.py).

---

## 11. CLOSE matching on symbol alone can wrong-close a multi-strike position

**Symptom**: 6/30 overnight, enrich posted `$TSLA 7/1 $425 calls for $1.55` —
we opened TSLA 425c. KC (independently, on his own pre-existing position)
posted `all out TSLA 420c runner @ 15.35` ~3 hours later. Our close parser
extracted `symbols=['TSLA']`, the listener applied that to all open TSLA
positions, and we closed our 425c at $14.58. The trade was lucky (420c and
425c had near-identical ITM intrinsic on expiry day) — we netted ~+770%.

**Why non-obvious**:
- The close parser was designed with the explicit comment "almost all
  close signals don't carry strike → symbol-level match". For most KC
  trims that's still true.
- The danger only surfaces when two unrelated signals on the same symbol
  (different strike or even side) exist simultaneously. Easy to miss in
  unit tests because each test sets up a single position.
- The reward (we made money) hides the wrongness of the mechanism.
- Next time the same pattern could close a winning position when KC
  was closing a losing one, or close our call when KC closes a put.

**Defense**: `_extract_strike_hint` in
[autotrade/parsing/close_parser.py](../autotrade/parsing/close_parser.py)
now extracts an optional `(strike, side)` when the close text contains
an explicit strike like `TSLA 420c` or `AMZN 255 calls`. The listener
filters positions by `(strike, side)` whenever the hint is set; when no
hint is present (most casual trims), behavior is unchanged. If hint is
set and no position matches, the close is **skipped** and a TG warning
fires. See `_handle_close_signal` in
[autotrade/listener/close_flow.py](../autotrade/listener/close_flow.py).

Tests in [tests/test_listener_close.py](../tests/test_listener_close.py):
strike mismatch → skip, strike match → execute, no hint → legacy
behavior.

---

## 12. `discord.py-self` IDENTIFY rate-limit produces ~7-minute outages with exponential backoff

**Symptom**: 6/30 04:36 — single `on_disconnect`, followed by
`Attempting reconnect in 1.90s`, then 0.29s, 6.23s, 14.72s, 3.92s,
32.68s, 82.51s, 117.86s, 138.85s. Each retry triggered another
`on_disconnect` callback. After ~7 minutes total, full
`Discord logged in as ...` (fresh login, not session resume). During
the 7-min window the bot was completely offline — any KC signal arriving
then would be missed.

**Why non-obvious**:
- Looks like our bot is broken (10+ rapid disconnects in 7 minutes).
- Actually it's Discord's IDENTIFY rate-limit: too many connect
  attempts in a short window trigger increasing backoff (`Retry-After`
  on the IDENTIFY response, library obeys it).
- Underlying cause is usually a single brief network blip or Mac
  partial wake, but the visible symptom looks catastrophic.
- `caffeinate -i` reduced normal-state reconnects from 17/night to 4-5,
  but it doesn't prevent the occasional storm — Discord's rate-limit
  kicks in regardless.

**Defense**: Storm detector in
[autotrade/app/connection.py](../autotrade/app/connection.py)
`on_disconnect`: 60s sliding window, threshold 3 disconnects → loud
`logger.error` + Telegram alert (with 5-min cooldown to prevent
spam during the storm itself). The bot stays running; the operator
gets a heads-up that we're in a degraded window and may need to
manually restart. Documented `1006 / EOF` close codes already get
captured by `_DiscordGatewayLogCapture` so the storm log line carries
context.

Open question for future: when the storm hits, should we auto-restart
the process (forfeit any in-flight state but reset the rate-limit
clock)? Currently no — restart loses the SQLite dedup state for a few
seconds and could double-execute a close signal that arrived mid-restart.
Leaving as manual decision until we see this fail in a way the alert
doesn't catch.

---

## 13. `math.ceil` on 1-contract positions turns every trim into a full close

**Symptom**: When we hold exactly 1 contract and KC posts a `trimmed X%`
signal, `calc_qty_to_sell` computes `max(1, math.ceil(1 * pct / 100))` =
1 for any pct in `[1, 100]`. So a 33% trim intended to preserve most of
the position closes the whole thing. Then KC's actual runner (the
remaining part *KC* is holding) keeps climbing and we miss it.

Concrete misses on record:
- 6/30 SPY 748c: opened @ $2.59, 33% trim signal → we sold @ $2.71.
  KC's runner went to $4.00 (+40%). Missed ~$130/contract.
- 7/1 MSFT 390c: opened @ $2.48, 33% trim signal → we sold @ $2.56.
  KC's runner went to $4.60 (+85%). Missed ~$200/contract.

**Why non-obvious**:
- The old comment on `calc_qty_to_sell` said "trim 33% but only 1 left
  → sell 1 is more reasonable than keeping". That intuition breaks
  down when the signal source (KC) is also holding partial runners:
  the "trim" *means* "sell some, keep the rest for higher"—not "close
  because it might drop".
- Numerically the calculation is correct (`ceil(0.33) = 1`), so no
  test failure signals the problem.
- Small P&L (+$8, +$3) makes it look like the system worked, hiding
  the massive opportunity cost.
- Two overnights before the pattern became obvious enough to name.

**Defense (strategy A, 2026-07-02)**: `calc_qty_to_sell` now returns
`0` when `remaining == 1 and pct < 100`, so trim signals on
single-contract positions are ignored while the position rides. The
100% path is untouched — explicit `closed all` / `out full` / KC
clearly finishing still gets executed. See
[autotrade/policy/positions.py](../autotrade/policy/positions.py)
(function moved there in the refactor;
[autotrade/position/manager.py](../autotrade/position/manager.py)
re-exports it) and tests in
[tests/test_positions.py](../tests/test_positions.py)
(`test_calc_qty_to_sell_single_contract_runner_preserve`).

**Strategy B, blocked on OPRA subscription**: The right answer is
quote-aware — early in a trade (say < +50% PnL) match KC's trims for
risk management, then transition to runner-hold after we've de-risked.
Requires `get_last_prices` to actually return prices, which requires
US MarketOptions Lv1 subscription. Filed as P0 ("Runner mode B") in the
old repo's TODO. Until then, strategy A holds.

**Trade-off strategy A carries**: If KC's early trim signal (e.g., at
+9%) is a genuine reversal warning, we now hold instead of exiting.
Accepting this asymmetry deliberately: past data shows KC's early
trims are usually profit-taking, not reversal calls; the reversal
calls are worded as `closed`, `out full`, `stop hit` (which parse as
100%). If evidence changes, drop strategy A back to the old ceil
behavior.

---

## 14. Long ITM options auto-exercise into shares at expiry (OCC/moomoo standard behavior)

**Symptom**: 7/2 → 7/3 morning, moomoo SIMULATE 持仓 showed HOOD 100 shares
($10,800 cost), IBM 200 shares ($57,000), TSLA 100 shares ($42,500), plus
some option positions we didn't recognize. Local DB still marked HOOD 108c,
IBM 285c ×2, TSLA 425c, RKLB 108c etc as `status='OPEN'`. RKLB 108c that had
just been bought a few hours earlier was gone from broker but present in local
DB.

`history_order_list_query` showed a burst of auto-generated orders at 20:40
UTC (post-market close ET) with pattern:
```
20:40:04 US.HOOD BUY 100 @ 108     dealt=0 status=N/A
20:40:03 US.HOOD260702C108000 SELL qty=1 dealt=0 status=N/A
20:40:02 US.IBM  BUY 200 @ 285     dealt=0 status=N/A
20:40:01 US.IBM260702C285000 SELL qty=2 dealt=0 status=N/A
20:40:00 US.TSLA BUY 100 @ 425     dealt=0 status=N/A
20:40:00 US.TSLA260701C425000 SELL qty=1 dealt=0 status=N/A
```

Every expired ITM long call generated a synthetic SELL (of the option) +
BUY (of underlying × strike × 100). Some assignments succeeded earlier
(HOOD/IBM/TSLA shares are actually held), others left the account with
just cash movement.

**Why non-obvious**:
- This is standard OCC/exchange behavior, not moomoo-specific. Any long
  option ITM by at least $0.01 at expiration auto-exercises.
- No documentation flag on the position that says "will auto-exercise
  Friday if ITM." moomoo just silently processes it.
- We had zero exercise/assignment code in the repo — expiry handling
  happened server-side without notifying us.
- Result was silent — the sold option position vanished from
  `position_list_query`, our local DB was never touched by anything.

**Defense**:
- [autotrade/ops/sync_positions.py](../autotrade/ops/sync_positions.py)
  (`python -m autotrade.ops.sync_positions`): queries broker, compares
  to local DB OPEN, marks stale entries as CLOSED with
  `trigger_source=broker_sync`. Recommend running before each
  `python -m autotrade.app.main` session (potentially as a preflight
  step in a future PR).
- Ongoing: EOD watcher should force-close ITM positions before market
  close on expiry day. Currently no-op because OPRA subscription is
  missing.
- The stray stock positions (HOOD/IBM/TSLA shares) still sit in the
  account — they're outside our system's execution scope (we only place
  option orders). Need to close them manually in moomoo.

---

## 15. `SELL` on an option we don't hold opens a naked short (broker accepts, we didn't intend)

**Symptom**: Direct consequence of #14. Once the local DB was out of sync
with broker, our close pipeline could easily fire `place_sell_order` for
an option the account no longer holds. moomoo would happily accept that
as **opening a new naked short position**, not "closing existing long"
— because we ran out of long inventory. Naked short calls have unlimited
loss; naked short puts are limited but still large.

Nothing in the pipeline would have caught this before 7/3:
- `place_sell_order` submitted straight to `ctx.place_order` with
  `TrdSide.SELL`. If broker accepted, we logged "success" and moved on.
- No pre-check that we actually owned the option we were selling.

**Why non-obvious**:
- On the surface, a SELL order looks like "close position." The broker
  semantics are actually "sell N contracts, however you want to source
  them" — long-close and open-short use the same trade side.
- The failure mode requires the local DB to be wrong first (#14). If
  the local DB were always correct, we'd never call sell on something
  we don't hold. But #14 shows the DB *does* go stale.

**Defense**: `_get_long_qty(option_code)` in
[autotrade/broker/trade.py](../autotrade/broker/trade.py) queries
`position_list_query(code=...)` and returns 0 if we don't hold `LONG`
inventory. `place_sell_order` calls it before submitting. If broker
`long qty < requested sell qty`, we refuse and return an explicit
"naked-short refused" message — no order goes out. Costs one extra RTT
per sell (probably ~50ms) but prevents unbounded downside from a state
mismatch. Applies to real-env only (`DRY_RUN` short-circuits earlier).

---

## 16. Full re-login (`on_ready`) does **not** replay missed messages; only RESUME does

**Symptom**: 7/20 overnight the local network flapped ~every 15-20 min for
10 hours. Discord dropped ~28 times, of which ~25 were **full re-logins**
(`logged back in after ~3000ms`) and only 3 were fast `RESUMED`. moomoo's
trade+quote contexts dropped in the same seconds (9 reconnects), confirming
it was the shared network layer flapping, not Discord blocking the token.

Two hidden failures fell out of this:
- **~75s of silent blindness.** A gateway RESUME replays events buffered
  during the gap; a full re-IDENTIFY does **not** — the session is gone and
  any message KC sent during those ~3s windows is never delivered to
  `on_message`. 25 × 3s ≈ 75s where a signal would vanish with zero trace.
  That night nothing fired in a gap, but it's a real loss window.
- **Zero alerting.** The existing storm detector only fires on "3
  disconnects in 60s" (the 6/30 identify-rate-limit burst pattern). Slow
  chronic churn every 15-20 min never trips a 60s window, so the bot
  limped all night with no TG warning.

**Why non-obvious**:
- `on_ready` and `on_resumed` both look like "we're back online." The
  critical difference — resume replays the gap, re-identify silently drops
  it — is a gateway-protocol detail, not visible in the callback names.
- The storm detector *existed* and looked like "reconnect churn is
  covered." It only covered the *fast* burst shape; the *slow* churn shape
  is a different failure the same code doesn't catch.
- Simultaneous Discord+moomoo drops are the tell for "network, not
  service" — easy to misread as a Discord/token problem and go chasing the
  wrong layer.

**Defense** (all in [autotrade/app/connection.py](../autotrade/app/connection.py)):
- `_backfill_missed()` runs in the `on_ready` **re-login** branch (not
  `on_resumed`): fetches each monitored channel's `history(after=...)`
  since the disconnect wall-clock time and re-feeds through
  `handle_message` ([autotrade/listener/router.py](../autotrade/listener/router.py)).
  Idempotent because `handle_message` already dedups on
  `_seen(message.id)` — replayed messages that were processed live are
  dropped, so no double orders. `_last_disconnect_wall` is set on the first
  disconnect of a gap and cleared on resume/backfill so we cover the whole
  gap exactly once.
- Chronic-churn detector: 30-min rolling window, ≥5 disconnects → one TG
  alert per 30 min, alongside the existing 60s storm detector.
- `shutdown()` is now idempotent (a second ^C is ignored) so two concurrent
  shutdown coroutines don't race on `close_ctx`/`client.close`.
- Root cause (WiFi power-save / router dropping idle connections) is
  environmental — code only *mitigates* (backfill + alert), it cannot stop
  the drops. Wired/ethernet or disabling WiFi power management is the real
  fix.

---

## 17. `channel.history(limit=N, after=X, oldest_first=True)` truncates the **newest** messages, not the oldest

**Symptom**: Found during review of the 7/28 backfill patch, before it ever
ran overnight. A 60-minute startup backfill on an active channel replayed
messages 1..50 of a 60-message window and silently dropped 51-60 — i.e.
exactly the most recent, most actionable calls. The code looked correct and
even logged "hit the 50 limit" — but the warning names the wrong casualty.

**Why non-obvious**:
- `limit` reads like "cap the work", not "choose which half of the window
  to throw away". Which half you lose is decided by `oldest_first`, a
  parameter that looks purely cosmetic (display order).
- discord.py's docs describe `oldest_first` as ordering, not as retention.
  The mechanism is in `abc.py`: `reverse = oldest_first` selects
  `_after_strategy`, which pages *forward* from `after` and stops once
  `limit` is consumed — so the tail of the window is never fetched.
- For a chat log the intuition is "limit trims history"; here the window is
  anchored in the past, so limit trims the *present*.
- Our own test fake ignored both `limit` and `oldest_first`, so the test
  suite could never have caught it.

**Defense**: [autotrade/app/connection.py](../autotrade/app/connection.py)
`_backfill_missed()` fetches with `oldest_first=False` (newest first) into a
list, then replays `reversed(...)` for chronological order — truncation now
drops the oldest. discord.py's `after` predicate breaks out as soon as it
pages past the anchor, so this does not walk the whole channel history.
Limit raised 50 → 200 (`BACKFILL_HISTORY_LIMIT`). `_FakeChannel` in
`tests/test_backfill.py` now honors both parameters.

---

## 18. `time.monotonic()` does not advance while macOS sleeps — every monotonic-based window silently stretches

**Symptom**: 7/27 overnight the churn detector reported "5 disconnects in
last 30min" at 01:55 and "32 in last 30min" at 09:09 — both were the
*running total for the whole night*, not a 30-minute count. The rolling
window never evicted anything because its clock barely moved.

**Why non-obvious**:
- `monotonic()` is the textbook-correct choice for measuring intervals
  (immune to NTP steps and DST) — it's what you reach for precisely to
  avoid clock bugs. That it also freezes across system sleep is a
  platform detail (`mach_absolute_time` on Darwin) that the "always use
  monotonic for durations" advice never mentions.
- The distortion is *silent and inverted*: the counter looks alarmingly
  high while the window is the thing that's broken. Easy to read the
  number as "the network got much worse" and chase the wrong layer.
- It bites windows in proportion to their length: the 60s storm window is
  effectively unaffected, the 30min churn window is destroyed. So a
  detector can be "half broken" with no obvious pattern.
- Same freeze applies to `asyncio.sleep()` (loop clock is monotonic), which
  is *why* wall-clock heartbeat gap detection works at all.

**Defense**: churn accounting in
[autotrade/app/connection.py](../autotrade/app/connection.py) switched to
`time.time()` (wall clock) for both the window and the notify cooldown;
storm (60s) stays on monotonic deliberately. The same insight is used
constructively by `run_alive_heartbeat()`: a wall-clock beat every 15s whose
gap > 90s *is* the sleep signal, used to rewind the backfill anchor over the
sleep period. Root cause is environmental — `caffeinate -is make run`.

---

## 19. A full disk erases the evidence of the outage it causes — log-only error handling is not error handling

**Symptom**: 7/31 overnight (09:50:20–09:50:42 AEST = 19:50 ET) the sl/tp/eod
watchers threw 33 consecutive `sqlite3.OperationalError: disk I/O error` as the
volume hit zero free space. Telegram said nothing. Worse, `app_2026-08-01.log`
jumps straight from `08:34:13` to `11:10:35` and `logs/errors/error_2026-08-01.log`
is **0 bytes** — the 33 ERROR records are the only thing that happened in that
window, and they are exactly what was lost. Without the terminal happening to be
open there would have been no trace at all that all three risk watchers went down.

**Why non-obvious**:
- Every watcher's `except` was `logger.exception(...)`, which reads as
  "loud and recorded". It is — right up until the failure mode *is* the log
  sink. The one incident class that most needs an audit trail is the one that
  cannot write one.
- `loguru` degrades quietly here: a failing sink prints `--- Logging error ---`
  to stderr and drops the record. The process stays healthy, other sinks keep
  working, and nothing raises — so a "log and continue" loop genuinely continues,
  just blind.
- sqlite and the log sink shared a failure domain (same volume). Two independent-
  looking defenses died to one cause.
- The disk pressure came from outside the repo entirely (the checkout is ~4 MB).
  Nothing in the app's own footprint hinted at it, and `preflight()` checks
  broker, OPRA and risk budget but never free space.
- The watcher loops *did* survive (`while True: try/except` held, and
  `asyncio.CancelledError` is a `BaseException` so it isn't swallowed) — so
  "process still up" was true and meaningless.

**Defense**: [autotrade/notify/watchdog.py](../autotrade/notify/watchdog.py) —
watcher `except` branches now call `notify_tick_error()`, which keeps the
traceback in the log *and* pushes a Telegram alert down an independent path,
first-failure-immediate then throttled per scope
(`WATCHER_ERROR_ALERT_COOLDOWN_SEC`, default 300s; 33 alerts in 22s would be its
own outage — see lesson 12 and the 7/23 runner-preserve noise). Recovery emits a
matching ✅ with the suppressed count, so "it broke" and "it's fine now" are both
observable. The alerting path is fully exception-guarded: a protection mechanism
must never become a new failure source. Still open (see ROADMAP P1): a preflight
free-space gate and a size-capped log rotation, so the condition is refused at
startup rather than discovered at 3am.

---

## 20. Signal grammar drifts mid-flight: the same author inverts price and strike without warning

**Symptom**: 7/31 13:41 ET, `$AAOI scalp 0DTE $.70 $98 calls` failed to parse and
the trade was missed — both the EN message and its ZH twin. Every Pattern B
variant (B0/B0.5/B1/B1b/B2/B3) hardcodes `$STRIKE calls … $PRICE`; this one put
the fill price first.

**Why non-obvious**:
- The message is otherwise perfectly ordinary — right ticker, right tag, right
  DTE. Nothing about it looks malformed to a human, which is why it doesn't
  register as "a new format" when you skim the channel.
- It failed *silently into the noise floor*: the same night produced legitimate
  `Parse failed` warnings for level broadcasts, weekly recaps and buy lists. One
  real miss inside a stream of correct rejections is invisible without
  reconstructing intent per message.
- The obvious fix (add an inverted copy of each B pattern) doubles a regex family
  that already has six members and six expiry paths.

**Defense**: normalize word order at the entrance instead —
`_normalize_inverted_price()` in
[autotrade/parsing/signal_parser.py](../autotrade/parsing/signal_parser.py)
rewrites `$PRICE $STRIKE calls` into canonical order, so the existing B ladder
supplies all the expiry/tag logic unchanged (same tactic as the ZH direction-word
normalization above it). Two guards keep it from swapping the fields the wrong
way — which would place a limit order at the *strike* — the price must carry a
decimal point (this channel quotes premiums with cents and strikes as integers,
so `$740 $745 calls` spread notation is rejected), and price must be < strike.

---

## 21. Bilingual redundancy is not redundancy when both channels degrade on the same message

**Symptom**: 8/3 10:27 ET, KC posted `out AMZN -15%` and its ZH twin
`减持亚马逊 -15%`. Neither closed the position. The EN copy never even reached
the close parser — `detect_action` routed it to OPEN, because `STRONG_CLOSE_RE`
only knew `out of` and `WEAK_CLOSE_RE` only knew `out half/full/majority` and
`out N%`; bare `out <TICKER>` was in neither. The ZH copy *did* parse, but
`减持` sits in `ZH_ACTION_VERBS` and not `ZH_FULL_CLOSE_VERBS`, so pct fell to
the trim default of 33, which a 1-contract position turns into a
runner-preserve skip. The author exited at −15%; we carried the put overnight.

**Why non-obvious**: the whole design leans on "ZH arrives, EN backstops it
1–3s later" (module docstring of `close_parser`, and the 63%/100% pairing stats
behind it). That reads like two independent samples. It isn't — the ZH text is a
*machine translation of the same sentence*, so an unusual phrasing perturbs both
copies at once, in different ways. Here `out` → `减持` was a lossy translation
(exit → reduce) and `out AMZN` was an unlisted EN form; each failure alone was
survivable, and the pair was not. Any postmortem that reads "but the twin should
have caught it" is describing a correlation the architecture never had.

The same night produced a second instance of the identical shape, opposite
direction: 16:06 ET `…if you don't want to swing you can close until 4:15pm EST.
I personally am swinging them` was correctly skipped in EN and executed as
`CLOSE 100%` from the ZH translation `若不想持仓过夜，可…平仓` — a conditional
whose main clause carries the verb, attached to a message where the author says
outright he is holding. Sold 1.63 against a 1.93 entry.

**Defense**: two layers, deliberately independent.
- *Semantic*, in [close_parser.py](../autotrade/parsing/close_parser.py):
  `_OUT_BARE_SYM_PATTERN` (bare `out <TICKER>` = full close, uppercase-only via
  `(?-i:)` plus a stopword list, still whitelist-gated downstream),
  `ZH_OPTIONAL_CLAUSE_RE` / `EN_OPTIONAL_CLAUSE_RE` (negated conditionals mask to
  *sentence* end, not comma — the action lives in the main clause), and
  `AUTHOR_HOLD_MARKERS` / `ZH_AUTHOR_HOLD_MARKERS`.
- *Structural*, in [heuristics.py](../autotrade/listener/heuristics.py):
  `_close_is_zh_twin_of_skipped_en` — if the EN source text was judged "not an
  instruction" within 60s, the ZH machine translation of it does not execute,
  whatever it says. Asymmetric on purpose: EN is the source, so ZH-skipped never
  blocks EN. This one costs nothing to maintain and catches the *next* weird
  translation, which is the failure we cannot enumerate in advance.

Note what was **not** changed: `减持` still means trim (pct=33). Promoting it to
full-close to fix this one message would reprice every genuine trim signal. The
fix belongs on the EN side, which arrives first and is the source of truth.

---

## 22. A tag that is parsed, stored and displayed is not a tag that does anything

**Symptom**: 8/3, `AMZN 275p 4DTE @ 1.65 day trade` opened with
`tags=['day_trade']` and `eod_force_close=False`, and was still open the next
morning. `categorize()` derived `eod_force` from `dte == 0` alone; `day_trade`
was consumed only by `policy/guards.py` as a DTE ceiling. The tag appeared in the
parse log, in the positions table, and in the Telegram fill notice — every
surface a human checks while convincing themselves the pipeline understood the
signal.

**Why non-obvious**: the failure has no error, no warning, and no silent branch
you can grep for. The tag is *used*, just not for the thing its name implies, and
the two consumers (`guards` and `categorize`) sit in different modules with no
reason to reference each other. Reading either one alone looks complete.

**Defense** — and this is where it gets instructive, because the obvious fix was
also inert. Three layers had to change, and the first two alone did nothing:

1. `eod_force = (dte == 0) or ("day_trade" in tags)` in
   [policy/positions.py](../autotrade/policy/positions.py);
2. …returned from **all** branches — the three non-0DTE returns hardcoded
   `False`, which was equivalent while `dte == 0` was the only source and became
   the bug the moment it wasn't;
3. …and `eod_watcher` had to *read* the flag. It didn't. An earlier fix (long ITM
   options auto-exercising, lesson #14) had **replaced** the flag with
   `expiry == today` as the selection criterion, correctly — a Monday-opened
   weekly still has `flag=False` on Friday. But replacing it left
   `eod_force_close` with zero behavioral consumers repo-wide, a write-only
   column. So steps 1–2 wrote a truer value into a field nobody read: the same
   lesson recurring one layer down, inside the fix for it.

The selection is now `expiry == today OR eod_force_close`, two independent
sources: (a) covers positions expiring today whatever their flag, (b) covers
"close today" declared at open regardless of expiry. Either alone leaks a real
case.

Note the contract flip this forces: `test_eod_skips_future_expiry` asserted that
`flag=1` + future expiry does *not* close — correct while the flag was
informational and that state could only be synthetic. `day_trade` makes it a
common legitimate state, so the test now asserts the opposite and says why.

General form: when adding a tag, name its consumer in the same commit — and when
*replacing* a field's consumer, delete the field or note that it is now inert.
A write-only column is a loaded gun for the next person who "wires it up" and
sees green tests.

---

## 23. A rejection with no state is an infinite loop — and the alert channel drowns before the log does

**Symptom**: 8/13, `US.MU260814C945000` was recorded OPEN with 2 contracts that
the broker never actually held. Five seconds later TP T1 fired, the broker
refused the sell as a naked short, and the watcher tried again 5 seconds later.
**1918 times, 00:11 → 06:00.** 5754 log lines — 70% of an 8183-line, 1.09 MB
overnight log, up from 363 lines the night before. It recurred on 8/14 (812
times) because the running listener predated the fix.

**Why non-obvious**: every individual layer looked correct in isolation.

1. `tp_hits` (the "this tier already fired" bitmask) is persisted *after* the
   order succeeds — correct, so a failed sell can be retried. But it means a
   rejection leaves **no trace at all**.
2. `_sell_rejected` additionally `discard`ed the per-tick dedup key — also
   deliberate, so another path could still act within the tick.
3. Neither knew about the other, and nothing distinguished *transient* failure
   (worth retrying) from *deterministic* refusal. "Broker has 0 long of this
   contract" answers the same way on attempt 1918 as on attempt 1.

The deeper trap: the phantom position itself came from a **silent** branch.
`confirm_buy_fill` logs and back-fills only when the dealt price *differs* from
the limit; when moomoo reported the order filled at exactly the limit, it
returned without a single line. Two sibling positions that night logged
`FILL_ADJUST` within 15s; this one logged nothing, and "nothing" is
indistinguishable from "never ran".

**The part that actually hurt**: the reconciler *saw it* at 00:27 —
`db_only: US.MU260814C945000 db=2 broker=0` — sent one Telegram, then went quiet
by design (same-signature throttle). It reported every hour for six hours and
never acted. Visibility was never the missing piece.

**And the alerts were worse than useless**: each of the 1918 rejections called
`send_telegram` directly rather than through the `notify()` wrapper. The wrapper
is the only thing that logs `[notify] TG sent`, so the operator's phone very
likely got ~1918 messages while the log showed none — inverting the evidence so
completely that the 8/14 review concluded "zero alerts reached the operator".
`transport.py`'s own docstring warns about exactly this bypass (8/5, the sleep
alert). Knowing about a footgun in a comment does not disarm it.

**Defense** (`position/retry_guard.py`, wired into tp / sl / eod / kc_close):

- deterministic refusals (`broker/errors.is_deterministic_reject`) trip the
  breaker on the **first** rejection; transient ones back off 30s→60s→120s→240s
  and trip after 5;
- tripping **does not** set `tp_hits` — marking the tier "done" would silently
  erase an unrealized take-profit. Stop and shout, in-memory only, restart clears;
- alerts fire at exactly two moments (first failure, trip) with suppressed counts
  folded in — same shape as `notify/watchdog`;
- polling paths (SL/TP) get backoff+trip; signal-driven paths (EOD, kc_close)
  take `backoff=False` and only trip, because their retry cadence is already
  deliberate (EOD's 60s `_skip_until`, kc_close's bilingual-twin retry).

`reconciler` now closes deterministic `db_only` drift itself, behind three gates
(env switch; never when the broker reports zero option positions, since that is
indistinguishable from a failed query; max 3 per round).

**General form**: a retry with no persisted failure state is not a retry, it is a
loop. Before adding one, answer two questions in the same commit — *what marks
that this attempt failed*, and *what class of failure is worth repeating at all*.
And when an error path emits a notification, route it through the wrapper the
rest of the system uses; the one that bypasses it is invisible exactly when
volume makes visibility matter.

---

## 24. Fixing one language of a bilingual pipeline is not fixing the pipeline

**Symptom**: 8/14, 16:13 ET. The same message arrived twice, 5 seconds apart:

```
EN  "nice drop on SPCX into end of day, still in the puts after the
     profit trims this morning ✅"        → no signal ✅
ZH  "临收盘SPCX跌得漂亮，早间利润减仓后仍持有看跌期权✅"
                                          → CLOSE 33% ❌
```

A recap that says *in both languages* "I am still holding" was parsed as an
instruction to sell. The only thing that prevented it was runner-preserve —
SPCX happened to have 1 contract left, so 33% rounded to 0. With 2 contracts it
would have sold one.

**Why non-obvious**: no single rule was wrong. `减仓` is a legitimate close verb.
`早间` was simply absent from a list that already had `今早`, `今天早些`,
`早些时候` — three synonyms of the fourth. The sentence used commas throughout,
so `_zh_action_sentences` (which splits on `。！？`) never separated the recap
clause from the verb. And `SPCX` is a Latin ticker, so symbol extraction
succeeded where two other Chinese recaps that night were saved only by
`[zh_unrecognized]` failing to find a symbol — luck, not defense.

This is the *third* recurrence of the same shape. 8/5: an EN `so far` recap
guard was added and the ZH twin (`目前为止`) was not — the note in
`ZH_RECAP_MARKERS` calls fixing one side "白修". 8/10: `chopping in half` and
`削减一半` both missed. Each time the fix was correct and one-sided.

The asymmetry has teeth because the two pipelines have **different structures**,
not just different word lists: EN has a `HOLDING` guard on the close path, ZH has
none; ZH splits sentences on CJK punctuation the EN side doesn't use. So "add the
translated word" is necessary and not sufficient — the guard itself may not exist
on the other side.

**Defense**: `早间` added to `ZH_RECAP_MARKERS`; the `仍持有 / 仍在持有 / 还持有
/ 还在持有` family added to **both** `close_parser.ZH_RECAP_MARKERS` and
`signal_parser.SKIP_KEYWORDS`, since the open path had the same gap (`$ASTS
仍在持有` reached `no signal` by luck the same night). Bare `持有` stays out —
it would swallow "buy X, plan to hold through September". Regression:
[tests/test_overnight_0814.py](../tests/test_overnight_0814.py), which pins both
languages *and* the reverse direction (real close instructions must still parse).

**General form**: when a parser change lands on one language of a bilingual feed,
the commit is not done until you have looked for the corresponding guard on the
other side and confirmed it exists. Whichever twin arrives first wins, and
historically ZH arrives first 63% of the time — so the unfixed side is not a
smaller risk, it is most of the risk.

---

## 28. A scheduled wake that succeeds is not a machine that is awake

**Symptom**: 8/29. The listener was supposed to start at 23:15 (15 minutes before
the US open). It started at **23:45:16** — 15 minutes *after* the open. The
`$UBER $78 calls $.31` lotto had gone out at ~23:32; startup backfill replayed it,
the age guard correctly refused to open a 803-second-old signal, and the weekly
recap later reported UBER at **+270%**.

Everything in the chain looked configured. `launchd` was loaded. `pmset repeat
wakepoweron` was set for 23:10. `launchd.night.err.log` was empty. And the wake
itself worked:

```
23:10:00  Wake                      ← the scheduled wake fired, correctly
23:10:30  Display is turned off
23:11:26  Entering Sleep 'Idle Sleep'   ← awake for 86 seconds, then back down
23:15:00  ← launchd should fire here. Machine is asleep. Nothing happens.
23:26:51  DarkWake → back to sleep
23:44:10  DarkWake → back to sleep
23:45:15  Display on → launchd finally runs the missed job, 30 minutes late
```

**Why non-obvious**: the wake and the job were configured as if they were one
mechanism, and every diagnostic said "fine". `pmset -g sched` shows the repeat
wake. `launchctl list` shows the agent loaded, last exit 0. The job *did* run,
so there is no failure anywhere to grep for — only a timestamp 30 minutes off,
in a log nobody reads when nothing broke.

The gap is that macOS treats them as unrelated: a scheduled wake buys you
**one idle-sleep timeout** of uptime, nothing more. On battery that was 86
seconds. The job was scheduled 5 minutes after the wake — four minutes past the
end of the window it was supposed to land in. `caffeinate -i` in `night_run.sh`
cannot help: it only starts running once the job has already started.

**Defense**: the plist now carries several `StartCalendarInterval` entries —
23:11, 23:15, 23:20, 23:25. The one that matters is **23:11**, 60 seconds after
the wake and inside the observed 86-second window; the rest are cheap insurance
for a wake that does not happen at all. Repeats are safe because `night_run.sh`
opens with a `pgrep` guard and exits if a listener is already running — the
retries are idempotent by construction, which is what makes "just fire it four
times" an acceptable fix rather than a hack.

**General form**: when a job depends on the machine being awake, the wake and the
job are one system with a **margin**, and that margin is an idle-sleep timeout
you do not control and did not measure. Schedule the job inside the window, not
merely after the wake. And note what made this expensive to find: the failure
produced no error anywhere — it is only visible as the distance between two
timestamps in two different logs.

---

## 29. The speaker's name is not a ticker, and a whitelist is not why you survived

**Symptom**: 8/28 23:45, the relay format in the KC channel changed. What had
been

```
@everyone
KC Trades Bot:trimmed AAPL 330c ...
```

became

```
核心期权交易综合BOT: KC Trades Bot:trimmed AAPL 330c ...
核心期权交易综合BOT: :red_circle:KC交易机器人：减仓340c @ 4.30 🚀
```

The ZH close path extracts symbols from free text, so it read the speaker labels
as instruments:

```
[close_parser] ZH close intent, symbols=['BOT', 'KC'] 不在当前持仓 —— 跳过
```

`BOT` came from `综合BOT`, `KC` from `KC交易机器人`. Nothing was traded, and the
log line even looks reassuring — the position whitelist caught it.

**Why non-obvious**: the whitelist made it invisible. `BOT` and `KC` were not in
holdings, so the guard fired and the message was skipped, exactly as designed.
That is a **second-line** defense doing a first line's job, and it hides two
costs that the skip line does not mention:

1. `[zh_unrecognized]` and its siblings exist to accumulate evidence about
   whether Chinese company-name → ticker mapping is worth building
   (ROADMAP P1 #13). Feeding speaker labels into that counter poisons the only
   sample the decision would ever be made from.
2. `BOT` is a real listed ticker. The whitelist is a coincidence, not a rule.

Checking the old format afterwards showed it had been leaking `KC` the whole
time in exactly the same way — the format change did not introduce the bug, it
made a pre-existing one produce a second symbol.

**Defense**: `strip_relay_prefix` removes leading speaker labels before parsing,
applied at the `parse_close` dispatcher so every language branch gets the same
clean text. Boundaries kept tight (start of string only, ≤24 chars per segment,
must contain BOT/机器人, at most 3 segments, optional leading `@everyone` and
`:emoji_shortcode:`) with reverse tests asserting real content is untouched.

Applied to the **close path only**, deliberately: the open path anchors on
`$TICKER` + strike + calls/puts and parses the prefixed and unprefixed message
identically (pinned by `test_relay_prefix_does_not_affect_open_path`). Lesson
#24 says to check the other side, not to change it unconditionally — the check
was done and the answer was no.

**General form**: when a parser reads instruments out of free text, everything
that is not the message body is a source of false instruments — speaker labels,
channel names, mentions, emoji shortcodes. Strip the envelope before parsing it.
And when a guard fires on data that should never have reached it, the guard
working is not the end of the investigation; ask what put that data there.

---

## 30. A position that becomes a runner by accident inherits a policy written for one that was chosen

**Symptom**: 8/28-8/29. AAPL 330c 10/16, opened with 2 contracts at 5.05. Over
the following night KC called the trim four times:

| time | message | result |
|---|---|---|
| 23:45 | `trimmed AAPL 330c +30% and also the 340c +30%` | runner-preserve, skipped |
| 23:45 | `trimmed AAPL 330 @ 6.85 ...` | dup of the above |
| 00:45 | `BOOM!!! Trimmed AAPL 330 @ 7.50` | runner-preserve, skipped |
| 00:46 | `trimmed the 340c @ 4.30` | no ticker, correctly skipped |

The parser was right every time — `strike-filter: AAPL 330.0C → 1/1 positions
selected` is a precise hit. Nothing sold, because one contract remained and
33% of one contract rounds to zero. Entry 5.05, called at 7.50, still open.

**Why non-obvious**: runner-preserve is a *good* rule with a documented price
(lesson #13, ~$330/contract) and it behaved exactly as specified. The failure is
in a premise the rule never states: it assumes a single remaining contract is a
**runner you decided to keep**. Here it was the residue of a bug — the 8/28
misrouted UNG war story (lesson #27) had sold one of the two contracts. The
position did not become a runner; it was made one by an error, and then
inherited "hold it forever" from a policy written for deliberate runners.

That is the second-order cost of #27, and it is larger than the first: the bad
sale was one contract at a bad limit; the consequence was that **four correct
sell signals over the next twelve hours all became no-ops**.

**Defense**: `STRATEGY_B` — already written, tested, and shipped dark since
0011 — takes exactly this case: when a trim is blocked by runner-preserve, take
a fresh quote, and if our own unrealized gain clears `STRATEGY_B_MIN_PNL_PCT`
(25%), sell the remainder instead of holding. Last night's two quotes score
+35.6% and +48.5%. It is now `true` in the template. The floor is unchanged:
no fresh quote → still preserve.

**General form**: a rule conditioned on *state* ("one contract left") silently
assumes something about *how the state arose* ("because we chose to keep it").
When another bug can produce the same state, the rule keeps applying and there
is no error to see — the second bug wears the first one's policy. Worth asking
of any preserve/skip rule: what else, other than the intended path, can put the
system into the state that triggers it?

---

## 31. The fallbacks a rule names in its own log line are not fallbacks unless you check them

**Symptom**: 9/1. COIN 190c 9/4, two contracts at 2.08. 02:42 T1 hit and sold
one at 2.99. 03:12 T2 hit (last ≥ 4.16), one contract left, 50% of one rounds
to zero, so runner-preserve kept it and logged — verbatim —

```
[tp] 🏃 T2 HIT ... 保留 runner 不卖；本档标记为已触发，
     后续交给 SL / EOD / 喊单员平仓信号
```

That line names three fallbacks. That night all three were empty:

| named fallback | actual state |
|---|---|
| SL | anchored on entry: 2.08 × (1 − 0.50) = **1.04** |
| EOD | `eod_force_close = 0` (not a day trade) |
| 喊单员平仓信号 | two real trims that night, both unparsed (lesson #32) |

A contract that had touched 4.16 would not be sold until it fell back to 1.04.
The whole +100% was giving-back range, and no log line anywhere says so.

**Why non-obvious**: nothing was broken. runner-preserve worked (lesson #13),
the ladder worked, `tp_hits` was set correctly, the SL watcher was running and
polling that contract every 5 seconds. Each component was inside its own
contract; the gap is that **the stop's anchor is a fact about entry, while the
thing being protected had become a fact about the highest tier already banked**.
The log line even enumerates the fallbacks, which makes it read like a handoff
to something — that phrasing is what stops you from checking whether the
recipient exists. #30 is the same shape one level down: a rule inherited a
premise nobody restated. Here the premise is "the stop still means something".

**Defense**: `policy/positions.sl_ratchet_floor` — once tier k is set in
`tp_hits`, the SL floor moves up to the rung below it (T1 → breakeven, T2 →
lock the T1 threshold), divided by `(1 − sell_slip)` because the threshold is a
**trigger** price and the fill is `last × (1 − slip)`; an unadjusted "breakeven
stop" loses exactly one slippage. `sl_watcher._sl_tick` takes `max(old, floor)`
— the ratchet may only tighten. `SL_RATCHET_AFTER_TP=0` restores the old
behavior verbatim.

**Known and deliberately out of scope**: this only reaches positions already in
the SL watch list (`apply_sl=True`, plus the lotto floor). `swing` is
`apply_sl=False` and never enters the watcher at all, so a swing runner that
banks T1 is still naked — that is the AAPL of #30, still true. Giving an entire
category a stop is a money-path decision that needs its own call, not a
side effect of this patch. Filed as ROADMAP P1 §16.

**General form**: when a component declines to act and logs "X / Y / Z will
handle it", that sentence is a claim about three other components, and nothing
verifies it. Write the check, or write the handoff as what it is — "nothing
else is watching this".

---

## 32. Same shape, new preposition: the fraction pipeline was there, the door wasn't

**Symptom**: 9/1, enrich called the COIN trim twice, both bilingual, four
messages, all four `[parser] no signal`, zero alerts:

| time | message | ZH twin |
|---|---|---|
| 01:45 | `Start securing some.` | `开始确保一些。` |
| 02:49 | `$COIN - Down to 1/2.` | `$COIN - 降至 1/2。` |

`FRACTION_DOWN_TO_PATTERN` ("down to X/Y" → sell 1−X/Y) has been in
`_extract_pct` since 7/6, and `ZH_FRACTION_TO_PATTERN` alongside it. Fed
directly, `parse_close` still returned `None` — the pct stage is never reached,
because `detect_action` said OPEN and `_has_action_verb` said no verb.

**Why non-obvious**: this is the *third* time the same defect has been filed
(8/3 "out 1/2" → `_OUT_FRACTION_PATTERN`; 8/25 `减半` → routing table;
lesson #25's general form). Each fix added the one missing shape. What keeps
recurring is the assumption underneath: **an extraction pattern existing feels
like the feature existing.** The pct stage is the visible, testable, satisfying
half; the verb/routing gate is a precondition two files away, and it is where
the message actually dies. `test_close_routing_sync.py` catches exactly one
direction of this (parse ⇒ route) and it did **not** fire here, because
`parse_close` didn't parse either — both gates were shut, so the invariant
was vacuously true.

**Defense**: `_DOWN_TO_FRACTION_PATTERN` / `_SECURE_SOME_PATTERN` and their ZH
twins, defined once in `close_parser` and imported by `signal_parser` (routing
and parsing move together, per the file's own rule). Both carry their boundary
conditions in the pattern rather than in a caller:

- down-to **must** be followed by a fraction with num<den, den∈[2,5], the same
  enumeration as `_OUT_FRACTION_PATTERN` — bare "down to" is narration
  ("down to 2.50 support") and KC slang ("down to runners");
- securing **must** take a trim object (`some|a few|half|part|profits`).
  Bare `secure/securing/确保` is the dangerous one, and the counterexample is
  in the *same night's* log: 00:59 `make sure it stays that way` / `确保它保持
  这样`. Two more sit in history: 8/18 `to secure small green trade`, and 7/23
  `stop at entry now to secure green trade` — whose safety this file already
  records as "the EN twin has no EN verb, so it's safe". Adding the bare word
  would have quietly falsified that recorded conclusion.

**Deliberately not fixed**: 04:31 `holding 1/4 into tomorrow` (and its 05:43
recap) went from our 1/2 to his 1/4, so semantically it *is* another trim. It
is caught by the holding/recap guard from lesson #24, on purpose. Turning a
"what I still hold" statement into a sell order is the opposite policy from
the one #24 was written to enforce; it needs its own decision, not a regex.
Filed as ROADMAP P1 §17.

**General form**: ask of any parser fix "which gate did the message actually
die at?" — not "does a pattern for this exist?". And when an invariant test
exists for the failure class, check whether it can fire on the new case;
"A ⇒ B" proves nothing when A is also false.

---

## 33. An alert that has no possible action trains you to ignore the alert that does

**Symptom**: 9/1, the reconciler logged the same three drift lines every round
for 49 rounds — 147 identical WARNINGs:

```
[reconcile] drift broker_only: US.NVDA260918C250000 db=0 broker=1
[reconcile] drift broker_only: US.SOFI270115C20000  db=0 broker=1
[reconcile] drift broker_only: US.UPS270115C130000  db=0 broker=1
```

All three are LEAPS (Sept '26 / Jan '27) in the paper account. This bot only
buys weeklies and swings. Checked against `trades.db`: none of the three has
**ever** had a row, in any status. They were not opened by us and never will be.

**Why non-obvious**: every layer behaved as designed. `broker_only` is a real
diff category; the module's docstring explicitly refuses to auto-handle it
("凭空造记录要猜入场价"), which is right; the TG throttle correctly sent one
message and then went quiet on an unchanged signature; and the per-round
WARNING is a *deliberate* 0016 decision so that a throttled night still leaves
forensics. Each of those is defensible, and together they produced 147 lines of
which **not one was actionable** — while the next real ghost (the 8/13 MU 945c
shape: we opened it, DB says closed, broker says it's still there) prints in
exactly the same format, at the same level, in the same place.

**Defense**: `KIND_FOREIGN`. `diff_positions` takes an optional `known_codes`
(codes that appear in `positions` in *any* status, via
`positions_db.known_option_codes`) and splits broker-side extras by **cause**,
not severity: seen before → `broker_only`, a real accounting split, still one
WARNING per round, unchanged; never seen → `foreign`, INFO through a 6-hour
`logdedup` window, and a TG section that says in words that nothing will ever
auto-handle it. Omitting `known_codes` reproduces 0016 verbatim.

**General form**: log volume is not the cost — the cost is that a recurring
unactionable line and a rare actionable one become indistinguishable, and the
human learns the wrong one. Before adding a periodic warning, ask what the
reader is supposed to *do*, and what happens on the thousandth time they can't.

---

## 34. "Deterministic" is a claim about time, not about the error string

**Symptom**: 9/2, TSLA 345P, four minutes end to end:

```
23:38:46  buy 2104813 submitted OK, DB books qty=2 @2.59 (optimistic path)
23:41:47  TG「买单超时未确认成交」— fill_checker polled 180s, never saw FILLED_ALL
23:43:52  "trimmed Tesla 2.95" parses perfectly (EN CLOSE symbols=['TSLA'] pct=33)
          → broker: naked-short refused: broker has only 0 long
          → is_deterministic_reject → retry_guard trips immediately
23:43:54  the ZH twin parses just as well, and is blocked by the trip
05:50:21  EOD force-closes 2 contracts @1.76 — the broker had them by then
```

The parser was right, the router was right, the naked-short guard was right,
and the position was real. -$166 on a trade whose exit signal arrived, in two
languages, ninety seconds after entry.

**Why non-obvious**: the classification was derived from a real postmortem
(8/13 MU: DB says 2, broker says 0, retried 1918 times from 00:11 to 06:00).
That night the cause genuinely was a DB/broker desync, which no amount of
backoff repairs. So `naked-short refused` went into
`_DETERMINISTIC_REJECT_HINTS` and the guard trips on the first rejection.
But the *same sentence from the broker* has two causes with opposite costs:
a desync (retrying is pure noise) and **a buy still in flight** (retrying is
the whole point). The error string cannot tell them apart, and the module's
own comment had already predicted the failure — "宁可漏判成瞬时…也不要把真
瞬时的失败错判成确定性而永久停掉一条止盈路径" — without a way to act on it.

**Defense**: `broker/inflight.py`, a state-only module in the shape of
`retry_guard` (imports no siblings, so `broker.trade` can read it and
`position.fill_checker` can write it without a cycle). `place_order` registers
on submit; `fill_checker` clears on a *terminal* status only — **a timeout
does not clear**, because "timed out" means "still unknown", which is exactly
the state that deserves the exemption. With a buy in flight, the naked-short
branch returns a differently-worded message (`naked-short deferred: …`) that
`is_deterministic_reject` does not match, so all four `on_reject` call sites
fall into the transient group with no change. The exemption expires after
`INFLIGHT_BUY_GRACE_SEC` (900s; the real rejection came 5.5 min after submit) —
after that a 0-long reading is a desync again and trips as before.

**General form**: when you classify a failure as permanent, you are asserting
something about the future, and the evidence you have is a string from the
past. Ask what else produces this exact message, and whether the system knows
something the message doesn't — here it did: another component was actively
polling that very order.

---

## 35. The leg that asks a human for help runs over the same cable as the failure

**Symptom**: 9/5, the host lost its network around 02:27 AEST. Discord flapped,
OpenD returned `Network interruption.` on every reconcile tick, and Telegram
returned `ConnectError` 101 times. At 15:50 ET the EOD watcher entered its
window exactly on schedule and retried every 30s until 16:05 — three contracts
expiring that day, all refusing to sell:

```
[eod] no quote for US.NBIS260904C215000, refusing entry-fallback sell, manual close required
[eod] no-quote TG FAILED for US.NBIS260904C215000
```

Ninety "a human must handle this" alerts, none delivered. All three contracts
expired unclosed: ~$714 of cost basis. Nothing in the system was broken —
the fail-closed refusal was *correct* (8/22's lesson: never fall back to the
entry price when you can't get a quote), the un-throttled expiry-day alert was
a deliberate fix that worked, and the machine was awake.

**Why non-obvious**: every fail-closed path in this codebase has the shape
"停手 + 大声喊人" — the circuit breaker, the no-quote refusal, the fill-price
gate, the buy timeout. Each was reviewed on the assumption that stopping is
safe *because* a human gets told. 7/31's postmortem had already found one
version of this ("告警链路不能只挂在日志上", when the disk filled and the logs
themselves stopped writing) and the fix was to add Telegram as a second
channel. What that fix did not consider is that Telegram, Discord and the
broker feed all run over one cable: the outage that creates the emergency is
the same outage that removes the ability to report it. The remaining fallback
was a WARNING line in a log nobody reads until morning — by which time a
same-day option is settled.

**Defense**: two legs that do not touch the network.
`transport.send_telegram` appends every undelivered message to
`logs/undelivered_alerts.tsv` (single-line TSV, not JSONL, because
`morning_collect.sh`'s contract is pure shell — no jq, no python), capped at
5 MB so the sink can't repeat 7/31's disk-full failure. `morning_collect.sh`
groups last night's entries by body with counts and first/last timestamps and
prepends them to the digest. Separately, the digest now leads with
**已过期 / 今日到期但仍未平** — those three rows were present in both the 9/4
and 9/5 digests with `expiry` right there in the column, indistinguishable
from a swing expiring in three weeks.

**General form**: for any "stop and escalate" design, ask what the escalation
depends on, and whether it shares a failure domain with the thing being
escalated. A safety mechanism whose alarm dies with the fault has no alarm —
it just stops.
## 36. When the parser is right to refuse, the fix belongs in the layer that has the missing context

**Symptom**: 9/2, KC opened TSLA 345P and then, ninety seconds later, asked to
trim it three times. None of the three executed:

```
23:40:03  out half 2.82                            → [CLOSE] parser skipped
23:40:05  2.82减半仓                                → [zh_unrecognized]
23:43:52  trimmed Tesla 2.95, stop is at 2.35 now  → parsed fine, hit the breaker (#34)
```

The first two carry no ticker. KC's habit is to name the ticker on entry and
omit it on every follow-up, so this is not an edge case — it is the normal
shape of an exit instruction from that channel.

**Why non-obvious**: the instinct after twenty word-list fixes (#21, #24, #25,
#32) is that this is another gap in a pattern — some preposition or verb form
the parser doesn't know. It isn't. `parse_close` already understood
"out half" perfectly; it returned `None` because it could not answer *which
position*, and with one sentence in hand that refusal is **correct**. Widening
the parser here would mean inventing a target, and this repo's whole close-side
posture is 宁漏平不误平. The evidence that decides it — which channel, which
position was opened minutes ago, whether 2.82 is even in that contract's price
range — was never in the sentence. It lives one layer up, in `close_flow`,
which knows the channel and the book.

**Defense**: `parse_symbolless_close` is a **second, additive entry point** that
only extracts (pct, price, language) and explicitly refuses when any ticker
appears anywhere — a ticker that isn't held means "no position to close", which
must keep returning `None`. `parse_close`'s contract, asserted by 48 existing
tests, is untouched. `close_flow._bind_symbolless_close` then supplies the
missing context: same channel, exactly one position opened today (ET) in that
channel, and the quoted price within [0.25×, 4×] of its cost basis. It emits an
**ordinary CLOSE dict** with `symbols` + `hint_strike` + `hint_side` — the
existing "pin to one contract" mechanism from #11 — so the execution loop,
channel filter, bilingual-twin guards, runner-preserve and quote fallback all
apply with no changes. That pinning is load-bearing: that night KC held both
TSLA 345P (fresh) and TSLA 380C (an 8/14 swing), and binding by symbol alone
would have trimmed a three-week-old position.

**General form**: a component that refuses for lack of information is not the
component to fix. Find the layer that already holds the missing fact and let it
answer, in the vocabulary the rest of the system already speaks — a new `kind`
would have meant new handling in every downstream guard; an ordinary CLOSE
inherits all of them for free.

---

## 37. A trailing space is not a word boundary — and the test that should have caught it always typed a space

**Symptom**: 9/9, 00:09:51, KC asked for a second trim on a position we held:

```
TSLA 3.90 💵 new high of day trim, stop at entry now to secure green trade
    detect_action -> CLOSE   ✅
    parse_close   -> None    ❌   _has_action_verb == False
```

`ACTION_VERBS` carries `"trim "` — a bare verb with a **trailing space standing in
for a word boundary**, matched by plain substring. `trim TSLA` hits; `trim,` does
not. Every bare `trim`/`cut` followed by a comma, period, exclamation mark or
newline was invisible to the parser while `detect_action`'s `trim(?:med|ming)?\b`
routed it as CLOSE. `_ACTION_RE`, used for sentence scoping, had the identical
`\btrim\s|\bcut\s`.

**Why non-obvious**: the trailing space was deliberate and documented — `"cut "`
exists that way to avoid matching *scout* and *circuit*. It reads like a word
boundary and behaves like one in the middle of a sentence, which is where every
example anyone tried put it. And `test_close_routing_sync.py`, written precisely
to catch routing/parsing drift, could not see it: its templates are
`"{v} NVDA 3.05"` and `"NVDA {v} 3.05"` — the verb is **always followed by a
space**. The test's invariant is also single-directional (parse ⇒ route), while
this bug is the other direction (route ⇒ parse), which the file's own docstring
declares as deliberately not an invariant, since routing is legitimately broader.

The damage propagated across languages: the ZH twin arrived 2s later and parsed
perfectly (`pct=33 price=3.9`), then `_close_is_zh_twin_of_skipped_en` suppressed
it — that guard's premise is "the EN verdict of *not a close* is trustworthy."
Here the EN verdict was a false negative, so a correct parse was discarded. Zero
loss that night only because one contract remained and runner-preserve would have
skipped the trim anyway.

**Defense**: `_BARE_ACTION_VERB_RE = re.compile(r"\b(?:trim|cut)\b")` feeding
`_has_action_verb`, and `\btrim\b|\bcut\b` in `_ACTION_RE`. `\b` still refuses
*scout* and *circuit* (both have a word character before the `c`), so the original
reason for the space survives. Regressions pin the punctuation variants
explicitly — comma, period, bang, newline — because those are the shapes a
space-terminated template can never produce.

**General form**: when a guard is expressed in one notation (substring) but
reasoned about in another (word boundary), the gap shows up only in inputs nobody
writes by hand. Ask what shapes your fixtures structurally cannot generate.

---

## 38. Text you strip to avoid misreading it still has to be read by someone

**Symptom**: three times in eight days, the caller trimmed and adjusted a stop in
the same breath. The trim executed all three times; the stop never did:

```
9/2   trimmed Tesla 2.95, stop is at 2.35 now
9/9   TSLA 3.90 new high of day trim, stop at entry now
9/9   $DELL - 减持一半，止损设置为保本
```

DELL's remaining contract sat at `entry × 0.5 = 1.60` while the caller had said
break-even, 3.20 — $160 of downside the instruction had explicitly removed.

**Why non-obvious**: the system was not ignoring these clauses by accident. It
was **deliberately deleting them**: `ZH_SL_ADJUST_CLAUSE_RE` (added 7/23 after an
AVGO misparse) strips stop-adjustment wording precisely so it is not mistaken for
a close instruction — "止损设在 2.35" contains a price and a position-ish verb and
had caused a wrong sell. Stripping was the correct fix for the wrong-close bug,
and it made the clause *invisible* rather than *handled*. Nothing in the logs says
"a stop instruction was discarded"; the message parses cleanly, the trim executes,
and the review sees a successful close.

**Defense**: `parse_stop_adjust` extracts the clause the stripper was hiding
(absolute price, or break-even anchored on **our** fill, not the caller's),
`close_flow._apply_stop_adjust` records it on the position, and `sl_watcher`
treats `manual_stop` exactly like the TP ratchet — `max(existing, declared)`,
divided by `(1 - sell_slip)` so the fill lands at the declared level rather than
a slip below it. It runs only on messages already routed to CLOSE, so it adds no
new routing surface, and it is independent of whether the trim executed: on 9/9
runner-preserve skipped the trim, and the stop still had to move.

**General form**: "strip it so it can't hurt us" and "handle it" look the same in
a green test suite. Every deliberate deletion is a silent decision to not act;
when the deleted thing is an instruction, write down where its consumer is
supposed to live — even if the answer is "nowhere, yet."

---

## 39. The installer and the thing it installed drifted, and re-running the installer is the rollback

**Symptom**: `com.chengqiu.autotrade.night.plist` has had four
`StartCalendarInterval` entries since 8/29 — 23:11 / 23:15 / 23:20 / 23:25, the
wake-window redundancy from #28. `ops/install.sh`, the script whose header says
"换机器就跑这一条", only ever emitted **one** (23:15). Running the documented
install command silently deletes three quarters of a fix.

**Why non-obvious**: the installer is idempotent *with respect to itself*. Every
check you would think to run after it passes — `plutil -lint` is clean,
`launchctl print` shows the job loaded and enabled, the plist is valid, the
23:15 trigger fires. Nothing anywhere compares the generated artifact to the one
it replaced, because a generator has no memory of what it generated last time
and the live file was the only place the extra three points existed. And the
failure it re-arms does not surface at install time: it surfaces weeks later, on
the one night the Mac happens to be asleep at 23:15, with no causal thread back
to the day someone re-ran a setup script.

This is the same asymmetry as #22 in a different medium. There, a parsed field
had no consumer. Here, a hand-edit had no generator — and the direction of the
silence is worse, because the artifact looks *more* correct than the source.

**Defense**: `emit_plist` now takes a variadic `HH:MM` list plus an optional
extra-env block, and the night job passes all four points; the summary line
prints them so a wrong count is visible at install time. Verification was a
sandboxed dry-run (`LA_DIR`/`APPSUP` redirected, `launchctl` stubbed) diffed
against the live plist — identical except the redirected paths. `ops/README.md`
now says the four points are deliberate and must not be trimmed.

**General form**: the moment you hand-edit something a script generates, the
script has become a loaded rollback. Fold the edit back into the generator in
the same change, or you have written a time bomb whose fuse is the next person
following your own setup instructions.

---

## 40. A script's error reporting cannot see the layer that builds the script

**Symptom**: every morning `morning_collect.sh` printed two lines nobody chased:

```
morning_collect.sh:91: command not found: expiry
morning_collect.sh:91: command not found: WHERE
```

**Why non-obvious**: that an unquoted heredoc runs command substitution is in the
shell manual, and the author clearly knew expansion was live — `\$714` is escaped
by hand a few lines away. The heredoc *has* to stay unquoted; it interpolates
`$TODAY_ET` and `$SINCE_UTC`. What is not in any manual is the second half: this
script had already been hardened on 9/5 to stop swallowing errors. It routes
sqlite3's stderr to a separate file (`DIGEST_ERR`) precisely so that a bad column
name cannot end up written into the digest and read by the review as "no records
of that kind" — and it shouts into `ops.log` if that file is non-empty.

These two errors bypass all of it. They are raised by zsh while **constructing**
the heredoc, before `sqlite3` is executed, so they belong to no redirection the
script set up for the command it was about to run. The mechanism built to catch
exactly this class of failure cannot see it, because it lives inside the process
whose own construction is failing.

The backticks here happened to sit in SQL `--` comments, so the query was still
correct and the digest was fine. That is luck, not design: the same 73 lines
would have executed anything else in backticks just as willingly.

**Defense**: both occurrences escaped as `` \` ``, matching the file's existing
`\$714` idiom. Verified by extracting lines 91-164 into a standalone script and
running the old and new versions side by side: errors gone, SQL output
byte-identical, line count matching that morning's real digest.

**General form**: an error channel you install inside a process cannot observe
that process being built. When a script's diagnostics live in the same file as
the thing being diagnosed, ask which layer they actually cover — and check the
terminal, not just the log the script writes.

---

## 41. A new source's format is not vocabulary drift

**Symptom**: the `ashley` channel was enabled and on its first live night lost
**9 of 9** `:RedAlert:` open signals — 18 messages counting the bilingual twins,
every one of them `[parser] no signal`.

**Why non-obvious**: every previous parser miss in this repo was drift — a
synonym, a punctuation change, a word order swap, a preposition, inside a channel
that was already working (#21, #24, #25, #32, #37). The reflex those built is to
widen a vocabulary. Nothing drifted here. A new channel writes a shape no pattern
family ever covered: bare ticker (`SKHY - $195 CALLS ...`) where all six Pattern B
variants anchor on `\$([A-Z]{1,5})`, while the families that *do* accept a bare
ticker want `195c` glued together (A/C) or an `NDTE` (D).

From the alert side the two are indistinguishable — both are `Parse failed` × N —
but the fixes point in opposite directions, and the wrong one is actively
dangerous. Widening means deleting the `$` anchor from six regexes, and that
anchor is the only thing stopping `[A-Z]{1,5}` under `IGNORECASE` from eating
ordinary English words; the A series already learned this on 7/8 when `but` in
"taking AAPL again but 300p 7/17" became a ticker. Six loops would each need an
`isupper()` check and a stopword table.

The tell is in the numbers: drift is partial (one phrasing of many stops working),
a shape gap is total. 100% loss on exactly one source, 0% on the others, starting
the night that source was switched on.

**Defense**: `_normalize_bare_ticker` rewrites the leading bare ticker to
`$TICKER` at the entry, the same move already used for ZH direction words and
inverted prices — every downstream pattern, guard and expiry rule is reused
verbatim. The shape is pinned hard: start of message, optional `:emoji:`
shortcode, 1-5 uppercase letters **matched without IGNORECASE**, optional dash,
and a mandatory `$digit` lookahead. Stopwords are the second line; the third is
that Pattern B still demands `calls/puts` and a second quoted price, which is
what stops `TODAY - $195 was the pivot` (a corpus row).

**General form**: before widening a matcher, ask whether the source is new. Drift
degrades a working path; a new source arrives with a total outage on one channel
and none anywhere else. Widening is the wrong tool for the second one, and it
spends your false-positive budget to fix a problem you do not have.

---

## 42. The fallback that asks for nothing wins every race it is entered in

**Symptom**: `:RedAlert: INTC - $110 CALLS 10/2 $4.70` parsed cleanly and
produced expiry **9/11** — this Friday — instead of 10/2. No warning, no alert,
no log line. A valid order for the wrong contract.

**Why non-obvious**: Pattern B0 exists and is literally titled "含明确 MM/DD". It
looks like it covers this. It does not: B0 requires the date to appear *before*
the strike, and this channel writes it after `CALLS`. The variant that does cover
it, B3, was last in the chain — and B2, which requires **no date at all**, sat in
front of it.

A pattern with fewer requirements matches a superset of the inputs of one with
more. Put it earlier in an ordered chain and it wins every input they share, so
in a chain like this the order is not a tiebreak between equals, it *is* the
specificity policy. The docstring even listed B3 last, which reads like priority
documentation and was in fact an accurate description of a bug.

And the failure mode is the dangerous one: not a miss, a silent downgrade. The
signal parses, the order fills, the position opens, and the only evidence is an
expiry field nobody diffs against the message. The B0.5 comment in the same
function records this exact shape — "英文月份日期被无视，expiry 悄悄退成 next
Friday" — from a different entry point, months earlier.

**Defense**: B3 moved ahead of B2; the docstring now states the rule ("带日期的
一律排在无日期兜底前面") instead of describing the order. The corpus asserts
`expiry_date` on both INTC rows in both languages, and a reverse invariant pins
that the genuinely date-less message still falls back to this Friday.

**General form**: in any ordered chain of matchers, sort by specificity and write
the rule down next to the chain. The branch that requires nothing must be last —
it is a default, and a default in front of a specific case is a silent downgrade
generator.

---

## 43. One fact, two entrances — you guarded one

**Symptom**: #38 shipped the entire declared-stop pipeline on 9/9: extraction
from the caller's wording, `manual_stop` on the position, a watcher that ratchets
the floor up to it. On 9/10 the new channel's opening messages said
`STOP LOSS AT $4.20` in three of nine signals and **none of it fired**.

**Why non-obvious**: #38 is not incomplete. Every layer it built works and is
tested. What it is not is *entrance*-complete: `parse_stop_adjust` is called from
exactly one place, `close_flow`, because on the channels that existed when it was
written the stop always arrived in a **follow-up** message — "trimmed Tesla 2.95,
stop is at 2.35 now". A new source puts the identical fact in the *opening*
message, which is routed to OPEN and never passes the function that knows how to
read it. The pipeline is not bypassed by a parsing failure; it is simply never
called.

Then it fails a second time, at a layer that also looks complete on its own. The
contract carrying that stop expires 10/2 — DTE 23 — so `categorize` returns
`swing`, `apply_sl=False`, and `sl_watcher`'s watch list never includes it. Even
had the stop been recorded, nothing would read it. Two layers, each correct by
its own contract, each silently dropping the same instruction.

**Defense**: `open_flow._apply_declared_stop` records the stop after
`on_order_filled` (the row has to exist first), and `sl_watcher` grows a third
watch-list branch for any position carrying a `manual_stop`, entered at
`pct=1.0` so it brings **no percentage floor of its own** — the threshold comes
entirely from the number the caller stated. That distinction is the whole scope:
this does not give swings a stop loss (still a money-path decision, ROADMAP P1
§16 and the tail of #31), it executes a stop the caller declared and we dropped.
Both layers have tests, plus the reverse invariant that a swing *without* a
declared stop is still not watched at -99.8%.

**General form**: #22 says a parsed field with no consumer is decoration. Its
sibling: a consumer wired to one entrance is decoration for every other entrance.
When you add a handler for a fact, enumerate the paths that fact can arrive on —
message kinds, channels, sources — rather than the one in front of you, and write
down which ones you are choosing not to cover.

---

# 中文 postmortem 记录（原 src/listener/LESSONS.md 并入）

> 以下为按日期记录的踩坑史，**原样保留**（其中的 `src/...`、`scripts/...`
> 路径指重构前的旧布局；新布局对应关系见文末映射表与 CONTRACT 的
> import 映射）。「重要架构决策」「关键文件索引」「关键命令速查」是
> 活文档，已更新为新布局。

经验教训与 Bug 修复记录
持续更新。每次踩坑后追加，按日期倒序。
目的：避免重复踩坑 + 快速回顾架构决策。

## 2026-06-17（周二 AEST 上午 / ET 周一晚）

### 背景
6/16 ET 实跑发现 QCOM/IREN 两单都 Cannot find ..260619..，0 真单。
表面是 moomoo 合约不存在，根因是 Juneteenth 6/19 休市 → 应前移到 6/18。
顺带挖出 3 个隐藏 Bug。

### 修复清单

**1. Bug A：下单失败仍写 risk DB**

症状：moomoo 返回错误 → record_order() 照样落 daily_orders → 占用每日 10 单额度 + 污染统计。

根因：discord_client.handle_message 没检查 order_result.get("success")。

修复：失败提前 return，TG 发 ❌ Order rejected by broker，DB 不写。

代码：src/listener/discord_client.py（现 autotrade/listener/open_flow.py）

```python
if not order_result.get("success"):
    await notify_error(...)
    return  # 提前退出，不 record_order
```

**2. Bug B：parser 主动 skip 触发 Parse failed 警告**

症状：消息含 holding / into tomorrow / 价格区间 → parser 返回 None → listener 当解析失败发 TG 警告。

根因：None 同时表示「解析失败」和「主动跳过」，语义混淆。

修复：parser 区分两种返回：

- None = 真·解析失败（应警告）
- `{"skip": "..."}` = 主动跳过（静默）

代码：src/parser/signal_parser.py（现 autotrade/parsing/signal_parser.py）

```python
return {"skip": "holding_or_remaining"}  # 主动 skip
return None  # 真失败
```

listener 端：

```python
if signal is None:
    await notify_parse_failed(...)
elif signal.get("skip"):
    return  # 静默
```

**3. Bug C：expiry 用本地日期算，回测会错**

症状：parser 用 date.today() 算 expiry，但消息可能是历史的（回测）或跨时区的（AEST vs ET）。

根因：消息时间戳没传进 parser。

修复：

- parse_signal(text, msg_ts: date = None) 加 msg_ts 参数
- listener 用 message.created_at.astimezone(ET_TZ).date() 提取 ET 日期传入

代码：src/listener/discord_client.py（现 autotrade/listener/router.py）

```python
def _extract_et_date(message) -> date:
    return message.created_at.astimezone(ET_TZ).date()
```

*[docs-fix 注 2026-07-22]：上面是当时的记录；实际 shipped 的
`_extract_et_date` 还带两层防御——FakeMessage 无 created_at 时
fallback 到 `datetime.now(timezone.utc)`，naive datetime 兜底
`replace(tzinfo=utc)`。见 autotrade/listener/router.py 与英文 #4 条目。*

**4. Juneteenth 假日调整（主菜）**

症状：6/16 信号 weekly → 算成 6/19 周五 → moomoo Cannot find ..260619..（6/19 是 Juneteenth 休市，CBOE 不挂这一天的合约）。

根因：parser 不知道美股假日，无脑算下周五。

修复：

- 新建 src/parser/holidays.py（现 autotrade/parsing/holidays.py）：硬编码 2026/2027 期权假日 set + is_trading_day() + adjust_to_trading_day()
- parser 所有 expiry 出口（7 处 + finalize）包 _adjust_expiry()，落到非交易日自动前移
- weekly 周四后 → 视作 0DTE（不算下周五）

```python
US_OPTION_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
}
```

维护提醒：每年 12 月手动加下一年假日（2028 还没写）。

**5. expiry 字符串与 expiry_date 不同步**

症状：holiday 调整后 expiry_date=6/18 但 expiry="6/19" 字段没改 → TG/DB 显示 6/19 实际下单 6/18，对账困惑。

根因：7 个 return 出口都手写 expiry 字符串，调整 expiry_date 后忘了同步。

修复：抽 _finalize_signal() helper，return 前统一用 expiry_date.month/day 重写 expiry。

副作用："7DTE" / "weekly" 字段全变 "M/D" —— 是好事，对账更直观。

```python
def _finalize_signal(sig: dict) -> dict:
    if sig.get("expiry_date"):
        d = sig["expiry_date"]
        sig["expiry"] = f"{d.month}/{d.day}"
    return sig
```

### 验证
- ✅ test_parser_rules.py 22/22 通过
- ✅ 6/19 端到端：SPY 700p 06/19 → log 显示 expiry 2026-06-19 → 2026-06-18 → option_code US.SPY260618P700000 → 下单成功 (order_id=2094607)
- ✅ TG 显示 expiry: 6/18（与实际下单一致）
- ✅ latency 1291ms（正常范围）
- ✅ DB 6/16 污染清理完成（QCOM/IREN failed + AAOI/CRWV DRY_RUN 残留）

### 教训
- None 不能同时表示「失败」和「跳过」。多状态返回用 sentinel dict / Enum。
- 任何时间相关字段都要带时区。消息时间戳必传 message.created_at，不能 date.today()。
- 假日表必须硬编码，第三方库（pandas-market-calendars）依赖太重、版本风险大。
- 错误处理要早 return，不要让失败的状态继续往下流。
- display 字段（字符串）必须从 source-of-truth（date 对象）派生，不要两边手写。
- 每次改 parser/risk 必须重启 listener，没有热加载。

### 已知待办
- smart_expiry 短距离（<2 天）过期日不跨年 → 6/15 信号在 6/17 不会算成 2027-06-15
- 60s 未成交自动撤单
- day trade 关键词带空格的 tag 提取
- _strip_chinese 边界情况 fallback
- 2028 假日表
- 止盈止损策略设计（Q1-Q5）

### 模板（每次踩坑后追加）
每次新增一节用以下结构：

- 背景：一句话说明发生了什么
- 修复清单：每个 Bug 含 症状 / 根因 / 修复 / 代码 四段
- 验证：测试通过的项
- 教训：抽象出的通用规则
- 已知待办：未做完的尾巴

## 时区 bug 历史脏数据（2026-06-18 修复）

### `trades.db.raw_signals.received_at` rowid 6-71（共 66 条）
- **现象**：无 Z 后缀，数字是 AEST 实际时间（不是 UTC）
- **根因**：`discord_client.handle_message` t0 = `datetime.now()` naive，
  `logger_db._utc_iso` 防御性 `dt.replace(tzinfo=utc)` 误当 UTC 贴 Z
- **复盘转换**：值 - 10h = 真 UTC；或值 - 14h = 真 ET（对应 KC 信号时间）
- **修复 commit**：见 fix(tz) commit hash
- **未 migrate 原因**：rowid 6-15 是 test fixture / backtest 数据无价值；
  rowid 16-71 是真信号但只用于复盘，心算 -10h 即可

### `risk.db.daily_orders.ts` rowid ≤ 3（共 3 条）
- **现象**：ET offset 格式 `2026-06-15T11:32:44.885107-04:00`
- **migrate**：已 normalize 为 UTC+Z 标准格式
- **migrate SQL**：`update daily_orders set ts = strftime('%Y-%m-%dT%H:%M:%fZ', datetime(ts)) where ts like '%-04:00'`

### 现行规范
- 所有 DB 时间字段统一 `YYYY-MM-DDTHH:MM:SS.sssZ`（UTC + Z + 毫秒）
- 调用方传入 `datetime` 必须 tz-aware
- `logger_db._utc_iso(dt)` 遇 naive 抛 `ValueError`（不再悄悄当 UTC）

## 重要架构决策（不变量）

这些是踩过坑总结的、不要再改的决策。

- 时区：写入用 UTC+Z naive 防御，展示用 ET，trading_date 永远用 ET 日期。
- 失败提前 return：broker 失败 → 不 record_order，不进风控统计，不污染 daily limit。
- parser 返回三态：dict 成功 / `{"skip": "..."}` 主动跳过（静默不通知）/ None 真失败（TG 警告）。
- expiry 字符串从 expiry_date 派生，禁止手写。统一在 _finalize_signal() 处理。
- 假日 set 硬编码，每年 12 月加下一年。
- 指纹去重 5min 窗口，不含 price/channel。
- 修改 parser/risk 后必须重启 listener（无热加载）。
- moomoo 模拟盘下单条件：DRY_RUN=false + TRD_ENV=SIMULATE 才会真下到模拟盘。
- DRY_RUN 不导出到 broker 模块接口，测试脚本自读 env。
- pip 包名 vs import 名：pip install moomoo-api，但 import moomoo。
- 同步 SDK 异步调用：moomoo SDK 同步，用 asyncio.to_thread() 包装。
- .env 加载用绝对路径 + override=True，避免 IDE / shell 环境变量干扰。
- self-bot 单账号挂载：Client.user 只读，不要尝试双连。
- TG Markdown fallback：含 $ _ 等字符易解析失败，自动 fallback 纯文本。
- 0DTE 限额小仓：DEFAULT_QTY=1，MAX_COST_PER_ORDER=$500，MAX_DAILY_COST=$2000。
- CBOE daily expiry：仅 SPY/QQQ/IWM + 头部股 M-F 每日；中小盘（如 IREN）仅 Friday weekly。

## 关键文件索引（新布局）

```
autotrade/parsing/signal_parser.py   # 4 规则解析 + _finalize_signal
autotrade/parsing/holidays.py        # 2026/2027 假日 set
autotrade/listener/router.py         # 消息路由 + crash barrier + _extract_et_date
autotrade/listener/open_flow.py      # OPEN 编排（Bug A 早 return 在这里）
autotrade/listener/close_flow.py     # CLOSE 编排（strike hint 过滤）
autotrade/broker/trade.py            # 下单（不导出 DRY_RUN）
autotrade/broker/quote.py            # 行情 snapshot / 校验 / probe
autotrade/broker/common.py           # env 常量 + QUOTE_TZ/_quote_epoch
autotrade/risk.py                    # 风控 + UTC+Z 时间戳
autotrade/notify/transport.py        # TG 推送（Markdown fallback）
autotrade/storage/logger_db.py       # SQLite 写入
autotrade/config/channel_loader.py   # channels.json 加载 + validate_channels
config/.env                          # 环境变量
config/channels.json                 # 频道配置
data/risk.db                         # daily_orders + circuit_breaker
data/trades.db                       # raw_signals + orders
logs/app_YYYY-MM-DD.log              # 应用日志
```

## 关键命令速查（新布局）

启动 listener：

```bash
source .venv311/bin/activate && python -m autotrade.app.main
```

后台启动：

```bash
nohup .venv311/bin/python -m autotrade.app.main > logs/listener.out 2>&1 &
echo $! > logs/listener.pid
```

停止：

```bash
pkill -f autotrade.app.main
# 或
kill $(cat logs/listener.pid)
```

防 Mac 睡眠：

```bash
caffeinate -i -d
```

查今日下单数：

```bash
sqlite3 data/risk.db "SELECT COUNT(*), SUM(cost) FROM daily_orders WHERE trading_date='YYYY-MM-DD';"
```

查最近 raw signals：

```bash
sqlite3 data/trades.db "SELECT msg_id, author, substr(content,1,80), received_at FROM raw_signals ORDER BY received_at DESC LIMIT 10;"
```

查最近订单：

```bash
sqlite3 data/trades.db "SELECT id, symbol, side, strike, expiry, success, message FROM orders ORDER BY id DESC LIMIT 10;"
```

重置 daily limit（紧急用）：

```bash
python -m autotrade.ops.reset_daily_limit
```

按 ET 查看今日流水：

```bash
python -m autotrade.ops.show_today
```

端到端测试（⚠️ 强制 DRY_RUN=false，会真下到 SIMULATE）：

```bash
python -m autotrade.diag.diag_handle_message_real
```

## 25. Fixing a miss can arm a landmine that never had a chance to go off

**Symptom**: 8/18. `out the rest of AMZN to secure small green trade ✅ Price has
on just about everything outside memory stocks has been slow and boring.` had
been failing to parse for weeks — the close was silently missed and the position
rode to EOD. The fix was small and obviously right: teach the parser the phrase
`out the rest`.

With that one line, the same message stopped being a miss and became this:

```
{'kind': 'BULK_TRIM', 'symbols': [], 'pct': 100, ...}
```

**Sell every open position at 100%.** The word `everything` — sitting in a clause
of market commentary at the end of the sentence — was in `BULK_MARKERS`, and
`_has_bulk_marker` scanned the whole message. That branch had simply never been
reachable for this message, because parsing failed two steps earlier.

**Why non-obvious**: the dangerous code was not touched, not new, and not wrong
in isolation — `everything` is a perfectly good bulk marker in
`sell everything here`. What changed was *reachability*. A defect that lives
downstream of a failing gate is invisible in production and invisible in tests,
because nothing ever gets far enough to reach it. Fixing the gate is what ships it.

The general shape: **when you fix a parse failure you are not adding one code
path — you are enabling every path downstream of it at once, none of which has
ever run on this input.** The 8/18 message had been exercising exactly one branch
(`return None`) for its entire life.

Practical consequence for this repo: a fix that turns "no signal" into "signal"
on the money path needs its **downstream** asserted, not just its parse result.
The regression for this one asserts `kind == "CLOSE"` and `symbols == ["AMZN"]`,
not merely that the message parses.

**Recurrence, 8/28 — and this one is worse, because the landmine was a *missing
guard*, not a missing branch.** `$ALAB - Scale out.` had been going to the OPEN
parser for weeks: `ACTION_VERBS` and `STRONG_CLOSE_RE` both carried only the
`-ing` form. Adding the imperative was, again, small and obviously right.

The regression that went red was `test_zh_after_clause_is_not_an_instruction`
(8/20 PLTR):

```
I'll be swinging $PLTR $TSLA & a few $LLY runners after I scale out most.
```

On 8/20 the ZH twin of that message sold a real PLTR contract, and the fix was
`ZH_AFTER_CLAUSE_RE`. The EN side was left alone — the postmortem note in this
very file says *"EN 正确判 no signal"*. It was not correct. It was **empty**:
EN reached `return None` because `scale out` was not in the verb table, not
because any guard rejected it. The word-table hole was standing in for a guard
that had never existed, and the note recorded the symptom as the mechanism.

Adding the word removed the hole, and the same sentence became a live sell —
caught only because 8/20's regression test existed. `EN_AFTER_CLAUSE_RE` now
mirrors the ZH guard.

So the shape has a second edge: **a passing test can be passing for a reason
that has nothing to do with the guard you think it is testing.** When you write
"the other language handled this correctly", check *which line* rejected it.
"Nothing matched" and "a guard matched" look identical from the outside and
behave completely differently the day the word table changes. (Compare #24:
there the missing half was a word list; here it was the guard itself, hidden
behind a word list.)

---

## 26. A fill confirmation from the broker can be a number that never existed

**Symptom**: 8/20, 02:57. `US.TSLA260828C470000` submitted at limit 2.97,
`[Risk] cost=$594`. Fifteen seconds later:

```
[fill] buy US.TSLA260828C470000 dealt_avg=0.13 (limit 2.97) → avg_entry 已回填
```

The position's cost basis was overwritten with **0.13** — a 95.6% deviation from
a limit order's limit price, which is arithmetically impossible for a real fill
(a limit buy fills at or below the limit, not at 4% of it). The DB then held a
position that cost $594 and claimed to cost $26, with no stop loss. Every P&L
number derived from that row was wrong, and wrong in the flattering direction.

The corroborating evidence arrived two hours later, on the same contract:

```
04:41  [CLOSE] no price ref for US.TSLA260828C470000, skipping sell (33%)
```

while `PLTR` in the same message got a quote fine (`quote_ref=0.95`). Both
symptoms have one cause: **this contract had no working OPRA quote**, and the
"filled average price" the broker returned for it was garbage rather than an
error.

**Why non-obvious**: the code did check the field — `if dealt > 0`. The trap is
that the bad value was *positive, finite, and plausible-looking in isolation*.
Nothing in the SDK signals "this number is not a price": no error code, no NaN,
no exception. The only way to know 0.13 is wrong is to compare it against
something you already knew — the limit price you submitted.

The general rule: **a value that came back successfully is not a value that is
true.** Any number from an external system that will be written to the money path
needs a sanity band derived from a value you control, not just a null check.
Here the band is `[limit × 0.50, limit × 1.05]` — the upper bound because a limit
buy cannot fill above its limit, the lower bound wide enough to admit genuinely
good fills (8/21: UBER limit 0.50, actual 0.37, −26%, real and correct).

When it fails the band, the right move is **refuse and alert**, not clamp or
accept. Keeping the limit price in the DB is knowingly a little high; accepting
0.13 is knowingly wrong by 20×.

---

## 27. A quoted price belongs to one contract; a sentence can mention three

**Symptom**: 8/28, 02:17. KC posts a war story:

```
got so wrapped up in SPY and AAPL I didn't see that UNG order filled for 1.90
trim as it approached the 10.75 level 😂💰 I have 6 contracts left by the way
```

The parser returned `symbols=['AAPL','UNG'] pct=33 price=1.9`, and one second
later sold a contract:

```
[CLOSE] sell US.AAPL261016C330000 qty=1 limit=1.8 (33%, ref=signal)
```

`1.90` is **UNG's** fill. AAPL 330c was quoted by the same author at **5.80–6.00**
in the surrounding messages. We placed a sell on AAPL at a limit derived from a
different underlying's price, three times below market.

**Why non-obvious**: three independent things all had to be reasonable.

1. `AAPL` really is in the text, and it really is in our open positions — the
   symbol whitelist did its job. It appears in `got so wrapped up in SPY and
   AAPL`, an adverbial aside, not as the object of any verb.
2. `trim` really is an action verb, and the message really is about a trim —
   just one that **already happened, to someone else's order, on a different
   ticker**.
3. `_extract_signal_price` scopes to action sentences (`scope_en`), which is
   more careful than most of the pipeline. It still returns a single float,
   and the caller applies that one float to every symbol in the list.

The third is the structural error and it is easy to miss when reading the code:
`signal_price` is computed once, per message. But a price is a property of **one
option contract**. The data model quietly assumes one message = one contract,
which holds for the overwhelming majority of signals and fails silently and
expensively when it doesn't. The existing test
`test_multi_symbol_close_hint_only_scopes_first_symbol` had encoded the same
hazard since July — `Trimmed $TSLA 420c and $MSFT here @ 7.00` applies TSLA's
$7.00 to MSFT — and asserted it as correct behaviour.

Note also what *did* work: the strike hint was already treated as
non-transferable. `_extract_strike_hint` is deliberately scoped to `symbols[0]`
with a comment explaining that using TSLA's 420c to filter MSFT would silently
skip MSFT. The same reasoning applies verbatim to price, and was not applied.
**A guard written for one attribute is evidence that its siblings need it too.**

**Defense**:

- `_drop_unattributable_price`: more than one symbol + a single quoted price →
  discard the price, fall through to the per-contract quote fallback, set
  `price_unattributable` so `close_flow` sends a TG. The close still executes —
  what is unattributable is the *price*, not the *intent*, and refusing outright
  would silently drop legitimate multi-symbol trims.
- `didn't see / didn't notice / never caught` joins `RECAP_PATTERNS`. A
  first-person statement that the author **was not watching** cannot also be an
  instruction to follow. (First version of this regex matched only the ASCII
  apostrophe and let the verbatim `didn’t` straight through — it passed a
  hand-typed test and failed the real corpus line. The `will|'ll|’ll` pattern
  three lines above had both forms already.)

**General form**: when a parser extracts a scalar from a message and a list of
targets from the same message, ask what happens when the list has length two.
If the scalar is a property of an individual target, the answer is usually that
you have to drop it — guessing which target it belongs to is a coin flip whose
downside is priced in dollars.

---

# Lesson → 回归测试映射表

每条 lesson 对应的自动回归（`tests/`，默认 `make test` 全跑）或活体检查
（`autotrade/diag/`，需要真实外部服务，手动跑）。「无自动回归」的条目
说明防御在哪、为什么测不了。

| # | Lesson | 回归测试 / 活体检查 |
|---|--------|---------------------|
| 1 | SIMULATE 不接受 unlock_trade | 无单测（需真实 OpenD 会话）。防御在 `broker/trade.py::_ensure_unlocked`；活体：`diag_moomoo_real`（下单全链路）＋启动时 `probe_broker` |
| 2 | snapshot 批量一坏全坏 | `test_quote_snapshot.py::test_validate_option_codes_definitely_missing_is_false`、`::test_validate_option_codes_transient_error_fails_open`、`::test_validate_option_codes_found` |
| 3 | OPRA 权限连 option_chain 也挡 | `test_quote_snapshot.py::test_probe_quote_access_chain_no_permission_routes_to_no_perm`、`::test_probe_quote_access_no_permission`；活体：`diag_quote_permission` |
| 4 | update_time 是 naive ET，pandas 当 UTC 解析 | `test_quote_snapshot.py::test_get_last_prices_et_realtime_not_stale`（回归：真实 ET 时间戳不得被 stale 过滤）＋`::test_get_last_prices_stale_filtered`（反向） |
| 5 | MOOMOO_TRD_ENV 大小写敏感 | 无单测（normalize 发生在 `broker/common.py` import 常量处）。防御：`.strip().upper()`；`risk.py::_effective_max_cost_per_order` 同法 |
| 6 | on_disconnect 成对触发 | 无单测（需 gateway 行为）。防御：`app/connection.py` 3s 防抖；观察 log 中 reconnect 是否单行 |
| 7 | Mac WiFi 省电断 TCP | 非代码问题。运维规程：`caffeinate -i` / launchd（见 SETUP.md） |
| 8 | asyncio.run 二次调用炸 module 级 async 原语 | 无专门单测。防御：`notify/transport.py::send_telegram_sync` 纯同步 httpx.Client，与 async 路径零共享 |
| 9 | sqlite3.Row 没有 .get() | `test_positions.py::test_reopen_resets_position_and_refreshes_flags`（覆盖当年出错的 reopen 判定路径） |
| 10 | on_ready 时 channel cache 为空 | 无单测（需真实 gateway）。防御：`config/channel_loader.py::validate_channels` 用 fetch_channel；活体：`diag_verify_channels` |
| 11 | 只按 symbol 匹配 close 会错杀多 strike 持仓 | `test_listener_close.py::test_strike_hint_mismatch_skips_close`、`::test_strike_hint_match_executes_close`、`::test_no_strike_hint_keeps_legacy_symbol_only_behavior`、`::test_wrong_side_close_blocked_end_to_end`、`::test_multi_symbol_close_hint_only_scopes_first_symbol` |
| 12 | IDENTIFY 限流风暴 ~7min 离线 | 无单测（需 gateway 行为）。防御：`app/connection.py` 60s 窗口 storm 告警＋TG cooldown |
| 13 | ceil() 把 1 张持仓的 trim 变全平 | `test_positions.py::test_calc_qty_to_sell_single_contract_runner_preserve`、`::test_calc_qty_to_sell_multi_contract_unchanged`；上层报告口径：`test_listener_close.py::test_runner_preserve_reports_truthfully` |
| 14 | ITM 长仓到期自动行权、本地 DB 变陈旧 | 过期清扫：`test_positions.py::test_sweep_expired_marks_and_excludes`、`::test_sweep_expired_keeps_today_and_future`、`::test_sweep_expired_idempotent`；EOD 强平窗口：`test_watchers.py::test_eod_*`；对账工具：`ops/sync_positions`（手动/开盘前跑） |
| 15 | 卖没持有的期权 = 开裸空 | 无单测（`place_sell_order` 在测试里始终被 mock，`_get_long_qty` 需真实 ctx）。防御：`broker/trade.py::place_sell_order` 提交前查 `_get_long_qty`，不足即拒单 |
| 16 | 全量 re-login 不回放漏掉的消息 | `test_backfill.py::test_backfill_replays_missed`、`::test_backfill_idempotent_against_already_seen`、`::test_backfill_noop_without_disconnect_wall`、`::test_backfill_consumes_wall_timestamp` |
| 17 | history 的 limit 截断掉的是**最新**几条 | `test_overnight_0728.py::test_backfill_truncation_drops_oldest_not_newest`；`test_backfill.py::_FakeChannel` 现按真实语义模拟 `oldest_first`/`limit` |
| 18 | monotonic 在系统睡眠中不走 | `test_overnight_0728.py::test_churn_counts_wall_clock_window`（挂钟记账）、`::test_alive_gap_pulls_backfill_anchor_and_alerts_once`（反向利用:挂钟心跳跳变=睡眠指纹） |
| 19 | 磁盘写满会擦掉它自己造成的故障证据 | `test_overnight_0731.py::test_first_error_alerts_immediately`、`::test_burst_is_throttled_to_one_alert`、`::test_scopes_throttle_independently`、`::test_recovery_notifies_once_with_missed_count`、`::test_alerting_failure_never_escapes`、`::test_healthy_ticks_are_silent`。**磁盘闸门与日志尺寸上限尚未实现**（ROADMAP P1 #10），当前防御只覆盖"告警发得出去"，不覆盖"提前拒绝启动" |
| 20 | 喊价/行权价语序会中途倒过来 | `test_overnight_0731.py::test_inverted_price_strike_now_parses`（EN+ZH 双播）、`::test_inverted_order_across_expiry_forms`、`::test_canonical_order_unaffected`、`::test_swap_guards_reject_non_premium`、`::test_price_levels_broadcast_still_skipped` |
| 21 | 双语孪生同时降级 = 冗余归零 | 语义层：`test_overnight_0803.py::test_bare_out_ticker_routes_and_parses_as_full_close`、`::test_out_forms_keep_their_pct`、`::test_bare_out_does_not_fire_on_prose`（误报护栏）、`::test_conditional_close_is_not_an_instruction`（当晚双语原文）、`::test_negated_conditional_masks_the_main_clause`、`::test_author_holding_skips_whole_message`、`::test_real_close_instructions_still_execute`（反向：真 trim 不受影响）、`::test_out_fraction_routes_and_parses`、`::test_expiry_dates_are_not_fractions`。结构层（不依赖措辞）：`::test_zh_twin_blocked_after_en_twin_skipped`、`::test_en_close_after_zh_skip_still_executes`（方向不对称）、`::test_zh_close_without_prior_en_skip_executes`、`::test_zh_skip_does_not_register_en_marker`。**不变量**：`::test_zh_trim_verb_still_means_trim`（减持 仍是 33，修复不许外溢到 ZH 侧） |
| 22 | tag 被解析/落库/展示 ≠ tag 有行为；write-only 字段是下一个人的陷阱 | 策略层：`test_overnight_0803.py::test_day_trade_forces_eod_close`、`::test_eod_force_matrix_otherwise_unchanged`（整张矩阵，防"新 flag 只改一个分支"复发）、`::test_zero_dte_unchanged`、`::test_open_signal_with_day_trade_still_routes_open`。**消费端**（缺了它前两层全是空转）：`test_watchers.py::test_eod_closes_day_trade_before_its_expiry`（契约翻转，前身断言相反行为）、`::test_eod_skips_future_expiry_without_force_flag`（反向安全属性：在途 swing 不许被碰）、`::test_eod_force_closes_weekly_expiring_today`（expiry 那条独立入选路径不受影响） |
| 23 | 拒单不留状态 = 无限循环；告警通道比日志先被淹 | 熔断：`test_tp_retry_guard.py::test_naked_short_reject_trips_after_one_attempt`（100 轮只打 1 次 broker）、`::test_transient_reject_backs_off_then_trips`、`::test_trip_is_scoped_to_one_contract_and_tier`、`::test_success_clears_backoff_state`。**不变量**：`::test_naked_short_reject_trips_after_one_attempt` 断言 `tp_hits` 保持 0（熔断不许把没落袋的止盈标记成已完成）。告警节流：`::test_naked_short_alert_is_sent_once_with_reconcile_hint`。日志收敛：`::test_log_throttled_*`（4 个）。自动落账三道闸门：`test_0016_reconciler.py::test_auto_close_*`（6 个，含 `::test_auto_close_vetoed_when_broker_returns_empty` —— 空查询不许清空全部活仓）。**幻影仓入口（`confirm_buy_fill` 的静默分支 + 限价/市价偏离闸门）尚未修**，见 ROADMAP P1 #14 |
| 24 | 只修双语管线的一侧 = 没修 | `test_overnight_0814.py::test_zh_still_holding_recap_is_not_a_close`（当晚原文）、`::test_en_twin_stays_correct`（另一侧不许被带坏）、`::test_recap_markers_block_close`（6 个词形）、`::test_holding_recap_is_not_an_open_signal`（开仓路径同批补）。**反向护栏**：`::test_real_close_signals_still_parse`（4 个真指令不许误伤）、`::test_buy_and_hold_phrasing_still_opens`（裸"持有"没进表）。同族前案见 #21 与 `test_overnight_0810.py` |
| 25 | 修一个漏平会踩响一颗从没触发过的雷（变的是可达性） | `test_overnight_0818_0824.py::test_out_the_rest_does_not_become_bulk_trim`（下游断言：必须是 CLOSE `['AMZN']` 而不是 BULK_TRIM 100%）、`::test_bulk_marker_requires_close_verb_object`（4 个 case，真 bulk 不许被误伤）。**不变量**：修 parse 失败时断言的是**下游结果**，不是「能解析了」。**8/28 复发**（补 `scale out` 踩响 8/20 的 EN 侧防护缺口）：`test_overnight_0828.py::test_scale_out_parses_as_close` + `test_overnight_0818_0824.py::test_zh_after_clause_is_not_an_instruction`（EN_AFTER_CLAUSE_RE 就是被它逼出来的）|
| 26 | broker 回来的成交价可以是个从未存在过的数字 | `test_fill_checker.py::test_buy_fill_rejects_absurd_dealt_price`（限价 2.97 / 回报 0.13 → 拒绝回填 + 告警）、`::test_buy_fill_accepts_a_genuinely_good_fill`（反向：UBER 限价 0.50 实成 0.37 必须放过）、`::test_buy_fill_reprices_only_its_own_leg_after_addon`（加仓按腿重算，前身断言的是保守跳过）|
| 27 | 喊价属于**一张合约**，而一句话可以提到三个标的 | `test_overnight_0828.py::test_ung_fill_recap_does_not_close_anything`（当晚原文，两层防护任一生效即不成交）、`::test_multi_symbol_single_price_drops_the_price`（丢价不丢意图 + `price_unattributable` 标志）、`::test_first_person_negated_perception_is_recap`（3 个词形，含 Unicode 撇号）。**反向护栏**：`::test_real_instructions_survive_the_recap_rule`（条件式 see/notice 是真指令）、`::test_single_symbol_price_is_untouched`（单标的不受影响）。契约翻转：`test_listener_close.py::test_multi_symbol_close_hint_only_scopes_first_symbol` 与 `test_0015_executor.py` 的两个混合结局用例现在显式注入报价 —— 前身把「TSLA 的 $7.00 用在 MSFT 上」断言为正确行为 |
| 28 | 唤醒成功 ≠ 机器醒着（定时唤醒只买到一个 idle-sleep 窗口） | 无自动回归（launchd / pmset 是宿主行为，测不了）。防御在 `~/Library/LaunchAgents/com.chengqiu.autotrade.night.plist` 的多个 `StartCalendarInterval`（23:11 落在唤醒窗口内 + 23:15/20/25 兜底），幂等性由 `ops/night_run.sh` 的 `pgrep` 守卫保证。**验证方法**：早上比对 `logs/ops.log` 的 `night_run: starting` 与 23:15，差值 > 1 分钟即复发 |
| 29 | 发言人标签不是 ticker；白名单挡住不代表你有防护 | `test_overnight_0829.py::test_new_relay_prefix_no_longer_leaks_bot_as_ticker`（当晚原文）、`::test_old_format_also_stopped_leaking_kc`（老格式一直在漏）、`::test_relay_prefix_shapes`（4 种形状）。**反向护栏**：`::test_relay_prefix_leaves_real_content_alone`（4 条正文一个字不许动）、`::test_prefixed_close_still_executes`（剥完真指令仍要执行）、`::test_relay_prefix_does_not_affect_open_path`（钉住"只在 close 侧剥"这个决定的前提） |
| 30 | 被动变成 runner 的仓位，继承了为"主动选择的 runner"写的策略 | `test_overnight_0829.py::test_strategy_b_would_have_sold_last_nights_aapl`（当晚两次报价都该全出）、`::test_strategy_b_threshold_boundaries`（无报价/低于阈值/刚过阈值三档）。上游成因见 #27；runner-preserve 本身的契约见 #13 |
| 31 | 规则日志里点名的兜底，不查就不是兜底 | 策略层：`test_overnight_0901.py::test_last_nights_runner_was_naked_below_entry`（契约翻转：前身阈值就是 1.04）、`::test_t1_floor_is_true_breakeven_not_bare_entry`（滑点折算——不折算的"保本止损"会亏掉一个 slip）、`::test_ratchet_locks_one_rung_below_the_highest_hit_tier`（weekly/swing × 档位矩阵，含 T1 熔断跳档）、`::test_ratchet_stays_out_of_the_way`（一档未中/无阶梯类目/坏 entry 一律沉默）。**不变量**：`::test_ratchet_never_loosens_the_stop`（棘轮只抬不放，钉死 `max(旧阈值, 棘轮底)` 这个决定）。**消费端**（缺了它策略层就是空转，见 #22）：`::test_watcher_sells_the_runner_at_the_ratchet_floor`、`::test_ratchet_off_reproduces_last_nights_silence`（开关关掉逐字复现 9/1 那晚）、`::test_ratchet_does_not_touch_a_position_that_never_hit_a_tier`。**swing 仍裸奔**（apply_sl=False 压根不进 watcher）未修，见 ROADMAP P1 §16 |
| 32 | 同一个形状换个介词；抽取管道在、门没开 | 语料：`tests/corpus/2026-09-01.jsonl`（4 条当晚漏平原文 + 2 条反向护栏 + 2 条 holding recap + 当晚唯一开仓）。断言：`test_overnight_0901.py::test_last_nights_missed_trims_now_route_and_parse`（断的是下游 kind/symbols/pct，不是"能解析了"）。**反向护栏**：`::test_commentary_still_routes_to_open`（8 条，含同夜 `确保它保持这样` 与 7/23 的移动止损子句）、`::test_secure_purpose_clause_still_means_full_close_not_a_new_trim`（8/18 AMZN 的 100% 不许退化成 33）。**不变量**：`::test_holding_recap_is_still_not_a_close`（#24 的结论不许被本次外溢）。同族前案：#21 #24 #25 |
| 33 | 无动作可做的告警会把有动作的那条训练成背景色 | `test_overnight_0901.py::test_never_seen_broker_positions_are_foreign_not_drift`（当晚三条 LEAPS 的真 code）、`::test_a_code_we_once_held_is_still_a_real_ghost`（8/13 MU 形状必须继续每轮 WARNING）、`::test_foreign_report_says_it_will_never_be_handled`（文案要说清"不会自动处理"）。**不变量**：`::test_known_codes_omitted_keeps_0016_behaviour_verbatim`（不传参数 = 0016 行为逐字）。契约扩充：`test_0016_reconciler.py::test_auto_close_never_touches_qty_mismatch_or_ghost` 现在两类各放一条 —— 分了类不许让"自动落账一条都不碰"只剩一类在被测 |
| 34 | "确定性"是对未来的断言，而证据只是一条来自过去的字符串 | `test_overnight_0902_0905.py::test_deferred_message_does_not_trip_the_breaker`（契约翻转：同一个 0 长仓不再熔断）、`::test_sell_order_picks_the_deferred_branch_while_buy_in_flight`（走真 `place_sell_order`，不只是措辞）、`::test_pending_buy_is_tracked_until_terminal`、`::test_pending_expires_after_grace`。**不变量**：`::test_refused_message_still_trips_the_breaker`（8/13 MU 那种真脱钩照旧立刻熔断）、`test_tp_retry_guard.py` 全部既有用例不变。**上游**（缺了它豁免窗口永远开着）：`::test_timeout_keeps_the_buy_in_flight`（超时=仍然不知道）、`::test_filled_clears_the_flight`、`::test_dead_order_clears_the_flight` |
| 35 | 求援的那条腿和故障走同一根网线 | `test_overnight_0902_0905.py::test_failed_alert_lands_on_disk`（含单行 TSV 的压平约定——morning_collect 的 awk 依赖它）、`::test_sink_stops_growing_past_the_cap`（7/31 磁盘写满不许重演）。**反向护栏**：`::test_successful_alert_is_not_recorded`、`::test_unconfigured_is_not_recorded`（没配 token 是部署问题，不是送不出去）。摘要侧（shell，无自动回归）：`ops/morning_collect.sh` 的「⛔ 昨晚有 TG 告警没送出去」与「⚠️ 已过期 / 今日到期但仍未平」两节，验证方法是 `zsh -n` + 对着 9/4 那晚的状态跑一遍那段 SQL |
| 36 | 解析器因缺信息而拒绝时，要改的不是它，是持有那条信息的那一层 | `test_symbolless_close_binding.py::test_last_nights_out_half_now_sells`（端到端重放 9/2 原文，断言下了一张卖单）、`::test_binds_to_the_lone_fresh_position_in_that_channel`、`::test_does_not_touch_the_old_swing_of_the_same_symbol`（同 symbol 的陈年 swing 不许被碰——钉合约不是钉 symbol，见 #11）。**四道闸门各一条**：`::test_refuses_when_two_positions_opened_today_in_the_channel`、`::test_refuses_across_channels`、`::test_refuses_when_the_quoted_price_is_a_different_order_of_magnitude`（含正向对照，证明拦下的是价格）、`::test_kill_switch_reproduces_todays_silence`。**不变量**：`::test_parse_close_contract_is_untouched`（主解析器仍返回 None）、`::test_these_must_stay_none`（9 条，含「有 ticker 但不在持仓 = 无仓可平」这条最该防的）|
| 37 | 尾空格不是词边界；而该抓住它的同步测试永远在动词后面打了个空格 | `test_overnight_0909.py::test_last_nights_trim_now_parses`（契约翻转：当晚原文逐字，含 @everyone 前缀与 emoji）、`::test_bare_verb_followed_by_punctuation`（5 个形状：逗号/句号/感叹号/换行/原本就能的祈使式 —— 前四个是空格模板结构上造不出来的）。**反向护栏**：`::test_word_boundary_does_not_widen_into_other_words`（scout / circuit，`"cut "` 加空格的原始理由不许丢）、`::test_the_zh_twin_is_no_longer_collateral_damage`（EN 误判经 zh-twin guard 传染中文这条链）。同族前案：#21 #25 #32 |
| 38 | 为了不误读而抹掉的文本，仍然需要有人去读它 | `test_overnight_0909.py::test_declared_stops_are_extracted`（7 条，含 9/2 与 9/9 的六条双语原文）、`::test_dell_replay_records_breakeven_against_our_own_entry`（端到端重放，保本锚在**我们**的成交均价上）、`::test_sl_uses_the_declared_stop_instead_of_entry_times_half`（消费端：底从 1.60 抬到 3.48，缺了它前两层全是空转，见 #22）。**不变量**：`::test_manual_stop_only_ever_ratchets_up`、`::test_declared_stop_never_loosens_a_tighter_threshold`（同棘轮 #31 的"只抬不放"）。**反向护栏**：`::test_these_carry_no_stop`（4 条，含 `stopped out` 是平仓动词不是设止损）|
| 39 | 生成器与它生成的东西漂移之后，重跑生成器就是回滚 | 无自动回归（launchd/plist 是宿主状态，测不了）。防御在 `ops/install.sh::emit_plist` 的可变触发点列表 + 安装时打印全部点位（数量不对当场看得见）。**验证方法**：`LA_DIR`/`APPSUP` 重定向到沙盒、`launchctl` 打桩跑一遍 install.sh，`plutil -convert xml1` 后与线上 plist diff —— 除重定向的三条路径外必须逐字节相同。`ops/README.md` 记着那 4 个点位是刻意的 |
| 40 | 脚本自己装的错误上报，看不见构造这个脚本的那一层 | 无自动回归（zsh heredoc 构造期行为）。防御是 `ops/morning_collect.sh` 里两处 `` \` `` 转义（与同文件 `\$714` 同一写法）。**验证方法**：把 91-164 行抽成独立脚本，新旧两版对跑 —— 旧版必然打出 `command not found: expiry` / `WHERE`，新版无报错且 SQL 输出逐字节一致 |
| 41 | 新来源的格式不是词表漂移（总丢 vs 部分丢是指纹） | `test_overnight_0910.py::test_dollar_prefixed_shapes_are_untouched`（不变量：老形状一个字不许变）。语料 `tests/corpus/2026-09-09.jsonl` 的 12 条当晚原文（6 信号 × 中英）逐字段断言。**反向护栏**：`bare_uppercase_word_is_not_a_ticker`（`TODAY - $195 …` 会被补成 `$TODAY`，靠 B 系列仍要求 calls/puts + 第二个喊价挡住）、`stopword_head_is_not_a_ticker`、`normalized_bare_ticker_still_needs_a_direction_word`（补 $ 不等于放宽方向词） |
| 42 | 无日期兜底排在带日期的前面 = 静默降级（顺序即语义） | `test_overnight_0910.py::test_explicit_mmdd_after_calls_beats_the_no_date_fallback`（契约翻转：前身返回 9/11）、`::test_next_week_is_a_week_past_this_friday`（同类，另一个入口）。**反向不变量**：`::test_no_date_still_falls_back_to_this_friday`（B3 前移不许把无日期那条带走）、`::test_bare_next_week_does_not_shift_the_expiry`（2 个形状，"持有到下周" 不是 "下周到期"）。语料 4 条 INTC/ARM 中英断 `expiry_date` |
| 43 | 同一个事实有两个入口，你只接了一个（#22 的兄弟命题） | 抽取侧：`test_overnight_0910.py::test_declared_stop_in_the_open_message_is_recorded`、`::test_an_open_message_without_a_stop_records_nothing`（反向：不许凭空造）。**消费端**（缺了它抽取就是空转）：`::test_a_swing_with_a_declared_stop_enters_the_watch_list`。**反向不变量**：`::test_a_swing_without_a_declared_stop_is_still_not_watched`（-99.8% 也不许动 —— 本条改的不是"给 swing 加止损"）|
| 回补幂等（跨进程） | 重启后 `_seen` 清零，靠 `raw_signals` 水位线 | `test_overnight_0728.py::test_backfill_skips_messages_already_processed_last_run`、`::test_backfill_keeps_anchor_when_fetch_fails`、`::test_backfill_consumes_anchor_on_success`、`::test_backfill_keeps_anchor_moved_by_a_second_sleep` |
| OPEN 年龄闸门 | 陈旧重放不下单、且不污染指纹表 | `test_overnight_0728.py::test_stale_open_signal_alerts_instead_of_ordering`、`::test_stale_open_does_not_mute_live_resend`、`::test_stale_open_bilingual_twins_alert_once`、`::test_fresh_open_signal_still_orders`、`::test_no_created_at_treated_as_realtime` |
| 中文 Bug A | 下单失败仍写 risk DB | close 侧：`test_listener_close.py::test_broker_reject_does_not_report_no_matching`；open 侧防御是 open_flow 的早 return 语句顺序（record_order 只在 success 后），由 `test_folded_full_flow.py` 全链路间接覆盖 |
| 中文 Bug B | skip 与失败语义混淆 | `test_folded_skip_returns_dict.py`（全部 4 个 case） |
| 中文 Bug C | expiry 用本地日期算 | `test_folded_qcom_replay.py`（历史 msg_ts 回放）；`test_parser.py` 中带 msg_ts 的用例 |
| 中文 #4 | Juneteenth 假日前移 | `test_folded_holidays.py`（5 case）、`test_folded_qcom_replay.py::test_qcom_replay_expiry_moves_off_juneteenth`、`::test_iren_replay_expiry_moves_off_juneteenth`、`::test_spy_weekly_non_holiday_friday_not_shifted` |
| 中文 #5 | expiry 字符串与 expiry_date 不同步 | `test_folded_parser_rules.py::test_parser_rule_sample`（parametrize 全样本，expiry 断言全部走 _finalize_signal 后的 M/D 口径） |
| 指纹去重不变量 | 5min 窗口、不含 price/channel | `test_folded_fingerprint.py`（7 case，含跨频道、价格修正同指纹、窗口过期释放） |

---

## Format guidelines for adding new lessons

Keep entries focused on **gotchas that weren't documented or
discoverable from the API surface alone**. Things like "we made a
typo" or "we forgot to handle X" don't belong here — those are just
bugs. The bar is: would a competent developer reading the official
docs have known to defend against this? If yes, skip the entry.
