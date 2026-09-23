# CLAUDE.md

Guidance for Claude Code (and any contributor) working in this repo. This file states the
contracts the code relies on and, where one exists, the test that guards each. Keep it present-tense: what
changed and why belongs in commit messages and [`CHANGELOG.md`](CHANGELOG.md), not here.

## What this is

TallyBar is a **KDE Plasma 6 widget (Plasmoid)** monitoring AI session usage (Codex/OpenAI, Gemini, Claude, Antigravity, Grok). Two halves:
- **QML frontend** (`io.github.dlansama.tallybar/contents/ui/`) — `main.qml` (state), `CompactRepresentation.qml`, `FullRepresentation.qml`, `CostPopout.qml`, `SettingsPopout.qml`, modular sections in `ui/components/` (`UsageLimitCard.qml`, `ProviderTabBar.qml`, `CostSection.qml`, `ActionFooter.qml`, etc.) and helpers in `ui/lib/` (`format.js`, `ui_helpers.js`).
- **Python backend** (`io.github.dlansama.tallybar/contents/code/`) — one-shot CLI, prints JSON, exits.

Communication is a process boundary: QML's `Plasma5Support.DataSource` runs `python3 backend.py --once ...`, captures stdout, `JSON.parse`s it. No daemon. (`-B` intentionally omitted — `__pycache__` speeds re-runs.)

## Commands

```bash
# Run tests (uses .venv with pytest + pytest-asyncio)
.venv/bin/pytest tests/
.venv/bin/pytest tests/test_backend.py::test_sqlite_copy   # single test

# Static gates — the same three CI and the release workflow run
make check       # = make lint (flake8 --select=F) + make qmllint + make typecheck (mypy via uvx)
.venv/bin/python -m flake8 --select=F io.github.dlansama.tallybar/contents/code/ tests/ integrations/

# Exercise the backend exactly as the widget does
python3 io.github.dlansama.tallybar/contents/code/backend.py --once --timeout 12 --background --pretty
python3 io.github.dlansama.tallybar/contents/code/backend.py --once --no-network --pretty   # skip remote API calls

# Package, install, upgrade, remove the plasmoid (kpackagetool6)
make build      # produces io.github.dlansama.tallybar.plasmoid (a ZIP — the only container KPackage 6 opens)
make install    # first install
make upgrade    # reinstall after changes — use this during dev
make remove

make pot                    # regenerate the translation template after adding i18n() strings
make preview / screenshots  # offscreen render of the UI from tools/preview/mock-telemetry.json
```

Runtime: stdlib-only (no third-party deps). `.venv` is for pytest only. Release: a `v*` tag triggers `.github/workflows/release-plasmoid.yml` → flake8 + qmllint + mypy + tests + `make build` + GitHub release asset.

## Backend module map

- `backend.py` — thin orchestrator + CLI entry; re-exports all public names for tests.
- `providers/gemini.py` — Gemini scraping + Google One AI credits.
- `providers/antigravity.py` — local language-server + Cloud Code API; `choose_antigravity_result`, `apply_google_one_credits`.
- `providers/codex.py` — `codex` CLI JSON-RPC + OpenAI cookie fallback.
- `providers/claude.py` — Claude browser-API path.
- `providers/grok.py` — Grok Build weekly credit bar from the local `unified.jsonl` log; no network.
- `providers/cost.py` — cost enrichment + Antigravity token-capture pipeline (`update_antigravity_token_ledger`).
- `providers/__init__.py` — re-exports public names backend.py imports.
- `parsers.py` — pure payload → limit-row functions. No I/O.
- `accounting/` — package decomposing pure token/cost math, pricing lookups, log parsers, and usage-bucket helpers (`buckets.py`, `formatting.py`, `limits.py`, `log_parsers.py`, `pricing.py`). Pure stdlib, no network. Re-exported via `accounting/__init__.py`.
- `proto_wire.py` — zero-dependency LEB128 varint and protobuf wire-format parser for Antigravity trajectories.
- `pricing_data.py` — LiteLLM catalog fetcher, disk-cached, USD per **million** tokens (MTok) everywhere.
- `cookies.py` / `crypto.py` — browser cookie extraction; `crypto.py` uses ctypes + KDE KWallet D-Bus.
- `http_helpers.py` — `http_json`/`http_text`/`http_json_async`, `bounded_provider`, `run_threaded_provider`.
- `io_helpers.py` — `atomic_write_text` (the one atomic-0600 write recipe) and `to_daemon_thread`.

