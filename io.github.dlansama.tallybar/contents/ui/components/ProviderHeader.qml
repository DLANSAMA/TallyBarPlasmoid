import QtQuick
import QtQuick.Layouts

Item {
    id: heroPanel

    property var root

    Layout.fillWidth: true
    Layout.preferredHeight: 50

    Text {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: 0
        anchors.rightMargin: 10
        anchors.topMargin: 3
        color: root.primaryTextColor()
        elide: Text.ElideRight
        font.family: root.displayFont
        font.pixelSize: 13
        font.weight: Font.DemiBold
        textFormat: Text.PlainText
        text: root.providerData().label
    }

    Text {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.rightMargin: 10
        anchors.topMargin: 30
        // Amber (fixed palette — never Kirigami/PlasmaCore theme) once the snapshot is
        // stale (> 2× refresh interval), so an old "Updated 18m ago" actually reads as old.
        // Also amber when THIS provider's entry is a carried-forward (per-provider stale)
        // one, even if the overall snapshot timestamp is fresh.
        color: (root.stalenessLevel() > 0 || root.refreshPaused() || root.providerStale()) ? "#e0a23c" : root.mutedTextColor(0.66)
        elide: Text.ElideRight
        font.family: root.uiFont
        font.pixelSize: 11
        font.weight: Font.Normal
        textFormat: Text.PlainText
        text: root.providerSubtitleText()
    }

    Rectangle {
        id: separator

        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 1
        color: root.separatorColor(0.14)
    }
}
