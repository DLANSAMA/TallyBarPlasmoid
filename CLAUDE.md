# CLAUDE.md

Guidance for Claude Code working in this repo. Full rationale, incident history, and
measurements: [`docs/DEVNOTES.md`](docs/DEVNOTES.md).

## What this is

TallyBar is a **KDE Plasma 6 widget (Plasmoid)** monitoring AI session usage (Codex/OpenAI, Gemini, Claude, Antigravity). Two halves:
- **QML frontend** (`io.github.dlansama.tallybar/contents/ui/`) — Plasma applet UI.
- **Python backend** (`io.github.dlansama.tallybar/contents/code/`) — one-shot CLI, prints JSON, exits.

Communication is a process boundary: QML's `Plasma5Support.DataSource` runs `python3 backend.py --once ...`, captures stdout, `JSON.parse`s it. No daemon. (`-B` intentionally omitted — `__pycache__` speeds re-runs.)

## Commands

```bash
# Run tests (uses .venv with pytest + pytest-asyncio)
.venv/bin/pytest tests/
.venv/bin/pytest tests/test_backend.py::test_sqlite_copy   # single test

# Lint (pyflakes only, matches CI) — keep the tree F-clean
.venv/bin/python -m flake8 --select=F io.github.dlansama.tallybar/contents/code/ tests/ integrations/

# Exercise the backend exactly as the widget does
python3 io.github.dlansama.tallybar/contents/code/backend.py --once --timeout 12 --background --pretty
python3 io.github.dlansama.tallybar/contents/code/backend.py --once --no-network --pretty   # skip remote API calls

# Package, install, upgrade, remove the plasmoid (kpackagetool6)
make build      # produces io.github.dlansama.tallybar.plasmoid (tarball)
make install    # first install
make upgrade    # reinstall after changes — use this during dev
make remove
```

Runtime: stdlib-only (no third-party deps). `.venv` is for pytest only. Release: a `v*` tag triggers `.github/workflows/release-plasmoid.yml` → flake8 + tests + `make build` + GitHub release asset.

## Backend module map

- `backend.py` — thin orchestrator + CLI entry; re-exports all public names for tests.
- `providers/gemini.py` — Gemini scraping + Google One AI credits.
- `providers/antigravity.py` — local language-server + Cloud Code API; `choose_antigravity_result`, `apply_google_one_credits`.
- `providers/codex.py` — `codex` CLI JSON-RPC + OpenAI cookie fallback.
- `providers/claude.py` — Claude browser-API path.
- `providers/cost.py` — cost enrichment + Antigravity token-capture pipeline (`update_antigravity_token_ledger`).
- `providers/__init__.py` — re-exports public names backend.py imports.
- `parsers.py` — pure payload → limit-row functions. No I/O.
- `accounting.py` — pure token/cost math, pricing lookups, usage-bucket helpers. No network. (Largest file.)
- `pricing_data.py` — LiteLLM catalog fetcher, disk-cached, USD per **million** tokens (MTok) everywhere.
- `cookies.py` / `crypto.py` — browser cookie extraction; `crypto.py` uses ctypes + KDE KWallet D-Bus.
- `http_helpers.py` — `http_json`/`http_text`/`http_json_async`, `bounded_provider`, `run_threaded_provider`.

## Backend contracts

**Python 3.11+ stdlib-only.** `asyncio.TaskGroup` and `asyncio.timeout` are load-bearing (added 3.11). `.venv` is tests-only. Don't add runtime imports outside stdlib.

**Daemon threads via `io_helpers.to_daemon_thread`, never `asyncio.to_thread`.** Non-daemon threads stall the one-shot backend's exit on stuck DNS/I/O (the whole refresh hangs). Every provider/cookie/pricing/cost-scan fetch routes through `to_daemon_thread`. → `tests/test_backend.py::test_run_threaded_provider_timeout_orphans_a_daemon_thread`.