## Backend contracts

**Python 3.11+ stdlib-only.** `asyncio.TaskGroup` and `asyncio.timeout` are load-bearing (added 3.11). `.venv` is tests-only. Don't add runtime imports outside stdlib.

**Daemon threads via `io_helpers.to_daemon_thread`, never `asyncio.to_thread`.** Non-daemon threads stall the one-shot backend's exit on stuck DNS/I/O (the whole refresh hangs). Every provider/cookie/pricing/cost-scan fetch routes through `to_daemon_thread`. → `tests/test_backend.py::test_run_threaded_provider_timeout_orphans_a_daemon_thread`.

**Backend must ALWAYS print parseable JSON.** `main()` catches `BaseException` and prints the last cached snapshot (or a minimal degraded one) on any fatal. The widget `JSON.parse`s stdout — a crash that prints nothing breaks the widget.

**`status` strings are the QML contract.** Values: `ok`, `missing-cookies`, `unauthorized`, `api-error`, `api-empty`, `timeout`, `not-running`, `wallet-locked`, etc. Adding a new status requires handling it in the UI too (FullRepresentation `emptyStateMessage`/`tabStatusBad`/`degradedText`). There is NO `parse-error` status — a parse failure re-uses `api-error`. A logged-OUT Google session (the `/usage` HTML renders but lacks the `SNlM0e` auth token while carrying a sign-in URL — `gemini._looks_signed_out`) is `unauthorized` with an actionable "sign in" message, NOT `api-empty`.

**Cost scan is compute-concurrently / apply-after, double-bounded — don't serialize or collapse it.** Kicked off at the very top of `build_snapshot` (overlaps network fetches), applied only after a clean `await cost_scan`. Timeout → `cost_summary_timeout` diagnostic, scan skipped entirely. A second cooperative deadline threads through `compute_local_cost_summaries(deadline)`. Tests patch `backend.compute_local_cost_summaries` (not `apply_local_cost_summaries`).

**`diagnostics.timings` reports per-phase wall clock (ms) in every snapshot** — `cookies`, `providers`, `cost_scan`, `total`. Read it before guessing where a slow refresh goes.

**Ledger must never load-into-empty.** Two coupled invariants — keep both: (1) `_save_antigravity_ledger` mirrors every good write to a `.bak`; (2) `_load_antigravity_ledger` recovers from `.bak` (quarantines corrupt main as `.corrupt`). A transient failure that resets the ledger re-stamps all history onto today. Do NOT "simplify" the loader to `try/except → empty`. → `tests/test_ledger_resilience.py`.

**Antigravity RPC harvest is incremental via `rpcWatermarks` — don't revert to full re-query.** A full harvest issues one `GetCascadeTrajectoryGeneratorMetadata` HTTP call per loaded cascade, which blows the cost-scan deadline once there are ~100 of them. The ledger carries a top-level `rpcWatermarks: {cascadeId: lastModifiedTime}` map (additive key, rides the `.bak` mirror, no schema version bump needed). `collect_antigravity_rpc_usage(timeout, known, deadline)` (providers/antigravity.py):
- Reads `lastModifiedTime` from the `GetAllCascadeTrajectories` list response (before the metadata call — safe direction).
- Skips the metadata call if `known.get(cid) == lmt` (exact equality, not ordering — equality makes an LS-restart clock regression a harmless re-query rather than a permanent silent skip).
- **Watermarks only on success** (status 200, dict body, including zero-usage — sets a mark so empty conversations stop being re-queried). Exception / non-200 / deadline skip → no mark (will be retried next refresh; if a failed cascade were marked its generations would be lost until it next changes).
- Deadline (`time.time() >= deadline`) is checked before each LS, each port probe, and each cascade metadata call; on expiry the function returns what was harvested so far (self-healing ratchet — already-queried cascades leave the work set).
- `update_antigravity_token_ledger` loads `marks = ledger.get("rpcWatermarks") or {}`, passes it as `known=marks, deadline=deadline`, then after the 35-day entry prune merges + prunes: keeps marks only for cascades with persisted `#` entries OR live this run — stale marks for entry-less gone cascades drop naturally. **The `new_marks != marks` gate is load-bearing** — don't drop it; without it every refresh re-writes the ledger even when nothing changed (double atomic write main + .bak).
→ `tests/test_rpc_watermarks.py`.

