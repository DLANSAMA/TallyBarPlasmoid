# Changelog

Notable changes to TallyBar. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
