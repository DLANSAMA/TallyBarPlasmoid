import QtQuick
import QtQuick.Layouts

Item {
    id: costSection

    property var root
    property var cost: root.costSummary() || ({})
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
        // Per-type token breakdown (Input · Output · Cached) for the 30-day window.
        textFormat: Text.PlainText
        text: String(costSection.cost.breakdown || "")
        visible: costSection.hasBreakdown
    }
}
