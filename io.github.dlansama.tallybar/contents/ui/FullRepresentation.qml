import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore
import "lib/format.js" as Fmt

Item {
    id: root

    property var telemetry: ({
    })
    property string selectedProvider: "claude"
    property bool loading: false
    property string lastError: ""
    // True once a live fetch has painted (mirrors main.qml's liveLoaded). When false the
    // body is showing the cold-start cache, so the subtitle prefixes "Cached · " (Item 2).
    property bool liveLoaded: false
    property bool configSaving: false
    property string configError: ""

    // The only config-save error surface ("Save failed" + tooltip) lives inside the
    // settingsPopout, which auto-closes on focus-out. A --set-config write completes
    // asynchronously, so it can fail AFTER the popout has closed (flock contention,
    // disk-full, the ~25s config watchdog) — leaving the failure invisible and the stale
    // value silently persisted. Re-open the popout whenever a fresh error appears so the
    // user always sees it. A successful save clears configError to "" (no re-open).
    onConfigErrorChanged: {
        if (root.configError.length > 0)
            root.settingsDrawerOpen = true;
    }
    property var tabs: ["codex", "claude", "gemini", "antigravity", "grok"]
    property var screenGeometry: null
    property bool hostExpanded: true
    property bool costDrawerOpen: false
    property bool settingsDrawerOpen: false
    property string costGraphMode: "week"
    readonly property real settingsWidth: 300
    // QML font.family takes a single family name (not a CSS fallback stack), so
    // resolve to the first installed family from an Apple-first preference list.
    // SF Pro Text/Display mirror macOS's optical-size split (body vs. headings).
    readonly property var availableFonts: Qt.fontFamilies()
    function pickFont(prefs) {
        for (var i = 0; i < prefs.length; ++i) {
            if (root.availableFonts.indexOf(prefs[i]) >= 0)
                return prefs[i];
        }
        return ""; // empty -> Qt's default application font
    }
    readonly property string uiFont: root.pickFont(["SF Pro Text", "SF Pro", "Inter", "Roboto", "Noto Sans"])
    readonly property string displayFont: root.pickFont(["SF Pro Display", "SF Pro", "Inter", "Roboto", "Noto Sans"])
    readonly property real contentWidth: 380
    readonly property real availableScreenHeight: screenGeometry && Number(screenGeometry.height) > 0 ? Number(screenGeometry.height) : Number(Screen.height || 760)
    readonly property real drawerWidth: 312
    // The cost popout sizes to its footer content (chartArea is fixed at 150) so a sparse
    // provider (no burn rate, no per-model data — e.g. Gemini web) doesn't leave dead glass.
    // 360 = header + tabs + chart + the 3 Today/7d/30d rows; +23 for the burn line; +per-model.
    // Per-section pixel heights of the cost-popout footer, shared by the footer rows
    // themselves AND drawerHeightFor()'s deterministic height mirror so the two cannot drift
    // (the same metricsBodyHeight() mirror foot-gun CLAUDE.md documents — but with a single
    // source of truth here). A row contributes its preferredHeight + the footer ColumnLayout
    // spacing (5). Change a row's height in ONE place and both the row and the mirror follow.
    readonly property int costFooterSpacing: 5
    readonly property int costBurnRowHeight: 18
    readonly property int costModelRowHeight: 16
    // Extra-usage section height — single source shared by the section's own
    // Layout.preferredHeight AND metricsBodyHeight()'s mirror, so the two can't drift
    // (the documented foot-gun; the metric rows + cost section are already single-sourced
    // via rowMetricHeight()/costSectionHeight()).
    readonly property int extraUsageBodyHeight: 86

    function drawerHeightFor() {
        const cost = root.costSummary() || ({});
        let h = 360;
        if (Number(cost.burnRatePerDay || 0) > 0)
            h += root.costBurnRowHeight + root.costFooterSpacing;   // burn line (= 23)
        const models = root.costModelBreakdown().length;
        if (models > 0)   // separator + "TOP MODELS" header (= 6 + 18) + N model rows (21 each)
            h += 6 + 18 + models * (root.costModelRowHeight + root.costFooterSpacing);
        return h;
    }
    readonly property real drawerHeight: root.drawerHeightFor()
    readonly property real drawerGap: 22

    signal providerRequested(string provider)
    signal refreshRequested()
    signal closeRequested()
    signal refreshIntervalRequested(real minutes)
    signal configChangeRequested(var changes)
    signal testNotificationRequested()
    // Item 1: request a FOREGROUND refresh (backend run without --background) so a locked
    // KWallet pops its native unlock dialog. main.qml builds the flag conditionally.
    signal unlockWalletRequested()

    readonly property var refreshIntervalOptions: [1, 2, 5, 15, 30]
    // Optimistic selection so the segmented control updates the instant it is
    // tapped, before the backend round-trip lands a fresh telemetry payload.
    property real pendingRefreshInterval: 0

    function currentRefreshInterval() {
        const cfg = root.telemetry && root.telemetry.config;
        const minutes = cfg ? Number(cfg.refreshIntervalMinutes) : 0;
        return (isFinite(minutes) && minutes > 0) ? minutes : 5;
    }

    function effectiveRefreshInterval() {
        return root.pendingRefreshInterval > 0 ? root.pendingRefreshInterval : root.currentRefreshInterval();
    }

    function selectRefreshInterval(minutes) {
        if (Number(minutes) === root.effectiveRefreshInterval())
            return ;

        root.pendingRefreshInterval = Number(minutes);
        root.refreshIntervalRequested(Number(minutes));
    }

    function providerData() {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({
        });
        return providers[root.selectedProvider] || {
            "label": root.selectedProvider,
            "limits": [],
            "status": "missing",
            "source": "none"
        };
    }

    function providerLimits() {
        const provider = root.providerData();
        return provider.limits || [];
    }

    function usageLimits() {
        const limits = root.providerLimits();
        let rows = [];
        for (let i = 0; i < limits.length; ++i) {
            if (!limits[i].isExtraUsage)
                rows.push(limits[i]);
        }
        return rows;
    }

    function extraUsageLimit() {
        const limits = root.providerLimits();
        for (let i = 0; i < limits.length; ++i) {
            if (limits[i].isExtraUsage)
                return limits[i];
        }
        return null;
    }

    function hasExtraUsage() {
        const provider = root.providerData();
        return provider.formattedExtraUsageDetail !== undefined && String(provider.formattedExtraUsageDetail).length > 0;
    }

    function safePercent(value) {
        let n = Number(value || 0);
        if (!isFinite(n)) return 0;
        return Math.max(0, Math.min(100, n));
    }

    // compactCount + trimDecimals live in lib/format.js (imported as Fmt) — a SINGLE source
    // shared with Python's accounting.compact_token_count via the node parity test
    // (tests/test_compact_count_parity.py), so the panel and the backend cost lines can't drift.

    function tabLabel(provider) {
        if (provider === "gemini") return "Gemini";
        if (provider === "antigravity") return "Antigravity";
        if (provider === "codex") return "Codex";
        if (provider === "claude") return "Claude";
        if (provider === "grok") return "Grok";
        return "Codex";
    }

    function switcherIconSource(provider, selected) {
        const suffix = selected ? "selected" : "inactive";
        return Qt.resolvedUrl("../images/tallybar-provider-icons/" + provider + "-" + suffix + ".svg");
    }

    function logoPixelSize(provider, baseSize) {
        if (provider === "antigravity") return baseSize * 1.04;
        if (provider === "gemini" || provider === "codex") return baseSize * 1.08;
        if (provider === "claude") return baseSize * 1.16;
        return baseSize;
    }

    function hexToRgba(c, alpha) {
        if (typeof c === 'string' && c.charAt(0) === '#') {
            var r = parseInt(c.substring(1, 3), 16) / 255.0;
            var g = parseInt(c.substring(3, 5), 16) / 255.0;
            var b = parseInt(c.substring(5, 7), 16) / 255.0;
            return Qt.rgba(r, g, b, alpha);
        }
        return Qt.rgba(c.r, c.g, c.b, alpha);
    }

    // The popup paints its own fixed dark "glass" background (NoBackground), so colors
    // are a fixed light palette — PlasmaCore.Theme does not exist in Plasma 6 and throws
    // a silent TypeError. Per-provider accents come from the backend; the pre-telemetry
    // fallback lives in lib/format.js (Fmt.providerAccent), shared with CompactRepresentation.
    function accentColor() {
        return root.providerData().accentColor || Fmt.providerAccent(root.selectedProvider);
    }

    function tabAccentColor(providerKey) {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({});
        const p = providers[providerKey] || {};
        return p.accentColor || Fmt.providerAccent(providerKey);
    }

    function tabAccentTintColor(providerKey, alpha) {
        return root.hexToRgba(root.tabAccentColor(providerKey), alpha);
    }

    function accentTintColor(alpha) {
        return root.tabAccentTintColor(root.selectedProvider, alpha);
    }

    function switcherTabWidth(provider) {
        if (provider === "claude" || provider === "gemini") return 60;
        if (provider === "antigravity") return 78;
        return 54;
    }

    function switcherTabs() {
        const source = root.tabs || [];
        return source.length === 0 ? [] : source;
    }

    function switcherContentWidth() {
        const source = root.switcherTabs();
        let width = 0;
        for (let i = 0; i < source.length; ++i) {
            width += root.switcherTabWidth(source[i]);
        }
        return width;
    }

    function switcherOuterPadding() {
        return 16;
    }

    function switcherComputedGap(containerWidth) {
        const count = root.switcherTabs().length;
        if (count <= 1) return 0;
        const available = Math.max(0, containerWidth - root.switcherOuterPadding() * 2);
        // Cap the inter-tab gap. The dock Row is horizontalCenter-anchored, so with providers
        // hidden via the visibility feature the raw fill gap balloons (~190px at 2 tabs) and
        // pins the tabs to opposite dock edges; clamping it clusters few-tab layouts centered.
        // The 4- and 5-tab defaults sit below the cap (~23px / ~4px), so their fill gap is
        // unchanged and the row still spans the full padded width — pixel-identical there.
        const raw = (available - root.switcherContentWidth()) / (count - 1);
        return Math.max(1, Math.min(24, raw));
    }

    function providerTabFontSize(provider) {
        return 11;
    }

    function primaryTextColor(alpha) {
        // Fixed light-on-dark palette — the popup paints its own dark glass, so
        // colors must NOT track Kirigami.Theme/PlasmaCore.Theme (a light global
        // theme would render dark text invisible against the glass). See CLAUDE.md.
        return Qt.rgba(0.96, 0.97, 1, alpha === undefined ? 0.96 : alpha);
    }

    function mutedTextColor(alpha) {
        return Qt.rgba(0.86, 0.9, 1, alpha);
    }

    function separatorColor(alpha) {
        return Qt.rgba(1, 1, 1, alpha);
    }

    function neutralUsageColor(alpha) {
        return Qt.rgba(1, 1, 1, alpha);
    }

    function usageOpacityFromRatio(ratio, minimum, maximum) {
        const value = Number(ratio);
        if (!isFinite(value) || value <= 0) return 0;
        const clamped = Math.max(0, Math.min(1, value));
        return minimum + clamped * (maximum - minimum);
    }

    function usageOpacityFromPercent(percent, minimum, maximum) {
        return root.usageOpacityFromRatio(root.safePercent(percent) / 100, minimum, maximum);
    }



    // The "session" (primary, short-window) limit for the per-tab usage preview:
    // a limit labelled Session, else the first non-extra-usage limit.
    function providerSessionPercent(providerKey) {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({});
        const provider = providers[providerKey] || ({});
        const limits = provider.limits || [];
        let firstUsage = -1;
        for (let i = 0; i < limits.length; ++i) {
            if (limits[i].isExtraUsage) continue;
            if (firstUsage < 0) firstUsage = i;
            if (String(limits[i].label || "").toLowerCase().indexOf("session") >= 0)
                return root.safePercent(limits[i].percent || 0);
        }
        return firstUsage >= 0 ? root.safePercent(limits[firstUsage].percent || 0) : 0;
    }

    function tabUsageColor(providerKey, percent, selected) {
        // Usage bars stay the provider color at every percentage — no warning/critical
        // recolor; the fill width still encodes how full it is.
        return root.tabAccentColor(providerKey);
    }

    function metricUsedText(limit) {
        return String(limit.formattedUsedText || "").trim();
    }

    function metricDetailLeftText(limit) {
        return String(limit.formattedDetailLeft || "").trim();
    }

    function metricDetailRightText(limit) {
        return String(limit.formattedDetailRight || "").trim();
    }

    function metricDetailText(limit) {
        return String(limit.formattedDetailText || "").trim();
    }

    // Position (0-100) of the elapsed-time marker on a usage bar, or -1 when the
    // backend didn't compute one. `pacePercent` is what usage WOULD be if the
    // window were spent evenly (parsers.usage_pace_detail), so the marker turns a
    // bare percentage into a pace read: fill past the marker = burning faster
    // than the window sustains, fill short of it = headroom.
    //
    // Deliberately NOT a colour change. Bars stay provider-accent at every
    // percentage; competitors signal pace by recolouring to amber/red, we mark
    // time instead. Endpoints are dropped (<=0, >=100) — a marker pinned to
    // either edge reads as chrome, not information.
    function metricPacePercent(limit) {
        if (!limit)
            return -1;
        const p = Number(limit.pacePercent);
        if (!isFinite(p) || p <= 0 || p >= 100)
            return -1;
        return p;
    }

    function extraUsageDetail() {
        return String(root.providerData().formattedExtraUsageDetail || "").trim();
    }

    function costSummary() {
        const provider = root.providerData();
        return provider.costSummary || provider.cost || null;
    }

    // Cross-provider "All AI" cost totals — summed from every provider's costSummary, the
    // data the --once snapshot already carries (only the --cost export surfaced it before).
    // Guarded like cost_export: a provider with no costSummary is skipped, never a throw.
    // mtd = true month-to-date (sum of inMonth calendar buckets), matching the budget.
    function allAiTotals() {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({});
        let m30 = 0, mtd = 0, any = false;
        for (const key in providers) {
            const c = providers[key] ? providers[key].costSummary : null;
            if (!c)
                continue;
            any = true;
            // 30-day total = the sum of each provider's "Last 30 days" line, so the All-AI
            // figure ADDS UP to what every tab shows. mtd (calendar month-to-date) is kept
            // only for the monthly-budget comparison, where it is the correct window.
            m30 += Number(c.cost30d || 0);
            const buckets = c.monthlyTokenUsage || [];
            for (let i = 0; i < buckets.length; ++i)
                if (buckets[i] && buckets[i].inMonth)
                    mtd += Number(buckets[i].cost || 0);
        }
        return { "has": any, "m30": m30, "mtd": mtd };
    }

    function monthlyBudgetValue() {
        return Number(root.configValue("monthlyBudget", 0)) || 0;
    }

    // Degraded-state banner text from telemetry.diagnostics — values the backend computes
    // but the UI showed nowhere. Empty string = no banner.
    function degradedText() {
        const d = root.telemetry && root.telemetry.diagnostics;
        if (!d)
            return "";
        // A whole-refresh fatal, or a post-snapshot (cache/notify) failure, reprints STALE
        // cached data with ok:true — the most severe cases, surfaced first. Truncate to ~80
        // chars so a stack string can't overflow the one-line banner (no raw tracebacks).
        if (d.fatal)
            return i18n("Backend error — showing last values (%1)", String(d.fatal).slice(0, 80));
        if (d.post_snapshot_error)
            return i18n("Post-refresh step failed — alerts and cache may be stale");
        if (d.cost_summary_timeout)
            return i18n("Cost scan timed out — totals may be stale");
        const wallet = d.browser && d.browser.kwallet ? d.browser.kwallet.status : "";
        if (wallet === "wallet-locked")
            return i18n("KWallet is locked — unlock it for full usage data");
        if (wallet === "wallet-state-unknown")
            return i18n("KWallet state couldn't be checked — some usage may be missing");
        if (d.cost_summary_error)
            return i18n("Cost summary error — totals may be incomplete");
        if (d.orchestrator_error)
            return i18n("Some providers failed to load — data may be partial");
        if (d.pricing_refresh_error)
            return i18n("Price list update failed — costs use cached rates");
        // refresh_skipped === "screen-locked" is deliberately NOT a banner: it happens on
        // every unlock, and the banner's +32px chrome (popupChromeHeight) squeezes the
        // metrics body into a scrollbar. The paused state rides the "Updated…" subtitle
        // suffix instead — same footprint, no layout shift (updatedText()/refreshPaused()).
        return "";
    }

    // A short, actionable hint for a provider whose local server/CLI isn't running
    // (status "not-running"). Empty for providers with no local process.
    function notRunningHint(providerKey) {
        switch (providerKey) {
        case "codex": return i18n("Codex isn't running — start the Codex app to read usage.");
        case "antigravity": return i18n("Antigravity isn't running — open it to read live session usage.");
        case "grok": return i18n("No recent Grok session — start one to see usage.");
        default: return i18n("%1 isn't running.", root.tabLabel(providerKey));
        }
    }

    // The site the user logs into for a given provider's cookies (used by the
    // missing-cookies remediation hint, Item 1).
    function providerLoginSite(providerKey) {
        switch (providerKey) {
        case "claude": return "claude.ai";
        case "codex": return "chatgpt.com";
        case "gemini": return "gemini.google.com";
        case "antigravity": return "antigravity";
        default: return providerKey;
        }
    }

    // The full login URL for a provider whose empty state is missing-cookies/unauthorized —
    // the "Sign in to <site>" button opens it so the browser lands a fresh session cookie.
    // Empty for providers with no browser-cookie login (e.g. Antigravity's OAuth/local path),
    // which suppresses the button for them.
    function providerLoginUrl(providerKey) {
        switch (providerKey) {
        case "claude": return "https://claude.ai/login";
        case "codex": return "https://chatgpt.com/";
        case "gemini": return "https://gemini.google.com/";
        default: return "";
        }
    }

    // Defense-in-depth: only hand https strings to the browser. Backend-supplied URLs
    // (e.g. the Gemini sign-in link scraped from fetched HTML) route through here so a
    // non-https or non-string value is never launched.
    function openExternalUrlSafe(url) {
        const s = String(url || "");
        if (s.indexOf("https://") === 0)
            Qt.openUrlExternally(s);
    }

    // True when the selected provider's empty state should offer a browser sign-in button:
    // its cookies are missing or its saved session was rejected, AND a login URL exists.
    function emptyStateShowsSignIn() {
        const s = String(root.providerData().status || "");
        return (s === "missing-cookies" || s === "unauthorized")
            && root.providerLoginUrl(root.selectedProvider).length > 0;
    }

    // Actionable hint for a missing-cookies state: name the browser/profile to log into,
    // or say no profiles were found. Reads the per-store stats the backend already ships in
    // diagnostics.browser.stores ({browser, profile, rows, decrypted, matched}). Item 1.
    function cookieHint() {
        const d = root.telemetry && root.telemetry.diagnostics;
        const browser = d && d.browser ? d.browser : null;
        const stores = browser && browser.stores ? browser.stores : [];
        const site = root.providerLoginSite(root.selectedProvider);
        if (!stores || stores.length === 0)
            return i18n("No supported browser profiles found — sign in to %1 in Chrome, Firefox, or a Chromium browser.", site);
        // Pick the store with the most decrypted rows — the one most likely to be the user's
        // active profile — and tell them to sign in there.
        let best = stores[0];
        for (let i = 1; i < stores.length; ++i) {
            if (Number(stores[i].decrypted || 0) > Number(best.decrypted || 0))
                best = stores[i];
        }
        const where = best.profile ? best.browser + " (" + best.profile + ")" : best.browser;
        return i18n("Sign in to %1 in %2, then refresh.", site, where);
    }

    // True when the selected provider's empty state is a locked/unreadable KWallet — the
    // states that get the "Unlock KWallet" button (Item 1). wallet-state-unknown (the wallet
    // couldn't be checked during a background refresh) is treated like wallet-locked here:
    // unlocking and a foreground refresh is the same remedy.
    function emptyStateShowsUnlock() {
        const s = String(root.providerData().status || "");
        return s === "wallet-locked" || s === "wallet-state-unknown";
    }

    // The empty-state message, enriched with a remediation hint per status (Item 1). Falls
    // back to the provider message / generic "No usage data".
    function emptyStateMessage() {
        if (root.lastError.length > 0)
            return root.lastError;
        const p = root.providerData();
        const status = String(p.status || "");
        // Cold start: a brand-new user's first open (no cached last_snapshot.json yet) hits the
        // empty state while the very first fetch runs. Without this branch it reads "No usage
        // data" — looks like failure though the hero subtitle already says "Refreshing". Only
        // fires when there's no concrete problem status to show.
        if (root.loading && (status === "" || status === "ok"))
            return i18n("Fetching usage data…");
        if (status === "wallet-locked")
            return i18n("KWallet is locked. Unlock it to read this provider's session cookies.");
        if (status === "wallet-state-unknown")
            return i18n("KWallet state couldn't be checked. Unlock it and refresh to read this provider's session cookies.");
        if (status === "missing-cookies")
            return root.cookieHint();
        // codex.py never emits "not-running" — it uses "missing-cli" for the CLI-absent
        // case, so both must route through the same actionable hint.
        if (status === "not-running" || status === "missing-cli")
            return root.notRunningHint(root.selectedProvider);
        if (status === "api-error" || status === "error" || status === "timeout" || status === "unauthorized")
            return String(p.message || "") || i18n("Couldn't reach %1.", root.tabLabel(root.selectedProvider));
        return String(p.message || "") || i18n("No usage data");
    }

    // Feature 3: the backend attaches an actionUrl to some bad states (e.g. Gemini/Claude
    // unauthorized → a sign-in page). When present, the empty-state message becomes a clickable
    // link that opens it. Empty string = no link.
    function providerActionUrl() {
        const s = String(root.providerData().status || "");
        if (!root.statusIsBad(s))
            return "";
        return String(root.providerData().actionUrl || "");
    }
    // Mirror of CompactRepresentation.statusIsBad — the bad/actionable statuses.
    function statusIsBad(status) {
        switch (status) {
        case "missing-cookies":
        case "missing-cli":
        case "unauthorized":
        case "api-error":
        case "error":
        case "timeout":
        case "wallet-locked":
        case "wallet-state-unknown":
        case "not-running":
        case "no-port":
        case "missing-oauth":
        case "oauth-expired":
        case "oauth-unavailable":
            return true;
        default:
            return false;
        }
    }

    // A provider tab whose live status is bad while it may still show cached limits — the
    // message is otherwise invisible, so the tab gets a warning glyph + tooltip (Item 1).
    function tabStatusBad(providerKey) {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({});
        const p = providers[providerKey];
        if (!p)
            return false;
        // Feature 7: a muted provider never shows the amber warning glyph / bad-status badge.
        if (root.providerMuted(providerKey))
            return false;
        const s = String(p.status || "");
        // choose_antigravity_result (providers/antigravity.py) can terminate on any of
        // these — they are real, reachable "bad" states, not just the generic ones above.
        return s === "api-error" || s === "error" || s === "timeout" || s === "unauthorized" || s === "wallet-locked" ||
               s === "wallet-state-unknown" ||
               s === "no-port" || s === "missing-oauth" || s === "oauth-expired" || s === "oauth-unavailable";
    }

    function tabStatusMessage(providerKey) {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({});
        const p = providers[providerKey];
        return p ? (String(p.message || "") || String(p.status || "")) : "";
    }

    function hasCostSummary() {
        const cost = root.costSummary();
        if (!cost)
            return false;

        return String(cost.today || cost.sessionLine || cost.last30Days || cost.monthLine || "").trim().length > 0;
    }

    // Render "$188" as "$ 188"; dropped in the refactor while callers stayed, which
    // silently killed the Today / Last 30 days cost lines.
    function menuMoneySpacing(text) {
        return String(text || "").replace(/\$(?=\d)/g, "$ ");
    }

    function normalizedCostLine(value, label) {
        const text = String(value || "").trim();
        if (text.length === 0)
            return "";

        const spacedText = root.menuMoneySpacing(text);
        const lower = text.toLowerCase();
        const lowerLabel = String(label || "").toLowerCase();
        if (lower.indexOf(lowerLabel + ":") === 0)
            return spacedText;

        if (label === "Today" && lower.endsWith(" today")) {
            const amount = text.slice(0, text.length - " today".length).trim();
            return amount.length > 0 ? "Today: " + root.menuMoneySpacing(amount) : spacedText;
        }
        if (label === "Last 30 days" && lower.endsWith(" last 30 days")) {
            const amount = text.slice(0, text.length - " last 30 days".length).trim();
            return amount.length > 0 ? "Last 30 days: " + root.menuMoneySpacing(amount) : spacedText;
        }
        return spacedText;
    }

    function todayCostText(cost) {
        return root.normalizedCostLine(cost.today || cost.sessionLine || "", "Today");
    }

    function weekCostText(cost) {
        return root.normalizedCostLine(cost.last7Days || cost.weekLine || "", "Last 7 days");
    }

    function monthCostText(cost) {
        return root.normalizedCostLine(cost.last30Days || cost.monthLine || "", "Last 30 days");
    }

    function costLineValue(text, label) {
        const value = String(text || "").trim();
        const prefix = String(label || "") + ":";
        if (value.toLowerCase().indexOf(prefix.toLowerCase()) === 0)
            return value.slice(prefix.length).trim();

        return value;
    }

    // Only show the cost section when we actually have data — empty
    // "Local estimate unavailable" placeholders look broken.
    function hasCostSection() {
        return root.hasCostSummary();
    }

    function costWeekHistory() {
        const cost = root.costSummary() || ({
        });
        const history = cost.weeklyTokenUsage || cost.daily || [];
        return history;
    }

    function costMonthHistory() {
        const cost = root.costSummary() || ({
        });
        return cost.monthlyTokenUsage || [];
    }

    function costHourHistory() {
        const cost = root.costSummary() || ({
        });
        return cost.hourlyTokenUsage || [];
    }

    function maxCostTokens(history, monthOnly) {
        let maxTokens = 0;
        for (let i = 0; i < history.length; ++i) {
            if (monthOnly && history[i].inMonth !== true)
                continue;

            maxTokens = Math.max(maxTokens, Number(history[i].tokens || 0));
        }
        return Math.max(1, maxTokens);
    }

    function costHistoryTotal(history, monthOnly) {
        let total = 0;
        for (let i = 0; i < history.length; ++i) {
            if (monthOnly && history[i].inMonth !== true)
                continue;

            total += Number(history[i].tokens || 0);
        }
        return total;
    }

    function maxWeekTokens() {
        return root.maxCostTokens(root.costWeekHistory(), false);
    }

    function maxMonthTokens() {
        return root.maxCostTokens(root.costMonthHistory(), true);
    }

    function maxHourTokens() {
        return root.maxCostTokens(root.costHourHistory(), false);
    }

    // Locale-aware date formatting (QML's Date.prototype.toLocaleDateString extension) so
    // the tooltip head follows the system locale's month names/order instead of a
    // hardcoded English array — i18n() alone wouldn't translate these (no catalog ships).
    function shortMonthDate(iso) {
        const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (!m)
            return "";

        const date = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
        return date.toLocaleDateString(Qt.locale(), "MMM d");
    }

    function costGraphTotal() {
        if (root.costGraphMode === "day")
            return root.costHistoryTotal(root.costHourHistory(), false);

        const monthMode = root.costGraphMode === "month";
        return root.costHistoryTotal(monthMode ? root.costMonthHistory() : root.costWeekHistory(), monthMode);
    }

    function costGraphSubtitle() {
        const total = Fmt.compactCount(root.costGraphTotal());
        if (root.costGraphMode === "day")
            return i18n("%1 today", total);

        return root.costGraphMode === "month" ? i18n("%1 this month", total) : i18n("%1 this week", total);
    }

    function monthCellOpacity(cell) {
        if (!cell || cell.inMonth !== true)
            return 0;

        const tokens = Number(cell.tokens || 0);
        if (tokens <= 0)
            return 1;

        const ratio = Math.max(0, Math.min(1, tokens / root.maxMonthTokens()));
        return root.usageOpacityFromRatio(ratio, 0.28, 0.92);
    }

    function monthCellColor(cell) {
        if (!cell || cell.inMonth !== true)
            return "transparent";

        const tokens = Number(cell.tokens || 0);
        if (tokens <= 0)
            return root.neutralUsageColor(0.16);

        return root.accentColor();
    }

    function formatTooltipUsd(value) {
        const amount = Number(value || 0);
        if (!isFinite(amount) || amount <= 0)
            return "$0.00";
        if (amount < 0.01)
            return "<$0.01";
        if (amount < 1)
            return "$" + amount.toFixed(3);
        return "$" + amount.toLocaleString(Qt.locale("en_US"), 'f', 2);
    }

    function longMonthDate(iso) {
        const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (!m)
            return "";
        const date = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
        return date.toLocaleDateString(Qt.locale(), "MMMM d");
    }

    // Tooltips are TWO compact lines (date on top, "N tok · $cost" below) so the box
    // stays narrow enough to never be cut off at the popout edge while still following
    // the cursor — see the chartTip x clamp.
    function monthCellTooltipText(cell) {
        if (!cell || cell.inMonth !== true)
            return "";
        const dateLabel = root.longMonthDate(cell.date);
        const tokens = Number(cell.tokens || 0);
        if (tokens <= 0)
            return dateLabel + "\n" + i18n("No usage");
        const costValue = Number(cell.cost || 0);
        const costText = costValue > 0 ? " · " + root.formatTooltipUsd(costValue) : "";
        return dateLabel + "\n" + i18n("%1 tok", Fmt.compactCount(tokens)) + costText;
    }

    function formatHourLong(hour) {
        const h = Math.max(0, Math.min(23, Number(hour || 0)));
        const date = new Date(2000, 0, 1, h, 0, 0);
        // "AP" is Qt's locale-aware AM/PM designator (translated per-locale), replacing the
        // hardcoded " AM"/" PM" literals.
        return date.toLocaleTimeString(Qt.locale(), "h AP");
    }

    function hourTooltipText(item) {
        if (!item)
            return "";
        const head = root.formatHourLong(item.hour);
        const tokens = Number(item.tokens || 0);
        if (tokens <= 0)
            return head + "\n" + i18n("No usage");
        const costValue = Number(item.cost || 0);
        const costText = costValue > 0 ? " · " + root.formatTooltipUsd(costValue) : "";
        return head + "\n" + i18n("%1 tok", Fmt.compactCount(tokens)) + costText;
    }

    function weekDayTooltipText(item) {
        if (!item)
            return "";
        const dayName = String(item.day || "").slice(0, 3);
        const dateLabel = root.shortMonthDate(item.date);
        const head = (dayName ? dayName + ", " : "") + dateLabel;
        const tokens = Number(item.tokens || 0);
        if (tokens <= 0)
            return head + "\n" + i18n("No usage");
        const costValue = Number(item.cost || 0);
        const costText = costValue > 0 ? " · " + root.formatTooltipUsd(costValue) : "";
        return head + "\n" + i18n("%1 tok", Fmt.compactCount(tokens)) + costText;
    }

    // Append the bucket's per-model breakdown to a chart tooltip — one "Model  Ntok · $x"
    // line per backend per-bucket models row (already top-4 by cost). Called only for the
    // CLICKED bucket (chartArea.expandedKey); hover tips stay the two base lines.
    // totalLabel ("Daily total"/"Hourly total") prefixes the base total line in the
    // expanded view only — once a single model dominates a bucket, the unlabeled total
    // reads as a nameless model row right above a near-identical named one.
    function withModelLines(base, bucket, totalLabel) {
        // Duck-type on length, NOT Array.isArray: a bucket that crossed a Repeater
        // boundary (modelData) carries `models` as a QVariantList, which indexes and
        // has .length like an array but fails Array.isArray — that silently dropped
        // every breakdown row.
        const rows = (bucket && bucket.models && bucket.models.length > 0) ? bucket.models : [];
        let text = base;
        if (rows.length > 0 && totalLabel) {
            // The total is base's second line ("<date>\n<N tok · $x>"); a "No usage"
            // bucket never gets here (no model rows), so the prefix can't land on it.
            const nl = text.indexOf("\n");
            if (nl >= 0)
                text = text.slice(0, nl + 1) + totalLabel + "   " + text.slice(nl + 1);
        }
        for (let i = 0; i < rows.length; ++i) {
            const r = rows[i];
            const costValue = Number(r.cost || 0);
            const costText = costValue > 0 ? " · " + root.formatTooltipUsd(costValue) : "";
            text += "\n" + root.prettyModelName(r.model) + "   "
                    + i18n("%1 tok", Fmt.compactCount(Number(r.tokens || 0))) + costText;
        }
        return text;
    }

    function toggleCostPopout() {
        if (!root.hasCostSection()) {
            root.costDrawerOpen = false;
            return ;
        }
        root.settingsDrawerOpen = false;  // the two flyouts are mutually exclusive
        // Always (re)open on the Week tab — Day/Month are opt-in each time.
        if (!root.costDrawerOpen)
            root.costGraphMode = "week";
        root.costDrawerOpen = !root.costDrawerOpen;
    }

    function toggleSettingsPopout() {
        root.costDrawerOpen = false;  // mutually exclusive with the cost flyout
        root.settingsDrawerOpen = !root.settingsDrawerOpen;
    }

    // --- Settings config reads (with defaults matching backend.public_config) ---
    function configValue(key, fallback) {
        const c = root.telemetry && root.telemetry.config;
        return (c && c[key] !== undefined && c[key] !== null) ? c[key] : fallback;
    }
    function panelModeValue() {
        return String(root.configValue("panelDisplayMode", "percent"));
    }
    function notificationsEnabledValue() {
        return root.configValue("notificationsEnabled", true) !== false;
    }
    function thresholdsValue() {
        const t = root.configValue("notificationThresholds", [90, 100]);
        return Array.isArray(t) ? t : [90, 100];
    }
    function thresholdActive(v) {
        return root.thresholdsValue().indexOf(v) >= 0;
    }
    function toggleThreshold(v) {
        let t = root.thresholdsValue().slice();
        const i = t.indexOf(v);
        if (i >= 0) {
            if (t.length <= 1)
                return ;  // keep at least one alert level on (don't clear to "never")
            t.splice(i, 1);
        } else {
            t.push(v);
        }
        t.sort((a, b) => a - b);
        root.configChangeRequested({
            "notificationThresholds": t
        });
    }

    readonly property var allProviderKeys: ["codex", "claude", "gemini", "antigravity", "grok"]
    function providersValue() {
        const p = root.configValue("providers", root.allProviderKeys);
        return Array.isArray(p) && p.length > 0 ? p : root.allProviderKeys;
    }
    function providerEnabled(key) {
        return root.providersValue().indexOf(key) >= 0;
    }
    function toggleProvider(key) {
        let p = root.providersValue().slice();
        const i = p.indexOf(key);
        if (i >= 0) {
            if (p.length <= 1)
                return ;  // keep at least one provider visible
            p.splice(i, 1);
        } else {
            p.push(key);
        }
        const ordered = root.allProviderKeys.filter((k) => p.indexOf(k) >= 0);
        root.configChangeRequested({
            "providers": ordered
        });
    }
    // Feature 7: per-provider mute. A muted provider raises no notifications and is dropped
    // from the panel's badge/attention logic; it stays visible in the popup with a muted hint.
    function mutedProvidersValue() {
        const m = root.configValue("mutedProviders", []);
        return Array.isArray(m) ? m : [];
    }
    function providerMuted(key) {
        return root.mutedProvidersValue().indexOf(key) >= 0;
    }
    function toggleMuted(key) {
        let m = root.mutedProvidersValue().slice();
        const i = m.indexOf(key);
        if (i >= 0)
            m.splice(i, 1);
        else
            m.push(key);
        const ordered = root.allProviderKeys.filter((k) => m.indexOf(k) >= 0);
        root.configChangeRequested({
            "mutedProviders": ordered
        });
    }

    function providerShortLabel(key) {
        if (key === "codex") return "Codex";
        if (key === "claude") return "Claude";
        if (key === "gemini") return "Gemini";
        if (key === "antigravity") return "Antigr.";
        if (key === "grok") return "Grok";
        return key;
    }

    function dashboardUrl() {
        if (root.selectedProvider === "gemini")
            return "https://gemini.google.com/usage";

        if (root.selectedProvider === "antigravity")
            return "https://gemini.google.com/usage";

        if (root.selectedProvider === "codex")
            return "https://chatgpt.com/codex/settings/usage";

        if (root.selectedProvider === "claude")
            return "https://claude.ai/settings/usage";

        if (root.selectedProvider === "grok")
            return "https://grok.com/?_s=usage";

        return "https://chatgpt.com/codex/settings/usage";
    }

    function statusUrl() {
        if (root.selectedProvider === "gemini" || root.selectedProvider === "antigravity")
            return "https://www.google.com/appsstatus/dashboard/products/npdyhgECDJ6tB66MxXyo/history";

        if (root.selectedProvider === "codex")
            return "https://status.openai.com/";

        if (root.selectedProvider === "claude")
            return "https://status.claude.com/";

        if (root.selectedProvider === "grok")
            return "https://status.x.ai/";

        return "https://status.openai.com/";
    }

    function actionIcon(name) {
        return Qt.resolvedUrl("../images/action-icons/" + name + ".svg");
    }

    function quickActionRows() {
        // NB: no "Add Account" entry — the widget has no multi-account concept, and it
        // opened the identical dashboardUrl() as "Usage Dashboard" below (a confusing dupe).
        // The strip is a fixed-height (44px) centered Row, so dropping an entry doesn't touch
        // actionFooterHeight()'s mirror.
        return [{
            "label": i18n("Usage Dashboard"),
            "shortLabel": i18n("Usage"),
            "icon": root.actionIcon("usage-dashboard"),
            "url": root.dashboardUrl()
        }, {
            "label": i18n("Status Page"),
            "shortLabel": i18n("Status"),
            "icon": root.actionIcon("status-page"),
            "url": root.statusUrl()
        }, {
            "label": i18n("Refresh Now"),
            "shortLabel": i18n("Refresh"),
            "icon": "view-refresh",
            "command": "refresh"
        }, {
            "label": i18n("Settings"),
            "shortLabel": i18n("Settings"),
            "icon": "configure",
            "command": "settings"
        }];
    }

    function systemActionRows() {
        return [{
            "label": i18n("Close Panel"),
            "icon": "window-close",
            "command": "quit"
        }];
    }

    function actionRowHeight(row, index) {
        return 30;
    }

    function actionFooterHeight() {
        const rows = root.systemActionRows();
        let height = 0;
        for (let i = 0; i < rows.length; ++i) {
            height += root.actionRowHeight(rows[i], i);
        }
        // All-AI cross-provider total row (26) + its top separator (1), shown only when some
        // provider has cost data; then 1px rule + quick-action strip (44) + 1px rule + system
        // action rows (Quit). (The refresh-interval picker moved into the Settings popout.)
        const allAi = root.allAiTotals().has ? 27 : 0;
        return allAi + 1 + 44 + 1 + height;
    }

    function triggerAction(action) {
        const command = String(action && action.command || "");
        if (command === "refresh") {
            root.refreshRequested();
            return ;
        }
        if (command === "settings") {
            root.toggleSettingsPopout();
            return ;
        }
        if (command === "quit") {
            root.costDrawerOpen = false;
            root.closeRequested();
            return ;
        }
        const target = String(action && action.url || "");
        if (target.length > 0)
            root.openExternalUrlSafe(target);

    }

    function providerTierText() {
        const provider = root.providerData();
        const providerLabel = String(provider.label || root.selectedProvider || "").trim().toLowerCase();
        const providerKey = String(root.selectedProvider || "").trim().toLowerCase();
        const candidates = [provider.tier, provider.plan, provider.usageTier, provider.subscription, provider.subscriptionTier];
        for (let i = 0; i < candidates.length; ++i) {
            const text = String(candidates[i] || "").trim();
            const normalized = text.toLowerCase();
            if (text.length > 0 && normalized !== providerLabel && normalized !== providerKey)
                return text;

        }
        return "";
    }

    function providerSubtitleText() {
        const tier = root.providerTierText();
        const updated = root.updatedText();
        return tier.length > 0 ? updated + " · " + tier : updated;
    }

    // Bumped every minute while the popup is open so updatedText() re-evaluates and the
    // relative age actually advances (otherwise it'd show "just now" until the next refresh).
    property int nowTick: 0
    Timer {
        // 30s so the relative "Updated Xm ago" line advances promptly while the popup is open.
        interval: 30000
        repeat: true
        running: root.hostExpanded
        onTriggered: root.nowTick = root.nowTick + 1
    }

    // Feature 5: the last background refresh was skipped because the screen was locked, so the
    // painted snapshot is deliberately stale ("paused"), not a failure.
    function refreshPaused() {
        const d = root.telemetry && root.telemetry.diagnostics;
        return !!(d && d.refresh_skipped === "screen-locked");
    }

    // Age of the painted snapshot in seconds, or -1 when there's no usable timestamp.
    function snapshotAgeSeconds() {
        const _ = root.nowTick;  // register the per-minute tick as a binding dependency
        if (!(root.telemetry && root.telemetry.timestamp))
            return -1;
        const t = Date.parse(root.telemetry.timestamp);
        if (isNaN(t))
            return -1;
        return Math.max(0, (Date.now() - t) / 1000);
    }

    // 0 = fresh, 1 = stale (older than ~2× the refresh interval). Keyed off nowTick so it
    // advances while the popup is open. While loading we treat data as fresh (a refresh is
    // already in flight). Cache paints keep their original timestamp, so age is honest.
    function stalenessLevel() {
        if (root.loading)
            return 0;
        const secs = root.snapshotAgeSeconds();
        if (secs < 0)
            return 0;
        return secs > root.currentRefreshInterval() * 2 * 60 ? 1 : 0;
    }

    // Per-provider staleness: the backend's carry_forward_provider_last_good replaces a
    // transiently-failed claude/gemini/codex entry with the last-known-good one and stamps
    // stale=true on THAT provider's dict (independent of the top-level snapshot timestamp,
    // which may be fresh because the OTHER providers just refreshed fine). Guarded with
    // === true so an old-shape cached snapshot (no `stale` key) is safe.
    function providerStale() {
        return root.providerData().stale === true;
    }

    function updatedText() {
        if (root.loading)
            return i18n("Refreshing");

        if (root.telemetry && root.telemetry.timestamp) {
            const secs = root.snapshotAgeSeconds();
            // Distinguish a cold-cache paint from live data: the cache keeps its original
            // (older) timestamp, so this prefix plus the relative age tells the honest story.
            const prefix = root.liveLoaded ? "" : i18n("Cached · ");
            // Feature 5: a screen-lock-paused refresh keeps the old snapshot on purpose — say so.
            // Per-provider carry-forward (this provider's live fetch failed, its last-known-good
            // is being shown) gets a subtle honest "(cached)" tag — but not when the whole paint
            // is already a cold-cache one (the "Cached · " prefix would say it twice).
            let suffix = root.refreshPaused() ? i18n(" (paused: screen locked)") : "";
            if (!suffix && root.liveLoaded && root.providerStale())
                suffix = i18n(" (cached)");
            if (secs < 0)
                return prefix + i18n("Updated just now") + suffix;
            if (secs < 45)
                return prefix + i18n("Updated just now") + suffix;
            const mins = Math.round(secs / 60);
            if (mins < 60)
                return prefix + i18n("Updated %1m ago", mins) + suffix;
            const hrs = Math.round(mins / 60);
            if (hrs < 24)
                return prefix + i18n("Updated %1h ago", hrs) + suffix;
            return prefix + i18n("Updated %1d ago", Math.round(hrs / 24)) + suffix;
        }

        return i18n("Waiting for update");
    }

    onSelectedProviderChanged: root.costDrawerOpen = false
    onVisibleChanged: {
        if (!root.visible) {
            root.costDrawerOpen = false;
            root.settingsDrawerOpen = false;
        }

    }
    onHostExpandedChanged: {
        if (!root.hostExpanded) {
            root.costDrawerOpen = false;
            root.settingsDrawerOpen = false;
        }

    }
    onTelemetryChanged: {
        if (root.costDrawerOpen && !root.hasCostSection())
            root.costDrawerOpen = false;

        // Telemetry is authoritative for the saved interval; drop the optimistic pending value
        // so the control tracks persisted state — but NOT while a config write is in flight. A
        // periodic refresh that lands mid-save carries the OLD interval (main.qml preserves the
        // in-flight config), so clearing here would snap the pills back to the old value until
        // the write completes. The post-save telemetry update (configSaving already false) clears it.
        if (!root.configSaving)
            root.pendingRefreshInterval = 0;
    }
    // The token popout (costPopout) is a separate window; it takes focus itself when
    // shown, so no explicit focus call is needed here.
    // Size the popup to exactly fit the current provider's content so the body
    // never scrolls. Computed deterministically from the data — NOT from
    // metricsContent.implicitHeight, which reads 0 until the Repeater's
    // delegates instantiate asynchronously (that race is what collapsed the
    // popup to a tiny scroller before). Each term mirrors a real
    // Layout.preferredHeight in the layout below.
    function rowMetricHeight(limit) {
        const hasSplit = String(root.metricDetailLeftText(limit)).length > 0 || String(root.metricDetailRightText(limit)).length > 0;
        const hasLine = String(root.metricDetailText(limit)).length > 0;
        return 58 + (hasSplit ? 16 : 0) + (hasLine ? 18 : 0);
    }

    // SINGLE source for the cost section's height — used by BOTH the section's own
    // Layout.preferredHeight AND the metricsBodyHeight() mirror, so the two can never
    // diverge (the documented scrollbar foot-gun). Base 107, +20 for the breakdown line.
    function costSectionHeight() {
        if (!root.hasCostSection())
            return 0;
        const cost = root.costSummary() || ({});
        const hasBreak = String(cost.breakdown || "").trim().length > 0;
        return 107 + (hasBreak ? 20 : 0);
    }

    function compactUsd(value) {
        const v = Number(value || 0);
        if (v >= 1000)
            return "$" + (v / 1000).toFixed(1).replace(/\.0$/, "") + "K";
        if (v >= 100)
            return "$" + Math.round(v);
        // A nonzero figure must never render as "$0.00" — e.g. a trailing burn rate of
        // ~$0.004/day (which is enough to SHOW the burn-rate row) would otherwise read as
        // zero spend. Floor it to a visible "<$0.01" instead.
        if (v > 0 && v < 0.01)
            return "<$0.01";
        return "$" + v.toFixed(2);
    }

    // Top per-model cost rows (from the backend's modelBreakdown, already top-6 by $),
    // capped to a handful for the popout. Each row: {model, cost, tokens}.
    function costModelBreakdown(limit) {
        const cost = root.costSummary() || ({});
        const rows = Array.isArray(cost.modelBreakdown) ? cost.modelBreakdown : [];
        return rows.slice(0, limit || 4);
    }

    // Prettify a pricing-catalog key for display: "claude-opus-4-7" -> "Claude Opus 4.7",
    // "gemini-3.5-flash" -> "Gemini 3.5 Flash", "claude-opus-4-6-20250929" -> "Claude Opus 4.6".
    // CLI display names (already spaced) pass through; the "Unknown" utility bucket reads "Other".
    function prettyModelName(name) {
        let s = String(name || "").trim();
        if (s.length === 0 || s === "Unknown")
            return i18n("Other");
        if (s.indexOf(" ") >= 0)
            return s;  // already a display name
        s = s.replace(/-\d{8}$/, "");                               // drop a trailing date snapshot
        s = s.replace(/-(\d)/g, " $1").replace(/-/g, " ");          // "claude-opus-4-7" -> "claude opus 4 7"
        s = s.replace(/(\d) (\d)/g, "$1.$2");                        // "4 7" -> "4.7"
        return s.replace(/\b\w/g, (c) => c.toUpperCase());          // title-case words
    }

    function metricsBodyHeight() {
        let parts = [];
        const rows = root.usageLimits();
        for (let i = 0; i < rows.length; ++i)
            parts.push(root.rowMetricHeight(rows[i]));

        if (root.hasExtraUsage())
            parts.push(root.extraUsageBodyHeight);

        if (root.hasCostSection())
            parts.push(root.costSectionHeight());

        if (parts.length === 0)
            // Empty state. The "Unlock KWallet" / "Sign in" button (Item 1) needs ~44px more
            // than the bare message — keep this mirror in lockstep with the empty-state
            // ColumnLayout. (The two buttons are mutually exclusive by status, so one 44px add.)
            return (root.emptyStateShowsUnlock() || root.emptyStateShowsSignIn()) ? 174 : 130;

        let total = 0;
        for (let i = 0; i < parts.length; ++i)
            total += parts[i];

        return total + (parts.length - 1) * 8;
    }

    // Chrome around the scrollable body: top margin 6 + dock 58 + 8 + hero 50
    // + 8 + flickable top margin 10 + 8 + footer + bottom margin 10 = 158 + footer.
    // 158 = fixed chrome (margins + dock + hero + flickable margins + footer rules); the
    // footer height (incl. the All-AI total row) lives in actionFooterHeight(); the degraded
    // banner, when shown, adds its own fixed strip (24) + one ColumnLayout gap (8) above the body.
    readonly property real popupChromeHeight: 158 + root.actionFooterHeight() + (root.degradedText().length > 0 ? 32 : 0)
    readonly property real popupScreenCap: Math.max(440, root.availableScreenHeight - 96)
    readonly property real popupFitHeight: Math.max(360, Math.min(root.popupScreenCap, root.popupChromeHeight + root.metricsBodyHeight() + 6))

    // The main widget keeps its NORMAL size at all times — opening the token-usage
    // graph no longer widens it. Instead the graph appears as a SEPARATE floating
    // window beside the widget (see costPopout below), so the two read as distinct
    // cards with the desktop showing between them (the macOS reference layout).
    readonly property bool drawerExpanded: root.costDrawerOpen && root.hasCostSection()

    implicitWidth: root.contentWidth
    implicitHeight: root.popupFitHeight
    Layout.minimumWidth: root.contentWidth
    Layout.preferredWidth: root.contentWidth
    Layout.maximumWidth: root.contentWidth
    Layout.minimumHeight: root.popupFitHeight
    Layout.maximumHeight: root.popupFitHeight


    // Invisible anchor pinned to the LEFT EDGE of the widget. The token popup attaches
    // to THIS outer edge and opens LEFTWARD — toward the open desktop, away from the
    // screen edge the tray sits against (top-right) — so it lands cleanly beside the
    // body, submenu-style, and never over it. (Anchoring to the right edge folded it
    // back over the body because there's no room between the widget and the screen
    // edge; anchoring to an interior item like the Cost row had the same problem.)
    Item {
        id: flyoutAnchor

        // A small anchor point (not a tall rect) so KWin centers the popup on it
        // predictably. Offset DOWN from center so the card pops out next to the Cost
        // row (lower part of the widget) rather than level with the middle.
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: 160
        width: 1
        height: 1
    }

    // Anchor for the Settings flyout — same left-edge attach as flyoutAnchor, pinned near
    // the footer (where the gear lives) so the settings card opens beside the lower body.
    Item {
        id: settingsAnchor

        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: 240
        width: 1
        height: 1
    }

    // Token-usage graph extracted to CostPopout.qml. `root` carries the helpers /
    // cost data / costGraphMode state; `anchorItem` is the left-edge flyoutAnchor KWin
    // places the window against (opens leftward, beside the Cost row).
    CostPopout {
        root: root
        anchorItem: flyoutAnchor
    }

    // Settings flyout — extracted to SettingsPopout.qml. `root` carries the helper
    // functions / config state / signals; `anchorItem` is the invisible left-edge
    // anchor KWin places the window against (opens leftward, beside the lower body).
    SettingsPopout {
        root: root
        anchorItem: settingsAnchor
    }

    ColumnLayout {
        id: mainContent

        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: Math.max(0, root.contentWidth - 38)
        anchors.leftMargin: 19
        anchors.rightMargin: 19
        anchors.topMargin: 6
        anchors.bottomMargin: 10
        spacing: 8

        Item {
            id: providerDock

            Layout.fillWidth: true
            Layout.leftMargin: -5
            Layout.rightMargin: -5
            Layout.preferredHeight: 58

            Row {
                // Content-width (not full-width) so a capped inter-tab gap clusters few-tab
                // layouts centered instead of pinning them to opposite dock edges. At the 4-/5-tab
                // defaults the gap fills to the padded width, so centring reproduces the same
                // ~16px side margin the old left/right anchors gave — pixel-identical there.
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                spacing: root.switcherComputedGap(parent.width)

                Repeater {
                    model: Math.max(1, root.switcherTabs().length)

                    delegate: Item {
                        id: providerTab

                        required property int index
                        property string providerKey: root.switcherTabs()[Math.min(index, root.switcherTabs().length - 1)]
                        property bool selected: root.selectedProvider === providerKey
                        property var tabProvider: root.telemetry && root.telemetry.providers ? root.telemetry.providers[providerKey] : ({
                        })
                        property real sessionPct: root.providerSessionPercent(providerKey)
                        readonly property real tabContentWidth: Math.max(root.switcherTabWidth(providerKey), 54)

                        width: root.switcherTabWidth(providerKey)
                        height: parent.height

                        // Item 1: a provider whose live status is bad (api-error / timeout /
                        // unauthorized / wallet-locked) while it may still show cached data —
                        // surface the otherwise-invisible message on hover, plus the glyph below.
                        QQC2.ToolTip.delay: 350
                        QQC2.ToolTip.text: root.providerMuted(providerTab.providerKey)
                            ? i18n("Muted — alerts off")
                            : root.tabStatusMessage(providerTab.providerKey)
                        QQC2.ToolTip.visible: tabMouse.containsMouse
                            && (root.tabStatusBad(providerTab.providerKey) || root.providerMuted(providerTab.providerKey))

                        Rectangle {
                            id: tabSurface

                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            anchors.topMargin: 6
                            anchors.bottomMargin: 6
                            width: providerTab.tabContentWidth
                            radius: 9
                            color: providerTab.selected ? Qt.rgba(1, 1, 1, 0.18) : (tabMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent")
                            border.width: 1
                            border.color: providerTab.selected ? Qt.rgba(1, 1, 1, 0.12) : "transparent"

                            // Per-tab session-usage preview: faint track + a fill whose width is
                            // the provider's session percent, shown on every tab so usage is
                            // legible before selecting. Selected tab reads brightest.
                            Rectangle {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.leftMargin: 7
                                anchors.rightMargin: 7
                                anchors.bottomMargin: 4
                                height: 2
                                radius: height / 2
                                color: root.neutralUsageColor(providerTab.selected ? 0.22 : 0.14)

                                Rectangle {
                                    anchors.left: parent.left
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    width: providerTab.sessionPct > 0 ? Math.max(parent.height, parent.width * providerTab.sessionPct / 100) : 0
                                    radius: parent.radius
                                    color: root.tabUsageColor(providerTab.providerKey, providerTab.sessionPct, providerTab.selected)
                                    opacity: providerTab.selected ? 1.0 : 0.8

                                    Behavior on width {
                                        SpringAnimation {
    spring: 4.0
    damping: 0.85
    epsilon: 0.01
}

                                    }

                                }

                            }

                            Behavior on color {
                                ColorAnimation {
                                    duration: 140
                                    easing.type: Easing.OutCubic
                                }

                            }

                        }

                        Kirigami.Icon {
                            id: providerIcon

                            anchors.horizontalCenter: tabSurface.horizontalCenter
                            anchors.top: tabSurface.top
                            anchors.topMargin: 6
                            width: root.logoPixelSize(providerTab.providerKey, 16)
                            height: width
                            source: root.switcherIconSource(providerTab.providerKey, providerTab.selected)
                            // Recolor every provider glyph to the provider-name colour (white when
                            // selected, light grey otherwise) so the baked-in SVG tints never show.
                            isMask: true
                            color: providerTab.selected ? root.primaryTextColor(0.96) : root.mutedTextColor(tabMouse.containsMouse ? 0.82 : 0.68)
                            smooth: true
                        }

                        Text {
                            anchors.left: tabSurface.left
                            anchors.right: tabSurface.right
                            anchors.top: providerIcon.bottom
                            anchors.topMargin: 0
                            color: providerTab.selected ? root.primaryTextColor(0.96) : root.mutedTextColor(tabMouse.containsMouse ? 0.78 : 0.62)
                            elide: Text.ElideRight
                            font.family: root.uiFont
                            font.pixelSize: root.providerTabFontSize(providerTab.providerKey)
                            font.weight: providerTab.selected ? Font.DemiBold : Font.Medium
                            horizontalAlignment: Text.AlignHCenter
                            text: root.tabLabel(providerTab.providerKey)
                        }

                        // Small amber warning glyph on a bad-status tab (Item 1).
                        Kirigami.Icon {
                            anchors.right: tabSurface.right
                            anchors.top: tabSurface.top
                            anchors.rightMargin: 4
                            anchors.topMargin: 4
                            width: 11
                            height: 11
                            visible: root.tabStatusBad(providerTab.providerKey)
                            source: "data-warning"
                            isMask: true
                            color: "#e0a23c"
                        }

                        // Feature 7: a muted-provider tab shows a small mute glyph instead of the
                        // bad-status warning (tabStatusBad is suppressed while muted).
                        Kirigami.Icon {
                            anchors.right: tabSurface.right
                            anchors.top: tabSurface.top
                            anchors.rightMargin: 4
                            anchors.topMargin: 4
                            width: 11
                            height: 11
                            visible: root.providerMuted(providerTab.providerKey)
                            source: "audio-volume-muted"
                            isMask: true
                            color: root.mutedTextColor(0.7)
                        }

                        MouseArea {
                            id: tabMouse
                            Accessible.role: Accessible.Button
                                                        Accessible.name: i18n("%1 tab", root.tabLabel(providerTab.providerKey))
                                                        Accessible.description: i18n("Switch to the %1 provider tab", root.tabLabel(providerTab.providerKey))

                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            activeFocusOnTab: true
                            onClicked: (mouse) => {
                                root.costDrawerOpen = false;
                                root.providerRequested(providerTab.providerKey);
                            }
                            Keys.onPressed: (event) => {
                                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                    event.accepted = true;
                                    root.costDrawerOpen = false;
                                    root.providerRequested(providerTab.providerKey);
                                }
                            }
                        }

                        // Visible keyboard-focus indicator — matches tabSurface's rounding,
                        // invisible unless tabMouse actually has keyboard focus.
                        Rectangle {
                            anchors.fill: tabSurface
                            radius: tabSurface.radius
                            color: "transparent"
                            border.width: 2
                            border.color: root.accentColor()
                            visible: tabMouse.activeFocus
                        }

                    }

                }

            }

        }

        Item {
            id: heroPanel

            Layout.fillWidth: true
            Layout.preferredHeight: 50

            Text {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.leftMargin: 0
                anchors.rightMargin: 10
                anchors.topMargin: 3
                color: root.primaryTextColor()
                elide: Text.ElideRight
                font.family: root.displayFont
                font.pixelSize: 13
                font.weight: Font.DemiBold
                textFormat: Text.PlainText
                text: root.providerData().label
            }

            Text {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.rightMargin: 10
                anchors.topMargin: 30
                // Amber (fixed palette — never Kirigami/PlasmaCore theme) once the snapshot is
                // stale (> 2× refresh interval), so an old "Updated 18m ago" actually reads as old.
                // Also amber when THIS provider's entry is a carried-forward (per-provider stale)
                // one, even if the overall snapshot timestamp is fresh.
                color: (root.stalenessLevel() > 0 || root.refreshPaused() || root.providerStale()) ? "#e0a23c" : root.mutedTextColor(0.66)
                elide: Text.ElideRight
                font.family: root.uiFont
                font.pixelSize: 11
                font.weight: Font.Normal
                textFormat: Text.PlainText
                text: root.providerSubtitleText()
            }

            Rectangle {
                id: separator

                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: root.separatorColor(0.14)
            }

        }

        // Degraded-state banner: first UI consumer of telemetry.diagnostics (cost-scan
        // timeout / KWallet locked / cost error). Lives in the fixed chrome above the
        // scrollable body — never inside it — so it doesn't touch metricsBodyHeight().
        // Its height (24) + one ColumnLayout gap (8) is mirrored in popupChromeHeight.
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 24 : 0
            visible: root.degradedText().length > 0
            radius: 6
            color: Qt.rgba(0.92, 0.6, 0.2, 0.16)
            border.width: 1
            border.color: Qt.rgba(0.96, 0.7, 0.3, 0.32)

            Text {
                anchors.fill: parent
                anchors.leftMargin: 9
                anchors.rightMargin: 9
                verticalAlignment: Text.AlignVCenter
                horizontalAlignment: Text.AlignLeft
                elide: Text.ElideRight
                color: Qt.rgba(1, 0.92, 0.78, 0.95)
                font.family: root.uiFont
                font.pixelSize: 11
                textFormat: Text.PlainText
                text: root.degradedText()
            }
        }

        Flickable {
            id: metricsScroller

            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 0
            Layout.topMargin: 10
            boundsBehavior: Flickable.StopAtBounds
            clip: true
            contentHeight: metricsContent.implicitHeight
            contentWidth: width
            flickableDirection: Flickable.VerticalFlick
            interactive: contentHeight > height + 1

            HoverHandler {
                id: metricsHover
            }

            ColumnLayout {
                id: metricsContent

                width: metricsScroller.width
                spacing: 8

                Repeater {
                    // Model is the COUNT (a stable int), not the array — so a refresh that
                    // returns the same number of limits does NOT destroy/recreate the
                    // delegates. Each delegate looks its row up reactively below, so on
                    // update only `pct` changes and the fill springs from its CURRENT width
                    // to the new one instead of snapping to 0 and growing back up (which is
                    // what array-model delegate churn caused: a brand-new fill evaluates
                    // parent.width as 0 pre-layout, then the Behavior animates 0 -> value).
                    model: root.usageLimits().length

                delegate: Item {
                    id: row

                    required property int index
                    // Reactive lookup (re-evaluates when telemetry changes) instead of a
                    // model-provided role, so the persistent delegate refreshes in place.
                    property var modelData: root.usageLimits()[index] || ({})
                    property real pct: root.safePercent(modelData.percent || 0)
                    property bool warning: pct >= 72
                    property bool critical: pct >= 90
                    property real pulse: 1.0

                    SequentialAnimation {

                        id: rowPulseAnim

                        running: root.visible && row.warning

                        loops: Animation.Infinite

                        NumberAnimation { target: row; property: "pulse"; to: 0.62; duration: row.critical ? 450 : 1250; easing.type: Easing.InOutSine }

                        NumberAnimation { target: row; property: "pulse"; to: 1.0; duration: row.critical ? 450 : 1250; easing.type: Easing.InOutSine }

                    }

                    onWarningChanged: {

                        if (!warning) {

                            rowPulseAnim.stop();

                            pulse = 1.0;

                        }

                    }
                    property string detailLeft: root.metricDetailLeftText(modelData)
                    property string detailRight: root.metricDetailRightText(modelData)
                    property string detailText: root.metricDetailText(modelData)
                    property bool hasSplitDetail: detailLeft.length > 0 || detailRight.length > 0
                    property bool hasDetailLine: detailText.length > 0

                    Layout.fillWidth: true
                    Layout.preferredHeight: root.rowMetricHeight(modelData)  // single source, mirrored in metricsBodyHeight()
                    clip: false

                    Text {
                        id: label

                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.topMargin: 1
                        color: root.primaryTextColor()
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        textFormat: Text.PlainText
                        text: modelData.label || i18n("Usage")
                        width: Math.min(implicitWidth, Math.max(0, parent.width - (sublabel.visible ? sublabel.implicitWidth + 7 : 0)))
                    }

                    Text {
                        id: sublabel

                        anchors.left: label.right
                        anchors.leftMargin: 7
                        anchors.baseline: label.baseline
                        visible: text.length > 0
                        color: root.mutedTextColor(0.45)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        font.weight: Font.Normal
                        textFormat: Text.PlainText
                        text: String(row.modelData && row.modelData.sublabel ? row.modelData.sublabel : "")
                        width: Math.min(implicitWidth, Math.max(0, parent.width - label.width - 7))
                    }

                    Rectangle {
                        id: track

                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: label.bottom
                        anchors.topMargin: 8
                        height: 5
                        radius: 2.5
                        color: root.neutralUsageColor(0.2)

                        Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            height: parent.height
                            width: row.pct > 0 ? Math.max(parent.height, parent.width * row.pct / 100) : 0
                            radius: parent.radius
                            color: root.accentColor()
                            // Near-solid fill at any value (macOS: width encodes the amount,
                            // not opacity). Keep only a whisper of gradient for subtle depth.
                            opacity: row.warning ? row.pulse : root.usageOpacityFromPercent(row.pct, 0.72, 0.96)

                            Behavior on width {
                                SpringAnimation {
    spring: 4.0
    damping: 0.85
    epsilon: 0.01
}

                            }

                        }

                        // Elapsed-time marker. Drawn after the fill so it stays
                        // legible over it; taller than the track so it reads as a
                        // tick rather than a gap in the bar.
                        Rectangle {
                            id: paceMarker

                            readonly property real pacePct: root.metricPacePercent(row.modelData)

                            visible: pacePct >= 0
                            anchors.verticalCenter: parent.verticalCenter
                            x: Math.round(parent.width * pacePct / 100) - (width / 2)
                            width: 2
                            height: parent.height + 5
                            radius: 1
                            color: root.primaryTextColor(0.58)

                            Behavior on x {
                                SpringAnimation {
                                    spring: 4.0
                                    damping: 0.85
                                    epsilon: 0.01
                                }
                            }

                        }

                    }

                    Text {
                        id: usedText

                        anchors.left: parent.left
                        anchors.right: resetText.left
                        anchors.top: track.bottom
                        anchors.rightMargin: 10
                        anchors.topMargin: 5
                        color: root.primaryTextColor(0.92)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        font.weight: Font.Normal
                        text: root.metricUsedText(modelData)
                    }

                    Text {
                        id: resetText

                        anchors.right: parent.right
                        anchors.top: track.bottom
                        anchors.topMargin: 5
                        color: root.mutedTextColor(0.64)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignRight
                        text: modelData.reset || ""
                    }

                    Text {
                        id: detailLeftText

                        anchors.left: parent.left
                        anchors.right: detailRightText.left
                        anchors.top: usedText.bottom
                        anchors.rightMargin: 10
                        anchors.topMargin: 2
                        color: root.primaryTextColor(0.88)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        visible: row.hasSplitDetail
                        text: row.detailLeft
                    }

                    Text {
                        id: detailRightText

                        anchors.right: parent.right
                        anchors.top: usedText.bottom
                        anchors.topMargin: 2
                        color: root.mutedTextColor(0.64)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignRight
                        visible: row.hasSplitDetail
                        text: row.detailRight
                    }

                    Text {
                        id: detailLineText

                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: row.hasSplitDetail ? detailLeftText.bottom : usedText.bottom
                        anchors.topMargin: 2
                        color: root.mutedTextColor(0.64)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        visible: row.hasDetailLine
                        text: row.detailText
                    }

                }

            }

            Item {
                id: extraUsageSection

                property var extraLimit: root.extraUsageLimit() || ({
                })
                property real pct: root.safePercent(extraLimit.percent || 0)
                // The section is shown whenever there's an extra-usage DETAIL line, but the
                // bar only makes sense with a real isExtraUsage limit. A credit-only provider
                // (detail set, no isExtraUsage limit) would otherwise render an empty 0% track.
                property bool hasBar: root.extraUsageLimit() !== null

                Layout.fillWidth: true
                Layout.preferredHeight: visible ? root.extraUsageBodyHeight : 0
                visible: root.hasExtraUsage()

                // Extra usage is an informational row (no click target), so it
                // gets no hover highlight — only the Cost section below reacts
                // to its own pointer.

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: 1
                    color: root.separatorColor(0.14)
                }

                Text {
                    id: extraTitle

                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.topMargin: 18
                    color: root.primaryTextColor()
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    text: i18n("Extra usage")
                }

                Rectangle {
                    id: extraTrack

                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: extraTitle.bottom
                    anchors.topMargin: 8
                    height: 5
                    radius: 2.5
                    color: root.neutralUsageColor(0.2)
                    // Keep the geometry (the detail text anchors to extraTrack.bottom and the
                    // section height mirror stays 86) but don't paint an empty track when
                    // there's no real limit behind it.
                    visible: extraUsageSection.hasBar

                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        height: parent.height
                        width: extraUsageSection.pct > 0 ? Math.max(parent.height, parent.width * extraUsageSection.pct / 100) : 0
                        radius: parent.radius
                        color: root.accentColor()
                        opacity: root.usageOpacityFromPercent(extraUsageSection.pct, 0.72, 0.96)
                    }

                }

                Text {
                    anchors.left: parent.left
                    anchors.right: extraPercent.left
                    anchors.top: extraTrack.bottom
                    anchors.rightMargin: 10
                    anchors.topMargin: 7
                    color: root.primaryTextColor(0.92)
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 11
                    text: root.extraUsageDetail()
                }

                Text {
                    id: extraPercent

                    anchors.right: parent.right
                    anchors.top: extraTrack.bottom
                    anchors.topMargin: 7
                    color: root.mutedTextColor(0.64)
                    font.family: root.uiFont
                    font.pixelSize: 11
                    horizontalAlignment: Text.AlignRight
                    text: root.metricUsedText(extraUsageSection.extraLimit)
                }

            }

            Item {
                id: costSection

                property var cost: root.costSummary() || ({
                })
                property bool hasRealCost: root.hasCostSummary()
                property bool hasBreakdown: String(costSection.cost.breakdown || "").trim().length > 0

                Layout.fillWidth: true
                Layout.preferredHeight: visible ? root.costSectionHeight() : 0
                visible: root.hasCostSection()

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: 1
                    color: root.separatorColor(0.14)
                }

                Text {
                    id: costTitle

                    anchors.left: parent.left
                    anchors.right: costArrow.left
                    anchors.top: parent.top
                    anchors.topMargin: 18
                    anchors.rightMargin: 8
                    color: root.primaryTextColor()
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    text: costSection.cost.title || i18n("Cost")
                }

                Text {
                    id: costArrow

                    anchors.right: parent.right
                    anchors.rightMargin: 2
                    anchors.verticalCenter: costTitle.verticalCenter
                    color: costMouse.containsMouse ? root.primaryTextColor() : root.mutedTextColor(0.58)
                    font.family: root.uiFont
                    font.pixelSize: 24
                    text: root.costDrawerOpen ? "‹" : "›"

                    Behavior on color {
                        ColorAnimation {
                            duration: 120
                        }

                    }

                }

                MouseArea {
                    id: costMouse
                    Accessible.role: Accessible.Button
                                        Accessible.name: i18n("Cost details")
                                        Accessible.description: i18n("Toggle the cost graph drawer")

                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    activeFocusOnTab: true
                    onClicked: {
                        root.toggleCostPopout();
                    }
                    Keys.onPressed: (event) => {
                        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                            event.accepted = true;
                            root.toggleCostPopout();
                        }
                    }
                }

                // Visible keyboard-focus indicator, invisible unless costMouse has focus.
                Rectangle {
                    anchors.fill: parent
                    radius: 6
                    color: "transparent"
                    border.width: 2
                    border.color: root.accentColor()
                    visible: costMouse.activeFocus
                }

                Text {
                    id: todayText
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.top: costTitle.bottom
                    anchors.topMargin: 6
                    color: root.primaryTextColor(0.92)
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 11
                    text: costSection.hasRealCost ? root.todayCostText(costSection.cost) : i18n("Local estimate unavailable")
                }

                Text {
                    id: weekText
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.top: costTitle.bottom
                    anchors.topMargin: 27
                    color: root.primaryTextColor(0.86)
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 11
                    text: costSection.hasRealCost ? root.weekCostText(costSection.cost) : ""
                    // Hide when a summary carries no 7-day row so there's no empty gap.
                    visible: text.length > 0
                }

                Text {
                    id: monthText
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.top: costTitle.bottom
                    anchors.topMargin: 48
                    color: root.primaryTextColor(0.8)
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 11
                    text: costSection.hasRealCost ? root.monthCostText(costSection.cost) : i18n("No local token cost summary found")
                    // Hide the second line when a summary has no 30-day row so there's no empty gap.
                    visible: text.length > 0
                }

                Text {
                    id: breakdownText
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.top: costTitle.bottom
                    anchors.topMargin: 69
                    color: root.mutedTextColor(0.64)
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 11
                    // Per-type token breakdown (Input · Output · Cached) for the 30-day window.
                    text: String(costSection.cost.breakdown || "")
                    visible: costSection.hasBreakdown
                }

            }

            // Empty state: an actionable message (remediation hint per status, Item 1) plus,
            // for a locked KWallet, an "Unlock KWallet" button that triggers a foreground
            // backend run. The column fills the scroller; the message takes the remainder above
            // the button so the exact body height (metricsBodyHeight()'s empty-state mirror)
            // doesn't have to be pixel-perfect.
            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(120, metricsScroller.height)
                // Cost-only providers (e.g. Grok, or Antigravity while the LS is down) have no
                // limit rows but DO carry a cost card — don't also show the empty-state message.
                // Same for providers whose only data is the extra-usage line (e.g. Claude
                // prepaid credits with no parseable limits) — mirrors the hasCostSection() term.
                visible: root.providerLimits().length === 0 && !root.hasCostSection() && !root.hasExtraUsage()
                spacing: 12

                Text {
                    id: emptyStateText

                    // Feature 3: when the bad state carries an actionUrl, the message reads as a
                    // link (accent colour + underline) and clicking it opens the sign-in page.
                    readonly property string actionUrl: root.providerActionUrl()
                    readonly property bool isLink: actionUrl.length > 0

                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    color: isLink ? root.accentColor() : root.mutedTextColor(0.68)
                    font.family: root.uiFont
                    font.pixelSize: 13
                    font.underline: isLink && emptyStateLinkHover.hovered
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    wrapMode: Text.WordWrap
                    textFormat: Text.PlainText
                    text: root.emptyStateMessage()

                    Accessible.role: isLink ? Accessible.Button : Accessible.StaticText
                    Accessible.name: text
                    Accessible.description: isLink ? i18n("Open the sign-in page in your browser") : ""

                    HoverHandler {
                        id: emptyStateLinkHover
                        enabled: emptyStateText.isLink
                        cursorShape: Qt.PointingHandCursor
                    }
                    TapHandler {
                        enabled: emptyStateText.isLink
                        onTapped: root.openExternalUrlSafe(emptyStateText.actionUrl)
                    }
                }

                Rectangle {
                    id: unlockButton
                    Layout.alignment: Qt.AlignHCenter
                    Layout.preferredHeight: 32
                    Layout.preferredWidth: unlockLabel.implicitWidth + 32
                    Layout.bottomMargin: 8
                    visible: root.emptyStateShowsUnlock()
                    radius: 8
                    color: unlockMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.18) : Qt.rgba(1, 1, 1, 0.10)
                    border.width: 1
                    border.color: Qt.rgba(1, 1, 1, 0.16)

                    Behavior on color {
                        ColorAnimation { duration: 120; easing.type: Easing.OutCubic }
                    }

                    Text {
                        id: unlockLabel
                        anchors.centerIn: parent
                        text: i18n("Unlock KWallet")
                        color: root.primaryTextColor(0.92)
                        font.family: root.uiFont
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }

                    MouseArea {
                        id: unlockMouse
                        Accessible.role: Accessible.Button
                        Accessible.name: i18n("Unlock KWallet")
                        Accessible.description: i18n("Run a foreground refresh that prompts for the KWallet password")
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        activeFocusOnTab: true
                        onClicked: root.unlockWalletRequested()
                        Keys.onPressed: (event) => {
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                event.accepted = true;
                                root.unlockWalletRequested();
                            }
                        }
                    }

                    // Visible keyboard-focus indicator, invisible unless unlockMouse has focus.
                    Rectangle {
                        anchors.fill: parent
                        radius: unlockButton.radius
                        color: "transparent"
                        border.width: 2
                        border.color: root.accentColor()
                        visible: unlockMouse.activeFocus
                    }
                }

                // Sign-in button (Item 1): for missing-cookies / unauthorized, opens the
                // provider's login site in the browser so the user can land a fresh session
                // cookie, then refresh. Styled/positioned like the unlock button; the two are
                // mutually exclusive by status (never both visible).
                Rectangle {
                    id: signInButton
                    Layout.alignment: Qt.AlignHCenter
                    Layout.preferredHeight: 32
                    Layout.preferredWidth: signInLabel.implicitWidth + 32
                    Layout.bottomMargin: 8
                    visible: root.emptyStateShowsSignIn()
                    radius: 8
                    color: signInMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.18) : Qt.rgba(1, 1, 1, 0.10)
                    border.width: 1
                    border.color: Qt.rgba(1, 1, 1, 0.16)

                    Behavior on color {
                        ColorAnimation { duration: 120; easing.type: Easing.OutCubic }
                    }

                    Text {
                        id: signInLabel
                        anchors.centerIn: parent
                        text: i18n("Sign in to %1", root.providerLoginSite(root.selectedProvider))
                        color: root.primaryTextColor(0.92)
                        font.family: root.uiFont
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }

                    MouseArea {
                        id: signInMouse
                        Accessible.role: Accessible.Button
                        Accessible.name: i18n("Sign in to %1", root.providerLoginSite(root.selectedProvider))
                        Accessible.description: i18n("Open the sign-in page in your browser, then refresh")
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        activeFocusOnTab: true
                        onClicked: Qt.openUrlExternally(root.providerLoginUrl(root.selectedProvider))
                        Keys.onPressed: (event) => {
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                event.accepted = true;
                                Qt.openUrlExternally(root.providerLoginUrl(root.selectedProvider));
                            }
                        }
                    }

                    // Visible keyboard-focus indicator, invisible unless signInMouse has focus.
                    Rectangle {
                        anchors.fill: parent
                        radius: signInButton.radius
                        color: "transparent"
                        border.width: 2
                        border.color: root.accentColor()
                        visible: signInMouse.activeFocus
                    }
                }
            }

            }

            Rectangle {
                anchors.right: parent.right
                anchors.rightMargin: 1
                y: Math.max(0, Math.min(metricsScroller.height - height, metricsScroller.visibleArea.yPosition * metricsScroller.height))
                width: 3
                height: Math.max(28, metricsScroller.visibleArea.heightRatio * metricsScroller.height)
                radius: width / 2
                visible: metricsScroller.interactive
                color: Qt.rgba(1, 1, 1, (metricsScroller.moving || metricsHover.hovered) ? 0.32 : 0.24)
                // macOS overlay scrollbar: hidden at rest, fades in on scroll or hover.
                opacity: (metricsScroller.moving || metricsHover.hovered) ? 0.85 : 0

                Behavior on opacity {
                    NumberAnimation {
                        duration: metricsScroller.moving ? 120 : 450
                        easing.type: Easing.OutCubic
                    }

                }

                Behavior on color {
                    ColorAnimation {
                        duration: 160
                        easing.type: Easing.OutCubic
                    }

                }

            }

        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: root.actionFooterHeight()
            spacing: 0

            // Cross-provider "All AI" spend — the snapshot's per-provider cost totals summed
            // (the popup body is per-provider; this is the only all-providers figure). Shows
            // the 30-day total (so it adds up to each tab's "Last 30 days"), or month-to-date
            // vs budget when a monthly budget is set. Height mirrored in actionFooterHeight().
            Item {
                id: allAiRow

                property var totals: root.allAiTotals()
                property real budget: root.monthlyBudgetValue()

                Layout.fillWidth: true
                Layout.preferredHeight: visible ? 27 : 0
                visible: allAiRow.totals.has

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: 1
                    color: root.separatorColor(0.14)
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.topMargin: 1
                    spacing: 8

                    Text {
                        Layout.alignment: Qt.AlignVCenter
                        color: root.mutedTextColor(0.72)
                        font.family: root.uiFont
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        text: i18n("All AI")
                    }

                    Item { Layout.fillWidth: true }

                    Text {
                        Layout.alignment: Qt.AlignVCenter
                        color: root.primaryTextColor(0.9)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignRight
                        text: {
                            const t = allAiRow.totals;
                            // Default: the 30-day total (sums each provider's "Last 30 days", so
                            // it adds up to the tabs). With a monthly budget set, switch to
                            // month-to-date vs that budget — the window the budget alert tracks.
                            if (allAiRow.budget > 0) {
                                const pct = Math.round(t.mtd / allAiRow.budget * 100);
                                return i18n("%1 of %2 this month · %3%", root.compactUsd(t.mtd), root.compactUsd(allAiRow.budget), pct);
                            }
                            return i18n("%1 · last 30 days", root.compactUsd(t.m30));
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: root.separatorColor(0.14)
            }

            Item {
                id: quickActionStrip

                Layout.fillWidth: true
                Layout.preferredHeight: 44

                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.verticalCenter: parent.verticalCenter
                    height: 34
                    spacing: 16

                    Repeater {
                        model: root.quickActionRows()

                        delegate: Item {
                            id: quickAction

                            required property int index
                            required property var modelData

                            width: 38
                            height: parent.height
                            QQC2.ToolTip.delay: 450
                            QQC2.ToolTip.text: quickAction.modelData.label
                            QQC2.ToolTip.visible: quickActionMouse.containsMouse

                            Rectangle {
                                anchors.fill: parent
                                radius: 8
                                // macOS toolbar/Control-Center style: borderless at rest,
                                // a soft rounded highlight appears only on hover.
                                color: quickActionMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.12) : "transparent"
                                border.width: 0

                                Behavior on color {
                                    ColorAnimation {
                                        duration: 120
                                        easing.type: Easing.OutCubic
                                    }
                                }

                            }

                            Kirigami.Icon {
                                anchors.centerIn: parent
                                width: 16
                                height: 16
                                source: quickAction.modelData.icon
                                color: quickActionMouse.containsMouse ? root.accentColor() : root.primaryTextColor(0.78)
                            }

                            MouseArea {
                                id: quickActionMouse
                                Accessible.role: Accessible.Button
                                                                Accessible.name: modelData.label
                                                                Accessible.description: i18n("Execute quick action: %1", modelData.label)

                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                hoverEnabled: true
                                activeFocusOnTab: true
                                onClicked: {
                                    root.triggerAction(quickAction.modelData);
                                }
                                Keys.onPressed: (event) => {
                                    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                        event.accepted = true;
                                        root.triggerAction(quickAction.modelData);
                                    }
                                }
                            }

                            // Visible keyboard-focus indicator, invisible unless quickActionMouse has focus.
                            Rectangle {
                                anchors.fill: parent
                                radius: 8
                                color: "transparent"
                                border.width: 2
                                border.color: root.accentColor()
                                visible: quickActionMouse.activeFocus
                            }

                        }

                    }

                }

            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: root.separatorColor(0.12)
            }

            Repeater {
                model: root.systemActionRows()

                delegate: Item {
                    id: actionRow

                    required property int index
                    required property var modelData

                    Layout.fillWidth: true
                    Layout.preferredHeight: root.actionRowHeight(actionRow.modelData, actionRow.index)

                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        height: 1
                        visible: Boolean(actionRow.modelData.separator)
                        color: root.separatorColor(0.14)
                    }

                    Rectangle {
                        anchors.fill: parent
                        anchors.leftMargin: -2
                        anchors.rightMargin: -2
                        radius: 5
                        color: actionMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.12) : Qt.rgba(1, 1, 1, 0)
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 0
                        anchors.rightMargin: 4
                        spacing: 10

                        Kirigami.Icon {
                            visible: String(actionRow.modelData.icon || "").length > 0
                            Layout.preferredWidth: visible ? 18 : 0
                            Layout.preferredHeight: 18
                            Layout.alignment: Qt.AlignVCenter
                            source: actionRow.modelData.icon
                            color: actionMouse.containsMouse ? root.accentColor() : root.primaryTextColor(0.84)
                        }

                        Text {
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            color: root.primaryTextColor()
                            elide: Text.ElideRight
                            font.family: root.uiFont
                            font.pixelSize: 13
                            text: actionRow.modelData.label
                        }

                    }

                    MouseArea {
                        id: actionMouse
                        Accessible.role: Accessible.Button
                                                Accessible.name: actionRow.modelData.label
                                                Accessible.description: i18n("Activate %1", actionRow.modelData.label)

                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        activeFocusOnTab: true
                        onClicked: (mouse) => {
                            root.triggerAction(actionRow.modelData);
                        }
                        Keys.onPressed: (event) => {
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                event.accepted = true;
                                root.triggerAction(actionRow.modelData);
                            }
                        }
                    }

                    // Visible keyboard-focus indicator, invisible unless actionMouse has focus.
                    Rectangle {
                        anchors.fill: parent
                        anchors.leftMargin: -2
                        anchors.rightMargin: -2
                        radius: 5
                        color: "transparent"
                        border.width: 2
                        border.color: root.accentColor()
                        visible: actionMouse.activeFocus
                    }

                }

            }

        }

    }

}