**Token semantics are verified — don't "simplify":**
- Ledger: `o = t + x` (total output = thinking + response); bill `u·input + c·cache + o·output` once.
- Volume = `u + c + o` (includes cached — industry standard); cost bills cache at the *discounted* rate.
- Antigravity ledger cache reads are billed PER ENTRY in every key namespace (`:` / `@` / `#` / `cli:`) — each entry is one API call and pays for the cache it re-reads. Never collapse a conversation to its peak `c`: the agy RPC and the CLI steps rows carry identical per-call values, so namespace-specific accounting makes cost depend on the capture path and shift when CLI ownership flips. → `tests/test_cli_usage.py::test_cache_read_billed_per_call_*`
- `usage_cost_usd` local-log shapes: Anthropic cache additive (never subtract from input); OpenAI subsets; Gemini `thoughts`+`tool` additive (the `tool` add is NOT double-counting).
- Anthropic 1h cache writes = 2× input (`cache_write_1h`), NOT 1.25×. `_litellm_to_tallybar` **synthesizes** `cache_write_1h = 2× input` for any Anthropic model the live catalog omits — don't drop that synthesis.
- Claude dedup: keep the FINAL (max-`output_tokens`) streamed line per `requestId`, not first-seen.
→ `tests/test_accounting.py`, `tests/test_cli_usage.py`, `tests/test_pricing_data.py`.

**`_pb_find_usage` gate is model-agnostic — never pin field 1.** Field 1 is the model enum and varies; pinning it to one enum silently drops every other model's usage.

**SQLite scans: fetch rows + close connection BEFORE parsing; `busy_timeout 3000`; check `key in entries` BEFORE `_pb_find_usage`.** Keeping the connection open during parsing blocks Antigravity agents with `SQLITE_BUSY` crashes. Read indices only first, drop seen keys, then fetch only unseen blobs, then close.

**Per-DB scan memo (`dbScanned` ledger key) — fail-open, don't widen the skip.** No-usage rows never create entries, so key-in-entries dedup alone re-parses every such blob on every refresh; the memo skips a DB whose `_db_signature` (main **and `-wal`** mtime_ns+size — WAL appends don't touch the main file) matches its last COMPLETE scan. Memo is set only when both passes finish clean: any sqlite error (except `no such table: gen_metadata` — stable content fact), deadline expiry, or stat failure blocks it; the CLI ownership flip force-scans past a stale disk-era memo. Deadline-cut runs carry unvisited DBs' old memos forward (no rescan churn); complete runs rebuild the map (deleted DBs prune out). Change-gated write like `rpcWatermarks`. → `tests/test_genmetadata_scan.py::test_scan_memo_*`.

**Antigravity 2.0 usage lives in `gen_metadata` (marker `field6==26`), not `steps.metadata` (marker 24) — scan BOTH.** `_pb_find_usage(buf, marker=…)` is marker-parameterized (default 24; field-1 stays model-agnostic — never pin it). The per-DB `gen_metadata` pass (same fetch-then-close discipline) ingests **marker-26 records only** under disjoint `<stem>@<idx>` keys — the 24-records duplicated there would double-count the `steps` scan. Store `u=f2,c=f5,o=f3`, **omit `t`/`x`** (`cost_of` bills `o` once → safe-by-default). The RPC-superseding purge drops `<id>@<int>` keys too. The stored model enum `me` comes from `_dominant_enum(found)` (the max-token record), NOT `found[0]` — a trajectory blob is one model in practice, so the two agree today, but a mixed blob must be attributed to its dominant model. One entry per idx is deliberate — do NOT split into per-enum sub-keys (it would break the `rsplit(":"/"@")` stem derivation, the RPC-purge prefix match, and the `:`/`#` coexistence invariant). First-run backlog (BOTH passes — steps too) is dated to the **DB file mtime** (not today, which would inflate "Today"); later new records stamp today_iso (gate: does the stem already have entries of that shape — the `colon_stems`/`at_stems` sets, snapshotted once per run). → `tests/test_genmetadata_scan.py`.

