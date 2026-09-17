// Offscreen screenshot renderer for the TallyBar UI.
//
// Renders a UI component against the shared mock telemetry fixture
// (tools/preview/mock-telemetry.json) and writes a PNG — no Plasma session, no
// display, and none of the developer's real usage numbers in the output.
// Driven by `make screenshots`; see tools/preview/README.md.
//
//   QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software \
//   qml6 tools/preview/screenshot.qml -- out=docs/screenshots/claude.png
//
// Arguments (positional key=value after `--`, read from Qt.application.arguments):
//   out=<path>        PNG destination                  (default ./shot.png)
//   component=<name>  FullRepresentation | CompactRepresentation | CostPopout
//   provider=<key>    provider tab to select           (default claude)
//   graphMode=<key>   CostPopout only: day | week | month  (default week)
//   scale=<n>         pixel ratio the grab renders at  (default 2)
//
// The Cost flyout IS capturable, with two constraints that took a while to find:
//
//   1. It is a PopupPlasmaWindow, so its mainItem only becomes grabbable once the
//      window is actually visible — that means driving the real state the widget
//      uses (`costDrawerOpen`, gated by `drawerExpanded = costDrawerOpen &&
//      hasCostSection()`), not just creating the object. Before that, grabToImage
//      refuses with "item's window is not visible".
//   2. Grab the mainItem, never its parent: the window's QQuickRootItem has no QML
//      engine and grabToImage refuses it. The card paints no background of its own
//      (Plasma's translucent surface normally supplies it, and that surface does not
//      exist offscreen), so a backdrop Rectangle is injected INTO the mainItem at a
//      low z rather than behind it.
//
// Window PLACEMENT still cannot be verified this way — KWin positions the flyout
// beside the widget and that needs a live session (see CLAUDE.md). This captures the
// card's contents only.

import QtQuick
import QtQuick.Window

