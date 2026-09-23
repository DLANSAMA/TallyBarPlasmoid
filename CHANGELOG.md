# Changelog

Notable changes to TallyBar. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Antigravity CLI cost was understated: cache reads captured from the CLI's
  conversation databases were collapsed to each conversation's peak instead of
  billed per call, while the same calls captured live were billed in full. Every
  capture path now bills cache reads per call (about a third higher 30-day cost on
  a real CLI-heavy ledger).
- The Claude Code statusline integration now does what its README said: when
  claude.ai can't be read (Cloudflare rate limit, signed out, locked wallet,
  offline), the Claude tab falls back to the quota Claude Code reported to the
  hook instead of showing an error. Previously the capture was written but never read.
- A monthly-budget alert no longer fires a second time after a refresh whose
  cost scan timed out (the missing cost data read as $0 and re-armed the alert).
- During a brief Claude/Gemini/Codex outage the widget keeps showing the last good
  usage bars, but the cost section now updates from this refresh's local logs
  instead of freezing at the cached figures for up to 15 minutes.

## [0.1.0] — 2026-09-17

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
- **Grok** — xAI's own weekly credit reading, plan and token history from the
  local CLI logs, accumulated into a daily archive so days that scroll out of
  the CLI's rolling log survive.

### Cost
- Pay-per-use cost estimates for every provider, priced from the LiteLLM catalog
  with a disk cache and a built-in fallback table.
- A separate chart window with a 7-day bar chart, month grid, 24-hour histogram
  and a per-model breakdown that expands on click.
- An append-only monthly archive so past months can't be eroded by the rolling
  35-day retention of the Antigravity ledger.
- Local log parsing is cached per file and, for Claude session logs, extended
  from the last complete line rather than reparsed; a snapshot carries
  per-phase timings in `diagnostics.timings`.

### Widget
- Panel display as percentage, cost or bars; per-provider tabs, mute and
  notification thresholds; optional monthly budget alert.
- An elapsed-time marker on every usage bar showing where usage would sit if
  the window were spent evenly.
- Cold start paints the cached snapshot immediately; background refreshes stop
  while the screen is locked.
- Accessible names and roles on interactive controls; user-visible strings
  wrapped for translation.

### Security
- Credentials are scrubbed from every diagnostic string; credentialed Google
  requests refuse redirects; cookie-database copies live in `$XDG_RUNTIME_DIR`
  and are swept; state files and lockfiles are created `0600`.
- Every `Text` element rendering a backend string is pinned to `Text.PlainText`,
  backend-supplied URLs open only when `https`, and `notify-send` arguments are
  `--`-terminated.