**Antigravity CLI (agy) capture — three coupled rules, don't "unify" them:**
- `~/.gemini/antigravity-cli/conversations` is the third scan root (plaintext trajectory DBs; the older CLI `.pb` history is encrypted and unrecoverable). CLI DBs are a HYBRID: real usage is marker-24 in `steps`, while their `gen_metadata` rows hold marker-24 records that are **exact 2× duplicates** of the steps usage and no marker-26 records. The scan therefore **skips the gen pass entirely for `antigravity-cli` dirs** (`conv_dir.parent.name` gate) — ingesting them would double-count every CLI generation, and with no `@` keys ever written that pass would also re-fetch every gen blob on every refresh.
- The RPC harvest covers live CLI sessions too: `agy` serves the same Connect-RPC API **in-process and token-less** (`_parse_agy_cmdline`; empty token → POST helpers omit the CSRF header; real language servers sort before agy in `find_antigravity_processes`). agy also answers `GetUserStatus`, so plan status works with the desktop app closed.
- **CLI stem ownership follows liveness.** agy's RPC dies with the session, but the session's tail keeps landing in its DB — so for `antigravity-cli` DBs a `#`-covered stem that is NOT in `live_marks`/`rpc_covered` gets its `#` entries purged and is re-owned by the steps scan (else the tail is suppressed forever — `#` coverage also drops the `cli:` fallback). `#` and `:` must never coexist for a stem. Desktop/IDE stems never flip (their steps rows carry no usage). → `tests/test_genmetadata_scan.py::test_cli_stem_*`.
- **The RPC heal is upgrade-only**: an empty `model_display` (GetAvailableModels failed / id retired from picker) must never overwrite a stored exact name with the family fallback — only fill a missing one. And `_LEGACY_MODEL_STRINGS` must NOT contain `gemini-3.1-pro` (it is the current unknown-Gemini fallback string — migrating it would morph honest unknowns into a specific effort claim).
- statusLine `cli:<id>` entries are a session's CUMULATIVE total → the summary merge **drops them when the ledger covers the same stem** (`<id>:`/`<id>@`/`<id>#` keys), else keeps them (the only record of the encrypted-`.pb` era). → `tests/test_genmetadata_scan.py`, `tests/test_cli_usage.py`.

**Monthly cost archive (`~/.tallybar/cost_archive.json`) is append-only — past months freeze.** `update_cost_archive` (providers/cost.py, best-effort at the end of `compute_local_cost_summaries`) upserts only `months[<current>]` from each summary's in-month buckets, so the 35-day Antigravity prune can't erode prior months. **Change-gated** like `new_marks != marks` (skip the write when the current-month dict is unchanged); atomic 0600 + `.bak` mirror + `.corrupt` quarantine loader (never-load-into-empty); flock bounded by the cost-scan deadline. First month recorded carries `"partial": true`. → `tests/test_cost_archive.py`.

