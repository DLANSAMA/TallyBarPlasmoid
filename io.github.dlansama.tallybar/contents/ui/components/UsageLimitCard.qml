import QtQuick
import QtQuick.Layouts

Item {
    id: row

    required property var root
    required property int index

    // Reactive lookup (re-evaluates when telemetry changes) instead of a
    // model-provided role, so the persistent delegate refreshes in place.
    property var modelData: (root && root.usageLimits) ? (root.usageLimits()[index] || ({})) : ({})
    property real pct: (root && root.safePercent) ? root.safePercent(modelData ? (modelData.percent || 0) : 0) : 0
    property bool warning: pct >= 72
    property bool critical: pct >= 90
    property real pulse: 1.0

    SequentialAnimation {
        id: rowPulseAnim

        running: (root && root.visible) ? row.warning : false
        loops: Animation.Infinite

        NumberAnimation {
            target: row
            property: "pulse"
            to: 0.62
            duration: row.critical ? 450 : 1250
            easing.type: Easing.InOutSine
        }

        NumberAnimation {
            target: row
            property: "pulse"
            to: 1.0
            duration: row.critical ? 450 : 1250
            easing.type: Easing.InOutSine
        }
    }

    onWarningChanged: {
        if (!warning) {
            rowPulseAnim.stop();
            pulse = 1.0;
        }
    }

    property string detailLeft: (root && root.metricDetailLeftText) ? root.metricDetailLeftText(modelData) : ""
    property string detailRight: (root && root.metricDetailRightText) ? root.metricDetailRightText(modelData) : ""
    property string detailText: (root && root.metricDetailText) ? root.metricDetailText(modelData) : ""
    property bool hasSplitDetail: detailLeft.length > 0 || detailRight.length > 0
    property bool hasDetailLine: detailText.length > 0

    Layout.fillWidth: true
    Layout.preferredHeight: (root && root.rowMetricHeight) ? root.rowMetricHeight(modelData) : 0  // single source, mirrored in metricsBodyHeight()
    clip: false

    Text {
        id: label

        anchors.left: parent.left
        anchors.top: parent.top
        anchors.topMargin: 1
        color: (root && root.primaryTextColor) ? root.primaryTextColor() : "#ffffff"
        elide: Text.ElideRight
        font.family: (root && root.uiFont) ? root.uiFont : ""
        font.pixelSize: 13
        font.weight: Font.DemiBold
        textFormat: Text.PlainText
        text: (modelData && modelData.label) ? modelData.label : i18n("Usage")
        width: Math.min(implicitWidth, Math.max(0, parent.width - (sublabel.visible ? sublabel.implicitWidth + 7 : 0)))
    }

    Text {
        id: sublabel

        anchors.left: label.right
        anchors.leftMargin: 7
        anchors.baseline: label.baseline
        visible: text.length > 0
        color: (root && root.mutedTextColor) ? root.mutedTextColor(0.45) : "#888888"
        elide: Text.ElideRight
        font.family: (root && root.uiFont) ? root.uiFont : ""
        font.pixelSize: 11
        font.weight: Font.Normal
        textFormat: Text.PlainText
        text: String(row.modelData && row.modelData.sublabel ? row.modelData.sublabel : "")
        width: Math.min(implicitWidth, Math.max(0, parent.width - label.width - 7))
    }

    Rectangle {
        id: track

        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: label.bottom
        anchors.topMargin: 8
        height: 5
        radius: 2.5
        color: (root && root.neutralUsageColor) ? root.neutralUsageColor(0.2) : "#333333"

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            height: parent.height
            width: row.pct > 0 ? Math.max(parent.height, parent.width * row.pct / 100) : 0
            radius: parent.radius
            color: (root && root.accentColor) ? root.accentColor() : "#3daee9"
            // Near-solid fill at any value (macOS: width encodes the amount,
            // not opacity). Keep only a whisper of gradient for subtle depth.
            opacity: row.warning ? row.pulse : ((root && root.usageOpacityFromPercent) ? root.usageOpacityFromPercent(row.pct, 0.72, 0.96) : 0.8)

            Behavior on width {
                SpringAnimation {
                    spring: 4.0
                    damping: 0.85
                    epsilon: 0.01
                }
            }
        }

        // Elapsed-time marker. Drawn after the fill so it stays
        // legible over it; taller than the track so it reads as a
        // tick rather than a gap in the bar.
        Rectangle {
            id: paceMarker

            readonly property real pacePct: (root && root.metricPacePercent) ? root.metricPacePercent(row.modelData) : -1

            visible: pacePct >= 0
            anchors.verticalCenter: parent.verticalCenter
            x: Math.round(parent.width * pacePct / 100) - (width / 2)
            width: 2
            height: parent.height + 5
            radius: 1
            color: (root && root.primaryTextColor) ? root.primaryTextColor(0.58) : "#ffffff"

            Behavior on x {
                SpringAnimation {
                    spring: 4.0
                    damping: 0.85
                    epsilon: 0.01
                }
            }
        }
    }

    Text {
        id: usedText

        anchors.left: parent.left
        anchors.right: resetText.left
        anchors.top: track.bottom
        anchors.rightMargin: 10
        anchors.topMargin: 5
        color: (root && root.primaryTextColor) ? root.primaryTextColor(0.92) : "#ffffff"
        elide: Text.ElideRight
        font.family: (root && root.uiFont) ? root.uiFont : ""
        font.pixelSize: 11
        font.weight: Font.Normal
        text: (root && root.metricUsedText) ? root.metricUsedText(modelData) : ""
    }

    Text {
        id: resetText

        anchors.right: parent.right
        anchors.top: track.bottom
        anchors.topMargin: 5
        color: (root && root.mutedTextColor) ? root.mutedTextColor(0.64) : "#888888"
        elide: Text.ElideRight
        font.family: (root && root.uiFont) ? root.uiFont : ""
        font.pixelSize: 11
        horizontalAlignment: Text.AlignRight
        text: (modelData && modelData.reset) ? modelData.reset : ""
    }

    Text {
        id: detailLeftText

        anchors.left: parent.left
        anchors.right: detailRightText.left
        anchors.top: usedText.bottom
        anchors.rightMargin: 10
        anchors.topMargin: 2
        color: (root && root.primaryTextColor) ? root.primaryTextColor(0.88) : "#ffffff"
        elide: Text.ElideRight
        font.family: (root && root.uiFont) ? root.uiFont : ""
        font.pixelSize: 11
        visible: row.hasSplitDetail
        text: row.detailLeft
    }

    Text {
        id: detailRightText

        anchors.right: parent.right
        anchors.top: usedText.bottom
        anchors.topMargin: 2
        color: (root && root.mutedTextColor) ? root.mutedTextColor(0.64) : "#888888"
        elide: Text.ElideRight
        font.family: (root && root.uiFont) ? root.uiFont : ""
        font.pixelSize: 11
        horizontalAlignment: Text.AlignRight
        visible: row.hasSplitDetail
        text: row.detailRight
    }

    Text {
        id: detailLineText

        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: row.hasSplitDetail ? detailLeftText.bottom : usedText.bottom
        anchors.topMargin: 2
        color: (root && root.mutedTextColor) ? root.mutedTextColor(0.64) : "#888888"
        elide: Text.ElideRight
        font.family: (root && root.uiFont) ? root.uiFont : ""
        font.pixelSize: 11
        visible: row.hasDetailLine
        text: row.detailText
    }
}