**Backend must ALWAYS print parseable JSON.** `main()` catches `BaseException` and prints the last cached snapshot (or a minimal degraded one) on any fatal. The widget `JSON.parse`s stdout — a crash that prints nothing breaks the widget.

**`status` strings are the QML contract.** Values: `ok`, `missing-cookies`, `unauthorized`, `api-error`, `api-empty`, `timeout`, `not-running`, `wallet-locked`, etc. Adding a new status requires handling it in the UI too (FullRepresentation `emptyStateMessage`/`tabStatusBad`/`degradedText`). There is NO `parse-error` status — a parse failure re-uses `api-error`. A logged-OUT Google session (the `/usage` HTML renders but lacks the `SNlM0e` auth token while carrying a sign-in URL — `gemini._looks_signed_out`) is `unauthorized` with an actionable "sign in" message, NOT `api-empty`.

**Cost scan is compute-concurrently / apply-after, double-bounded — don't serialize or collapse it.** Kicked off at the very top of `build_snapshot` (overlaps network fetches), applied only after a clean `await cost_scan`. Timeout → `cost_summary_timeout` diagnostic, scan skipped entirely. A second cooperative deadline threads through `compute_local_cost_summaries(deadline)`. Tests patch `backend.compute_local_cost_summaries` (not `apply_local_cost_summaries`). → `docs/DEVNOTES.md § Orchestration`.

**Ledger must never load-into-empty.** Two coupled invariants — keep both: (1) `_save_antigravity_ledger` mirrors every good write to a `.bak`; (2) `_load_antigravity_ledger` recovers from `.bak` (quarantines corrupt main as `.corrupt`). A transient failure that resets the ledger re-stamps all history onto today. Do NOT "simplify" the loader to `try/except → empty`. → `tests/test_ledger_resilience.py`.

**Antigravity RPC harvest is incremental via `rpcWatermarks` — don't revert to full re-query.** Each 5-min refresh used to issue one `GetCascadeTrajectoryGeneratorMetadata` HTTP call per loaded cascade (110+ cascades = 3.4s/run — the "Cost scan timed out" regression, 2026-06-07). Fix: the ledger carries a top-level `rpcWatermarks: {cascadeId: lastModifiedTime}` map (additive key, rides the `.bak` mirror, no schema version bump needed). `collect_antigravity_rpc_usage(timeout, known, deadline)` (providers/antigravity.py) now:
- Reads `lastModifiedTime` from the `GetAllCascadeTrajectories` list response (before the metadata call — safe direction).
- Skips the metadata call if `known.get(cid) == lmt` (exact equality, not ordering — equality makes an LS-restart clock regression a harmless re-query rather than a permanent silent skip).
- **Watermarks only on success** (status 200, dict body, including zero-usage — sets a mark so empty conversations stop being re-queried). Exception / non-200 / deadline skip → no mark (will be retried next refresh; if a failed cascade were marked its generations would be lost until it next changes).
- Deadline (`time.time() >= deadline`) is checked before each LS, each port probe, and each cascade metadata call; on expiry the function returns what was harvested so far (self-healing ratchet — already-queried cascades leave the work set).
- `update_antigravity_token_ledger` loads `marks = ledger.get("rpcWatermarks") or {}`, passes it as `known=marks, deadline=deadline`, then after the 35-day entry prune merges + prunes: keeps marks only for cascades with persisted `#` entries OR live this run — stale marks for entry-less gone cascades drop naturally. **The `new_marks != marks` gate is load-bearing** — don't drop it; without it every refresh re-writes the ledger even when nothing changed (double atomic write main + .bak).
- Steady-state timing: 9.3s → 3.1s overall; cost scan 6.3s → ~1s.
→ `tests/test_rpc_watermarks.py` (17 tests covering skip, query, failure, deadline, round-trip, .bak, prune, no-churn, old-ledger).

