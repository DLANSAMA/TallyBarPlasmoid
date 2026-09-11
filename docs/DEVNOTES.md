# DEVNOTES — TallyBar developer notes

Full rationale, incident history, measurements, and tuning chronicles for the
invariants listed in `CLAUDE.md`. Sections are referenced by name from CLAUDE.md.

---

## Orchestration (build_snapshot)

All provider fetches run concurrently inside a single `asyncio.TaskGroup`, itself bounded by `asyncio.timeout(args.timeout + 1.0)`. Results are read only via `.done() and not .cancelled()` guards after the group exits — a timeout on one provider never blocks the snapshot. Cookie collection runs first (separately, with its own `wait_for`) because most remote providers need the decrypted jar.

**Cost enrichment is compute-concurrently / apply-after, double-bounded — don't "simplify" it.** The cost scan (parsing `~/.claude`, `~/.codex`, `~/.gemini` logs + the Antigravity ledger SQLite DBs) is the slowest part of a refresh and is **provider-result-INDEPENDENT** (the `tier` arg is a dead passthrough in `token_summary`). So `build_snapshot` **kicks it off at the very top** — `compute_local_cost_summaries(cost_deadline)` on a `to_daemon_thread` under `asyncio.wait_for(timeout=args.timeout)`, captured as the `cost_scan` future — so its CPU/disk work **overlaps the network provider fetches** instead of running serially after them. It reclaimed ~1 s (2026-06-02). `compute_local_cost_summaries` returns a **separate** `{provider: summary}` map and **never touches `providers`**, so there is no torn-write to roll back and **no deepcopy is needed** (the old design deep-copied `providers`, ran `apply_local_cost_summaries` on the copy, and merged back — that's gone). After the provider TaskGroup finalises, `await cost_scan` + `apply_cost_summaries(providers, summaries)` runs **only after the await returns cleanly** — propagating the Antigravity tier to Gemini and attaching each summary — so a timeout (caught → `diagnostics["cost_summary_timeout"]`) skips the apply entirely and leaves the snapshot exactly as the providers produced it. Beneath the outer `wait_for` is a **second, cooperative** timeout: a wall-clock `cost_deadline = time.time() + args.timeout` threaded through `compute_local_cost_summaries(deadline) → antigravity_ledger_cost_summary(deadline=…) → update_antigravity_token_ledger(now, deadline)`, where the per-DB scan loop does `if deadline and time.time() > deadline: break` — so a huge `~/.gemini/antigravity*/conversations` set abandons mid-scan (saving partial ledger progress) instead of being hard-killed. `apply_local_cost_summaries` is kept as a thin `compute`+`apply` wrapper for direct callers/tests. Moving the apply back before the fetches, collapsing the two halves, or dropping the `deadline` param (it defaults to `None`, so tests still pass) silently reintroduces the serial-hang / torn-snapshot / multi-second-hang bugs. Tests patch the `backend.compute_local_cost_summaries` seam (not `apply_local_cost_summaries`).

The `await cost_scan` guard catches `(TimeoutError, asyncio.TimeoutError)` → `cost_summary_timeout` diagnostic, **re-raises `CancelledError`/`KeyboardInterrupt`/`SystemExit`, and catches `BaseException` (not just `Exception`)** — because `to_daemon_thread` faithfully sets a `BaseException` raised in the scan thread onto the future, and a bare `except Exception` would let it escape and crash `build_snapshot` (no JSON printed). As a final backstop, `main()` wraps `asyncio.run(build_snapshot(...))` in `except BaseException` and, on any fatal, prints the last cached snapshot (or a minimal degraded one) — the widget `JSON.parse`s stdout, so the backend must ALWAYS emit parseable JSON even when the orchestrator dies (2026-06-03 audit fix).

**Provider quirks encoded in the orchestrator:**

- **Antigravity** has *two* sources — `run_antigravity_local` (queries the local language-server process found by scanning `/proc`) and `run_antigravity_remote` (OAuth → Cloud Code API). `choose_antigravity_result` merges them, preferring an `ok` local result.
- **Codex** also has two: `run_codex_rpc` (spawns the `codex` CLI as a JSON-RPC subprocess) and a cookie-based fallback (`run_openai_cookie_api`) used only if the RPC path isn't `ok`.
- **Google One credits** (`apply_google_one_credits`) deliberately *replaces* Antigravity's misleading Code Assist prompt/flow "credits" with the real Ultra/Pro AI credit pool from one.google.com, and drops the bar entirely when offline rather than showing wrong numbers. Don't "fix" this by restoring the plan-status figure.
- **Gemini/Antigravity tier**: the Google AI subscription tier (Ultra/Pro/Free) is never hardcoded — only Antigravity's `GetUserStatus` exposes the real name, and `apply_local_cost_summaries` propagates it to Gemini.

