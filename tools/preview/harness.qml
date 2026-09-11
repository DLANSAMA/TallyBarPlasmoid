// QML preview harness for TallyBar UI components.
// Stubs the Plasma/Plasmoid API surface so components can be loaded in the
// standalone `qml` runner without a real KDE Plasma session.
//
// Usage (via Makefile):
//   make preview                          # loads FullRepresentation.qml
//   make preview COMPONENT=CostPopout     # loads CostPopout.qml
//
// The COMPONENT name is spliced in by the Makefile via -DcomponentName=...
// QML's qt.uiLanguage/command-line -D definitions land in Qt.application.arguments,
// but the simpler approach used here is a plain env-var read at startup via
// Qt.application.arguments parsing (see below).

import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import QtQuick.Window

Window {
    id: harness
    title: "TallyBar Preview — " + componentName
    width: 420
    height: 700
    visible: true
    color: "#1e1e2e"

    // -----------------------------------------------------------------------
    // Component name: injected by -DcomponentName=Foo on the qml command line.
    // The `qml` binary exposes -D key=value pairs via Qt.application.arguments
    // starting at index 1; we parse them here.
    // -----------------------------------------------------------------------
    property string componentName: {
        var args = Qt.application.arguments;
        for (var i = 1; i < args.length; ++i) {
            var m = args[i].match(/^componentName=(.+)$/);
            if (m) return m[1];
        }
        return "FullRepresentation";
    }

    // -----------------------------------------------------------------------
    // Mock telemetry — loaded from tools/preview/mock-telemetry.json, the same
    // fixture `make screenshots` renders, so the preview and the published
    // screenshots can't drift apart. It matches the JSON shape backend.py emits
    // (providers[].limits / .costSummary / .tier), which is what the UI reads.
    //
    // Needs QML_XHR_ALLOW_FILE_READ=1 (set by the Makefile) — QML disables local
    // file reads through XMLHttpRequest by default.
    // -----------------------------------------------------------------------
    property var mockTelemetry: ({})

    function loadMockTelemetry() {
        var req = new XMLHttpRequest();
        req.open("GET", Qt.resolvedUrl("mock-telemetry.json"), false); // sync: nothing to race
        req.send(null);
        if (req.status !== 200 && req.status !== 0) {
            console.error("could not read mock-telemetry.json (status " + req.status + ")");
            return;
        }
        try {
            var snap = JSON.parse(req.responseText);
            snap.timestamp = new Date().toISOString();   // else the UI shows "Updated 180d ago"
            harness.mockTelemetry = snap;
        } catch (e) {
            console.error("mock-telemetry.json is not valid JSON: " + e);
        }
    }

    Component.onCompleted: harness.loadMockTelemetry()

    // -----------------------------------------------------------------------
    // Stub: PlasmaCore / Plasmoid globals not available outside Plasma.
    // These QtObject stubs satisfy property lookups on commonly accessed names.
    // Components that *inherit* PlasmaCore types (PopupPlasmaWindow) cannot be
    // loaded this way — see README.md.
    // -----------------------------------------------------------------------
    QtObject {
        id: plasmoidStub
        property string pluginName: "io.github.dlansama.tallybar"
        property bool expanded: true
        property var configuration: QtObject {
            property int refreshInterval: 5
        }
    }

    // -----------------------------------------------------------------------
    // Loader — resolves path relative to the harness file location
    // -----------------------------------------------------------------------
    property string uiDir: {
        // Qt.resolvedUrl gives us an absolute file:// URL to this harness.
        // Walk up two levels (tools/preview → root) then into the UI directory.
        var here = String(Qt.resolvedUrl("."));
        // Strip trailing slash if any
        if (here.charAt(here.length - 1) === "/") here = here.slice(0, -1);
        // up two levels: preview -> tools -> repo root
        var root = here.replace(/\/[^/]+\/[^/]+$/, "");
        return root + "/io.github.dlansama.tallybar/contents/ui/";
    }

    Loader {
        id: loader
        anchors.fill: parent
        anchors.margins: 8

        source: harness.uiDir + harness.componentName + ".qml"

        onStatusChanged: {
            if (status === Loader.Error)
                console.error("Loader error for " + source);
            else if (status === Loader.Ready)
                console.log("Loaded: " + source);
        }

        // Inject mock data after the component is ready
        onLoaded: {
            if (item && "telemetry" in item)
                item.telemetry = harness.mockTelemetry;
            if (item && "selectedProvider" in item)
                item.selectedProvider = "claude";
            if (item && "loading" in item)
                item.loading = false;
            if (item && "liveLoaded" in item)
                item.liveLoaded = true;
            if (item && "lastError" in item)
                item.lastError = "";
            if (item && "screenGeometry" in item)
                item.screenGeometry = Qt.rect(0, 0, 1920, 1080);
        }
    }

    // Status bar at bottom
    Rectangle {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 24
        color: "#11111b"

        Text {
            anchors.centerIn: parent
            text: loader.status === Loader.Ready ? "Loaded: " + harness.componentName
                : loader.status === Loader.Error ? "ERROR loading " + harness.componentName + " (see console)"
                : "Loading..."
            color: loader.status === Loader.Error ? "#f38ba8" : "#a6e3a1"
            font.pixelSize: 11
        }
    }
}
