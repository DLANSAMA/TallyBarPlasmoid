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
//   component=<name>  FullRepresentation | CompactRepresentation
//   provider=<key>    provider tab to select           (default claude)
//   scale=<n>         pixel ratio the grab renders at  (default 2)
//
// Only the two in-widget components can be captured. The Cost and Settings
// flyouts are each a separate PopupPlasmaWindow, and grabbing an item that lives
// in another window's scene from here fails with "cannot call function with
// argument created in a different engine" — those two need a live Plasma session
// and a real screen grab.

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
    readonly property real grabScale: Number(argValue("scale", "2"))
    readonly property bool compact: componentName === "CompactRepresentation"

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

    Component.onCompleted: {
        loadTelemetry();
        loader.setSource(repoRoot + "/io.github.dlansama.tallybar/contents/ui/" + componentName + ".qml");
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
            const size = Qt.size(backdrop.width * shot.grabScale,
                                 backdrop.height * shot.grabScale);
            const requested = backdrop.grabToImage(function (result) {
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
