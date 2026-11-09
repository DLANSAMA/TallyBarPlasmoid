// Offscreen state probe: loads a UI component against a backend snapshot, evaluates JS
// expressions against the live item and prints the results, so a test can assert what the
// widget WOULD show (subtitle, strip, empty-state message, tab flags) without comparing pixels.
//
//   QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software \
//   qml6 tools/preview/probe.qml -- snapshot=/abs/snap.json provider=grok \
//        'expr=["item.updatedText()", "item.degradedText()"]'
//
// Arguments (key=value after `--`, as in screenshot.qml):
//   snapshot=<abs path>  backend snapshot JSON (required)
//   component=<name>     FullRepresentation (default) | CompactRepresentation
//   provider=<key>       selected provider tab           (default claude)
//   loading=<0|1>        the widget's `loading` flag     (default 0)
//   live=<0|1>           the widget's `liveLoaded` flag  (default 1)
//   lastError=<text>     the widget's `lastError`
//   expr=<json array>    JS expressions; `item` is the loaded component
//
// Prints one line, `PROBE <json array of results>`, and exits 0 (3 when a load or an
// expression fails).

import QtQuick
import QtQuick.Window

Window {
    id: probe

    function argValue(key, fallback) {
        const args = Qt.application.arguments;
        for (let i = 1; i < args.length; ++i) {
            const prefix = key + "=";
            if (args[i].indexOf(prefix) === 0)
                return args[i].slice(prefix.length);
        }
        return fallback;
    }

    readonly property string componentName: argValue("component", "FullRepresentation")
    readonly property string repoRoot: {
        let here = String(Qt.resolvedUrl("."));
        if (here.charAt(here.length - 1) === "/")
            here = here.slice(0, -1);
        return here.replace(/\/[^/]+\/[^/]+$/, "");
    }

    width: 380
    height: 620
    visible: true

    function fail(message) {
        console.error("probe: " + message);
        Qt.exit(3);
    }

    Loader {
        id: loader
        anchors.fill: parent
        onStatusChanged: {
            if (status === Loader.Error)
                probe.fail("failed to load " + probe.componentName + ".qml");
        }
        onLoaded: {
            const req = new XMLHttpRequest();
            req.open("GET", "file://" + probe.argValue("snapshot", ""), false);
            req.send(null);
            if (!req.responseText) {
                probe.fail("could not read the snapshot");
                return;
            }
            item.telemetry = JSON.parse(req.responseText);
            item.selectedProvider = probe.argValue("provider", "claude");
            if ("loading" in item)
                item.loading = probe.argValue("loading", "0") === "1";
            if ("liveLoaded" in item)
                item.liveLoaded = probe.argValue("live", "1") === "1";
            if ("lastError" in item)
                item.lastError = probe.argValue("lastError", "");
            if ("hostExpanded" in item)
                item.hostExpanded = true;
            if ("screenGeometry" in item)
                item.screenGeometry = Qt.rect(0, 0, 1920, 1080);
            settle.start();
        }
    }

    // Bindings and Repeater delegates settle before the expressions run.
    Timer {
        id: settle
        interval: 400
        onTriggered: {
            const out = [];
            try {
                const exprs = JSON.parse(probe.argValue("expr", "[]"));
                for (let i = 0; i < exprs.length; ++i)
                    out.push(new Function("item", "return (" + exprs[i] + ");")(loader.item));
            } catch (e) {
                probe.fail("expression failed: " + e);
                return;
            }
            console.log("PROBE " + JSON.stringify(out));
            Qt.exit(0);
        }
    }

    Component.onCompleted: loader.setSource(repoRoot + "/io.github.dlansama.tallybar/contents/ui/"
                                            + componentName + ".qml")
}