---

## Antigravity token capture

Cost numbers come from a single ledger (`providers/cost.py`, `update_antigravity_token_ledger`) fed by three sources:

1. **Language-server RPC (primary, authoritative, format-stable)** — `antigravity.collect_antigravity_rpc_usage` queries **every** running Antigravity language server (Desktop *and* IDE each spawn their own — see `find_antigravity_processes`) over Connect-RPC: `GetAllCascadeTrajectories` lists conversations loaded in LS memory, then `GetCascadeTrajectoryGeneratorMetadata(cascadeId=<conv UUID>)` returns each generation's `usage` (`inputTokens`/`cacheReadTokens`/`outputTokens`) + `apiProvider` + a `MODEL_PLACEHOLDER_M<N>` model id (enum `== 1000 + N`). This reuses CSRF/port discovery from `run_antigravity_local`. Each generation is keyed `<cascadeId>#<stepIndex>`, dated by its real `createdAt`, priced per-model via `_rpc_model_pricing_key`. **The RPC supersedes the disk scan for any conversation it covers** — its legacy `<cascadeId>:<int>` disk entries are dropped so the two never double-count. Limitation: the RPC only sees conversations *currently loaded in LS memory*.

2. **Trajectory DBs (disk fallback)** — `_pb_find_usage` scans `~/.gemini/antigravity{,-ide}/conversations/*.db` (plaintext SQLite) for conversations the RPC didn't cover. Reads `steps.metadata` protobuf usage records (`field 6 == 24`, token fields 2/3/5/9/10, model enum in field 1). **The gate must NOT pin field 1** — it's the model enum (1016=Pro, 1133=Flash, 1026=Claude Opus, 330/1050=utility); an old `field1==1016` gate dropped ~60% of usage. ⚠ **Antigravity 2.0 (2026-05-29) moved the usage record out of `steps.metadata` into the `gen_metadata` table (path `.1.4`) and flipped the marker `field 6` 24→26 for the new `claude-opus-4-6` model (enum 1026).** The disk scan still only reads `steps.metadata`, so it now captures only the Gemini records on disk; Claude/other new-format generations are captured via the RPC instead. Captured entries: `{d, u, c, o, t, x, me}`.