**Background refreshes skip while the screen is locked.** `main()` gates `--once --background` on `_screen_locked()` (freedesktop ScreenSaver D-Bus via `busctl`, **fail-open** on any error); on lock it re-paints the cached snapshot with `diagnostics.refresh_skipped="screen-locked"`, emits no notifications, and **does NOT `save_snapshot`** (mustn't clobber last-known-good). Foreground/manual refreshes (no `--background`, e.g. the KWallet-unlock path) always run. The UI shows the paused state in the "Updated…" subtitle only — not as a degraded banner, which would push the metrics into a scrollbar. → `tests/test_backend.py::test_screenlock_*`.

**`~/.gemini` scan prunes `antigravity*` subtrees — don't revert to `rglob`.** Use `os.walk` with `dirnames[:] = [d for d in dirnames if not d.startswith("antigravity")]`. The subtrees are multi-GB and hold no Gemini-cli sessions.

**Parse caches (`~/.tallybar/cache/{claude,codex,grok,gemini}_logs.json`) must be pure functions of file bytes.** `_parse_*_file` stores all-time records with no window baked in; summarizer re-applies the window each call. Records keep only the usage fields the math reads (`pricing.slim_usage` — a test fails if a reader consumes a key it would discard). Regenerable (missing/corrupt cache → full reparse). **Bump `_CLAUDE_PARSE_VERSION` / `_CODEX_PARSE_VERSION` / `_GROK_PARSE_VERSION` / `_GEMINI_PARSE_VERSION` on any output-shape change.** `local_gemini_token_summary` routes through the same `_cached_log_records` seam via `walker=_gemini_session_files` — the walker (NOT rglob) prunes the multi-GB `antigravity*` subtrees; keep the prune. → `tests/test_accounting.py::test_parse_cache_*`, `::test_slim_usage_covers_every_key_the_readers_consume`.

**Claude parse cache is tail-incremental (`off` + `fp`) — Claude ONLY, and the window end is fixed BEFORE reading.** A changed session JSONL is extended from `off` (end of the last complete line) instead of reparsed. Invariant: an entry's `records` are exactly the parse of bytes `[0, off)` — `_cached_log_records` takes `end = _safe_resume_offset(path)` first and `_parse_claude_file(path, start, end)` is bounded to it; never parse to EOF and measure the offset afterwards (that double-stores a final line whose `\n` hasn't landed, and the requestId dedup doesn't cover keyless records). `_tail_parse` is fail-closed → full reparse on shrink, same-size rewrite, or an `fp` mismatch (CRC32 of the 256 bytes before `off` — catches a rewrite that GREW). `_parse_codex_file`/`_parse_grok_file` carry per-line state (`current_model`/`model_by_sid`) so resuming mid-file would mis-attribute models — they must never pass `incremental=True`. → `tests/test_accounting.py::test_incremental_*`, `::test_grown_rewrite_*`, `::test_unterminated_*`, `::test_append_during_*`, `::test_stateful_parsers_are_not_wired_for_incremental`.

**All atomic-0600 writes go through the ONE `io_helpers.atomic_write_text` recipe — don't re-inline it.** `backend._atomic_write_text` IS that import; the `.bak`-mirroring savers (`_save_antigravity_ledger`, `_save_cost_archive`, `_save_grok_archive`) and the cache savers (`pricing_data._save_cache`, `accounting._save_parse_cache`, `antigravity.save_antigravity_credentials`) delegate their inner write to it and keep only their mirror/format wrapper. A future durability fix must land in `io_helpers` alone. → `tests/test_io_durability.py::test_no_duplicate_atomic_write_definitions`.

**`parsers.normalize_tier` is the ONE plan/tier keyword ladder** (enterprise→team→ultra→max→plus→pro→free, most-specific-first), shared by `parse_claude_tier` and Codex's `run_codex_rpc` planType. NOT for Antigravity's `userTier` ids (those map to Google-branded names in `accounting.antigravity_user_tier`). Codex keeps an unrecognized plan title-cased rather than blank.

**Cookie jars come from ONE unexpired profile — `cookies.select_session_cookies`.** Readers normalize `expires_unix`/`last_used_unix` to unix seconds (Chromium: µs since 1601; Firefox: `expiry` is now **milliseconds**, `lastAccessed` µs — unit taken from magnitude). Expired cookies are dropped; the profile with the latest `last_used_unix` for the domain set wins (then most cookies, then discovery order). Never merge profiles into one jar: the last store read won every collision and could splice two Google accounts. Provider "missing-cookies" checks use `has_session_cookies` (all-expired ⇒ sign-in prompt, not a 403). → `tests/test_cookie_session_selection.py`.

**Atomic 0600 writes + fsync + flock are security/correctness measures — preserve.** `tempfile.mkstemp(mode=0600) → os.replace`; `chmod(0o700)` parent; `fsync` before replace; `fcntl.flock` on `.config.lock` for config writes; `os.open(O_NOFOLLOW)` for cookie copies. Don't revert to `Path.with_name(".tmp").write_text(...).replace()`.

**Two compact-count formatters must stay in sync:** Python `accounting.compact_token_count` and QML `Fmt.compactCount` (`ui/lib/format.js`). Carry at 1000-of-unit boundary, trim trailing zeros, half-UP rounding (`int(x + 0.5)`, matching JS `Math.round`). Change one → change the other. → `tests/test_accounting.py::test_compact_token_count`, `tests/test_compact_count_parity.py`.

**`get_pricing` resolves exact-key-first, then longest `pattern in name`, then longest `name in pattern`.** Unknown non-empty model → provider-family default (not $0); `model=None`/`""` → $0. Resolution is **memoized in `_resolve_memo`** (keyed by lowercased name) — it's called once per usage record by the local-log summarizers. The memo MUST be cleared wherever `_active_pricing` is (re)built (`refresh_pricing`, the bootstrap branch, `invalidate_cache`) or a stale catalog's answer survives a refresh. → `tests/test_pricing_data.py`.

**Provider quirks — don't "fix" these:**
- `apply_google_one_credits` *replaces* Antigravity's Code Assist credits with the real Ultra/Pro AI pool; drops bar when offline. Don't restore the plan-status figure. The one.google.com RPC is throttled: a <300s-old `google-one-ai` balance from the previous snapshot is carried forward and the RPC skipped entirely (`gemini.google_one_credit_fresh`, mirrors Claude's `CREDIT_REFRESH_SECONDS`; the carried `fetchedAt` is NOT re-stamped, so the carry is age-bounded — keep both halves: skip the task AND re-apply the carried balance at the apply site, else the credit bar drops).
- Gemini/Antigravity tier is never hardcoded — only Antigravity's `GetUserStatus` has it; `apply_local_cost_summaries` propagates it to Gemini.
- Codex's `planType` is nested INSIDE the `rateLimits` payload, and lane durations vary by plan (a free account's primary lane is a 30-day window) — label by the payload's window, never assume primary = 5h.
- Antigravity RPC key: `stepIndices[0]` else `g<gen_index>` — NOT `len(records)` (shifts across polls → double-count/silent-drop).
- Antigravity model names are the EXACT picker display names ("Gemini 3.5 Flash (High)"): live `GetAvailableModels` lookup per harvest → `_MODEL_ENUM_NAMES` (enum-first at summary time) → `learnedModelNames` → family fallback. `_LEGACY_MODEL_STRINGS` migrates old ledgers under the updater's flock — never offline (lost-update race with the widget's refresh). "gemini-3-pro" never existed: enum 1016/M16 IS Gemini 3.1 Pro (High). Pricing strips the effort parenthetical (`_normalize_model_name`).
- **`learnedModelNames` self-heals a model enum the static table doesn't know yet.** `collect_antigravity_rpc_usage`'s `learned_names` output param (an existing dict it MERGES into — an optional param rather than a return-tuple change, so the many 2-tuple-unpacking test mocks stay valid) collects every `MODEL_PLACEHOLDER_M<N>` → displayName pair `GetAvailableModels` returns per harvest, i.e. every model CURRENTLY on offer, not just ones with usage this run. `update_antigravity_token_ledger` converts placeholder strings to the same enum space as `_MODEL_ENUM_NAMES` (`1000 + N`) and merges them into the ledger's `learnedModelNames` (additive, change-gated like `rpcWatermarks`/`dbScanned` — never pruned). `_resolve_model_display(e, learned=...)` checks `_MODEL_ENUM_NAMES` → `learned` → the entry's own `model` string → a self-documenting `"Model M<N>"` placeholder (only a genuinely unresolvable entry — no `me`, or one outside the `1000+N` convention — falls to `"Unknown"`/"Other" in the QML). A brand-new model therefore heals to its real name on the next refresh after Antigravity was open, with no code update; `_MODEL_ENUM_NAMES` only needs manual maintenance for machines that never run the live RPC path.
- The DISPLAY breakdown groups by family — `_model_family` wraps the `mname` choke point in `antigravity_ledger_cost_summary` (single point feeding modelBreakdown + every bucket tooltip): Gemini keeps version+tier ("Gemini 3.1 Pro"), Claude/GPT collapse to one bucket each. Ledger entries and pricing stay EXACT (cost_of runs before grouping) — never persist family names into the ledger, and never group in the shared `accounting/` serializers (they serve claude/codex/gemini too).
- `last_snapshot.json` is only overwritten when snapshot has live data (`_snapshot_has_live_data`) OR the existing cache is itself degraded.