**Token semantics are verified — don't "simplify":**
- Ledger: `o = t + x` (total output = thinking + response); bill `u·input + c·cache + o·output` once.
- Volume = `u + c + o` (includes cached — industry standard); cost bills cache at the *discounted* rate.
- `usage_cost_usd` local-log shapes: Anthropic cache additive (never subtract from input); OpenAI subsets; Gemini `thoughts`+`tool` additive (the `tool` add is NOT double-counting — a 2026-05-28 audit wrongly flagged it).
- Anthropic 1h cache writes = 2× input (`cache_write_1h`), NOT 1.25×. `_litellm_to_tallybar` **synthesizes** `cache_write_1h = 2× input` for any Anthropic model the live catalog omits — don't drop that synthesis.
- Claude dedup: keep the FINAL (max-`output_tokens`) streamed line per `requestId`, not first-seen.
→ `tests/test_accounting.py`, `tests/test_cli_usage.py`, `tests/test_pricing_data.py`, detail in `docs/DEVNOTES.md § Token semantics`.

**`_pb_find_usage` gate is model-agnostic — never pin field 1.** Field 1 is the model enum and varies; the old `field1==1016` gate dropped ~60% of usage.

**SQLite scans: fetch rows + close connection BEFORE parsing; `busy_timeout 3000`; check `key in entries` BEFORE `_pb_find_usage`.** Keeping the connection open during parsing blocks Antigravity agents with `SQLITE_BUSY` crashes. Read indices only first, drop seen keys, then fetch only unseen blobs, then close.

**Per-DB scan memo (`dbScanned` ledger key) — fail-open, don't widen the skip.** No-usage rows never create entries, so key-in-entries dedup re-parsed ~35k blobs every refresh (3.3s/run); the memo skips a DB whose `_db_signature` (main **and `-wal`** mtime_ns+size — WAL appends don't touch the main file) matches its last COMPLETE scan (0.14s/run). Memo is set only when both passes finish clean: any sqlite error (except `no such table: gen_metadata` — stable content fact), deadline expiry, or stat failure blocks it; the CLI ownership flip force-scans past a stale disk-era memo. Deadline-cut runs carry unvisited DBs' old memos forward (no rescan churn); complete runs rebuild the map (deleted DBs prune out). Change-gated write like `rpcWatermarks`. → `tests/test_genmetadata_scan.py::test_scan_memo_*`.

**Antigravity 2.0 usage lives in `gen_metadata` (marker `field6==26`), not `steps.metadata` (marker 24) — scan BOTH.** `_pb_find_usage(buf, marker=…)` is marker-parameterized (default 24; field-1 stays model-agnostic — never pin it). The per-DB `gen_metadata` pass (same fetch-then-close discipline) ingests **marker-26 records only** under disjoint `<stem>@<idx>` keys — the 24-records duplicated there would double-count the `steps` scan. Store `u=f2,c=f5,o=f3`, **omit `t`/`x`** (`cost_of` bills `o` once → safe-by-default). The RPC-superseding purge drops `<id>@<int>` keys too. The stored model enum `me` comes from `_dominant_enum(found)` (the max-token record), NOT `found[0]` — a single trajectory blob is one model in practice (verified 748/748 on-disk 2026-07-02), so this equals `found[0]` today but attributes a hypothetical mixed blob to its dominant model. One entry per idx is deliberate — do NOT split into per-enum sub-keys (it would break the `rsplit(":"/"@")` stem derivation, the RPC-purge prefix match, and the `:`/`#` coexistence invariant for zero real-world gain). First-run backlog (BOTH passes — steps too) is dated to the **DB file mtime** (not today, which would inflate "Today"); later new records stamp today_iso (gate: does the stem already have entries of that shape — the `colon_stems`/`at_stems` sets, snapshotted once per run). → `tests/test_genmetadata_scan.py`.