3. **CLI (`agy`)** — `integrations/antigravity_cli/cli_statusline_capture.py` reads the JSON the gemini CLI pipes to its `statusLine.command` on stdin and writes `~/.tallybar/antigravity_cli_usage.json` keyed `cli:<session_id>`. Wire via `statusLine.command` in `~/.gemini/antigravity-cli/settings.json`. CLI entries carry `model` as a display-name string. (A `google-antigravity` SDK hook was removed — don't re-add unless asked.)

`antigravity_ledger_cost_summary` unions all sources (key namespaces are disjoint: `<cascadeId>#<idx>` RPC, `<db>:<idx>` disk, `cli:<session>`) and prices per entry via `_prices_for`. For **RPC** entries (which carry an `apiProvider`) unknown placeholders fall back by provider family (Claude/GPT/Gemini) via `_rpc_model_pricing_key`, never silently to Gemini Pro. **Disk** entries with an enum *not* in `_MODEL_ENUM_NAMES` (the `330`/`1050` utility enums) carry no provider hint, so they do fall back to the Gemini 3.1 Pro default — low volume.

**RPC per-generation key:** uses `stepIndices[0]` else `g<gen_index>` (the generation's stable list position, `g`-prefixed so it can't collide with an integer `stepIndices[0]`). NOT `len(records)` — which shifted across polls (double-count) and collided with `stepIndices[0]==0` (silent drop).

**Date stamping:** RPC entries use the generation's real `createdAt`, read from **`gen.chatModel.chatStartMetadata.createdAt`** (it is nested under `chatModel`, NOT on `gen` directly — an earlier path read `gen.chatStartMetadata` and always got `""`, so every RPC generation silently fell back to today). It's a **UTC** instant with nanosecond precision; `_rpc_local_date` trims it to microseconds and **localises** before bucketing — naive `[:10]` slicing misdates near-midnight usage by a day (a gen at 17:30 local can carry a UTC `…T00:30Z` stamp on the *next* date). Disk entries use first-seen date (a first-time backlog scan stamps everything "today", then normalises). Re-scans never double-count (keyed per generation).

**Ledger resilience:** `_save_antigravity_ledger` mirrors every good write to a `.bak`, and `_load_antigravity_ledger` recovers from that `.bak` (quarantining a corrupt main as `.corrupt`) instead of falling through to an empty ledger — a transient read failure that resets the ledger makes the next scan re-stamp the *entire* history onto today (this happened 2026-05-29; recovered by re-dating from live RPC `createdAt` + a pre-reset ledger backup). Guarded in `tests/test_ledger_resilience.py`.

**Hourly buckets (Day view) + `last7Days`.** Every cost summary carries `hourlyTokenUsage` — 24 buckets for **today**, keyed by local hour 0–23 — feeding the popout's Day tab. The local-log path buckets each of today's records by `timestamp.hour` (already local). The Antigravity ledger persists a per-entry `h` (local hour) on RPC entries, read from the same `createdAt` via `_rpc_local_hour`; today's **hour-less** entries (disk first-seen, CLI, or RPC generations captured before `h` existed and since unloaded) are pooled into the **current** hour by a residual fold, so the Day bars always sum to "Today". `last7Days` is the sum of the daily/weekly buckets — equals the Week graph's bar total. Regression-guarded in `tests/test_accounting.py`.

**Antigravity model rotation (2026-05-28):** Google rotated Gemini 3.1 → 3.5 server-side. `Gemini 3.1 Pro` is retired — a CLI/IDE pinned to it errors `NOT_FOUND`/`INVALID_ARGUMENT`; only `Gemini 3.5 Flash (Medium|Low)` actually generates. The new `gemini-3.5-flash` key is in the LiteLLM catalog — only unmapped enums/names fall back to 3.1 Pro pricing.

---

## Token semantics

**Token VOLUME = `u + c + o` (incl. cached, industry standard); COST bills cache at discounted rate.**

The displayed token total is `tok = u + c + o` (uncached input + cached re-read + output), matching how ccusage and the OpenAI/Anthropic/Gemini dashboards count "total tokens". `c` (RPC `cacheReadTokens` / disk field 5) is the cumulative context re-read on every step, so this runs large for cache-heavy use (~86% of the raw total) — but it is what was metered. Don't "fix" it by excluding cache (an earlier `tok = u + o` attempt was reverted). **The number to compare on is the COST, not the token count** — `cost_of` / `usage_cost_usd` bill uncached input @ input rate, output @ output rate, and **cached @ the *discounted* cache-read rate** (Gemini ~25% / Anthropic ~10%) — the standard pay-per-use calc. Regression-guarded: `test_summary_includes_cli_tokens` = `u+c+o`; `test_cache_counted_in_volume_and_billed`.

**Ledger `o = t + x` (total output = thinking + response), bill once.** `cost_of` bills `u·input + c·cache + o·output`; the old `o + t` (+ `x` at input rate) double-counted thinking and re-billed the response.

**`usage_cost_usd` local-log shapes are provider-specific and verified:**
- **Anthropic:** `cache_creation`/`cache_read` reported SEPARATELY from `input_tokens` (additive — never subtract them off input).
- **OpenAI/Codex:** `cached_input_tokens` ⊂ `input_tokens` and `reasoning_output_tokens` ⊂ `output_tokens` (subsets).
- **Gemini:** `thoughts` + `tool` reported SEPARATELY (`total = input + output + thoughts + tool`). The `tool` add is NOT double-counting — a 2026-05-28 audit wrongly flagged it; the real bugs (fixed) were an Anthropic cache subtraction and a Gemini `output − thoughts` clamp. Regression-guarded: flat $10/MTok → Claude $1.75, Codex/Gemini $1.10.

**Anthropic 1h cache writes = 2× input (NOT 1.25×).** Anthropic bills cache writes at two rates: 5-minute cache = 1.25× input (`cache_write`), 1-hour cache = 2× input (`cache_write_1h`). Claude Code uses 1h caching pervasively (~56% of cache-write tokens in a heavy Claude Code workload), so billing all at the flat rate undercounted Claude cost by ~8.7%. `usage_cost_usd` now splits `cache_creation` by the on-disk `ephemeral_1h/5m` breakdown. `cache_write_1h` comes from LiteLLM's `cache_creation_input_token_cost_above_1hr`. **The live LiteLLM catalog doesn't populate that field for every model (e.g. `claude-sonnet-4-5` omits it), so `_litellm_to_tallybar` SYNTHESIZES `cache_write_1h = 2× input` for any `anthropic` model missing it (2026-06-03)** — otherwise the live-catalog path silently re-opens the undercount. Guarded: `test_usage_cost_usd_anthropic_1h_cache_write` + `test_pricing_data.py`.

**Claude dedup keeps FINAL (max-`output_tokens`) streamed line per `requestId`.** Claude Code writes ONE jsonl line per content block; same `requestId`, CONSTANT input/cache, but `output_tokens` is a GROWING snapshot. The old first-wins dedup billed a partial output (~20% undercount). The loop is now two-pass: fold each `requestId` to its largest-output entry. Guarded: `test_local_claude_summary_uses_final_streamed_output`. Together with the 1h-cache fix, this corrected 30-day Claude cost by +13%.

**`pricing_data.get_pricing` resolution order:** exact-key-first, then longest `pattern in name` match, then longest `name in pattern`. NOT the old "first bidirectional substring hit", which let a short key (`gpt-5`) resolve to a longer one (`gpt-5.5`) and mis-bill ~2×. Unknown non-empty model → provider-family default (not $0); `model=None`/`""` → $0. Guarded: `test_pricing_data.py`.

**Enums `330`/`1050` are unmapped (disk path).** Utility models absent from `_MODEL_ENUM_NAMES`; they hit the Gemini-Pro fallback. RPC-captured generations don't hit this. Low volume.

**Disk Claude gap.** Claude (enum 1026) usage in conversations NOT currently loaded in a language server is missed by the disk fallback (it reads only `steps.metadata`). A future enhancement could read `gen_metadata.1.4` on disk — but that duplicates the Gemini records `steps.metadata` already has, so it needs careful dedup/migration against the existing ledger (don't add it naively).

---

## Parse cache

`local_claude_token_summary` / `local_codex_token_summary` used to re-`json.loads` every line of every jsonl on every 5-min refresh (a multi-hundred-MB log set) — measured **2.06 s + 0.69 s**, ~70% of a ~5.4 s refresh, all in the serial post-TaskGroup cost scan. (Threads don't help: `json.loads` is GIL-bound — verified 0% gain. mtime-skip doesn't help either: an active user touches every file inside 30 days.) The fix: extract each file's usage records **once**, keyed `(path, mtime_ns, size)` in `_cached_log_records`, and reuse while the file is unchanged — only grown/new files reparse. Warm refresh measured **0.79 s / 0.13 s** (≈5.4 s → ≈3.6 s overall).

**Invariants:**
1. The cache is a **pure function of the files' bytes** — `_parse_{claude,codex}_file` stores all-time records with **no clock/window baked in**; the summarizer re-applies the 30-day/today/hour window + the Claude requestId-final-output dedup each call, in `rglob` order, so the result is byte-identical to a fresh parse (proven on a static multi-hundred-MB copy: `fresh == cold == warm`).
2. **Regenerable, never authoritative** — a missing/corrupt/older-`version` cache silently triggers a full reparse (`_load_parse_cache` → `{}`); writes are atomic 0600 + fsync.
3. **Bump `_CLAUDE_PARSE_VERSION` / `_CODEX_PARSE_VERSION` whenever you change a `_parse_*_file` output shape** — else stale cached records desync the cost.
4. Production (default log dir) caches; a caller-supplied `projects_dir`/`sessions_dir` defaults to no cache (so tests stay hermetic) — pass an explicit `cache_dir` to exercise it.

Guarded: `tests/test_accounting.py::test_parse_cache_*`.

---

## Cost popout history

The Cost row opens the token-usage graph (**Day / Week / Month** tabs) as a **genuinely separate floating window beside the widget** — `costPopout`, a `PlasmaCore.PopupPlasmaWindow` in `FullRepresentation.qml`. This is the desired macOS-submenu look, confirmed working on a Wayland (KWin) session.

**Why separate, not inline:** An earlier inline/grow-wider version looked like "one big tacky widget" and was explicitly rejected. `implicitWidth` must stay `contentWidth` — the main widget never grows.

**Anchoring is the whole trick.** The popup's `visualParent` is `flyoutAnchor`, an invisible 1×1 item pinned to the widget's **left edge** (`anchors.left: parent.left`), with `popupDirection: Qt.LeftEdge`. Anchoring to the widget's *outer edge* (not to an interior item like the Cost row) is what makes KWin place it cleanly **beside** the body instead of on top of it. Earlier attempts that anchored to the Cost row, or to the right edge, landed the popup *over* the body — that's the bug that made prior sessions wrongly conclude "separate windows can't be placed on Wayland."

**Vertical position:** `flyoutAnchor.anchors.verticalCenterOffset: 160` drops the card down so its center lines up with the Cost row. Tune this one number if the alignment drifts.

`floating: true`, `animated: true`, `margin: drawerGap` give it its own glass background + drop shadow + a gap from the body. `onActiveChanged → costDrawerOpen = false` closes it on focus-out (flyout behaviour).

**Colors:** fixed light-on-dark palette (`primaryTextColor`, `mutedTextColor`, etc.) — never `PlasmaCore.Theme` / `Kirigami.Theme`, which don't exist in Plasma 6 / throw silent TypeErrors against the dark glass.

**Month tab calendar width LOCKED at `fillFraction: 0.96`.** The 7×6 day grid sizes cells to `widthFill * fillFraction`. `1.0` = edge-to-edge (rejected as "a smidge too wide"); square/`min()`-based sizing (the old default) was rejected as too narrow with empty side gaps. Adjust only via `fillFraction`, re-verify live.

**Tabs default to Week.** `toggleCostPopout` resets `costGraphMode` to `"week"` on every open. Day renders 24 thin hourly bars (`hourBase`) from `hourlyTokenUsage`; Week renders 7 per-day bars (`barBase`) from `weeklyTokenUsage`; Month is the calendar from `monthlyTokenUsage`. Both bar rows are anchored top *and* bottom so the tallest bar reaches the top (pinning to bottom only left a dead band at the top, which was rejected). Adding the footer's "Last 7 days" row bumped `drawerHeight` 336 → 360.

**Hover tooltips:** Day/Week/Month bars each carry a `MouseArea` (`acceptedButtons: Qt.NoButton`) driving a shared `chartTip` overlay in `chartArea`. `chartTipTimer` gates reveal with ~700ms hover-intent delay; `chartTipHideTimer` (50ms) debounces the hide so the tip doesn't flicker off/on when the cursor crosses the gap between adjacent bars — don't revert `onExited` to an immediate `tipShown = false`. The tip fades + scales in via `Behavior on opacity`/`scale`.

**Tip is TWO compact lines** (date on top, `N tok · $cost` below — `\n` in the `*TooltipText` fns, `lineHeight 1.25` + `horizontalAlignment: AlignHCenter`) specifically so the box stays narrow.

**`chartTip.x` is backstop-clamped** to the popout window (`Math.max(-10, Math.min(chartArea.width - width + 10, follow))`) — the popout is a separate window, so an un-clamped wide tip gets cut off at the window edge. The narrow two-line tip is what keeps the clamp from almost ever engaging (an earlier wide-tip-with-chart-bounds-clamp felt "stuck/centered" — avoid that combination).

---

## Incidents & audits

**2026-05-29 — Ledger collapse.** A transient load failure returned an empty ledger; the next scan re-stamped the entire generation history onto today, collapsing the per-day graph to a single giant bar. Recovered by re-dating from live RPC `createdAt` + a pre-reset `.bak`. Fix: the `.bak` mirror + recovery path described in CLAUDE.md and `tests/test_ledger_resilience.py`.

**2026-05-28 — `_pb_find_usage` gate.** The old `field1==1016` gate silently dropped ~60% of usage (all non-Pro-enum models). Removed; gate is now model-agnostic. Same date: Antigravity 2.0 moved usage records in `gen_metadata`; RPC became primary.

**2026-05-28 — Gemini `tool` false audit.** An audit wrongly flagged the Gemini `tool` add in `usage_cost_usd` as double-counting. It is correct. The real bugs fixed that day were an Anthropic cache subtraction and a Gemini `output − thoughts` clamp.

**2026-05-30 — Pricing resolution order.** The old bidirectional substring hit let `gpt-5` resolve to `gpt-5.5` and mis-bill ~2×. Fixed to exact-key-first resolution.

**2026-05-31 — Claude cost undercounts (+13%).** (1) Anthropic 1h cache writes were billed at 1.25× instead of 2×. (2) Claude dedup kept the first streamed line (partial output) instead of the final. Together these corrected the 30-day Claude cost by +13%.

**2026-06-01 — Daemon threads.** `asyncio.to_thread` (non-daemon ThreadPoolExecutor) stalled pytest exit when providers timed out. Replaced with `io_helpers.to_daemon_thread` throughout. Audit by external tool revealed the hang.

**2026-06-02 — Cost-scan concurrency.** Cost scan was running serially after the provider TaskGroup (~1 s of dead time). Moved to overlap with network fetches; overall refresh ~5.4 s → ~3.6 s. Parse caches added same day.

**2026-06-03 — LiteLLM synthesis + BaseException.** (1) Live LiteLLM catalog omits `cache_creation_input_token_cost_above_1hr` for some Claude models; `_litellm_to_tallybar` now synthesizes `cache_write_1h = 2× input` for missing Anthropic entries. (2) `main()` upgraded from `except Exception` to `except BaseException` to catch daemon-thread failures surfaced by `to_daemon_thread`.

**Audit-hardening invariants (2026-05-30) — full list:**
1. `pricing_data.get_pricing` exact-key-first resolution. → `test_pricing_data.py`.
2. `usage_cost_usd` falls back to provider-family default for non-empty unknown model (not $0). `model=None`/`""` → $0.
3. `backend.main` only overwrites `last_snapshot.json` when snapshot has live data (`_snapshot_has_live_data`: any provider `ok`/`cookies-ready`) OR existing cache is itself degraded.
4. Antigravity RPC key: `stepIndices[0]` else `g<gen_index>` (NOT `len(records)`).
5. Provider error messages routed through `scrub_credentials`; atomic writers `_fsync_dir` the parent after `os.replace`; crypto decrypt methods validate key/iv/nonce/tag lengths; CLI shim raw-payload dump gated behind `TALLYBAR_DEBUG`, written 0600. Cost-scan deadline breaks the OUTER per-DB loop. → `test_deadline_break_skips_remaining_db_connects`.

---

## Frontend detail

**State ownership.** `main.qml` (`PlasmoidItem`) owns: `telemetry` (parsed snapshot), `selectedProvider`, `loading`, error strings, and three `DataSource`s:
- Refresh: runs `backend.py --once` on the Timer interval.
- Config-write: runs `backend.py --set-refresh-interval`; separate so it doesn't clobber a concurrent refresh.
- `cacheLoader`: reads `backend.py --last-snapshot` on `Component.onCompleted` for instant cold-start paint.

**`liveLoaded` flag** prevents a slower cache read from clobbering a fresh fetch that wins the race.

**`configSaving` flag + `configWatchdog`.** Writing the refresh interval is its own DataSource. A periodic refresh started just before that write completes carries the *old* config. So `onNewData` preserves the in-flight config: `if (root.configSaving && root.telemetry && root.telemetry.config) parsed.config = root.telemetry.config;`. `configSaving` is set on save and cleared in the config DataSource's `onNewData` (which also surfaces `configError`). `configWatchdog` (~25s) force-clears `configSaving` and drops the hung source if `onNewData` never fires (SIGKILLed/stuck backend). Don't drop the `configSaving` branch — it's load-bearing for the race.

**`metricsBodyHeight()` context.** `popupFitHeight` is computed deterministically from the data (NOT from `implicitHeight`, which reads 0 until Repeater delegates instantiate asynchronously — that race once collapsed the popup to a tiny scroller). Each term in `metricsBodyHeight()` is a hardcoded copy of a section's `Layout.preferredHeight`. Adding the "Last 7 days" row bumped the cost section `86/106 → 107/127` and forgetting to mirror it produced exactly the scrollbar bug.

**Bar colour decision.** Usage bars stay the provider accent colour at every percentage. The fill *width* encodes how full a limit is; there is intentionally NO warning/critical recolour to amber/red. Removed from `compactFillColor` (panel icon), `tabUsageColor` (tab session preview), and the expanded metric-bar fill. The warning *pulse* (opacity throb) is kept. `warning`/`critical` still drive that pulse and the attention badge, just not bar colour.
