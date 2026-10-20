# TallyBar security audit — 2026-10-20

Worktree: `/home/dylanr/Projects/tallybar-audit`, branch `overnight/security-audit-2026-10-20`, started at `863b6b8`. Fakes only, under `/var/tmp/tallybar-fixtures`. No real cookies, tokens, keyrings, or browser profiles were opened. Nothing was pushed.

## Summary

One finding, fixed. Hovering a provider tab painted `provider.message` with Qt's rich-text sniffer, so an `<img src="http://…">` in a Codex app-server error was fetched by the widget. The desktop tooltip style leaves `textFormat` unset, and the existing Text scanner does not look at `ToolTip.text`.

The other four areas were reviewed against the current tree. The September hardening (HTTPS-only redirects, bearer redirect refusal, cookie-copy permissions, credential scrubbing, plain-text `Text` items, pinned release actions) still holds. No second issue rose to a finding. A loopback TLS prefix check is recorded below as a broken guard with no current caller that can hit it; it is not counted above.

## Findings

### 1. Tab tooltip renders provider messages as rich text

- Severity: medium
- Confidence: high that the sink was AutoText and that the Codex error string is what it shows. Not observed in a live Codex session (no real account was used).
- Attack: The Codex tab tooltip shows the app-server's error text whenever that tab is in a bad state (unauthorized, api-error, timeout, and the other statuses `tabStatusBad` lists). Qt treats a string that starts with `<` and a letter as HTML. An error containing `<img src="http://example/pixel">` makes the widget request that URL when the pointer rests on the tab. That is a beacon or a request to an address the user did not choose. It does not attach browser cookies or the provider token.
- Where:
  - `io.github.dlansama.tallybar/contents/ui/components/ProviderTabBar.qml:43` (before the fix, `QQC2.ToolTip.text` at the same spot)
  - `io.github.dlansama.tallybar/contents/ui/FullRepresentation.qml:561` `tabStatusMessage` returns `provider.message`
  - `io.github.dlansama.tallybar/contents/code/providers/codex.py:234` `_codex_rpc_error_message` returns the RPC `error.message`; assigned at line 293 after `scrub_credentials` only
  - Style, not in this repo: `/usr/lib/qt6/qml/org/kde/desktop/ToolTip.qml:74` (`Label` with no `textFormat`); Qt Basic does the same at `/usr/lib/qt6/qml/QtQuick/Controls/Basic/ToolTip.qml:25`
- Test commit: `85ee1da2636af44c034c3f305571aead3cc19c07`
- Fix commit: `99e4e95699a2e908c03c3af86e9ae98452b34b36`
- Fix: `PlainToolTip` replaces the style's content item with a `Text` whose `textFormat` is `Text.PlainText`. Confirmed offscreen: that text's format is `0` (PlainText) and the `<img …>` string is kept as text. The cost-section and action-footer tooltips use the same type. Their strings are counts and `i18n` labels, not provider messages; they moved so the check can be mechanical. The panel tooltip (`main.qml` `toolTipSubText`) was not this bug: Plasma's `PlasmoidItem` defaults `toolTipTextFormat` to plain text.
- Suite before the fix: 908 passed, the two new tests failed. Suite after the fix: 911 passed. `qmllint` on the edited files reported only pre-existing `id-shadows-member` warnings in `FullRepresentation.qml`.

## Reviewed, not findings

### Credentials

Cookie copies use `O_NOFOLLOW` and mode `0600` in a `0700` temp dir. Snapshots, token caches, ledgers, and statusline files go through `atomic_write_text` (`0600`, directory `0700`). `scrub_credentials` runs on provider messages that include exception text. Grok's access token and Grok Bot's bearer are not interpolated into messages or diagnostics. KWallet diagnostics store lengths, not the secret. Gemini `at`/`sid` caches are `0600` under `~/.tallybar/cache/`.

### Network

`http_json` / `http_text` reject non-HTTPS before opening the connection. Cookie requests follow HTTPS redirects only. Any request with `Authorization` refuses redirects, so a bearer is not copied onto another host. Antigravity's OAuth and Cloud Code calls refuse 3xx from hosts ending in `oauth2.googleapis.com` or `cloudcode-pa.googleapis.com`. The Cloud Code host is taken from a regex that only captures a name ending in `cloudcode-pa.googleapis.com` (probed: a lookalike suffix, userinfo, and a percent-encoded dot did not produce another host). The pricing catalog fetch sends no credentials.

`post_local_json` and `_post_local_rpc` turn TLS verification off when the URL string starts with `https://127.0.0.1` or `https://localhost`. That is also true for `https://127.0.0.1.evil.com` and `https://localhost.evil.com` (probed). Every current caller builds `f"{scheme}://127.0.0.1:{port}..."`. The CSRF token would go out with verification off only if a later change passed a different URL. Not filed as a finding: no reachable caller, and a fix is a hostname parse rather than a behavior change for today's URLs. Left as a note so it is not rediscovered as an exploit.

### Untrusted input

Dynamic `Text` items pin `Text.PlainText` (`tests/test_qml_text_format.py`), which is what missed the tooltip. The cost-chart tip at `CostPopout.qml:687` already pins plain text. The off-screen clipboard `TextEdit` in `CostPopout.qml:53` is left on AutoText, but a probe with `visible: false` and `width: 0` did not request an `<img>` URL (a visible `TextEdit` did). Right-click copy is not a fetch. Not filed. `openExternalUrlSafe` allows only `https://`. The only dynamic `actionUrl` is a regex locked to `https://accounts.google.com/` sign-in paths. Protobuf parsing caps varints and recursion. Log walks skip non-regular files via `lstat`. Notification bodies pass through `escapeNotificationMarkup`. Notification titles are shell-quoted and not markup-escaped; the freedesktop spec treats the summary as plain text, and this machine has no KDE notification QML to contradict that, so it was not filed.

### Integrations

`integrations/claude_code/statusline_capture.py` and `integrations/antigravity_cli/cli_statusline_capture.py` write `0600` files under `~/.tallybar/` (directory `0700`) and replace via `os.replace`, which does not follow a symlink. The Antigravity session id is a JSON key (`cli:` + id), not a filesystem path. Another user on the machine cannot read the files. The same user can plant them; that is the trust boundary of `~/.tallybar/`.

### Release workflow

`.github/workflows/release-plasmoid.yml` pins checkout, setup-python, and action-gh-release to commit SHAs. `permissions` is `contents: write`. `GITHUB_TOKEN` is set only on the release step. The tag version is passed to `jq --arg`, so it is not shell-injected. `make build` zips the applet directory and excludes caches and `requirements.txt`. CI (`ci.yml`) is `contents: read` and uses the same pins. `pip install --upgrade pip` is unpinned; that is a CI supply-chain choice, not a defect in the plasmoid, and there is no useful unit test for it.