**Antigravity CLI (agy) capture — three coupled rules, don't "unify" them:**
- `~/.gemini/antigravity-cli/conversations` is the third scan root (plaintext trajectory DBs since 2026-06-02; the older CLI `.pb` history is encrypted/unrecoverable). CLI DBs are a HYBRID: real usage is marker-24 in `steps`, while their `gen_metadata` rows hold marker-24 records that are **exact 2× duplicates** of the steps usage (verified against the live agy RPC 2026-06-09 — cache tokens matched steps to the digit) and no marker-26 records. The scan therefore **skips the gen pass entirely for `antigravity-cli` dirs** (`conv_dir.parent.name` gate) — ingesting them would double-count every CLI generation, and with no `@` keys ever written that pass would also re-fetch every gen blob on every refresh.
- The RPC harvest covers live CLI sessions too: `agy` serves the same Connect-RPC API **in-process and token-less** (`_parse_agy_cmdline`; empty token → POST helpers omit the CSRF header; real language servers sort before agy in `find_antigravity_processes`). agy also answers `GetUserStatus`, so plan status works with the desktop app closed.
- **CLI stem ownership follows liveness.** agy's RPC dies with the session, but the session's tail keeps landing in its DB — so for `antigravity-cli` DBs a `#`-covered stem that is NOT in `live_marks`/`rpc_covered` gets its `#` entries purged and is re-owned by the steps scan (else the tail is suppressed forever — `#` coverage also drops the `cli:` fallback). `#` and `:` must never coexist for a stem. Desktop/IDE stems never flip (their steps rows carry no usage). → `tests/test_genmetadata_scan.py::test_cli_stem_*`.
- **The RPC heal is upgrade-only**: an empty `model_display` (GetAvailableModels failed / id retired from picker) must never overwrite a stored exact name with the family fallback — only fill a missing one. And `_LEGACY_MODEL_STRINGS` must NOT contain `gemini-3.1-pro` (it is the current unknown-Gemini fallback string — migrating it would morph honest unknowns into a specific effort claim).
- statusLine `cli:<id>` entries are a session's CUMULATIVE total → the summary merge **drops them when the ledger covers the same stem** (`<id>:`/`<id>@`/`<id>#` keys), else keeps them (only record of the pre-06-02 encrypted era). → `tests/test_genmetadata_scan.py`, `tests/test_cli_usage.py`.

**Monthly cost archive (`~/.tallybar/cost_archive.json`) is append-only — past months freeze.** `update_cost_archive` (providers/cost.py, best-effort at the end of `compute_local_cost_summaries`) upserts only `months[<current>]` from each summary's in-month buckets, so the 35-day Antigravity prune can't erode prior months. **Change-gated** like `new_marks != marks` (skip the write when the current-month dict is unchanged); atomic 0600 + `.bak` mirror + `.corrupt` quarantine loader (never-load-into-empty); flock bounded by the cost-scan deadline. First month recorded carries `"partial": true`. → `tests/test_cost_archive.py`.

