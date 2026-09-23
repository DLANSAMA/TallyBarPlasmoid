import QtQuick
import QtQuick.Layouts
import "lib/format.js" as Fmt
import "lib/ui_helpers.js" as UIHelpers

Item {
    id: root

    signal toggleRequested()
    // Panel-icon interactions. The mouse wheel and middle-click cycle the panel
    // display MODE (percent → reset → cost → pace → percent); the choice is persisted by
    // main.qml via the config-write DataSource. tabs is kept for Accessible naming.
    property var tabs: []
    signal cycleModeRequested(int direction)
    // Accumulate wheel deltas so a high-resolution touchpad doesn't machine-gun the cycle;
    // one notch (Qt reports 120 per detent) = one provider step.
    property int wheelAccumulator: 0
    property var telemetry: ({
    })
    property string selectedProvider: "gemini"
    readonly property real primaryPercent: percentAt(0)
    readonly property real secondaryPercent: percentAt(1)
    // Worst-of-both-windows: a near-exhausted weekly/secondary limit must still pulse
    // the systray warning even while the fast-resetting primary/session window is fresh.
    readonly property real worstPercent: Math.max(primaryPercent, secondaryPercent)
    readonly property bool warning: worstPercent >= 72 && !root.isMuted()
    readonly property bool critical: worstPercent >= 90 && !root.isMuted()
    property real warningPulse: 1.0
    property bool vertical: false
    readonly property real indicatorWidth: root.vertical ? 16 : Math.max(24, Math.min(root.width > 0 ? root.width - 8 : 30, 30))
    readonly property real barHeight: 3
    readonly property real barGap: 4
    property bool hovered: false

    function providerData() {
        const providers = root.telemetry && root.telemetry.providers ? root.telemetry.providers : ({
        });
        return providers[root.selectedProvider] || {
            "label": "TallyBar",
            "limits": []
        };
    }

    // The panel's bars, pulse and text modes read CAPACITY windows only — an extra-usage /
    // credit row (e.g. Codex "Credits" as the 2nd limit) must not become the secondary bar
    // or trigger the warning pulse. Same filter as the tray badge (ui_helpers.usageLimits).
    function usageRows() {
        return UIHelpers.usageLimits(root.providerData().limits);
    }

    function percentAt(index) {
        const limits = root.usageRows();
        if (limits.length <= index)
            return 0;

        return Math.max(0, Math.min(100, Number(limits[index].percent || 0)));
    }

    function usageSummary() {
        const limits = root.usageRows();
        if (limits.length === 0)
            return "--";

        let parts = [];
        const count = Math.min(2, limits.length);
        for (let i = 0; i < count; ++i) {
            const pct = Math.max(0, Math.min(100, Number(limits[i].percent || 0)));
            parts.push(pct.toFixed(0) + "%");
        }
        return parts.join("/");
    }

    function providerStatus() {
        return String(root.providerData().status || "");
    }

    // A shown provider whose live status can't report real usage (sign-in / unlock needed,
    // timed out, not running) renders identical empty bars to a healthy 0%-used account —
    // so the panel icon dims and shows a small badge, keyed off STATUS not usage percent
    // (usage-keyed amber/red recolour stays locked out). Mirrors FullRepresentation's
    // tabStatusBad set, plus the sign-in / not-running actionable states.
    function statusIsBad(status) {
        // The ONE bad-status set lives in lib/ui_helpers.js (shared with FullRepresentation).
        return UIHelpers.statusIsBad(status);
    }
    // A muted provider is dropped from the badge/dim logic (still visible in popup).
    function isMuted() {
        const cfg = root.telemetry && root.telemetry.config;
        const muted = cfg && Array.isArray(cfg.mutedProviders) ? cfg.mutedProviders : [];
        return muted.indexOf(root.selectedProvider) >= 0;
    }
    readonly property bool statusBad: root.statusIsBad(root.providerStatus()) && !root.isMuted()

    // Short human phrase for the bad status, for the screen-reader description.
    function statusText() {
        switch (root.providerStatus()) {
        case "missing-cookies":
        case "unauthorized":
            return i18n("sign-in needed");
        case "wallet-locked":
        case "wallet-state-unknown":
            return i18n("KWallet locked");
        case "not-running":
        case "missing-cli":
            return i18n("not running");
        case "timeout":
            return i18n("timed out");
        default:
            return i18n("unavailable");
        }
    }

    // Accent fallback lives in lib/format.js (Fmt.providerAccent) — a SINGLE source shared
    // with FullRepresentation so the panel and popup can't drift. PlasmaCore.Theme does not
    // exist in Plasma 6, so accents come from the backend with this hardcoded fallback.
    function accentColor() {
        return root.providerData().accentColor || Fmt.providerAccent(root.selectedProvider);
    }

    function compactFillColor() {
        // Usage bars stay the provider color at every percentage — no warning/critical
        // recolor. The fill width (and the warning pulse) still convey how full it is.
        return root.accentColor();
    }

    // Panel-text font resolved via the shared Fmt.pickFont (same list as
    // FullRepresentation.uiFont) so the panel and popup never diverge.
    readonly property var availableFonts: Qt.fontFamilies()
    readonly property string uiFont: Fmt.pickFont(root.availableFonts,
        ["SF Pro Text", "SF Pro", "Inter", "Roboto", "Noto Sans"])

    // Panel display mode (config.panelDisplayMode): "percent" keeps the two bars;
    // "reset"/"cost"/"pace" swap the bars for a compact text value of the selected
    // provider's primary limit. Default "percent" preserves the original look.
    readonly property string panelMode: {
        const cfg = root.telemetry && root.telemetry.config;
        const m = cfg && cfg.panelDisplayMode ? String(cfg.panelDisplayMode) : "percent";
        return (m === "reset" || m === "cost" || m === "pace") ? m : "percent";
    }
    // Text metric only applies to a horizontal panel; a narrow vertical panel keeps the
    // two-bar glyph (rotated horizontal text wouldn't fit).
    readonly property bool textMode: root.panelMode !== "percent" && !root.vertical

    function primaryLimit() {
        const limits = root.usageRows();
        return limits.length > 0 ? limits[0] : null;
    }

    function panelResetText() {
        const l = root.primaryLimit();
        let r = String(l && l.reset ? l.reset : "").replace(/^Resets in\s*/i, "").trim();
        const m = r.match(/\d+\s*[dhm]/);   // first unit, e.g. "3d" / "20m"
        if (m)
            return m[0].replace(/\s+/g, "");
        // A non-time reset ("Enterprise spend limit", "Monthly credit pool", "") would
        // otherwise balloon the panel icon — keep it short.
        return r.length > 0 ? "—" : "--";
    }

    function panelCostText() {
        const cs = root.providerData().costSummary || root.providerData().cost || ({});
        const v = Number(cs.costToday || 0);
        if (!(v > 0))
            return "$0";
        if (v >= 1000)
            return "$" + (v / 1000).toFixed(1).replace(/\.0$/, "") + "K";  // uppercase K matches the rest of the UI
        if (v < 1)
            return "<$1";  // a real sub-dollar spend shouldn't read as "$0" (= no cost)
        return "$" + Math.round(v);
    }

    function panelPaceText() {
        // Usage percentages as "session/weekly" (e.g. "4/74%"); just the primary
        // percent when the provider has no weekly window.
        const l = root.primaryLimit();
        if (!l)
            return "--";
        const session = Math.round(Number(l.percent || 0));
        const limits = root.usageRows();
        const lane = String(l.label || "");
        for (let i = 1; i < limits.length; i++) {
            const w = limits[i] || {};
            const weekly = String(w.window || "") === "weekly"
                || /^week/i.test(String(w.label || ""))
                || Number(w.windowMinutes || 0) === 10080;
            // Antigravity carries two lanes (Gemini / Claude·GPT) — pair the
            // primary limit with its own lane's weekly, not another lane's.
            if (weekly && (String(w.label || "") === lane || /^week/i.test(String(w.label || ""))))
                return session + "/" + Math.round(Number(w.percent || 0)) + "%";
        }
        return session + "%";
    }

    function panelText() {
        if (root.panelMode === "reset")
            return root.panelResetText();
        if (root.panelMode === "cost")
            return root.panelCostText();
        if (root.panelMode === "pace")
            return root.panelPaceText();
        return "";
    }

    Accessible.role: Accessible.Button
    Accessible.name: i18n("%1 usage monitor", providerData().label)
    Accessible.description: root.statusBad
        ? i18n("%1 — %2", providerData().label, root.statusText())
        : providerData().label + " " + usageSummary()
    // In text mode the icon sizes to the value (e.g. "$1.2k" / "3d 18h"); in percent
    // mode it keeps the original compact bar-glyph width.
    Layout.minimumWidth: root.textMode ? (panelLabel.implicitWidth + 8) : (vertical ? 24 : 32)
    Layout.preferredWidth: root.textMode ? (panelLabel.implicitWidth + 12) : (vertical ? 28 : 36)
    Layout.maximumWidth: root.textMode ? (panelLabel.implicitWidth + 16) : (vertical ? 32 : 38)
    Layout.minimumHeight: 0
    Layout.preferredHeight: vertical ? 28 : 24
    Layout.maximumHeight: Infinity
    clip: true

    SequentialAnimation {
        id: pulseAnim
        running: root.visible && root.warning
        loops: Animation.Infinite
        NumberAnimation { target: root; property: "warningPulse"; to: 0.62; duration: root.critical ? 450 : 1250; easing.type: Easing.InOutSine }
        NumberAnimation { target: root; property: "warningPulse"; to: 1.0; duration: root.critical ? 450 : 1250; easing.type: Easing.InOutSine }
    }
    onWarningChanged: {
        if (!warning) {
            pulseAnim.stop();
            warningPulse = 1.0;
        }
    }

    // Text mode: a compact metric value replaces the bars (provider-coloured, with the
    // same warning pulse). Sized by the Layout above.
    Text {
        id: panelLabel

        visible: root.textMode
        anchors.centerIn: parent
        textFormat: Text.PlainText
        text: root.panelText()
        color: root.compactFillColor()
        // Dim (not recolour) when the provider's status is bad — a status signal, not a
        // usage one, so the accent-colour rule is preserved.
        opacity: (root.warning ? root.warningPulse : 0.95) * (root.statusBad ? 0.5 : 1.0)
        font.pixelSize: root.vertical ? 11 : 12
        font.weight: Font.DemiBold
        font.family: root.uiFont
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }

    Item {
        id: menuGlyph

        visible: !root.textMode
        anchors.centerIn: parent
        width: root.indicatorWidth
        height: root.barHeight * 2 + root.barGap
        // A bad-status provider shows empty bars identical to a healthy 0%-used account;
        // dimming the whole glyph reads it as inactive (paired with the badge below). This
        // is a status signal, so it doesn't touch the accent-colour rule.
        opacity: root.statusBad ? 0.4 : 1.0

        Rectangle {
            id: primaryTrack

            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: root.barHeight
            radius: height / 2
            color: Qt.rgba(1, 1, 1, root.hovered ? 0.3 : 0.19)
            border.width: 0

            Rectangle {
                id: primaryFill

                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: root.primaryPercent > 0 ? Math.max(parent.height, parent.width * root.primaryPercent / 100) : 0
                radius: parent.radius
                color: root.compactFillColor()
                opacity: root.warning ? root.warningPulse : 0.95

                Behavior on width {
                    SpringAnimation {
    spring: 4.0
    damping: 0.85
    epsilon: 0.01
}

                }

            }

        }

        Rectangle {
            id: secondaryTrack

            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: root.barHeight
            radius: height / 2
            color: Qt.rgba(1, 1, 1, root.hovered ? 0.24 : 0.14)
            border.width: 0

            Rectangle {
                id: secondaryFill

                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: root.secondaryPercent > 0 ? Math.max(parent.height, parent.width * root.secondaryPercent / 100) : 0
                radius: parent.radius
                color: root.compactFillColor()
                opacity: root.warning ? Math.max(0.62, root.warningPulse) : 0.72

                Behavior on width {
                    SpringAnimation {
    spring: 4.0
    damping: 0.85
    epsilon: 0.01
}

                }

            }

        }

    }

    // Small, unobtrusive status badge (a corner dot) whenever the shown provider is in a
    // bad/actionable status — the one cue that distinguishes an unreachable provider from a
    // healthy 0%-used one. Amber is a status colour (like FullRepresentation's tab warning
    // glyph), not a usage recolour, so the accent-colour-at-every-percent rule still holds.
    Rectangle {
        visible: root.statusBad
        width: 5
        height: 5
        radius: 2.5
        color: "#e0a23c"
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 1
        anchors.rightMargin: 1
    }

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        onEntered: () => {
            root.hovered = true;
        }
        onExited: () => {
            root.hovered = false;
        }
        onPressed: (mouse) => {
            mouse.accepted = true;
        }
        onClicked: (mouse) => {
            // Middle-click cycles the panel display mode; left-click keeps expand/collapse.
            if (mouse.button === Qt.MiddleButton)
                root.cycleModeRequested(1);
            else
                root.toggleRequested();
        }
        onWheel: (wheel) => {
            root.wheelAccumulator += wheel.angleDelta.y;
            // One detent (±120) = one step. Scroll-up cycles forward, scroll-down back.
            while (root.wheelAccumulator >= 120) {
                root.wheelAccumulator -= 120;
                root.cycleModeRequested(1);
            }
            while (root.wheelAccumulator <= -120) {
                root.wheelAccumulator += 120;
                root.cycleModeRequested(-1);
            }
            wheel.accepted = true;
        }
    }

}
