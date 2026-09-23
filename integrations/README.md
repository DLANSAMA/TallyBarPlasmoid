# Optional integrations

Nothing here is required. The widget works without any of it; these are hooks for
data TallyBar cannot reach on its own.

Each one needs an edit **outside this repository**, so none of them are wired up
by installing the plasmoid — you have to opt in by hand.

## `claude_code/statusline_capture.py`

Captures Claude Code's **official** rate-limit data via its `statusLine` hook.

Since v2.1.x, Claude Code pipes the subscriber's real quota to the statusline
command on stdin — `rate_limits.five_hour` / `.seven_day` / `.spend_limit`, each
with `used_percentage` and a `resets_at` epoch
([docs](https://code.claude.com/docs/en/statusline)). This script records them to
`~/.tallybar/claude_statusline.json` and prints a short line so it still works as
a status line.

**Why it is worth wiring.** TallyBar's normal Claude path reads claude.ai with
your browser cookies — which is what tripped Cloudflare into 403/429 flapping.
This route is official, needs no cookies, no network and no credentials of its
own: Claude Code simply hands over numbers it already has.

**It complements rather than replaces the cookie path.** `rate_limits` appears
only for Pro/Max subscribers, and only after a session's first API response — so
it goes quiet whenever Claude Code is not running. The capture is stamped with
its own `capturedAt` so a reading is never presented as fresher than it is.

**How the widget uses it.** Whenever the claude.ai cookie path produces no live
limits — a Cloudflare 403/429, a signed-out or locked-wallet session, or a
`--no-network` refresh — the Claude tab shows the captured Session and Weekly
windows instead of an error. A live cookie reading always wins (it also carries
the extra-usage and credit rows the statusline doesn't have). Windows whose reset
time has passed are dropped, captures older than 6 hours are ignored, and a
capture older than 15 minutes is marked "(cached)" in the tab's subtitle.

To wire it up, add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/path/to/TallyBarPlasmoid/integrations/claude_code/statusline_capture.py"
  }
}
```

The script consumes stdin, so it cannot be chained by piping. If you already have
a statusline, call both from a small wrapper that reads stdin once and feeds a
copy to each.

## `antigravity_cli/cli_statusline_capture.py`

Captures per-session token usage from the Antigravity CLI (`agy`) by acting as
its `statusLine` command: the CLI pipes a JSON telemetry context to that command
on every status update, and this script records the cumulative token counts into
`~/.tallybar/antigravity_cli_usage.json` in the same shape as the main ledger.

**This is a fallback.** Since the CLI began writing plaintext trajectory
databases, TallyBar reads CLI usage directly — and `agy` answers the same
language-server RPC the desktop app does, in-process. The statusline capture only
adds value for sessions predating that, and its entries are dropped whenever the
ledger already covers the same session.

To wire it up, point `~/.gemini/antigravity-cli/settings.json` at the script:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/path/to/TallyBarPlasmoid/integrations/antigravity_cli/cli_statusline_capture.py"
  }
}
```

The script prints a short status string to stdout, so it still functions as a
statusline. It never raises — a statusline command that errors breaks the CLI's
status bar.
