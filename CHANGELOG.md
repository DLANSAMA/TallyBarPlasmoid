# Changelog

Notable changes to TallyBar. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

TallyBar has not been released yet — there is no tag and nothing on the KDE
Store. `0.1.0` below is what the first release will contain.

## [Unreleased]

### Changed
- **Plugin id is now `io.github.dlansama.tallybar`** (was `org.kde.tallybar`).
  The `org.kde.*` namespace is reserved for widgets contributed to KDE itself;
  third-party widgets are expected to use a reverse-domain id they own. Done
  before first release on purpose — renaming afterwards orphans every installed
  widget, since a plasmoid id has no migration path.

### Fixed
- Grok's weekly bar reported its own read time as the capture time. The number is
  xAI's own `creditUsagePercent`, but it only reaches the log when the CLI runs —
  so a 15-hour-old reading was displayed under "Updated just now". `fetchedAt` is
  now the event's own timestamp, and the provider marks itself stale past an hour,
  which the UI already renders as "(cached)".
- Per-model Claude caps (Anthropic returns these keyed by model, e.g. "Fable")
  rendered differently from the weekly lane they are: they skipped the polished
  pace line and leaked internal phrasing — "3% in deficit", which reads as
  *behind* when it means ahead of pace. Session and weekly lanes are now
  classified by their actual window length, not by a literal label match.
- Grok Build CLI usage was priced on xAI's grok-4.6 **API** row instead of the
  build row — a ~2.4x overstatement on a cache-heavy workload. Nothing on disk
  records that the CLI runs a build model: `grok usage` reports
  `grok-4.6-build`, while `unified.jsonl` and `summary.json` both record the
  bare `grok-4.6`.
- Codex reported no plan at all. `planType` is nested inside the `rateLimits`
  object; the code read it from the level above.
- Codex labelled a 30-day quota window "Session". Lanes are returned
  positionally and their durations vary by plan, so a free account's 43200-minute
  primary window rendered as "Session - Resets in 29d 23h". Labels are now
  derived from the window length.

### Added
- A Grok daily archive (`~/.tallybar/grok_archive.json`). xAI keeps its billing
  telemetry in a single rolling log that truncates in place — measured at ~47
  hours — so Grok history is now accumulated forward instead of re-read. Days
  that scroll out of the log survive; a truncated day can never erode an
  archived one.
- An elapsed-time marker on every usage bar, showing where usage would be if the
  window were spent evenly. Fill past the marker means burning faster than the
  window sustains; fill short of it means headroom. The backend already computed
  this (`pacePercent`); nothing rendered it.
- `make screenshots`: renders `docs/screenshots/` offscreen from a synthetic
  telemetry fixture, with no display, no Plasma session and no real usage data.
- A README written for someone who has never seen the project.

### Changed
- The QML preview harness renders the real UI again. Its inline mock telemetry
  used a shape the UI never read, so `make preview` showed an empty widget; it
  now reads the same fixture the screenshots do.

### Security
- `scrub_credentials()` covers Google session cookies and JWTs, and is applied at
  every diagnostic sink that can echo an exception string.
- Credentialed Google requests refuse redirects — CPython's redirect handler
  copies the `Authorization` header cross-origin.
- Cookie-database copies live in `$XDG_RUNTIME_DIR` rather than shared `/tmp`,
  and stale copies from a killed run are swept.
- `~/.tallybar` lockfiles are created `0600` instead of inheriting `0644`.
- Every `Text` element rendering a backend string is pinned to `Text.PlainText`,
  backend-supplied URLs are opened only when `https`, and `notify-send`
  arguments are `--`-terminated.

## [0.1.0] — unreleased

First public release.

### Providers
- **Codex** — 5-hour and weekly windows plus plan, from the `codex` CLI's
  app-server over JSON-RPC, with a browser-cookie fallback.
- **Claude** — 5-hour window and both weekly caps, from claude.ai using the
  browser's existing session; plan read from local OAuth credentials.
- **Gemini** — daily usage and the Google One AI credit pool.
- **Antigravity** — Gemini and Claude/GPT quota windows and plan, from the local
  language server and Google's Cloud Code API; token usage harvested from the
  trajectory databases and an incremental RPC watermark.
- **Grok** — weekly window and token history from the local CLI logs.

### Cost
- Pay-per-use cost estimates for every provider, priced from the LiteLLM catalog
  with a disk cache and a built-in fallback table.
- A separate chart window with a 7-day bar chart, month grid, 24-hour histogram
  and a per-model breakdown that expands on click.
- An append-only monthly archive so past months can't be eroded by the rolling
  35-day retention of the Antigravity ledger.

### Widget
- Panel display as percentage, cost or bars; per-provider tabs, mute and
  notification thresholds; optional monthly budget alert.
- Cold start paints the cached snapshot immediately; background refreshes stop
  while the screen is locked.
- Accessible names and roles on interactive controls; user-visible strings
  wrapped for translation.
