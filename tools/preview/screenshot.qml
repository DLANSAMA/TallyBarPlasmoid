// Offscreen screenshot renderer for the TallyBar UI.
//
// Renders a UI component against a telemetry snapshot and writes a PNG — no Plasma
// session, no display. By default the snapshot is the shared mock fixture
// (tools/preview/mock-telemetry.json), which `make render-baselines` and
// tests/test_qml_render.py use; `snapshot=` renders a real backend snapshot instead,
// which is how `make screenshots` produces the README images. See tools/preview/README.md.
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
//   today=<YYYY-MM-DD> reference day the fixture's week/month buckets are rebased onto
//                     (default: the real today). The committed render baselines and
//                     tests/test_qml_render.py pin it, so a render is byte-stable on any
//                     day — rebasing onto the real today made the week labels and month
//                     grid (and so the PNG bytes) change daily.
//   snapshot=<abs path> a backend.py --once JSON snapshot to render as-is, in place of
//                     the mock fixture. Its dates are real, so it is never rebased and
//                     today= is ignored.
//   walk=<name>       CostPopout only: move a pointer through the flyout before grabbing.
//                     The pointer is driven through Qt's own test events, so the widget's
//                     real hover/click code runs. `hover` jumps from the bar beside the
//                     busiest one straight onto it (no gap crossed) and rests until its
//                     tooltip shows; `tour` (the default with demo=) rests on each tab's
//                     busiest bar, clicks it open to the per-model breakdown, then the next tab.
//   demo=<abs dir>    with a walk: write one frame per tick into <dir> as f-<n>-<ms>.png
//                     instead of one PNG; `make screenshots` stitches them (demo_gif.py).
//   cursors=<abs dir> with a walk: cursors.json + arrow/hand PNGs drawn as the pointer
//                     (offscreen has no cursor of its own; demo_gif.py cursors writes them).
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
    readonly property string snapshotPath: argValue("snapshot", "")
    readonly property string demoDir: argValue("demo", "")
    readonly property string cursorDir: argValue("cursors", "")
    readonly property string walk: argValue("walk", demoDir.length > 0 ? "tour" : "")
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
        const real = shot.snapshotPath.length > 0;
        const source = real ? "file://" + shot.snapshotPath : Qt.resolvedUrl("mock-telemetry.json");
        const req = new XMLHttpRequest();
        req.open("GET", source, false); // sync: nothing to race
        req.send(null);
        // A missing file:// read reports status 0 with an empty body, not 404.
        if ((req.status !== 200 && req.status !== 0) || !req.responseText) {
            console.error("could not read " + source + " (status " + req.status + ")");
            Qt.exit(2);
            return;
        }
        try {
            const snap = JSON.parse(req.responseText);
            shot.telemetry = real ? snap : rebaseDates(snap);
        } catch (e) {
            console.error(source + " is not usable: " + e);
            Qt.exit(2);
        }
    }

    function isoDate(d) {
        const p = (n) => (n < 10 ? "0" : "") + n;
        return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
    }

    // Noon on the requested day, so no timezone offset can push it across midnight.
    function referenceDay() {
        const arg = argValue("today", "");
        const m = arg.match(/^(\d{4})-(\d{2})-(\d{2})$/);
        if (!m)
            return new Date();
        return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12, 0, 0);
    }

    function rebaseDates(snap) {
        const today = referenceDay();
        // The snapshot's own timestamp stays REAL now: the widget ages it against the
        // live clock ("Updated just now"), so pinning it would make that line drift instead.
        snap.timestamp = new Date().toISOString();

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
        running: shot.walk.length === 0
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

    // ---------------------------------------------------------------------
    // Pointer walk (walk=, demo=). Everything runs on the real clock, because the widget's
    // own timers (the 0.7s tooltip rest) and animations do; each frame's file name carries
    // the time it was grabbed so the GIF can replay at real speed however slow a grab was.
    // ---------------------------------------------------------------------
    readonly property int demoFrameMs: 50
    readonly property string demoCanvas: "#141416"
    property var demo: null

    Timer {
        interval: 1500
        running: shot.walk.length > 0
        repeat: false
        onTriggered: shot.startDemo()
    }

    Timer {
        id: demoTimer
        repeat: false
        onTriggered: shot.demoTick()
    }

    function findItem(item, test) {
        if (!item)
            return null;
        if (test(item))
            return item;
        for (let k = 0; k < item.children.length; ++k) {
            const hit = findItem(item.children[k], test);
            if (hit)
                return hit;
        }
        return null;
    }

    // The bar / hour / day delegates of the chart on screen. chartArea is the item that owns
    // the tooltip state; the hidden tabs' Repeaters are empty.
    function chartBuckets(card) {
        const chart = findItem(card, (it) => typeof it.expandedKey === "string" && it.tipShown !== undefined);
        const found = [];
        const walk = (it) => {
            if (!it.visible)
                return;
            const d = it.modelData;
            if (d && d.tokens !== undefined && d.inMonth !== false) {
                found.push(it);
                return;
            }
            for (let k = 0; k < it.children.length; ++k)
                walk(it.children[k]);
        };
        if (chart)
            walk(chart);
        return found;
    }

    // The busiest bucket on the current tab, or its neighbour `offset` places along.
    function barPoint(card, offset) {
        const buckets = chartBuckets(card);
        if (buckets.length === 0) {
            console.error("walk: no chart bars on the " + loader.item.costGraphMode + " tab");
            Qt.exit(6);
            return Qt.point(0, 0);
        }
        let best = 0;
        for (let k = 1; k < buckets.length; ++k)
            if (Number(buckets[k].modelData.tokens || 0) > Number(buckets[best].modelData.tokens || 0))
                best = k;
        let at = best + offset;
        if (at < 0 || at >= buckets.length)  // no neighbour that side: take the other one
            at = best - offset;
        const bucket = buckets[Math.max(0, Math.min(buckets.length - 1, at))];
        // Aim inside the drawn bar (the column's first rounded child); a month cell is its own.
        let target = bucket;
        for (let k = 0; k < bucket.children.length; ++k) {
            if (bucket.children[k].radius !== undefined) {
                target = bucket.children[k];
                break;
            }
        }
        return target.mapToItem(card, target.width / 2, target.height * 0.6);
    }

    function tabPoint(card, mode) {
        const tab = findItem(card, (it) => it.modelData !== undefined && it.modelData !== null
                                          && it.modelData.mode === mode);
        return tab.mapToItem(card, tab.width / 2, tab.height / 2);
    }

    // The cursor the widget would show: the shape of the MouseArea under the pointer.
    function pointerShape(card) {
        let shape = Qt.ArrowCursor;
        const walk = (it) => {
            if (!it.visible)
                return;
            if (it.containsMouse === true && it.cursorShape !== undefined)
                shape = it.cursorShape;
            for (let k = 0; k < it.children.length; ++k)
                walk(it.children[k]);
        };
        walk(card);
        return shape;
    }

    // A hand-like glide: ease in and out along a shallow arc, not a ruler line.
    function glide(a, b, k) {
        const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
        const dx = b.x - a.x, dy = b.y - a.y;
        const bow = Math.sin(Math.PI * e) * 0.12;
        return Qt.point(a.x + dx * e - dy * bow, a.y + dy * e + dx * bow);
    }

    function demoSteps(card) {
        const home = Qt.point(card.width * 0.74, card.height * 0.9);
        if (shot.walk === "hover") {
            // The jump between two neighbouring bars is ONE move event, so Qt delivers the
            // busiest bar's enter before the neighbour's exit: the tooltip must still show.
            return [{ at: home }, { to: () => shot.barPoint(card, -1), ms: 500 }, { wait: 150 },
                    { at: () => shot.barPoint(card, 0) }, { wait: 1200 }];
        }
        if (shot.walk !== "tour") {
            console.error("walk: unknown walk=" + shot.walk + " (hover | tour)");
            Qt.exit(1);
            return [];
        }
        // Day -> Week -> Month: rest on the busiest bar until its tooltip shows, click it open
        // to the per-model breakdown, then the next tab; back to Day and home so the GIF loops.
        const showBar = () => [{ to: () => shot.barPoint(card, 0), ms: 700 }, { wait: 1100 },
                               { click: true }, { wait: 1900 }];
        const openTab = (mode) => [{ to: () => shot.tabPoint(card, mode), ms: 600 }, { wait: 150 },
                                   { click: true }, { wait: 350 }];
        return [{ at: home }, { wait: 400 }].concat(
            showBar(), openTab("week"), showBar(), openTab("month"), showBar(), openTab("day"),
            [{ to: () => home, ms: 700 }, { wait: 400 }]);
    }

    function startDemo() {
        const card = shot.popoutItem;
        if (!card) {
            console.error("walk: the cost flyout did not open");
            Qt.exit(6);
            return;
        }
        let spec;
        try {
            const req = new XMLHttpRequest();
            req.open("GET", "file://" + shot.cursorDir + "/cursors.json", false);
            req.send(null);
            spec = JSON.parse(req.responseText);
        } catch (e) {
            console.error("walk: no cursors.json in cursors=" + shot.cursorDir + ": " + e);
            Qt.exit(2);
            return;
        }
        const s = shot.grabScale;
        const sprite = (role, shown) => {
            const c = spec[role];
            return 'Image { visible: ' + shown + '; smooth: true; source: "file://' + shot.cursorDir
                + '/' + c.file + '"; width: ' + c.width / s + '; height: ' + c.height / s
                + '; x: parent.px - ' + c.hotX / s + '; y: parent.py - ' + c.hotY / s + ' } ';
        };
        // Above everything in the card; a plain Item takes no hover, so it can't steal it.
        const overlay = Qt.createQmlObject(
            'import QtQuick; Item { z: 100000; property real px; property real py; '
            + 'property bool hand; property real ring: -1; '
            + 'Rectangle { visible: parent.ring >= 0; width: 12 + 22 * Math.max(0, parent.ring); '
            + 'height: width; radius: width / 2; x: parent.px - width / 2; y: parent.py - height / 2; '
            + 'color: "transparent"; border.width: 2; '
            + 'border.color: Qt.rgba(1, 1, 1, 0.6 * (1 - Math.max(0, parent.ring))) } '
            + sprite("arrow", "!parent.hand") + sprite("hand", "parent.hand") + '}',
            card, "demoPointer");
        // GIF frames must be opaque: ffmpeg's GIF encoder only stores the changed pixels of
        // frames without transparency, and the card's rounded corners are transparent. Same
        // colour as the padding demo_gif.py adds when the card is shorter than the tallest frame.
        if (shot.demoDir.length > 0)
            Qt.createQmlObject('import QtQuick; Rectangle { anchors.fill: parent; z: -2000; color: "'
                               + shot.demoCanvas + '" }', card, "demoCanvas");
        const events = Qt.createQmlObject("import QtTest; TestEvent {}", shot, "demoEvents");
        shot.demo = { card: card, overlay: overlay, events: events, steps: demoSteps(card),
                      i: 0, stepAt: 0, pos: Qt.point(0, 0), clickAt: -1e9, start: Date.now(), frame: 0,
                      resizingSince: -1 };
        demoTick();
    }

    // Runs every step whose time has come; true once the walk is over.
    function advanceDemo(d, t) {
        const move = (p) => {
            d.pos = p;
            d.events.mouseMove(d.card, p.x, p.y, 0, Qt.NoButton, Qt.NoModifier);
        };
        while (d.i < d.steps.length) {
            const step = d.steps[d.i];
            const elapsed = t - d.stepAt;
            let ms = 0;
            if (step.at !== undefined) {
                move(typeof step.at === "function" ? step.at() : step.at);
            } else if (step.wait !== undefined) {
                ms = step.wait;
                if (elapsed < ms)
                    return false;
            } else if (step.click) {
                ms = 90;
                if (!step.pressed) {
                    step.pressed = true;
                    d.clickAt = d.stepAt;
                    d.events.mousePress(d.card, d.pos.x, d.pos.y, Qt.LeftButton, Qt.NoModifier, 0);
                }
                if (elapsed < ms)
                    return false;
                d.events.mouseRelease(d.card, d.pos.x, d.pos.y, Qt.LeftButton, Qt.NoModifier, 0);
            } else {
                ms = step.ms;
                if (!step.target) {
                    step.from = d.pos;
                    step.target = step.to();
                }
                const k = Math.min(1, elapsed / ms);
                move(glide(step.from, step.target, k));
                if (k < 1)
                    return false;
            }
            d.stepAt += ms;
            d.i += 1;
        }
        return true;
    }

    function demoTick() {
        const d = shot.demo;
        const t = Date.now() - d.start;
        const finished = advanceDemo(d, t);
        d.overlay.px = d.pos.x;
        d.overlay.py = d.pos.y;
        d.overlay.hand = pointerShape(d.card) === Qt.PointingHandCursor;
        d.overlay.ring = (t - d.clickAt) < 380 ? (t - d.clickAt) / 380 : -1;
        const frames = shot.demoDir.length > 0;
        // Right after a tab switch the card already holds the new tab while its window, and
        // so the card, still has the old tab's size: the offscreen window resizes a beat
        // later, and a grab then catches the new tab squeezed or stretched into the old
        // height. A compositor never shows that state (it keeps the last frame until the
        // resized one is drawn), so wait for the size to land and let the last frame stand.
        const resizing = Math.abs(d.card.width - d.card.implicitWidth) >= 1
                      || Math.abs(d.card.height - d.card.implicitHeight) >= 1;
        if (resizing && !finished) {
            if (d.resizingSince < 0)
                d.resizingSince = t;
            if (t - d.resizingSince < 1000) {
                demoTimer.interval = 5;
                demoTimer.restart();
                return;
            }
            console.warn("walk: the flyout's size did not settle; grabbing anyway");
        }
        d.resizingSince = -1;
        if (!frames && !finished) {
            // A single-PNG walk only grabs its last state.
            demoTimer.interval = shot.demoFrameMs;
            demoTimer.restart();
            return;
        }
        const name = frames ? shot.demoDir + "/f-" + String(d.frame).padStart(5, "0") + "-"
                              + String(t).padStart(6, "0") + ".png"
                            : shot.outFile;
        d.frame += 1;
        const card = d.card;
        const requested = card.grabToImage(function (result) {
            if (!result.saveToFile(name)) {
                console.error("saveToFile failed: " + name);
                Qt.exit(4);
                return;
            }
            if (finished) {
                console.log(frames ? "wrote " + d.frame + " frames (" + (t / 1000).toFixed(1) + "s) to " + shot.demoDir
                                   : "wrote " + name + " (" + Math.round(card.width * shot.grabScale) + "x"
                                     + Math.round(card.height * shot.grabScale) + ")");
                Qt.exit(0);
                return;
            }
            demoTimer.interval = Math.max(1, d.start + t + shot.demoFrameMs - Date.now());
            demoTimer.restart();
        }, Qt.size(card.width * shot.grabScale, card.height * shot.grabScale));
        if (!requested) {
            console.error("grabToImage refused (no render surface?)");
            Qt.exit(5);
        }
    }
}