**Background refreshes skip while the screen is locked.** `main()` gates `--once --background` on `_screen_locked()` (freedesktop ScreenSaver D-Bus via `busctl`, **fail-open** on any error); on lock it re-paints the cached snapshot with `diagnostics.refresh_skipped="screen-locked"`, emits no notifications, and **does NOT `save_snapshot`** (mustn't clobber last-known-good). Foreground/manual refreshes (no `--background`, e.g. the KWallet-unlock path) always run. → `tests/test_backend.py::test_screenlock_*`.

**`~/.gemini` scan prunes `antigravity*` subtrees — don't revert to `rglob`.** Use `os.walk` with `dirnames[:] = [d for d in dirnames if not d.startswith("antigravity")]`. The subtrees are multi-GB and hold no Gemini-cli sessions.

**Parse caches (`~/.tallybar/cache/{claude,codex,grok,gemini}_logs.json`) must be pure functions of file bytes.** `_parse_*_file` stores all-time records with no window baked in; summarizer re-applies the window each call. Regenerable (missing/corrupt cache → full reparse). **Bump `_CLAUDE_PARSE_VERSION` / `_CODEX_PARSE_VERSION` / `_GROK_PARSE_VERSION` / `_GEMINI_PARSE_VERSION` on any output-shape change.** `local_gemini_token_summary` routes through the same `_cached_log_records` seam via `walker=_gemini_session_files` — the walker (NOT rglob) prunes the multi-GB `antigravity*` subtrees; keep the prune. → `tests/test_accounting.py::test_parse_cache_*`.

**All atomic-0600 writes go through the ONE `io_helpers.atomic_write_text` recipe — don't re-inline it.** `backend._atomic_write_text` IS that import; the `.bak`-mirroring savers (`_save_antigravity_ledger`, `_save_cost_archive`) and the cache savers (`pricing_data._save_cache`, `accounting._save_parse_cache`, `antigravity.save_antigravity_credentials`) delegate their inner write to it and keep only their mirror/format wrapper. A future durability fix must land in `io_helpers` alone. → `tests/test_io_durability.py::test_no_duplicate_atomic_write_definitions`.

**`parsers.normalize_tier` is the ONE plan/tier keyword ladder** (enterprise→team→ultra→max→plus→pro→free, most-specific-first), shared by `parse_claude_tier` and Codex's `run_codex_rpc` planType. NOT for Antigravity's `userTier` ids (those map to Google-branded names in `accounting.antigravity_user_tier`). Codex keeps an unrecognized plan title-cased rather than blank.

**Atomic 0600 writes + fsync + flock are security/correctness measures — preserve.** `tempfile.mkstemp(mode=0600) → os.replace`; `chmod(0o700)` parent; `fsync` before replace; `fcntl.flock` on `.config.lock` for config writes; `os.open(O_NOFOLLOW)` for cookie copies. Don't revert to `Path.with_name(".tmp").write_text(...).replace()`.

**Two compact-count formatters must stay in sync:** Python `accounting.compact_token_count` and QML `compactCount` (`FullRepresentation.qml`). Carry at 1000-of-unit boundary, trim trailing zeros, half-UP rounding (`int(x + 0.5)`, matching JS `Math.round`). Change one → change the other. → `tests/test_accounting.py::test_compact_token_count`.

**`get_pricing` resolves exact-key-first, then longest `pattern in name`, then longest `name in pattern`.** Unknown non-empty model → provider-family default (not $0); `model=None`/`""` → $0. Resolution is **memoized in `_resolve_memo`** (keyed by lowercased name) — it's called once per usage record by the local-log summarizers. The memo MUST be cleared wherever `_active_pricing` is (re)built (`refresh_pricing`, the bootstrap branch, `invalidate_cache`) or a stale catalog's answer survives a refresh. → `tests/test_pricing_data.py`.

**Provider quirks — don't "fix" these:**
- `apply_google_one_credits` *replaces* Antigravity's Code Assist credits with the real Ultra/Pro AI pool; drops bar when offline. Don't restore the plan-status figure. The one.google.com RPC is throttled: a <300s-old `google-one-ai` balance from the previous snapshot is carried forward and the RPC skipped entirely (`gemini.google_one_credit_fresh`, mirrors Claude's `CREDIT_REFRESH_SECONDS`; the carried `fetchedAt` is NOT re-stamped, so the carry is age-bounded — keep both halves: skip the task AND re-apply the carried balance at the apply site, else the credit bar drops).
- Gemini/Antigravity tier is never hardcoded — only Antigravity's `GetUserStatus` has it; `apply_local_cost_summaries` propagates it to Gemini.
- Antigravity RPC key: `stepIndices[0]` else `g<gen_index>` — NOT `len(records)` (shifts across polls → double-count/silent-drop).
- Antigravity model names are the EXACT picker display names ("Gemini 3.5 Flash (High)"): live `GetAvailableModels` lookup per harvest → `_MODEL_ENUM_NAMES` (enum-first at summary time) → `learnedModelNames` → family fallback. `_LEGACY_MODEL_STRINGS` migrates old ledgers under the updater's flock — never offline (lost-update race with the widget's refresh). "gemini-3-pro" never existed: enum 1016/M16 IS Gemini 3.1 Pro (High). Pricing strips the effort parenthetical (`_normalize_model_name`).
- **`learnedModelNames` self-heals a model enum the static table doesn't know yet.** `collect_antigravity_rpc_usage`'s `learned_names` output param (an existing dict it MERGES into — kept as an optional param, not a return-tuple change, so the ~25 existing 2-tuple-unpacking test mocks stay untouched) collects every `MODEL_PLACEHOLDER_M<N>` → displayName pair `GetAvailableModels` returns per harvest, i.e. every model CURRENTLY on offer, not just ones with usage this run. `update_antigravity_token_ledger` converts placeholder strings to the same enum space as `_MODEL_ENUM_NAMES` (`1000 + N`) and merges them into the ledger's `learnedModelNames` (additive, change-gated like `rpcWatermarks`/`dbScanned` — never pruned, since there's no staleness concern for a handful of small strings). `_resolve_model_display(e, learned=...)` checks `_MODEL_ENUM_NAMES` → `learned` → the entry's own `model` string → a self-documenting `"Model M<N>"` placeholder (only a genuinely unresolvable entry — no `me`, or one outside the `1000+N` convention — falls to `"Unknown"`/"Other" in the QML). This means a brand-new model Antigravity starts using heals from "Model M84" to its real name on the very next refresh after Antigravity was open, with no code update required — `_MODEL_ENUM_NAMES` only needs manual maintenance for machines that never run this codebase's live RPC path.
- The DISPLAY breakdown groups by family — `_model_family` wraps the `mname` choke point in `antigravity_ledger_cost_summary` (single point feeding modelBreakdown + every bucket tooltip): Gemini keeps version+tier ("Gemini 3.1 Pro"), Claude/GPT collapse to one bucket each. Ledger entries and pricing stay EXACT (cost_of runs before grouping) — never persist family names into the ledger, and never group in the shared `accounting.py` serializers (they serve claude/codex/gemini too).
- `last_snapshot.json` is only overwritten when snapshot has live data (`_snapshot_has_live_data`) OR the existing cache is itself degraded.

