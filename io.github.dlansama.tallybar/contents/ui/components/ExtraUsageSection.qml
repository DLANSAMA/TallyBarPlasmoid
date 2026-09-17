import QtQuick
import QtQuick.Layouts

Item {
    id: extraUsageSection

    property var root
    property var extraLimit: root.extraUsageLimit() || ({})
    property real pct: root.safePercent(extraLimit.percent || 0)
    // The section is shown whenever there's an extra-usage DETAIL line, but the
    // bar only makes sense with a real isExtraUsage limit. A credit-only provider
    // (detail set, no isExtraUsage limit) would otherwise render an empty 0% track.
    property bool hasBar: root.extraUsageLimit() !== null

    Layout.fillWidth: true
    Layout.preferredHeight: visible ? root.extraUsageBodyHeight : 0
    visible: root.hasExtraUsage()

    // Extra usage is an informational row (no click target), so it
    // gets no hover highlight — only the Cost section below reacts
    // to its own pointer.

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 1
        color: root.separatorColor(0.14)
    }

    Text {
        id: extraTitle

        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: 18
        color: root.primaryTextColor()
        elide: Text.ElideRight
        font.family: root.uiFont
        font.pixelSize: 13
        font.weight: Font.DemiBold
        text: i18n("Extra usage")
    }

    Rectangle {
        id: extraTrack

        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: extraTitle.bottom
        anchors.topMargin: 8
        height: 5
        radius: 2.5
        color: root.neutralUsageColor(0.2)
        // Keep the geometry (the detail text anchors to extraTrack.bottom and the
        // section height mirror stays 86) but don't paint an empty track when
        // there's no real limit behind it.
        visible: extraUsageSection.hasBar

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            height: parent.height
            width: extraUsageSection.pct > 0 ? Math.max(parent.height, parent.width * extraUsageSection.pct / 100) : 0
            radius: parent.radius
            color: root.accentColor()
            opacity: root.usageOpacityFromPercent(extraUsageSection.pct, 0.72, 0.96)
        }
    }

    Text {
        anchors.left: parent.left
        anchors.right: extraPercent.left
        anchors.top: extraTrack.bottom
        anchors.rightMargin: 10
        anchors.topMargin: 7
        color: root.primaryTextColor(0.92)
        elide: Text.ElideRight
        font.family: root.uiFont
        font.pixelSize: 11
        text: root.extraUsageDetail()
    }

    Text {
        id: extraPercent

        anchors.right: parent.right
        anchors.top: extraTrack.bottom
        anchors.topMargin: 7
        color: root.mutedTextColor(0.64)
        font.family: root.uiFont
        font.pixelSize: 11
        horizontalAlignment: Text.AlignRight
        text: root.metricUsedText(extraUsageSection.extraLimit)
    }
}
