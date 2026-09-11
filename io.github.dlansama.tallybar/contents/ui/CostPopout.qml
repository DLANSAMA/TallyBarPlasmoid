// Token-usage graph as a SEPARATE floating glass window beside the widget (the macOS-
// submenu look — Day / Week / Month tabs, per-bucket hover tooltips that expand with a
// per-model breakdown on CLICK only, a close x). Extracted
// verbatim from FullRepresentation.qml; `root` is the FullRepresentation root (all the helper
// functions / cost data / costGraphMode state live there) and `anchorItem` is the invisible
// 1x1 LEFT-EDGE anchor (flyoutAnchor) KWin places this window against so it opens leftward,
// beside the Cost row, never over the body. Auto-closes on focus-out.
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.core as PlasmaCore
import "lib/format.js" as Fmt

PlasmaCore.PopupPlasmaWindow {
    id: costPopout

    // The FullRepresentation root (helpers, cost data, costGraphMode state, signals) and the
    // invisible left-edge anchor this window is placed against — both injected by the parent so
    // the block below stays a verbatim copy of the original inline version.
    property var root
    property Item anchorItem

    visualParent: anchorItem
    popupDirection: Qt.LeftEdge
    floating: true
    animated: true
    margin: root.drawerGap
    width: tokenCard.implicitWidth + leftPadding + rightPadding
    height: tokenCard.implicitHeight + topPadding + bottomPadding
    // visualParent is assigned declaratively (always present) and drawerExpanded
    // starts false, so binding visible here can't flash before placement.
    visible: root.drawerExpanded

    // Close when the popout loses focus (click on the body/elsewhere) so it
    // behaves like a normal flyout. costDrawerOpen is the single source of truth.
    onActiveChanged: {
        if (!active)
            root.costDrawerOpen = false;
    }

    mainItem: Item {
        id: tokenCard

        implicitWidth: root.drawerWidth
        implicitHeight: root.drawerHeight
        focus: true

        Keys.onEscapePressed: (event) => {
            root.costDrawerOpen = false;
            event.accepted = true;
        }

        // Feature 4: copy a bucket's full breakdown (date + total + per-model rows) to the
        // clipboard on right-click. QML has no clipboard API, so route the text through an
        // off-screen TextEdit and use its selectAll()/copy().
        TextEdit {
            id: clipboardHelper
            visible: false
            width: 0
            height: 0
        }
        function copyBucketText(text) {
            const s = String(text || "");
            if (s.length === 0)
                return;
            clipboardHelper.text = s;
            clipboardHelper.selectAll();
            clipboardHelper.copy();
            clipboardHelper.deselect();
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 12
            anchors.topMargin: 12
            anchors.bottomMargin: 12
            spacing: 10

            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 34
                spacing: 8

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1

	                        Text {
	                            Layout.fillWidth: true
	                            color: root.primaryTextColor()
	                            elide: Text.ElideRight
	                            font.family: root.displayFont
	                            font.pixelSize: 13
	                            font.weight: Font.DemiBold
	                            text: i18n("Token usage")
	                        }

                    Text {
                        Layout.fillWidth: true
                        color: root.mutedTextColor(0.64)
	                            elide: Text.ElideRight
	                            font.family: root.uiFont
	                            font.pixelSize: 11
	                            textFormat: Text.PlainText
	                            text: root.costGraphSubtitle()
	                        }

                }

                Text {
                    Layout.preferredWidth: 22
                    Layout.preferredHeight: 22
                    color: closeCostDrawerMouse.containsMouse ? root.primaryTextColor() : root.mutedTextColor(0.7)
                    font.family: root.uiFont
                    font.pixelSize: 21
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    text: "×"

                    MouseArea {
                        id: closeCostDrawerMouse
                        Accessible.role: Accessible.Button
                        Accessible.name: i18n("Close token usage")
                        Accessible.description: i18n("Close the token usage graph")

                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: root.costDrawerOpen = false
                    }

                }

	                }

                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 26

                    Row {
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.verticalCenter: parent.verticalCenter
                        height: parent.height
                        spacing: 6

                        Repeater {
                            model: [{
                                "mode": "day",
                                "label": i18n("Day")
                            }, {
                                "mode": "week",
                                "label": i18n("Week")
                            }, {
                                "mode": "month",
                                "label": i18n("Month")
                            }]

                            delegate: Rectangle {
                                required property var modelData

                                property bool selected: root.costGraphMode === modelData.mode

                                width: 84
                                height: parent.height
                                radius: 7
                                color: selected ? Qt.rgba(1, 1, 1, 0.18) : (graphModeMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.1) : Qt.rgba(1, 1, 1, 0.04))
                                border.width: selected ? 1 : 0
                                border.color: Qt.rgba(1, 1, 1, 0.18)

                                Text {
                                    anchors.centerIn: parent
                                    color: parent.selected ? root.primaryTextColor(0.96) : root.mutedTextColor(0.72)
                                    font.family: root.uiFont
                                    font.pixelSize: 11
                                    font.weight: parent.selected ? Font.DemiBold : Font.Medium
                                    horizontalAlignment: Text.AlignHCenter
                                    textFormat: Text.PlainText
                                    text: modelData.label
                                }

                                MouseArea {
                                    id: graphModeMouse
                                    Accessible.role: Accessible.Button
                                    Accessible.name: modelData.label
                                    Accessible.description: i18n("Show %1 token usage", modelData.label)

                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    hoverEnabled: true
                                    onClicked: root.costGraphMode = modelData.mode
                                }

                                Behavior on color {
                                    ColorAnimation {
                                        duration: 120
                                    }

                                }

                            }

                        }

                    }

                }

                Item {
                    id: chartArea

                    Layout.fillWidth: true
                    Layout.fillHeight: false
                    Layout.minimumHeight: 150
                    Layout.preferredHeight: 150
                    Layout.maximumHeight: 150

                    // Hover tooltip shared by the weekly bars and the month calendar.
                    // The per-cell MouseAreas set pendingTip + the pointer position (mapped
                    // into chartArea) and restart chartTipTimer; the tip is only REVEALED
                    // (tipShown) once the timer fires — i.e. after the pointer rests ~0.7s, so
                    // it doesn't flash on pass-over — then fades/scales in. tipText latches the
                    // displayed string (kept during fade-out so the box doesn't collapse).
                    property string tipText: ""
                    property string pendingTip: ""
                    property bool tipShown: false
                    property real tipX: 0
                    property real tipY: 0
                    // Identity of the bucket whose tip is click-EXPANDED with the
                    // per-model breakdown ("w:<date>" / "h:<hour>" / "m:<date>"). NOT a
                    // pin: the tip still follows hover and hides on exit — leaving the
                    // bucket (or clicking it again) collapses back to the two base lines.
                    property string expandedKey: ""

                    Timer {
                        id: chartTipTimer
                        interval: 700
                        repeat: false
                        onTriggered: {
                            chartArea.tipText = chartArea.pendingTip;
                            chartArea.tipShown = chartArea.pendingTip.length > 0;
                        }
                    }
                    Timer {
                        id: chartTipHideTimer
                        interval: 50
                        onTriggered: chartArea.tipShown = false
                    }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: barBase.top
                        anchors.bottomMargin: -1
                        height: 1
                        // Bars share a baseline in both Day and Week mode (barBase keeps its
                        // geometry while hidden, so anchoring to barBase.top is valid in Day too).
                        visible: root.costGraphMode !== "month"
                        color: root.separatorColor(0.11)
                    }

                    Row {
                        id: barBase

                        visible: root.costGraphMode === "week"
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        spacing: 9

                        Repeater {
                            model: root.costGraphMode === "week" ? root.costWeekHistory() : []

                            delegate: Item {
                                id: dayColumn

                                required property var modelData
                                property int tokens: Number(modelData.tokens || 0)
                                property real usageRatio: tokens > 0 ? Math.max(0, Math.min(1, tokens / root.maxWeekTokens())) : 0
                                property real barRatio: Math.max(0.025, usageRatio)

                                width: (barBase.width - barBase.spacing * 6) / 7
                                height: barBase.height

                                Text {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: tokenBar.top
                                    anchors.bottomMargin: 6
                                    color: root.mutedTextColor(0.68)
                                    elide: Text.ElideRight
                                    font.family: root.uiFont
                                    font.pixelSize: 11
                                    horizontalAlignment: Text.AlignHCenter
                                    textFormat: Text.PlainText
                                    text: dayColumn.tokens > 0 ? Fmt.compactCount(dayColumn.tokens) : ""
                                }

                                Rectangle {
                                    id: tokenBar

                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.bottom: dayLabel.top
                                    anchors.bottomMargin: 8
                                    width: Math.max(10, parent.width - 6)
                                    height: Math.max(5, (parent.height - 44) * dayColumn.barRatio)
                                    radius: 3
                                    // Hover lift: brighten the neutral track a touch so the bar
                                    // reads as interactive (the click-to-expand breakdown is
                                    // otherwise undiscoverable). Stays within the neutral palette.
                                    color: root.neutralUsageColor((dayColumn.tokens > 0 ? 0.1 : 0.16) + (weekBarMouse.containsMouse ? 0.06 : 0))

                                    // Clean, near-solid provider-tinted fill (matches the macOS
                                    // reference). Lower-usage days stay slightly muted via opacity
                                    // so the neutral track shows through and they read as lighter.
                                    Rectangle {
                                        anchors.fill: parent
                                        radius: parent.radius
                                        visible: dayColumn.tokens > 0
                                        color: root.accentColor()
                                        // Hover bumps the accent fill brighter (clamped) — same
                                        // provider accent, no new colour.
                                        opacity: Math.min(1, root.usageOpacityFromRatio(dayColumn.usageRatio, 0.7, 1.0) + (weekBarMouse.containsMouse ? 0.15 : 0))
                                    }

                                    Behavior on height {
                                        SpringAnimation {
spring: 4.0
damping: 0.85
epsilon: 0.01
}

                                    }

                                }

                                Text {
                                    id: dayLabel

                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    color: root.primaryTextColor(0.82)
                                    elide: Text.ElideRight
                                    font.family: root.uiFont
                                    font.pixelSize: 11
                                    font.weight: Font.Medium
                                    horizontalAlignment: Text.AlignHCenter
                                    textFormat: Text.PlainText
                                    text: String(modelData.day || "").slice(0, 3)
                                }

                                MouseArea {
                                    id: weekBarMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                                    Accessible.role: Accessible.Button
                                    Accessible.name: root.weekDayTooltipText(dayColumn.modelData).replace(/\n/g, ", ")
                                    function tipKey() {
                                        return "w:" + String(dayColumn.modelData.date);
                                    }
                                    function updateTip() {
                                        const p = mapToItem(chartArea, mouseX, mouseY);
                                        chartArea.tipX = p.x;
                                        chartArea.tipY = p.y;
                                        const base = root.weekDayTooltipText(dayColumn.modelData);
                                        chartArea.pendingTip = (chartArea.expandedKey === tipKey())
                                            ? root.withModelLines(base, dayColumn.modelData, i18n("Daily total")) : base;
                                        // Already shown (moving within a bar): refresh text live.
                                        if (chartArea.tipShown)
                                            chartArea.tipText = chartArea.pendingTip;
                                    }
                                    onEntered: { updateTip(); chartTipHideTimer.stop(); chartTipTimer.restart(); }
                                    onPositionChanged: updateTip()
                                    onExited: { chartArea.expandedKey = ""; chartTipTimer.stop(); chartTipHideTimer.restart(); }
                                    onClicked: (mouse) => {
                                        if (mouse.button === Qt.RightButton) {
                                            // Copy the full breakdown (date + total + per-model rows).
                                            const base = root.weekDayTooltipText(dayColumn.modelData);
                                            tokenCard.copyBucketText(root.withModelLines(base, dayColumn.modelData, i18n("Daily total")));
                                            return;
                                        }
                                        chartArea.expandedKey = (chartArea.expandedKey === tipKey()) ? "" : tipKey();
                                        updateTip();
                                        // Reveal immediately on click — no 700ms hover-rest wait.
                                        chartTipTimer.stop();
                                        chartArea.tipText = chartArea.pendingTip;
                                        chartArea.tipShown = chartArea.pendingTip.length > 0;
                                    }
                                }

                            }

                        }

                    }

                    // Day view: today broken down by local hour (24 thin bars), same
                    // visual language as the weekly bars. Empty hours render as a faint
                    // track; only every 6th hour is labelled (12a / 6a / 12p / 6p) so the
                    // axis stays legible across 24 columns. Hover reuses the shared chartTip.
                    Row {
                        id: hourBase

                        visible: root.costGraphMode === "day"
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        spacing: 2

                        Repeater {
                            model: root.costGraphMode === "day" ? root.costHourHistory() : []

                            delegate: Item {
                                id: hourColumn

                                required property var modelData
                                property int tokens: Number(modelData.tokens || 0)
                                property int hour: Number(modelData.hour || 0)
                                property real usageRatio: tokens > 0 ? Math.max(0, Math.min(1, tokens / root.maxHourTokens())) : 0
                                property real barRatio: Math.max(0.02, usageRatio)
                                property bool tick: (hour % 6) === 0

                                width: (hourBase.width - hourBase.spacing * 23) / 24
                                height: hourBase.height

                                Rectangle {
                                    id: hourBar

                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.bottom: parent.bottom
                                    anchors.bottomMargin: 20
                                    width: Math.max(4, parent.width - 1)
                                    height: Math.max(3, (parent.height - 30) * hourColumn.barRatio)
                                    radius: 2
                                    // Hover lift within the neutral palette (see week bars).
                                    color: root.neutralUsageColor((hourColumn.tokens > 0 ? 0.1 : 0.16) + (hourBarMouse.containsMouse ? 0.06 : 0))

                                    Rectangle {
                                        anchors.fill: parent
                                        radius: parent.radius
                                        visible: hourColumn.tokens > 0
                                        color: root.accentColor()
                                        opacity: Math.min(1, root.usageOpacityFromRatio(hourColumn.usageRatio, 0.7, 1.0) + (hourBarMouse.containsMouse ? 0.15 : 0))
                                    }

                                    Behavior on height {
                                        SpringAnimation {
spring: 4.0
damping: 0.85
epsilon: 0.01
}

                                    }

                                }

                                Text {
                                    id: hourLabel

                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.bottom: parent.bottom
                                    anchors.bottomMargin: 2
                                    color: root.primaryTextColor(0.7)
                                    font.family: root.uiFont
                                    font.pixelSize: 11
                                    font.weight: Font.Medium
                                    horizontalAlignment: Text.AlignHCenter
                                    // Unconstrained width centred on the column so the label can
                                    // overflow into the (blank) neighbouring columns without eliding.
                                    textFormat: Text.PlainText
                                    text: hourColumn.tick ? String(modelData.label || "") : ""
                                }

                                MouseArea {
                                    id: hourBarMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                                    Accessible.role: Accessible.Button
                                    Accessible.name: root.hourTooltipText(hourColumn.modelData).replace(/\n/g, ", ")
                                    function tipKey() {
                                        return "h:" + String(hourColumn.modelData.hour);
                                    }
                                    function updateTip() {
                                        const p = mapToItem(chartArea, mouseX, mouseY);
                                        chartArea.tipX = p.x;
                                        chartArea.tipY = p.y;
                                        const base = root.hourTooltipText(hourColumn.modelData);
                                        chartArea.pendingTip = (chartArea.expandedKey === tipKey())
                                            ? root.withModelLines(base, hourColumn.modelData, i18n("Hourly total")) : base;
                                        // Already shown (moving within a bar): refresh text live.
                                        if (chartArea.tipShown)
                                            chartArea.tipText = chartArea.pendingTip;
                                    }
                                    onEntered: { updateTip(); chartTipHideTimer.stop(); chartTipTimer.restart(); }
                                    onPositionChanged: updateTip()
                                    onExited: { chartArea.expandedKey = ""; chartTipTimer.stop(); chartTipHideTimer.restart(); }
                                    onClicked: (mouse) => {
                                        if (mouse.button === Qt.RightButton) {
                                            const base = root.hourTooltipText(hourColumn.modelData);
                                            tokenCard.copyBucketText(root.withModelLines(base, hourColumn.modelData, i18n("Hourly total")));
                                            return;
                                        }
                                        chartArea.expandedKey = (chartArea.expandedKey === tipKey()) ? "" : tipKey();
                                        updateTip();
                                        // Reveal immediately on click — no 700ms hover-rest wait.
                                        chartTipTimer.stop();
                                        chartArea.tipText = chartArea.pendingTip;
                                        chartArea.tipShown = chartArea.pendingTip.length > 0;
                                    }
                                }

                            }

                        }

                    }

                    Item {
                        id: monthView

                        visible: root.costGraphMode === "month"
                        anchors.fill: parent

                        Item {
                            id: monthGridHolder

                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            anchors.topMargin: 5
                            anchors.bottomMargin: 5

                            Grid {
                                id: monthCalendar

                                // Fill the holder's full width across 7 columns so the calendar spans
                                // the card instead of leaving side gaps, while cell height fills the 6
                                // rows. (Previously cells were square at min(width,height)-based size,
                                // so the shorter height constraint won and left horizontal whitespace.)
                                readonly property real cellH: (parent.height - rowSpacing * 5) / 6
                                // widthFill = cell width that fully fills the holder across 7 columns.
                                // fillFraction tunes how much of that width to use: 1.0 = edge-to-edge
                                // (looked a smidge too wide), lower leaves a centered side margin.
                                readonly property real widthFill: (parent.width - columnSpacing * 6) / 7
                                readonly property real fillFraction: 0.96
                                readonly property real cellW: widthFill * fillFraction

                                anchors.centerIn: parent
                                width: cellW * 7 + columnSpacing * 6
                                height: cellH * 6 + rowSpacing * 5
                                columns: 7
                                columnSpacing: 5
                                rowSpacing: 5

                                Repeater {
                                    model: root.costGraphMode === "month" ? root.costMonthHistory() : []

                                    delegate: Rectangle {
                                        required property var modelData

                                        width: monthCalendar.cellW
                                        height: monthCalendar.cellH
                                        radius: 3
                                        color: root.monthCellColor(modelData)
                                        // Hover lift: brighten the in-month cell a touch so it reads
                                        // as clickable. Out-of-month padding cells stay at 0 opacity
                                        // (transparent colour) — the bump can't reveal them.
                                        opacity: Math.min(1, root.monthCellOpacity(modelData) + (monthCellMouse.containsMouse && modelData.inMonth === true ? 0.12 : 0))

                                        MouseArea {
                                            id: monthCellMouse
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            // Only the real month days are clickable; blank padding
                                            // cells keep the arrow cursor.
                                            cursorShape: (modelData && modelData.inMonth === true) ? Qt.PointingHandCursor : Qt.ArrowCursor
                                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                                            Accessible.role: Accessible.Button
                                            Accessible.name: root.monthCellTooltipText(modelData).replace(/\n/g, ", ")
                                            function tipKey() {
                                                return "m:" + String(modelData.date);
                                            }
                                            function updateTip() {
                                                const p = mapToItem(chartArea, mouseX, mouseY);
                                                chartArea.tipX = p.x;
                                                chartArea.tipY = p.y;
                                                const base = root.monthCellTooltipText(modelData);
                                                chartArea.pendingTip = (base && chartArea.expandedKey === tipKey())
                                                    ? root.withModelLines(base, modelData, i18n("Daily total")) : base;
                                                // Already shown (moving within a day): refresh text live.
                                                if (chartArea.tipShown)
                                                    chartArea.tipText = chartArea.pendingTip;
                                            }
                                            onEntered: { updateTip(); chartTipHideTimer.stop(); chartTipTimer.restart(); }
                                            onPositionChanged: updateTip()
                                            onExited: { chartArea.expandedKey = ""; chartTipTimer.stop(); chartTipHideTimer.restart(); }
                                            onClicked: (mouse) => {
                                                if (mouse.button === Qt.RightButton) {
                                                    const base = root.monthCellTooltipText(modelData);
                                                    if (base)
                                                        tokenCard.copyBucketText(root.withModelLines(base, modelData, i18n("Daily total")));
                                                    return;
                                                }
                                                chartArea.expandedKey = (chartArea.expandedKey === tipKey()) ? "" : tipKey();
                                                updateTip();
                                                // Reveal immediately on click — no 700ms hover-rest wait.
                                                chartTipTimer.stop();
                                                chartArea.tipText = chartArea.pendingTip;
                                                chartArea.tipShown = chartArea.pendingTip.length > 0;
                                            }
                                        }
                                    }

                                }

                            }

                        }

                    }

                    Rectangle {
                        id: chartTip

                        // Smooth fade + subtle grow-in on reveal (and fade-out on leave),
                        // driven by tipShown. visible tracks opacity so the fade-out renders.
                        visible: opacity > 0
                        opacity: chartArea.tipShown ? 1 : 0
                        scale: chartArea.tipShown ? 1 : 0.92
                        z: 50
                        radius: 5
                        color: Qt.rgba(0.1, 0.11, 0.13, 0.96)
                        border.width: 1
                        border.color: root.separatorColor(0.18)
                        width: chartTipLabel.implicitWidth + 16
                        height: chartTipLabel.implicitHeight + 10
                        // Follow the pointer (right-of-cursor on the chart's left half,
                        // left-of-cursor on the right half, so the edge days stay visible),
                        // but BACKSTOP-clamp to the popout window — the chart plus the card's
                        // ~12px side margins — so a tip can never be cut off at the window
                        // edge (it "only shows where the popout is" and clips otherwise). The
                        // two-line tips are narrow enough that this clamp almost never engages,
                        // so it keeps the smooth following without the old chart-bounds "pinned"
                        // feel. Vertically it sits above the cursor, dropping below near the top.
                        x: Math.max(-10, Math.min(chartArea.width - width + 10,
                               (chartArea.tipX < chartArea.width / 2)
                               ? chartArea.tipX + 12
                               : chartArea.tipX - 12 - width))
                        // Pinned (clicked) tips grow taller with the per-model lines, so the
                        // vertical position gets the same backstop clamp as x — a breakdown
                        // opened near the chart bottom must not run past the popout edge.
                        y: Math.max(-6, Math.min(chartArea.height - height + 10,
                               (chartArea.tipY - height - 8 >= 0)
                               ? chartArea.tipY - height - 8
                               : chartArea.tipY + 14))

                        Behavior on opacity {
                            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
                        }

                        Behavior on scale {
                            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
                        }

                        Text {
                            id: chartTipLabel

                            anchors.centerIn: parent
                            color: root.primaryTextColor(0.95)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            // Two-line tips (date + total) stay centred; once the per-model
                            // rows are appended the ragged list reads better left-aligned.
                            horizontalAlignment: chartArea.tipText.split("\n").length > 2
                                                 ? Text.AlignLeft : Text.AlignHCenter
                            lineHeight: 1.25
                            textFormat: Text.PlainText
                            text: chartArea.tipText
                        }

                    }

                }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: root.separatorColor(0.12)
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 18
                    spacing: 10

                    Text {
                        Layout.preferredWidth: 82
                        color: root.mutedTextColor(0.68)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        text: i18n("Today")
                    }

                    Text {
                        Layout.fillWidth: true
                        color: root.primaryTextColor(0.92)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 13
                        horizontalAlignment: Text.AlignRight
                        textFormat: Text.PlainText
                        text: root.costLineValue(root.todayCostText(root.costSummary() || ({
                        })), "Today")
                    }

                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 18
                    spacing: 10

                    Text {
                        Layout.preferredWidth: 82
                        color: root.mutedTextColor(0.68)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        text: i18n("Last 7 days")
                    }

                    Text {
                        Layout.fillWidth: true
                        color: root.primaryTextColor(0.86)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 13
                        horizontalAlignment: Text.AlignRight
                        textFormat: Text.PlainText
                        text: root.costLineValue(root.weekCostText(root.costSummary() || ({
                        })), "Last 7 days")
                    }

                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 18
                    spacing: 10

                    Text {
                        Layout.preferredWidth: 82
                        color: root.mutedTextColor(0.68)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        text: i18n("Last 30 days")
                    }

                    Text {
                        Layout.fillWidth: true
                        color: root.primaryTextColor(0.8)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 13
                        horizontalAlignment: Text.AlignRight
                        textFormat: Text.PlainText
                        text: root.costLineValue(root.monthCostText(root.costSummary() || ({
                        })), "Last 30 days")
                    }

                }

                // Burn rate: trailing-7-day average $/day + projected month at that rate.
                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: root.costBurnRowHeight  // mirrored in drawerHeightFor()
                    spacing: 10
                    visible: Number((root.costSummary() || ({})).burnRatePerDay || 0) > 0

                    Text {
                        Layout.preferredWidth: 82
                        color: root.mutedTextColor(0.68)
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 11
                        text: i18n("Burn rate")
                    }

                    Text {
                        Layout.fillWidth: true
                        color: root.accentColor()
                        elide: Text.ElideRight
                        font.family: root.uiFont
                        font.pixelSize: 13
                        horizontalAlignment: Text.AlignRight
                        textFormat: Text.PlainText
                        text: {
                            const c = root.costSummary() || ({});
                            return i18n("%1/day · ~%2/mo",
                                root.compactUsd(Number(c.burnRatePerDay || 0)),
                                root.compactUsd(Number(c.projectedMonthlyCost || 0)));
                        }
                    }

                }

                // Per-model cost attribution (top models by 30-day spend).
                Rectangle {
                    Layout.fillWidth: true
                    Layout.topMargin: 3
                    Layout.preferredHeight: 1
                    color: root.separatorColor(0.1)
                    visible: root.costModelBreakdown().length > 0
                }

                Text {
                    Layout.fillWidth: true
                    color: root.mutedTextColor(0.55)
                    font.family: root.uiFont
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                    text: i18n("TOP MODELS · 30 DAYS")
                    visible: root.costModelBreakdown().length > 0
                }

                Repeater {
                    model: root.costModelBreakdown(4)

                    delegate: RowLayout {
                        required property var modelData

                        Layout.fillWidth: true
                        Layout.preferredHeight: root.costModelRowHeight  // mirrored in drawerHeightFor()
                        spacing: 10

                        Text {
                            Layout.fillWidth: true
                            color: root.primaryTextColor(0.86)
                            elide: Text.ElideRight
                            font.family: root.uiFont
                            font.pixelSize: 11
                            textFormat: Text.PlainText
                            text: root.prettyModelName(modelData.model)
                        }

                        Text {
                            color: root.mutedTextColor(0.72)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            horizontalAlignment: Text.AlignRight
                            textFormat: Text.PlainText
                            text: root.compactUsd(Number(modelData.cost || 0)) + " · "
                                + i18n("%1 tok", Fmt.compactCount(Number(modelData.tokens || 0)))
                        }
                    }
                }

            }

        }

    }

}
