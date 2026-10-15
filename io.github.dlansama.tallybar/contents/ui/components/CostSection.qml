import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import "../lib/format.js" as Fmt

Item {
    id: costSection

    property var root
    property var cost: root.costSummary() || ({})
    property bool hasRealCost: root.hasCostSummary()
    property string breakdownLine: root.costBreakdownText()
    property bool hasBreakdown: costSection.breakdownLine.length > 0
    // Output tokens the backend estimated because the log had no final usage for them
    // (subagent turns from some Claude Code versions). Explained on hover only, so the
    // section's height — mirrored by costSectionHeight() — never changes.
    property real estimatedOutputTokens: Number(costSection.cost.estimatedOutputTokens || 0)
    readonly property string estimateNote: costSection.estimatedOutputTokens > 0
        ? i18n("Includes about %1 estimated output tokens: some subagent messages were logged without their final usage.", Fmt.compactCount(costSection.estimatedOutputTokens))
        : ""
    // The window whose line the pointer is on: "today", "7d", or "30d" (the breakdown line
    // under the 30-day line belongs to it); "" over the title.
    readonly property string hoverWindow: {
        if (!costMouse.containsMouse || !costSection.hasRealCost)
            return "";
        const y = costMouse.mouseY;
        if (y >= monthText.y - 3)
            return "30d";
        if (weekText.visible && y >= weekText.y - 3)
            return "7d";
        return y >= todayText.y - 3 ? "today" : "";
    }
    // Hovering a line shows that window by type (hover only, so the height never changes);
    // the estimate note rides with the 30-day figures it is part of.
    readonly property string tipText: {
        const split = costSection.hoverWindow.length > 0 ? root.costTypeTooltip(costSection.hoverWindow) : "";
        if (costSection.estimateNote.length === 0 || costSection.hoverWindow === "today" || costSection.hoverWindow === "7d")
            return split;
        return split.length > 0 ? split + "\n\n" + costSection.estimateNote : costSection.estimateNote;
    }
    // Under the line it describes, so it never covers it.
    readonly property real tipY: {
        if (costSection.hoverWindow === "today")
            return todayText.y + todayText.height + 4;
        if (costSection.hoverWindow === "7d")
            return weekText.y + weekText.height + 4;
        if (costSection.hoverWindow === "30d")
            return (breakdownText.visible ? breakdownText.y + breakdownText.height : monthText.y + monthText.height) + 4;
        return costTitle.y + costTitle.height + 4;
    }

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
        textFormat: Text.PlainText
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
        textFormat: Text.PlainText
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

    QQC2.ToolTip {
        parent: costSection
        x: Math.round((costSection.width - width) / 2)
        y: costSection.tipY
        delay: 450
        text: costSection.tipText
        visible: costMouse.containsMouse && costSection.tipText.length > 0
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
        textFormat: Text.PlainText
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
        textFormat: Text.PlainText
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
        textFormat: Text.PlainText
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
        // 30-day tokens by type: Input · Output · Cached (cache reads + writes).
        textFormat: Text.PlainText
        text: costSection.breakdownLine
        visible: costSection.hasBreakdown
    }
}
