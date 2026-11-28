# CLAUDE.md

Guidance for Claude Code (and any contributor) working in this repo. This file states the
contracts the code relies on and, where one exists, the test that guards each. Keep it present-tense: what
changed and why belongs in commit messages and [`CHANGELOG.md`](CHANGELOG.md), not here.

## What this is

TallyBar is a **KDE Plasma 6 widget (Plasmoid)** monitoring AI session usage (Codex/OpenAI, Gemini, Claude, Antigravity, Grok; Grok Bot is part of the Grok tab). Two halves:
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
make preview                # the UI in a standalone window, from tools/preview/mock-telemetry.json
make screenshots            # README PNGs + cost-demo.gif (docs/screenshots/) from a live backend snapshot: real spend, published — look at each; MOCK=1 renders the mock fixture instead
make render-baselines       # tests/render-baselines/ from the mock fixture, byte-stable; what test_qml_render compares
```

Runtime: stdlib-only (no third-party deps). `.venv` is for pytest only. Release: a `v*` tag triggers `.github/workflows/release-plasmoid.yml` → flake8 + qmllint + mypy + tests + `make build` + GitHub release asset.

## Backend module map

- `backend.py` — thin orchestrator + CLI entry; re-exports all public names for tests.
- `providers/gemini.py` — Gemini scraping + Google One AI credits.
- `providers/antigravity.py` — local language-server + Cloud Code API; `choose_antigravity_result`, `apply_google_one_credits`.
- `providers/codex.py` — `codex` CLI JSON-RPC + OpenAI cookie fallback.
- `providers/claude.py` — Claude browser-API path, the Claude Code sign-in (OAuth) fallback and the statusLine fallback.
- `providers/grok.py` — the Grok tab: weekly credit bar (the one limit all Grok products share) read live from xAI's billing endpoint with the grok CLI's access token when the network is allowed, else from the local `unified.jsonl` log; token/cost history from the same log, no network; `fold_grok_bot` merges Grok Bot's reading into it.
- `providers/grok_bot.py` — Grok Bot reading (plan, on-demand, prepaid, and a weekly bar for users without the CLI) from Cursor's dashboard service, signed in through the Grok Bot desktop app; network only.
- `providers/cost.py` — cost enrichment + Antigravity token-capture pipeline (`update_antigravity_token_ledger`).
- `providers/__init__.py` — re-exports public names backend.py imports.
- `parsers.py` — pure payload → limit-row functions. No I/O.
- `accounting/` — package decomposing pure token/cost math, pricing lookups, log parsers, and usage-bucket helpers (`buckets.py`, `formatting.py`, `limits.py`, `log_parsers.py`, `pricing.py`). Pure stdlib, no network. Re-exported via `accounting/__init__.py`.
- `proto_wire.py` — zero-dependency LEB128 varint and protobuf wire-format parser for Antigravity trajectories.
- `pricing_data.py` — LiteLLM catalog fetcher, disk-cached, USD per **million** tokens (MTok) everywhere.
- `cookies.py` / `crypto.py` — browser cookie extraction; `crypto.py` uses ctypes + KDE KWallet D-Bus.
- `http_helpers.py` — `http_json`/`http_text`/`http_json_async`, `bounded_provider`, `run_threaded_provider`.
- `io_helpers.py` — `atomic_write_text` (the one atomic-0600 write recipe), `save_json_with_backup`/`load_json_with_backup` (the `.bak`/`.corrupt` pair), `flock_with_timeout`, `deadline_passed`/`clamp_to_deadline`, and `to_daemon_thread`.

## Backend contracts

**Python 3.11+ stdlib-only.** `asyncio.TaskGroup` and `asyncio.timeout` are load-bearing (added 3.11). `.venv` is tests-only. Don't add runtime imports outside stdlib.

**Daemon threads via `io_helpers.to_daemon_thread`, never `asyncio.to_thread`.** Non-daemon threads stall the one-shot backend's exit on stuck DNS/I/O (the whole refresh hangs). Every provider/cookie/pricing/cost-scan fetch routes through `to_daemon_thread`. → `tests/test_backend.py::test_run_threaded_provider_timeout_orphans_a_daemon_thread`.

**Backend must ALWAYS print parseable JSON.** `main()` catches `BaseException` and prints the last cached snapshot (or a minimal degraded one) on any fatal. The widget `JSON.parse`s stdout — a crash that prints nothing breaks the widget.

**`status` strings are the QML contract.** Values: `ok`, `missing-cookies`, `unauthorized`, `api-error`, `api-empty`, `timeout`, `not-running`, `wallet-locked`, etc. Adding a new status requires handling it in the UI too (FullRepresentation `emptyStateMessage`/`tabStatusBad`/`degradedText`). There is NO `parse-error` status — a parse failure re-uses `api-error`. A logged-OUT Google session (the `/usage` HTML renders but lacks the `SNlM0e` auth token while carrying a sign-in URL — `gemini._looks_signed_out`) is `unauthorized` with an actionable "sign in" message, NOT `api-empty`.

**All atomic-0600 writes go through the ONE `io_helpers.atomic_write_text` recipe — don't re-inline it.** `backend._atomic_write_text` IS that import; the `.bak`-mirroring savers (`_save_antigravity_ledger`, `_save_cost_archive`, `_save_grok_archive` — all `io_helpers.save_json_with_backup`, loaded by `load_json_with_backup`) and the cache savers (`pricing_data._save_cache`, `accounting._save_parse_cache`, `antigravity.save_antigravity_credentials`) delegate their inner write to it and keep only their format wrapper. A future durability fix must land in `io_helpers` alone. → `tests/test_io_durability.py::test_no_duplicate_atomic_write_definitions`.

**Atomic 0600 writes + fsync + flock are security/correctness measures — preserve.** `tempfile.mkstemp(mode=0600) → os.replace`; `chmod(0o700)` parent; `fsync` before replace; `fcntl.flock` on `.config.lock` for config writes; `os.open(O_NOFOLLOW)` for cookie copies. Don't revert to `Path.with_name(".tmp").write_text(...).replace()`.

**Two compact-count formatters must stay in sync:** Python `accounting.compact_token_count` and QML `Fmt.compactCount` (`ui/lib/format.js`). Carry at 1000-of-unit boundary, trim trailing zeros, half-UP rounding (`int(x + 0.5)`, matching JS `Math.round`). Change one → change the other. → `tests/test_accounting.py::test_compact_token_count`, `tests/test_compact_count_parity.py`.

**A saved `config.providers` is the enabled set relative to `providerCatalog`.** `public_config` enables every `PROVIDER_ORDER` key the saved list names OR the catalog it was saved under didn't know (absent catalog = the five pre-Grok-Bot keys), so a provider added in a release appears instead of reading as "turned off". `update_config_values` settles the stored list against its old catalog BEFORE stamping `providerCatalog = PROVIDER_ORDER` on every save; reversing that order would hide a new provider on the first unrelated save. → `tests/test_backend.py::test_public_config_*`, `::test_update_config_values_settles_*`.

## Area rules

Per-area contracts live in `.claude/rules/*.md`; Claude Code loads each when a matching file is touched.
Other agents (Gemini / Antigravity): read the matching file by hand before editing that area.

- `antigravity.md` — Antigravity ledger, RPC watermarks, SQLite/`gen_metadata` scans, agy CLI capture, model names.
- `cost-pricing.md` — cost scan, token semantics, typeBreakdown, cost archive, parse caches, price tiers.
- `providers.md` — orchestrator, screen-lock gate, tiers, cookies, Claude/Codex quirks, statusLine fallback.
- `grok.md` — Grok Build bar + log fallback, Grok Bot.
- `frontend.md` — `main.qml` state, Plasma 6 traps, QML helpers, accessibility, cost popout.

## Frontend contracts

**Verify UI changes by rendering them.** Tests, lint and qmllint can all pass on a UI that renders empty. After touching QML, render it (`make render-baselines`, or the live widget) across tabs and an error-state fixture and compare against the previous render. → `tests/test_qml_render.py`.

**Deploy:** `make upgrade` + `systemctl --user restart plasma-plasmashell.service`. Re-open the popup to see changes. Window placement can only be verified live (offscreen renders can't show it).

## Comments

A comment explains why the code can't be simpler: an outside constraint (Qt/Plasma/KWin, the OS, a
third-party API or file format, security, a measured cost). Never write what the next line does,
history ("used to", "the old bug", "fixed in", dates, audit or ticket IDs: that goes in the commit),
line numbers, or another file's internals. A rule ("never X", "keep in sync") ships with the test that
enforces it, and the comment is one line ending `→ tests/<file>::<test>`. A comment that explains a
workaround for our own code means the code should change instead. Changing code means fixing or
deleting the comments that describe it.

## Misc

- `--background` skips GUI-prompt credential actions (e.g. KWallet unlock); widget always passes it. `--no-network` skips all remote API calls.
- Backend state under `~/.tallybar/`: `config.json`, `last_snapshot.json`, `antigravity_token_ledger.json`, `cost_archive.json`, `grok_archive.json`, usage caches, parse caches (`cache/`).
- Tests add `io.github.dlansama.tallybar/contents/code` to `sys.path`; imports within `code/` are flat (`from cookies import ...`), matching how Plasma runs the script.
- `tests/conftest.py` runs every test against an empty per-test home: it patches `Path.home()` in-process (a test that sets `HOME` itself wins) and rewrites every home-relative `Path` constant in the code and integration modules. Subprocesses keep the real `HOME`, so a test that runs a script against state passes its own. Without it the suite would write the real cost archive and parse caches.
- `integrations/` wiring requires edits OUTSIDE the repo (e.g. `~/.gemini/antigravity-cli/settings.json`) — tell the user and let them make or approve the change; never do it silently.
- Anything published from this repo (package metadata, README, release assets, commits) carries the project identity already in `metadata.json` — never a contributor's personal address. Check before publishing, not after.