**Grok is local-only — context gauge + token/cost history, no live quota.** The xAI CLI stores NO quota/credit/weekly data on disk (server-only). `run_grok_local` (providers/grok.py) surfaces the most-recently-active session's **context-window usage** as the "Context" bar — read from `~/.grok/sessions/*/*/signals.json` (`contextWindowUsage`/`contextTokensUsed`/`contextWindowTokens`); it skips just-opened sessions with no context yet so the gauge never blanks (`_most_recent_session` requires `contextWindowTokens>0`). Token/cost history is `local_grok_token_summary`, parsing `~/.grok/logs/unified.jsonl` lines `msg=="shell.turn.inference_done"` (OpenAI-shaped `ctx`: `prompt_tokens` INCLUDES `cached_prompt_tokens`, `completion_tokens` INCLUDES `reasoning_tokens` — don't add reasoning). Model isn't on the usage line; resolved out-of-band via `_grok_sid_model_map` (sid → `summary.json#current_model_id`, default `grok-build`) in the summarizer, NOT the parser (parse-cache purity). CLI models aren't in LiteLLM → priced at xAI's published grok-code-fast-1 rate via `pricing_data` fallback ("Cost (if pay-per-use)"). Limitations: `unified.jsonl` looks rolling (lossy if it rotates); live tier/credits (console.x.ai) deferred — needs a network capture. → `tests/test_accounting.py::test_local_grok_token_summary`, `tests/test_providers.py::test_run_grok_local_*`.

## Frontend contracts

**`main.qml` owns all state.** Three DataSources: refresh, config-write, cold-start `cacheLoader`. `liveLoaded` flag prevents slow cache read from clobbering a faster fresh fetch. `configSaving` flag preserves the in-flight config during a concurrent refresh — don't drop either branch. → `docs/DEVNOTES.md § Frontend detail`.