**Claude Code statusLine capture is a FALLBACK, never an override.** `integrations/claude_code/statusline_capture.py` writes `~/.tallybar/claude_statusline.json`; `build_snapshot` calls `apply_claude_statusline_fallback` (providers/claude.py) AFTER the transient carry-forward. It replaces the Claude provider only when it has no live limits (a carried `stale` reading only if the capture is newer), drops windows whose `resetsAt` passed, ignores captures >6h old, flags >15min `stale`, and stamps `source: claude-statusline` + `fetchedAt` = capture time. `tests/conftest.py` points `CLAUDE_STATUSLINE_PATH` at a tmp file for every test — keep that, or any build_snapshot test turns on whether Claude Code ran recently on the dev machine. → `tests/test_claude_statusline_fallback.py`.

**Grok is local-only — weekly credit bar + token/cost history, no network.**
- **Live bar:** `run_grok_local` (providers/grok.py) reads the newest usable `billing: fetched credits config` event from `~/.grok/logs/unified.jsonl` → the weekly credit-pool bar (xAI's own `creditUsagePercent`), tier, and billing period (`parsers.parse_grok_billing_config`). The event is logged at session start, so a long session buries it far from EOF — the chunked reverse scan (`_iter_billing_events_reverse`) is load-bearing; don't replace it with a fixed "last N KB" window. An event without `creditUsagePercent` means 0% only when it opens a NEW period; mid-period it is skipped in favour of the last real reading (`_latest_billing_event`).
- **Freshness is the CLI's, not ours:** `fetchedAt` is the event's own timestamp, and a reading older than `_BILLING_STALE_AFTER_SECONDS` sets `stale` (the UI's "(cached)" suffix). Don't stamp `now_iso()` on it.
- There is no context-window gauge on the Grok tab — it was removed deliberately; don't reintroduce one.
- **History:** `local_grok_token_summary` parses `unified.jsonl` lines `msg=="shell.turn.inference_done"` (OpenAI-shaped `ctx`: `prompt_tokens` INCLUDES `cached_prompt_tokens`, `completion_tokens` INCLUDES `reasoning_tokens` — don't add reasoning). The usage line carries no model: `_parse_grok_file` tracks it from the log's model-change lines (`model_by_sid` — the per-line state that bars it from incremental parsing), and the summarizer fills remaining gaps out-of-band from `sessions/**/summary.json` (`_load_grok_session_models`, default `grok-build`) — NOT in the parser, which must stay a pure function of the log's bytes. `grok_billing_period` feeds the summary's billing-week window (`billingWeekTokens`/`billingWeekCost`).
- `unified.jsonl` rotates, so per-day totals are mirrored into `~/.tallybar/grok_archive.json` (`merge_grok_archive`: a day only ever grows, 120 days kept, change-gated, `.bak` mirror).
- The CLI's `grok-build`/`grok-composer` models are subscription-proxied and absent from LiteLLM → priced at xAI's published coding-model API rate via the embedded `pricing_data` rows ("Cost (if pay-per-use)"). `grok-4.6` needs its own exact key or family matching resolves it to `grok-4`.
→ `tests/test_providers.py::test_run_grok_local_*`, `::test_grok_*`, `tests/test_accounting.py::test_local_grok_*`, `tests/test_parsers.py::test_parse_grok_billing_config_weekly_credits`.

## Frontend contracts

**`main.qml` owns all state.** Three DataSources: refresh, config-write, cold-start `cacheLoader`. `liveLoaded` flag prevents slow cache read from clobbering a faster fresh fetch. `configSaving` flag preserves the in-flight config during a concurrent refresh — don't drop either branch.

**Plasma 6 API traps — both fail SILENTLY (no test catches them, only the live journal).** Enum constants (status / `backgroundHints` / `formFactor`) live on `PlasmaCore.Types`, not `Plasmoid.X` (which resolves to `undefined` and breaks the attention badge + vertical layout). `PlasmaCore.Theme` / `Kirigami.Theme` colours throw TypeErrors — the widget paints its own dark background, so use the fixed light-on-dark palette (`primaryTextColor`, `mutedTextColor`, etc.).

**Every QML `Text` that renders a DYNAMIC string pins `textFormat: Text.PlainText`.** Provider messages, model names and error strings originate outside the app (API responses, local logs, the `GetAvailableModels` RPC); Qt's default AutoText would render markup in them, and an `<img src=…>` becomes an outbound network fetch from the widget. Static bindings (a string literal or `i18n()` of literals) are exempt. → `tests/test_qml_text_format.py`.

**Shared QML helpers live in `lib/format.js` (imported `as Fmt`) — don't re-inline them.** `Fmt.compactCount`/`trimDecimals` (the backend-parity formatter), `Fmt.providerAccent` (the per-provider fallback colour map), and `Fmt.pickFont(availableFonts, prefs)` (Apple-first font resolution) are each the SINGLE source shared by CompactRepresentation and FullRepresentation. `providerAccent` mirrors the backend accents; keep them aligned. The CommonJS `module.exports` at the file end is only for the `node` parity test — QML ignores it. → `tests/test_compact_count_parity.py`.

**Interactive controls carry `Accessible.role`/`Accessible.name` (and `.checked` for toggles).** All SettingsPopout chips/toggles and the close buttons are annotated for screen readers — match the pattern when adding a control. New user-visible prose gets `i18n()` (then `make pot`); brand names, match-keys, `$`/`×`/`$50` glyphs stay bare (see `po/`).

**Bars stay provider-accent colour at every percentage.** No warning/critical recolour to amber/red (`compactFillColor`, `tabUsageColor`, expanded metric bars). The warning *pulse* (opacity throb) is kept. Don't re-add recolour.

**`metricsBodyHeight()` mirrors each section's real height deterministically** (the popup height can't come from `implicitHeight`, which reads 0 until Repeater delegates instantiate). Section heights are single-sourced — `rowMetricHeight()`, `costSectionHeight()`, `extraUsageBodyHeight` feed BOTH the section's `Layout.preferredHeight` and the mirror; add a new section the same way. A mismatch makes `metricsScroller` interactive → unwanted scrollbar. The cost popout's `drawerHeightFor()` follows the same rule with `costBurnRowHeight`/`costModelRowHeight`.

**Cost popout is a SEPARATE `PopupPlasmaWindow` (`CostPopout.qml`) — never inline it into the widget body.** Details:
- `visualParent: flyoutAnchor` (invisible 1×1 in `FullRepresentation.qml`, `anchors.left: parent.left`), `popupDirection: Qt.LeftEdge` — the widget's outer left edge, not an interior item. This is what makes KWin place it beside (not over) the body on Wayland.
- `flyoutAnchor.anchors.verticalCenterOffset: 160` — aligns card center with the Cost row.
- `floating: true`, `animated: true`. `onActiveChanged → costDrawerOpen = false` (focus-out closes).
- Month grid width is tuned through the single `fillFraction: 0.96` value — adjust only there and re-verify live.
- Tooltip: hover shows two compact lines (date + total) ONLY — a deliberate design choice; don't add the breakdown to hover. **Click** a bar/month-day expands that bucket's tip with its per-model rows (backend per-bucket `models`, top-4 by cost — `bucket_add` + serializers in `accounting/buckets.py`); not a pin — exit or click-again collapses (`chartArea.expandedKey`). QVariantList caveat: bucket `models` crossing a Repeater fails `Array.isArray` — duck-type on `.length` (`withModelLines`). `chartTip` x AND y backstop-clamped to popout window bounds; >2-line tips left-align.

**Verify UI changes by rendering them.** Tests, lint and qmllint can all pass on a UI that renders empty. After touching QML, render it (`make screenshots`, or the live widget) across tabs and an error-state fixture and compare against the previous render. → `tests/test_qml_render.py`.

**Deploy:** `make upgrade` + `systemctl --user restart plasma-plasmashell.service`. Re-open the popup to see changes. Window placement can only be verified live (offscreen renders can't show it).

## Misc

- `--background` skips GUI-prompt credential actions (e.g. KWallet unlock); widget always passes it. `--no-network` skips all remote API calls.
- Backend state under `~/.tallybar/`: `config.json`, `last_snapshot.json`, `antigravity_token_ledger.json`, `cost_archive.json`, `grok_archive.json`, usage caches, parse caches (`cache/`).
- Tests add `io.github.dlansama.tallybar/contents/code` to `sys.path`; imports within `code/` are flat (`from cookies import ...`), matching how Plasma runs the script.
- `integrations/` wiring requires edits OUTSIDE the repo (e.g. `~/.gemini/antigravity-cli/settings.json`) — tell the user and let them make or approve the change; never do it silently.
- Anything published from this repo (package metadata, README, release assets, commits) carries the project identity already in `metadata.json` — never a contributor's personal address. Check before publishing, not after.
