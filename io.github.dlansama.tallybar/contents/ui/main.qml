import QtQuick
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import "lib/ui_helpers.js" as UIHelpers

PlasmoidItem {
    id: root

    property string selectedProvider: "claude"
    property bool loading: false
    property bool liveLoaded: false
    // Wall-clock (Date.now() ms) of the last SUCCESSFUL live refresh. Used to debounce the
    // refresh fired on every popup expand: actively opening/closing the widget must not burst
    // the internal APIs (each open = a full 5-provider fetch, which contributes to Cloudflare
    // rate-limiting Claude). 0 = never refreshed live this session.
    property double lastRefreshMs: 0
    // Skip a popup-open refresh when the last successful one was under this many ms ago.
    readonly property int popupRefreshDebounceMs: 20000
    property string lastError: ""
    property bool configSaving: false
    property string configError: ""
    // Monotonic counter bumped each time a config write (writeConfig/writeRefreshInterval)
    // successfully lands a new config (configExecutable.onNewData). refresh()/refreshForeground()
    // snapshot it into configSeqAtRefreshStart when a refresh begins; if the counter has moved
    // by the time that refresh's onNewData arrives, a config write completed mid-flight and this
    // refresh's own (pre-write) parsed.config is stale -- see the preserve check below. This
    // covers the window configSaving alone misses: it flips back to false as soon as the fast
    // --set-config call returns, well before a slower concurrent refresh's backend process exits.
    property int configWriteSeq: 0
    property int configSeqAtRefreshStart: 0
    property var providerOrder: ["codex", "claude", "gemini", "antigravity", "grok"]
    property var activeProviders: activeProviderTabs()
    property var telemetry: ({
        "ok": false,
        "timestamp": "",
        "providers": {
            "gemini": {
                "label": "Gemini",
                "status": "loading",
                "source": "gemini-web",
                "limits": []
            },
            "antigravity": {
                "label": "Antigravity",
                "status": "loading",
                "source": "local",
                "limits": []
            },
            "codex": {
                "label": "Codex",
                "status": "loading",
                "source": "json-rpc",
                "limits": []
            },
            "claude": {
                "label": "Claude",
                "status": "loading",
                "source": "browser",
                "limits": []
            },
            "grok": {
                "label": "Grok",
                "status": "loading",
                "source": "local-grok-logs",
                "limits": []
            }
        }
    })

    function localPath(url) {
        let text = String(url);
        if (text.indexOf("file://") === 0)
            return decodeURIComponent(text.substring(7));

        return text;
    }

    function shellQuote(text) {
        return "'" + String(text).replace(/\0/g, "").replace(/'/g, "'\"'\"'") + "'";
    }

    // The plasmoid ships no config/main.xml declaring a pythonPath key, so the interpreter
    // is NOT user-configurable today -- it's fixed to the system python3 (3.11+ required;
    // the backend relies on asyncio.TaskGroup/asyncio.timeout). Splicing a user-controlled
    // value in here unquoted (unlike the shellQuote'd backend path below) would be a shell
    // injection vector if this were ever wired up to real config, so don't reintroduce it
    // without also shellQuote()-ing it.
    property string pythonExec: "/usr/bin/env python3"
    // Per-provider timeout handed to the backend (--timeout). The backend's refresh can take
    // up to backend.refresh_worst_case_seconds(timeout) = 3*timeout + 2 (38s at 12) plus
    // interpreter startup, so the watchdog below must sit above that — at 25s it abandoned
    // slow-but-healthy refreshes and dropped their result. tests/test_backend.py pins the
    // relation (test_refresh_watchdog_covers_backend_worst_case).
    readonly property int backendTimeoutSeconds: 12
    readonly property int refreshWatchdogMs: 45000

    // background defaults true (the widget's normal timer refresh). Pass false for the
    // "Unlock KWallet" path: dropping --background lets the backend's KWallet open()
    // pop the native unlock dialog (and runs the documented GUI-prompt credential action).
    function backendCommand(background) {
        const backend = root.localPath(Qt.resolvedUrl("../code/backend.py"));
        const cmd = root.pythonExec + " " + root.shellQuote(backend) + " --once --timeout " + root.backendTimeoutSeconds;
        return (background === false) ? cmd : cmd + " --background";
    }

    function configCommand(minutes) {
        const backend = root.localPath(Qt.resolvedUrl("../code/backend.py"));
        const safeMinutes = (isFinite(Number(minutes)) && Number(minutes) > 0) ? Number(minutes) : 5;
        return root.pythonExec + " " + root.shellQuote(backend) + " --set-refresh-interval " + safeMinutes;
    }

    function lastSnapshotCommand() {
        const backend = root.localPath(Qt.resolvedUrl("../code/backend.py"));
        return root.pythonExec + " " + root.shellQuote(backend) + " --last-snapshot";
    }

    function telemetryWithConfig(config) {
        const copy = JSON.parse(JSON.stringify(root.telemetry || ({
        })));
        copy.config = config;
        return copy;
    }

    function writeRefreshInterval(minutes) {
        if (root.configSaving)
            return ;

        root.configSaving = true;
        root.configError = "";
        configWatchdog.restart();
        configExecutable.exec(root.configCommand(minutes));
    }

    function setConfigCommand(obj) {
        const backend = root.localPath(Qt.resolvedUrl("../code/backend.py"));
        return root.pythonExec + " " + root.shellQuote(backend) + " --set-config " + root.shellQuote(JSON.stringify(obj));
    }

    // Generic config write (notifications + panel display) — shares configExecutable's
    // round-trip + configSaving race handling with writeRefreshInterval.
    function writeConfig(obj) {
        if (root.configSaving)
            return ;

        root.configSaving = true;
        root.configError = "";
        configWatchdog.restart();
        configExecutable.exec(root.setConfigCommand(obj));
    }

    function handleError(context, exitCode, stdout, stderr) {
        if (exitCode !== 0)
            return stderr.length > 0 ? stderr.trim() : i18n("%1 exited with %2", context, exitCode);
        try {
            const parsed = JSON.parse(stdout);
            return parsed.ok ? "" : (parsed.message || "");
        } catch (error) {
            return i18n("invalid backend JSON: %1", error);
        }
    }

    // backend.py's main() has three fallback paths (BaseException from build_snapshot,
    // a post-build notifications/save error, a non-serializable snapshot) that all reprint
    // a prior cached/degraded snapshot with diagnostics.fatal or diagnostics.post_snapshot_error
    // set -- but leave `ok` true (inherited from the cached snapshot, which is always ok:true
    // per contract) and never set a top-level `message`. handleError() alone can't see this
    // (it only looks at exitCode/ok/message), so a backend that's fatally failing on every
    // refresh would otherwise look like an uninterrupted stream of fresh live successes.
    // Returns the diagnostic string, or "" when the snapshot isn't degraded this way.
    function degradedSnapshotReason(parsed) {
        const d = parsed && parsed.diagnostics;
        if (!d)
            return "";
        return String(d.fatal || d.post_snapshot_error || "");
    }

    function providerData(provider) {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({
        });
        return providers[provider] || {
            "label": provider,
            "limits": [],
            "status": "missing",
            "source": "none"
        };
    }


    function activeProviderTabs() {
        // Respect the user's provider-visibility choice (config.providers) — keep the
        // canonical order, fall back to all providers if unset or empty (never 0 tabs).
        const cfg = root.telemetry && root.telemetry.config;
        const enabled = cfg && Array.isArray(cfg.providers) ? cfg.providers : null;
        if (!enabled || enabled.length === 0)
            return root.providerOrder;
        const tabs = root.providerOrder.filter((p) => enabled.indexOf(p) >= 0);
        return tabs.length > 0 ? tabs : root.providerOrder;
    }

    function syncActiveProviders() {
        // activeProviders is a live binding on activeProviderTabs() (which reads telemetry +
        // providerOrder, both reactive), so it self-updates — don't reassign it here (that would
        // destroy the binding). Only reconcile the selection so it never points at a hidden tab.
        const tabs = root.activeProviders;
        if (tabs.indexOf(root.selectedProvider) < 0)
            root.selectedProvider = tabs[0];
    }

    function tooltipText() {
        const provider = root.providerData(root.selectedProvider);
        const limits = provider.limits || [];
        if (root.loading)
            return i18n("Refreshing %1 usage", provider.label);

        if (limits.length === 0)
            return provider.label + ": " + (provider.message || provider.status || i18n("No usage data"));

        // One line per usage window — "<label> <pct>% · <reset>" — so the tooltip
        // shows the pace/reset breakdown for the selected provider without opening the popup.
        let lines = [provider.label];
        for (let i = 0; i < limits.length; ++i) {
            const limit = limits[i];
            const pct = Math.max(0, Math.min(100, Number(limit.percent || 0)));
            const label = String(limit.label || "").trim();
            let line = (label.length > 0 ? label + " " : "") + pct.toFixed(0) + "%";
            const reset = String(limit.reset || "").trim();
            if (reset.length > 0)
                line += " · " + reset;
            lines.push(line);
        }
        return lines.join("\n");
    }

    function refresh() {
        if (root.loading)
            return ;

        root.loading = true;
        root.lastError = "";
        root.configSeqAtRefreshStart = root.configWriteSeq;
        console.info("io.github.dlansama.tallybar backend refresh starting");
        refreshWatchdog.restart();
        executable.exec(root.backendCommand());
    }

    // A foreground refresh (no --background) so a locked KWallet pops its native
    // unlock dialog; the normal onNewData path paints full data once the user unlocks.
    // Guarded by the same `loading` flag. The 25s refreshWatchdog may fire while the dialog
    // is still up — that just re-enables the button (a fresh click retries), which is fine.
    function refreshForeground() {
        if (root.loading)
            return ;

        root.loading = true;
        root.lastError = "";
        root.configSeqAtRefreshStart = root.configWriteSeq;
        console.info("io.github.dlansama.tallybar foreground (unlock) refresh starting");
        refreshWatchdog.restart();
        executable.exec(root.backendCommand(false));
    }

    // Watchdog: if the backend process never delivers a result (hung pipe, the
    // process getting SIGKILLed, a stuck DataSource), onNewData never fires and
    // loading stays true forever — refresh() then early-returns on every Timer
    // tick and the widget is permanently stuck "Refreshing". This forces loading
    // back to false after refreshWatchdogMs (above the backend's worst case — see
    // backendTimeoutSeconds) so the next tick can retry. The normal path disarms
    // this in executable.onNewData.
    Timer {
        id: refreshWatchdog
        interval: root.refreshWatchdogMs
        repeat: false
        onTriggered: () => {
            if (root.loading) {
                console.warn("io.github.dlansama.tallybar backend refresh watchdog fired; forcing loading=false");
                root.loading = false;
                if (root.lastError === "")
                    root.lastError = i18n("Backend did not respond in time");
                // The watchdog fires precisely because onNewData never arrived, so its
                // disconnectSource() never ran. Drop the hung source(s) here, otherwise
                // Plasma's source-name dedup reuses the dead source on every later refresh
                // (the command string is constant) and the widget is stuck forever.
                // Iterate a COPY — disconnectSource() mutates connectedSources in place.
                const sources = executable.connectedSources.slice();
                for (let i = 0; i < sources.length; ++i)
                    executable.disconnectSource(sources[i]);
            }
        }
    }

    Timer {
        id: configWatchdog
        interval: 25000
        repeat: false
        onTriggered: () => {
            if (root.configSaving) {
                console.warn("io.github.dlansama.tallybar config watchdog fired; forcing configSaving=false");
                root.configSaving = false;
                if (root.configError === "")
                    root.configError = i18n("Backend did not respond in time");
                // Same as refreshWatchdog: onNewData never fired, so drop the hung
                // source(s) to keep Plasma's source dedup from reusing a dead source.
                const sources = configExecutable.connectedSources.slice();
                for (let i = 0; i < sources.length; ++i)
                    configExecutable.disconnectSource(sources[i]);
            }
        }
    }

    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground
    Plasmoid.icon: "utilities-system-monitor"
    // Providers the user has muted are dropped from the panel's attention/badge
    // logic (they stay visible in the popup). Reads config.mutedProviders written via the
    // config-write DataSource.
    function providerMuted(provider) {
        const cfg = root.telemetry && root.telemetry.config;
        const muted = cfg && Array.isArray(cfg.mutedProviders) ? cfg.mutedProviders : [];
        return muted.indexOf(provider) >= 0;
    }

    // NeedsAttention for an actionable status (sign in / unlock) or a capacity window at
    // >= 90% — never for extra-usage rows (Claude overage, credit pools), which the
    // backend's notifications skip too. Rule lives in ui_helpers.needsAttention (node-tested).
    Plasmoid.status: UIHelpers.needsAttention(root.providerData(root.selectedProvider),
                                              root.providerMuted(root.selectedProvider))
        ? PlasmaCore.Types.NeedsAttentionStatus : PlasmaCore.Types.ActiveStatus
    Plasmoid.title: "TallyBar"
    toolTipMainText: "TallyBar"
    toolTipSubText: root.tooltipText()
    preferredRepresentation: compactRepresentation
    onExpandedChanged: {
        if (root.expanded) {
            // Debounce: only refresh on open when the last successful refresh is stale enough.
            // The periodic Timer keeps data fresh in the background, so a just-opened popup
            // already shows recent numbers — re-fetching on every open only adds API load.
            if (root.lastRefreshMs === 0 || (Date.now() - root.lastRefreshMs) > root.popupRefreshDebounceMs)
                root.refresh();
        } else if (fullRepresentationItem) {  // lazily instantiated — may be null before first expand
            fullRepresentationItem.costDrawerOpen = false;
        }
    }
    Component.onCompleted: {
        // Paint last-known values instantly; the Timer's triggeredOnStart fires
        // the live refresh in parallel.
        cacheLoader.exec(root.lastSnapshotCommand());
    }

    Plasma5Support.DataSource {
        id: executable

        function exec(command) {
            executable.connectSource(command);
        }

        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            const stdout = data["stdout"] || "";
            const stderr = data["stderr"] || "";
            const exitCode = Number(data["exit code"] || 0);
            console.info("io.github.dlansama.tallybar backend completed exitCode=" + exitCode + " stdoutBytes=" + stdout.length + " stderrBytes=" + stderr.length);
            refreshWatchdog.stop();
            root.loading = false;
            executable.disconnectSource(sourceName);
            
            root.lastError = root.handleError("backend", exitCode, stdout, stderr);
            if (root.lastError === "" && exitCode === 0) {
                try {
                    const parsed = JSON.parse(stdout);
                    // configSaving covers "a write is still in flight"; configWriteSeq covers
                    // "a write started before this refresh but already completed mid-flight"
                    // (configSaving alone misses that window — see configWriteSeq's
                    // declaration above). Either way this refresh's own parsed.config was read
                    // from disk before the write landed, so keep the newer in-memory one.
                    const configWriteLandedMidRefresh = root.configWriteSeq !== root.configSeqAtRefreshStart;
                    if ((root.configSaving || configWriteLandedMidRefresh) && root.telemetry && root.telemetry.config) {
                        parsed.config = root.telemetry.config;
                    }
                    root.telemetry = parsed;
                    // A backend fatal (or post-snapshot-processing error) reprints a stale
                    // cached/degraded snapshot rather than fresh live data — ok stays true and
                    // no top-level message is set (see degradedSnapshotReason's comment), so
                    // don't paint it as a live success: keep/drop liveLoaded back to false so
                    // the existing "Cached · " indicator reflects the staleness, and surface
                    // the reason as the visible error text.
                    const degradedReason = root.degradedSnapshotReason(parsed);
                    if (degradedReason !== "") {
                        root.liveLoaded = false;
                        root.lastError = degradedReason;
                    } else {
                        root.liveLoaded = true;
                        // Record the moment of this live success for the popup-open debounce.
                        root.lastRefreshMs = Date.now();
                    }
                    root.syncActiveProviders();
                    // Threshold notifications: the backend already de-duped these (only
                    // newly-crossed limits arrive here) and attaches them ONLY to the live
                    // --once stdout, never the cached snapshot — so this can't re-fire on
                    // cold start. Fire each via the desktop notification service.
                    if (parsed.notifications && parsed.notifications.length)
                        root.fireNotifications(parsed.notifications);
                } catch (e) {
                    console.error("io.github.dlansama.tallybar: failed to parse/process backend JSON in executable: " + e + "\n" + e.stack);
                    root.lastError = i18n("JSON processing failed: %1", e.message);
                }
            }
        }
    }

    // Read-only loader: seeds last-known telemetry from disk on cold start so the
    // widget paints real values immediately instead of empty "loading" bars. A
    // live refresh runs in parallel; the liveLoaded guard ensures this slower
    // cache read can never overwrite fresher data if the live fetch wins the race.
    Plasma5Support.DataSource {
        id: cacheLoader

        function exec(command) {
            cacheLoader.connectSource(command);
        }

        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            const stdout = data["stdout"] || "";
            const exitCode = Number(data["exit code"] || 0);
            cacheLoader.disconnectSource(sourceName);
            if (root.liveLoaded || exitCode !== 0)
                return ;

            try {
                const cached = JSON.parse(stdout);
                if (cached && cached.providers) {
                    root.telemetry = cached;
                    root.syncActiveProviders();
                }
            } catch (e) {
                console.error("io.github.dlansama.tallybar: failed to parse/process cached JSON in cacheLoader: " + e + "\n" + e.stack);
            }
        }
    }

    // Fires desktop notifications via notify-send (libnotify) — routes through the same
    // org.freedesktop.Notifications D-Bus service KDE owns, so alerts appear in the
    // Plasma notification popup/history and respect the user's notification settings.
    Plasma5Support.DataSource {
        id: notifier

        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            notifier.disconnectSource(sourceName);
        }
    }

    // Monotonic counter making each notify-send source NAME unique. The executable engine
    // keys running sources by their command string, so two identical commands (two alerts
    // with the same title+body, or a double-clicked Test button before onNewData
    // disconnects) would collapse to one connectSource() no-op and silently drop the
    // duplicate. A harmless env-var prefix the shell consumes (notify-send ignores it)
    // makes every invocation a distinct source.
    property int notifySeq: 0

    // Fire desktop notifications. Reuses the existing root.shellQuote (defined above) to
    // escape the title/body for the shell — they come from provider labels + numbers.
    function fireNotifications(list) {
        for (let i = 0; i < list.length; ++i) {
            const n = list[i];
            if (!n || !n.title)
                continue;
            const urgency = n.urgency === "critical" ? "critical" : "normal";
            const cmd = "TALLYBAR_NSEQ=" + (++root.notifySeq) + " "
                + "notify-send --app-name=TallyBar --urgency=" + urgency
                + " --icon=utilities-system-monitor -- "
                + root.shellQuote(n.title) + " " + root.shellQuote(n.body || "");
            notifier.connectSource(cmd);
        }
    }

    Plasma5Support.DataSource {
        id: configExecutable

        function exec(command) {
            configExecutable.connectSource(command);
        }

        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            const stdout = data["stdout"] || "";
            const stderr = data["stderr"] || "";
            const exitCode = Number(data["exit code"] || 0);
            configWatchdog.stop();
            configExecutable.disconnectSource(sourceName);
            root.configSaving = false;
            
            root.configError = root.handleError("config", exitCode, stdout, stderr);
            if (root.configError === "" && exitCode === 0) {
                try {
                    const parsed = JSON.parse(stdout);
                    if (parsed && parsed.config) {
                        root.telemetry = root.telemetryWithConfig(parsed.config);
                        // Bump the write generation so a slower, already-in-flight refresh that
                        // started before this write (and so is carrying a stale, pre-write
                        // parsed.config) knows to preserve this newer config instead of
                        // clobbering it once it lands — see configWriteSeq's declaration above.
                        root.configWriteSeq++;
                        // Re-sync after a config write too (the fetch paths already do):
                        // if the user just hid the SELECTED provider, this re-points
                        // selectedProvider to a visible tab so the body/tab/popout/icon
                        // follow immediately instead of orphaning until the next refresh.
                        root.syncActiveProviders();
                    }
                } catch (e) {
                    console.error("io.github.dlansama.tallybar: failed to parse/process config JSON in configExecutable: " + e + "\n" + e.stack);
                    root.configError = i18n("JSON processing failed: %1", e.message);
                }
            }
        }
    }

    Timer {
        interval: {
            const cfg = root.telemetry && root.telemetry.config;
            const minutes = cfg ? Number(cfg.refreshIntervalMinutes) : 0;
            return (isFinite(minutes) && minutes > 0) ? Math.round(minutes * 60000) : 300000;
        }
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: () => {
            root.refresh();
        }
    }

    compactRepresentation: CompactRepresentation {
        vertical: Plasmoid.formFactor === PlasmaCore.Types.Vertical
        selectedProvider: root.selectedProvider
        telemetry: root.telemetry
        tabs: root.activeProviders
        onToggleRequested: () => {
            root.expanded = !root.expanded;
        }
        // Wheel / middle-click cycle the panel display mode and persist it via the
        // config-write DataSource (same path SettingsPopout uses). writeConfig no-ops while a
        // config write is already in flight, so a rapid scroll settles on the last landed mode.
        onCycleModeRequested: (direction) => {
            const modes = ["percent", "reset", "cost", "pace"];
            const cfg = root.telemetry && root.telemetry.config;
            const cur = cfg && cfg.panelDisplayMode ? String(cfg.panelDisplayMode) : "percent";
            let i = modes.indexOf(cur);
            if (i < 0)
                i = 0;
            const n = modes.length;
            const next = modes[((i + direction) % n + n) % n];
            root.writeConfig({ "panelDisplayMode": next });
        }
    }

    fullRepresentation: FullRepresentation {
        id: fullRepresentationItem

        selectedProvider: root.selectedProvider
        hostExpanded: root.expanded
        screenGeometry: Plasmoid.containment ? Plasmoid.containment.screenGeometry : null
        tabs: root.activeProviders
        telemetry: root.telemetry
        loading: root.loading
        liveLoaded: root.liveLoaded
        lastError: root.lastError
        configSaving: root.configSaving
        configError: root.configError
        onProviderRequested: (provider) => {
            root.selectedProvider = provider;
        }
        onRefreshRequested: () => {
            root.refresh();
        }
        onUnlockWalletRequested: () => {
            root.refreshForeground();
        }
        onCloseRequested: () => {
            root.expanded = false;
        }
        onRefreshIntervalRequested: (minutes) => {
            root.writeRefreshInterval(minutes);
        }
        onConfigChangeRequested: (changes) => {
            root.writeConfig(changes);
        }
        onTestNotificationRequested: () => {
            root.fireNotifications([{
                "title": i18n("TallyBar · test notification"),
                "body": i18n("Usage alerts are working."),
                "urgency": "normal"
            }]);
        }
    }

}