**Shared QML helpers live in `lib/format.js` (imported `as Fmt`) — don't re-inline them.** `Fmt.compactCount`/`trimDecimals` (the backend-parity formatter), `Fmt.providerAccent` (the per-provider fallback colour map), and `Fmt.pickFont(availableFonts, prefs)` (Apple-first font resolution) are each the SINGLE source shared by CompactRepresentation and FullRepresentation — they previously each hard-coded identical copies that could drift. `providerAccent` mirrors the backend accents; keep them aligned. The CommonJS `module.exports` at the file end is only for the `node` parity test — QML ignores it. → `tests/test_compact_count_parity.py`.

**Interactive controls carry `Accessible.role`/`Accessible.name` (and `.checked` for toggles).** All SettingsPopout chips/toggles and the close buttons are annotated for screen readers — match the pattern when adding a control. New user-visible prose gets `i18n()` (then `make pot`); brand names, match-keys, `$`/`×`/`$50` glyphs stay bare (see `po/` and `make pot`).

**Bars stay provider-accent colour at every percentage.** No warning/critical recolour to amber/red (removed from `compactFillColor`, `tabUsageColor`, expanded metric bars). Warning *pulse* (opacity throb) is kept. Don't re-add recolour.

**`metricsBodyHeight()` mirrors each section's real height deterministically.** Change a section's `Layout.preferredHeight` → update its mirrored constant in `metricsBodyHeight()`. Mismatch → `metricsScroller` goes interactive → unwanted scrollbar. Cost section: `hasBreakdown ? 127 : 107`.

**Cost popout is a SEPARATE `PopupPlasmaWindow` — LOCKED IN, never inline.** Details:
- `visualParent: flyoutAnchor` (invisible 1×1, `anchors.left: parent.left`), `popupDirection: Qt.LeftEdge` — outer left edge, not an interior item. This is what makes KWin place it beside (not over) the body on Wayland.
- `flyoutAnchor.anchors.verticalCenterOffset: 160` — aligns card center with the Cost row.
- `floating: true`, `animated: true`. `onActiveChanged → costDrawerOpen = false` (focus-out closes).
- Colors: fixed light-on-dark palette (`primaryTextColor`, `mutedTextColor`, etc.) — never `PlasmaCore.Theme` / `Kirigami.Theme` (silent TypeErrors in Plasma 6).
- Month grid `fillFraction: 0.96` — LOCKED. Adjust only via this one value; re-verify live.
- Tooltip: hover shows two compact lines (date + total) ONLY — user preference, don't add the breakdown to hover. **Click** a bar/month-day expands that bucket's tip with its per-model rows (backend per-bucket `models`, top-4 by cost — `bucket_add` + serializers in `accounting.py`); not a pin — exit or click-again collapses (`chartArea.expandedKey`). QVariantList gotcha: bucket `models` crossing a Repeater fails `Array.isArray` — duck-type on `.length` (`withModelLines`). `chartTip` x AND y backstop-clamped to popout window bounds; >2-line tips left-align.
→ Full tuning history: `docs/DEVNOTES.md § Cost popout`.

**Deploy:** `make upgrade` + `systemctl --user restart plasma-plasmashell.service`. Re-open the popup to see changes. Placement can only be verified live (offscreen renders can't show real window placement).

## Misc

- `--background` skips GUI-prompt credential actions (e.g. KWallet unlock); widget always passes it. `--no-network` skips all remote API calls.
- Backend state under `~/.tallybar/`: `config.json`, `last_snapshot.json`, `antigravity_token_ledger.json`, usage caches, parse caches.
- Tests add `io.github.dlansama.tallybar/contents/code` to `sys.path`; imports within `code/` are flat (`from cookies import ...`), matching how Plasma runs the script.
- `integrations/` wiring requires out-of-repo edits (e.g. `~/.gemini/antigravity-cli/settings.json`) — surface that to the user; don't do it silently.
- `*.bak-*` directories under `contents/` are manual backups; ignore them.
