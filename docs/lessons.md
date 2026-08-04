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

**Defense**: `eod_force = (dte == 0) or ("day_trade" in tags)` in
[policy/positions.py](../autotrade/policy/positions.py), returned from **all**
branches — the three non-0DTE returns previously hardcoded `False`, which was
equivalent when `dte == 0` was the only source and became the actual bug the
moment it wasn't. Regression pins the whole matrix, not just the new row, so the
next flag added to `eod_force` cannot quietly stop at the `weekly` branch again.

General form: when adding a tag, write down its consumer in the same commit. A
tag with zero behavioral consumers should fail review, not ship as documentation.

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
| 22 | tag 被解析/落库/展示 ≠ tag 有行为 | `test_overnight_0803.py::test_day_trade_forces_eod_close`、`::test_eod_force_matrix_otherwise_unchanged`（整张矩阵，防"新 flag 只改一个分支"复发）、`::test_zero_dte_unchanged`、`::test_open_signal_with_day_trade_still_routes_open`（tag 解析口径不变） |
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
