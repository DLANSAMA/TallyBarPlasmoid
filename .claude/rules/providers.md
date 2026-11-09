---
paths:
  - "io.github.dlansama.tallybar/contents/code/backend.py"
  - "io.github.dlansama.tallybar/contents/code/parsers.py"
  - "io.github.dlansama.tallybar/contents/code/cookies.py"
  - "io.github.dlansama.tallybar/contents/code/crypto.py"
  - "io.github.dlansama.tallybar/contents/code/http_helpers.py"
  - "io.github.dlansama.tallybar/contents/code/providers/**/*"
  - "integrations/**/*"
  - "tests/test_backend.py"
  - "tests/test_providers.py"
  - "tests/test_parsers.py"
  - "tests/test_cookie_session_selection.py"
  - "tests/test_codex_rpc.py"
  - "tests/test_claude_statusline_*.py"
---

# Orchestrator, parsers, cookies and per-provider quirks

Area contracts moved out of the root CLAUDE.md; they load only when a matching file is touched.

**Background refreshes skip while the screen is locked.** `main()` gates `--once --background` on `_screen_locked()` (freedesktop ScreenSaver D-Bus via `busctl`, **fail-open** on any error); on lock it re-paints the cached snapshot with `diagnostics.refresh_skipped="screen-locked"`, emits no notifications, and **does NOT `save_snapshot`** (mustn't clobber last-known-good). Foreground/manual refreshes (no `--background`, e.g. the KWallet-unlock path) always run. The UI shows the paused state in the "Updated…" subtitle only — not as a degraded banner, which would push the metrics into a scrollbar. → `tests/test_backend.py::test_screenlock_*`.

**`parsers.normalize_tier` is the ONE plan/tier keyword ladder** (enterprise→team→ultra→max→plus→pro→free, most-specific-first), shared by `parse_claude_tier` and Codex's `run_codex_rpc` planType. NOT for Antigravity's `userTier` ids (those map to Google-branded names in `accounting.antigravity_user_tier`). Codex planType ids go through `providers/codex.codex_plan_name` first (`CODEX_PLAN_NAMES`: the ladder would read `edu_plus` as Plus and `prolite` as Pro); an unrecognized one keeps its words title-cased rather than blank. → `tests/test_providers.py::test_codex_plan_name_maps_chatgpt_plan_ids`. Claude plan ids go through `parsers.claude_plan_name` first: Anthropic renamed Max 20x → Ultra and Max 5x → Max (2026-10-30) but kept the old ids (`default_claude_max_20x`, `subscriptionType: "max"` for both), so claude.ai's `plan_display_name` wins, then `rate_limit_tier` (credentials: `rateLimitTier` before the ambiguous `subscriptionType`). A plan in `CLAUDE_PLANS_WITHOUT_SESSION` (verified Ultra has no 5-hour window) gets no idle "Not started" Session row — `five_hour: null` there is permanent. → `tests/test_parsers.py::test_claude_plan_name_maps_renamed_plans`, `tests/test_providers.py::test_claude_ultra_has_no_idle_session_row`

**Cookie jars come from ONE unexpired profile — `cookies.select_session_cookies`.** Readers normalize `expires_unix`/`last_used_unix` to unix seconds (Chromium: µs since 1601; Firefox: `expiry` is now **milliseconds**, `lastAccessed` µs — unit taken from magnitude). Expired cookies are dropped; the profile with the latest `last_used_unix` for the domain set wins (then most cookies, then discovery order). Never merge profiles into one jar: the last store read won every collision and could splice two Google accounts. Provider "missing-cookies" checks use `has_session_cookies` (all-expired ⇒ sign-in prompt, not a 403). → `tests/test_cookie_session_selection.py`.

**Provider quirks — don't "fix" these:**

- Claude's `usage_summary` is probed first; a definite miss (404/410, or 200 with no limits) is stamped as `usageSummaryUnavailableSince` on the provider dict and later refreshes go straight to `/usage` for `USAGE_SUMMARY_RETRY_SECONDS` (24h) before re-probing. Transient 403/429/5xx never stamp it — a Cloudflare blip must not switch the probe off. When no org yields limits and the usage GETs failed (429/401/403/5xx/exception), the result is the matching transient status (`api-error`, `unauthorized`, `timeout`; a 429 stamps `rateLimitedUntil`) so carry-forward and the back-off apply; only a definite miss is `api-empty`. The statusLine fallback carries `rateLimitedUntil` (while future), `creditBalance` and `usageSummaryUnavailableSince`. Reset epochs above 1e11 are milliseconds (`parsers.epoch_to_utc`); an unparseable one means no reset time for that row. The run-out projection waits for 5% of the window to elapse. → `tests/test_providers.py::test_usage_summary_*`, `tests/test_limit_row_edge_cases.py`.
- Codex's `planType` is nested INSIDE the `rateLimits` payload, and lane durations vary by plan (a free account's primary lane is a 30-day window) — label by the payload's window, never assume primary = 5h.

- `last_snapshot.json` is only overwritten when snapshot has live data (`_snapshot_has_live_data`) OR the existing cache is itself degraded.

**Claude Code statusLine capture is a FALLBACK, never an override.** `integrations/claude_code/statusline_capture.py` writes `~/.tallybar/claude_statusline.json`; `build_snapshot` calls `apply_claude_statusline_fallback` (providers/claude.py) AFTER the transient carry-forward. It replaces the Claude provider only when it has no live limits (a carried `stale` reading only if the capture is newer), drops windows whose `resetsAt` passed, ignores captures >6h old, flags >15min `stale`, and stamps `source: claude-statusline` + `fetchedAt` = capture time. `tests/conftest.py` points `CLAUDE_STATUSLINE_PATH` at a tmp file for every test — keep that, or any build_snapshot test turns on whether Claude Code ran recently on the dev machine. → `tests/test_claude_statusline_fallback.py`.