Window {
    id: shot

    // ---------------------------------------------------------------------
    // Arguments
    // ---------------------------------------------------------------------
    function argValue(key, fallback) {
        const args = Qt.application.arguments;
        for (let i = 1; i < args.length; ++i) {
            const m = args[i].match(new RegExp("^" + key + "=(.+)$"));
            if (m)
                return m[1];
        }
        return fallback;
    }

    readonly property string outFile: argValue("out", "shot.png")
    readonly property string componentName: argValue("component", "FullRepresentation")
    readonly property string provider: argValue("provider", "claude")
    readonly property string graphMode: argValue("graphMode", "week")
    readonly property real grabScale: Number(argValue("scale", "2"))
    readonly property bool compact: componentName === "CompactRepresentation"
    // The cost flyout is hosted BY FullRepresentation (it needs root's helpers, cost data
    // and costGraphMode state), so this mode loads the full widget and then opens the
    // flyout against it — the same path the running widget takes.
    readonly property bool popout: componentName === "CostPopout"
    property var popoutItem: null

    // Repo root = two levels up from tools/preview/.
    readonly property string repoRoot: {
        let here = String(Qt.resolvedUrl("."));
        if (here.charAt(here.length - 1) === "/")
            here = here.slice(0, -1);
        return here.replace(/\/[^/]+\/[^/]+$/, "");
    }

    width: compact ? 132 : 380
    height: compact ? 40 : 620
    visible: true
    color: "transparent"

    property var telemetry: ({})

    // ---------------------------------------------------------------------
    // Mock telemetry, shared with harness.qml so the preview and the published
    // screenshots can't drift apart.
    //
    // The fixture is written against a fixed reference week; rebaseDates() slides
    // it onto the current one at load time so a committed screenshot never renders
    // "Updated 180d ago" or a month grid from last spring.
    // ---------------------------------------------------------------------
    function loadTelemetry() {
        const req = new XMLHttpRequest();
        req.open("GET", Qt.resolvedUrl("mock-telemetry.json"), false); // sync: nothing to race
        req.send(null);
        if (req.status !== 200 && req.status !== 0) {
            console.error("could not read mock-telemetry.json (status " + req.status + ")");
            Qt.exit(2);
            return;
        }
        try {
            shot.telemetry = rebaseDates(JSON.parse(req.responseText));
        } catch (e) {
            console.error("mock-telemetry.json is not usable: " + e);
            Qt.exit(2);
        }
    }

    function isoDate(d) {
        const p = (n) => (n < 10 ? "0" : "") + n;
        return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
    }

    function rebaseDates(snap) {
        const today = new Date();
        snap.timestamp = today.toISOString();

        const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
        for (const key in snap.providers) {
            const cs = snap.providers[key].costSummary;
            if (!cs)
                continue;

            // Weekly bars: the fixture's 7 rows become the 7 days ending today.
            const weekly = cs.weeklyTokenUsage || [];
            for (let i = 0; i < weekly.length; ++i) {
                const d = new Date(today);
                d.setDate(today.getDate() - (weekly.length - 1 - i));
                weekly[i].date = isoDate(d);
                weekly[i].day = dayNames[d.getDay()];
            }

            // Month grid: 6 aligned weeks whose Sunday-start cell precedes the 1st
            // of the current month, matching what the backend emits.
            const monthly = cs.monthlyTokenUsage || [];
            const first = new Date(today.getFullYear(), today.getMonth(), 1);
            const gridStart = new Date(first);
            gridStart.setDate(1 - first.getDay());
            for (let i = 0; i < monthly.length; ++i) {
                const d = new Date(gridStart);
                d.setDate(gridStart.getDate() + i);
                monthly[i].date = isoDate(d);
                monthly[i].inMonth = d.getMonth() === today.getMonth();
                if (!monthly[i].inMonth || d > today) {
                    monthly[i].tokens = 0;
                    monthly[i].cost = 0;
                    monthly[i].label = "0";
                }
            }
        }
        return snap;
    }

    // ---------------------------------------------------------------------
    // Cost flyout. Driven through the SAME state the running widget uses —
    // costGraphMode for the Day/Week/Month tab, costDrawerOpen to open it — so a
    // screenshot can't show a configuration the widget can't actually reach.
    // ---------------------------------------------------------------------
    function openCostPopout(host) {
        host.costGraphMode = shot.graphMode;
        host.costDrawerOpen = true;
        if (!host.drawerExpanded) {
            // drawerExpanded = costDrawerOpen && hasCostSection(); a provider with no cost
            // data would leave the window hidden and grabToImage would refuse it.
            console.error("cost flyout stayed closed for provider=" + shot.provider
                          + " (no cost section in the fixture?)");
            Qt.exit(6);
            return;
        }
        const component = Qt.createComponent(
            repoRoot + "/io.github.dlansama.tallybar/contents/ui/CostPopout.qml");
        if (component.status === Component.Error) {
            console.error("failed to load CostPopout.qml: " + component.errorString());
            Qt.exit(3);
            return;
        }
        const win = component.createObject(shot, { root: host, anchorItem: flyoutAnchor });
        if (!win || !win.mainItem) {
            console.error("CostPopout did not instantiate");
            Qt.exit(3);
            return;
        }
        // The card paints no background of its own — Plasma's translucent surface normally
        // supplies it, and offscreen there is no such surface, so the light-on-dark palette
        // would land on transparent black. Inject the same backdrop the widget shots use,
        // INSIDE the mainItem (its parent is a QQuickRootItem, which grabToImage refuses).
        Qt.createQmlObject(
            'import QtQuick; Rectangle { anchors.fill: parent; z: -1000; radius: 18; '
            + 'color: "#1c1c1e"; border.width: 1; border.color: Qt.rgba(1, 1, 1, 0.08) }',
            win.mainItem, "costPopoutBackdrop");
        shot.popoutItem = win.mainItem;
    }

    // Stand-in for FullRepresentation's invisible 1x1 left-edge anchor.
    Item { id: flyoutAnchor; width: 1; height: 1 }

    Component.onCompleted: {
        loadTelemetry();
        loader.setSource(repoRoot + "/io.github.dlansama.tallybar/contents/ui/"
                         + (popout ? "FullRepresentation" : componentName) + ".qml");
    }

    // ---------------------------------------------------------------------
    // Backdrop. The real widget draws on Plasma's translucent popup surface (and
    // the compact representation on the panel), neither of which exists outside a
    // Plasma session — paint an equivalent dark surface so the light-on-dark
    // palette reads correctly in the PNG.
    // ---------------------------------------------------------------------
    Rectangle {
        id: backdrop

        anchors.fill: parent
        radius: shot.compact ? 10 : 18
        color: shot.compact ? "#141416" : "#1c1c1e"
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, 0.08)

        Loader {
            id: loader

            anchors.fill: parent
            anchors.margins: shot.compact ? 6 : 0

            onStatusChanged: {
                if (status === Loader.Error) {
                    console.error("failed to load " + shot.componentName + ".qml");
                    Qt.exit(3);
                }
            }
            onLoaded: {
                if (!item)
                    return;
                item.telemetry = shot.telemetry;
                item.selectedProvider = shot.provider;
                if ("tabs" in item)
                    item.tabs = shot.telemetry.config.providers;
                if ("loading" in item)
                    item.loading = false;
                if ("liveLoaded" in item)
                    item.liveLoaded = true;
                if ("lastError" in item)
                    item.lastError = "";
                if ("hostExpanded" in item)
                    item.hostExpanded = true;
                if ("screenGeometry" in item)
                    item.screenGeometry = Qt.rect(0, 0, 1920, 1080);
                // Size the surface to the widget's own implicit geometry so the PNG
                // carries the real proportions instead of an arbitrary crop.
                if (!shot.compact && item.implicitWidth > 0) {
                    shot.width = item.implicitWidth;
                    shot.height = item.implicitHeight;
                }
                if (shot.popout)
                    shot.openCostPopout(item);
            }
        }
    }

    // ---------------------------------------------------------------------
    // Grab — one settle pass first (fonts resolve, Repeaters instantiate,
    // entrance animations finish), then exit non-zero on any failure so
    // `make screenshots` reports it instead of leaving a stale PNG in place.
    // ---------------------------------------------------------------------
    Timer {
        interval: 2500
        running: true
        repeat: false
        onTriggered: {
            const target = shot.popout ? shot.popoutItem : backdrop;
            if (!target) {
                console.error("nothing to grab for component=" + shot.componentName);
                Qt.exit(6);
                return;
            }
            const size = Qt.size(target.width * shot.grabScale,
                                 target.height * shot.grabScale);
            const requested = target.grabToImage(function (result) {
                if (!result.saveToFile(shot.outFile)) {
                    console.error("saveToFile failed: " + shot.outFile);
                    Qt.exit(4);
                    return;
                }
                console.log("wrote " + shot.outFile + " ("
                            + Math.round(size.width) + "x" + Math.round(size.height) + ")");
                Qt.exit(0);
            }, size);
            if (!requested) {
                console.error("grabToImage refused (no render surface?)");
                Qt.exit(5);
            }
        }
    }
}
