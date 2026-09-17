import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Item {
    id: providerDock

    property var root

    Layout.fillWidth: true
    Layout.leftMargin: -5
    Layout.rightMargin: -5
    Layout.preferredHeight: 58

    Row {
        // Content-width (not full-width) so a capped inter-tab gap clusters few-tab
        // layouts centered instead of pinning them to opposite dock edges. At the 4-/5-tab
        // defaults the gap fills to the padded width, so centring reproduces the same
        // ~16px side margin the old left/right anchors gave — pixel-identical there.
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        spacing: root.switcherComputedGap(parent.width)

        Repeater {
            model: Math.max(1, root.switcherTabs().length)

            delegate: Item {
                id: providerTab

                required property int index
                property string providerKey: root.switcherTabs()[Math.min(index, root.switcherTabs().length - 1)]
                property bool selected: root.selectedProvider === providerKey
                property var tabProvider: root.telemetry && root.telemetry.providers ? root.telemetry.providers[providerKey] : ({
                })
                property real sessionPct: root.providerSessionPercent(providerKey)
                readonly property real tabContentWidth: Math.max(root.switcherTabWidth(providerKey), 54)

                width: root.switcherTabWidth(providerKey)
                height: parent.height

                // A provider whose live status is bad (api-error / timeout /
                // unauthorized / wallet-locked) while it may still show cached data —
                // surface the otherwise-invisible message on hover, plus the glyph below.
                QQC2.ToolTip.delay: 350
                QQC2.ToolTip.text: root.providerMuted(providerTab.providerKey)
                    ? i18n("Muted — alerts off")
                    : root.tabStatusMessage(providerTab.providerKey)
                QQC2.ToolTip.visible: tabMouse.containsMouse
                    && (root.tabStatusBad(providerTab.providerKey) || root.providerMuted(providerTab.providerKey))

                Rectangle {
                    id: tabSurface

                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    anchors.topMargin: 6
                    anchors.bottomMargin: 6
                    width: providerTab.tabContentWidth
                    radius: 9
                    color: providerTab.selected ? Qt.rgba(1, 1, 1, 0.18) : (tabMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent")
                    border.width: 1
                    border.color: providerTab.selected ? Qt.rgba(1, 1, 1, 0.12) : "transparent"

                    // Per-tab session-usage preview: faint track + a fill whose width is
                    // the provider's session percent, shown on every tab so usage is
                    // legible before selecting. Selected tab reads brightest.
                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.leftMargin: 7
                        anchors.rightMargin: 7
                        anchors.bottomMargin: 4
                        height: 2
                        radius: height / 2
                        color: root.neutralUsageColor(providerTab.selected ? 0.22 : 0.14)

                        Rectangle {
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: providerTab.sessionPct > 0 ? Math.max(parent.height, parent.width * providerTab.sessionPct / 100) : 0
                            radius: parent.radius
                            color: root.tabUsageColor(providerTab.providerKey, providerTab.sessionPct, providerTab.selected)
                            opacity: providerTab.selected ? 1.0 : 0.8

                            Behavior on width {
                                SpringAnimation {
                                    spring: 4.0
                                    damping: 0.85
                                    epsilon: 0.01
                                }
                            }
                        }
                    }

                    Behavior on color {
                        ColorAnimation {
                            duration: 140
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                Kirigami.Icon {
                    id: providerIcon

                    anchors.horizontalCenter: tabSurface.horizontalCenter
                    anchors.top: tabSurface.top
                    anchors.topMargin: 6
                    width: root.logoPixelSize(providerTab.providerKey, 16)
                    height: width
                    source: root.switcherIconSource(providerTab.providerKey, providerTab.selected)
                    // Recolor every provider glyph to the provider-name colour (white when
                    // selected, light grey otherwise) so the baked-in SVG tints never show.
                    isMask: true
                    color: providerTab.selected ? root.primaryTextColor(0.96) : root.mutedTextColor(tabMouse.containsMouse ? 0.82 : 0.68)
                    smooth: true
                }

                Text {
                    anchors.left: tabSurface.left
                    anchors.right: tabSurface.right
                    anchors.top: providerIcon.bottom
                    anchors.topMargin: 0
                    color: providerTab.selected ? root.primaryTextColor(0.96) : root.mutedTextColor(tabMouse.containsMouse ? 0.78 : 0.62)
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: root.providerTabFontSize(providerTab.providerKey)
                    font.weight: providerTab.selected ? Font.DemiBold : Font.Medium
                    horizontalAlignment: Text.AlignHCenter
                    textFormat: Text.PlainText
                    text: root.tabLabel(providerTab.providerKey)
                }

                // Small amber warning glyph on a bad-status tab.
                Kirigami.Icon {
                    anchors.right: tabSurface.right
                    anchors.top: tabSurface.top
                    anchors.rightMargin: 4
                    anchors.topMargin: 4
                    width: 11
                    height: 11
                    visible: root.tabStatusBad(providerTab.providerKey)
                    source: "data-warning"
                    isMask: true
                    color: "#e0a23c"
                }

                // A muted-provider tab shows a small mute glyph instead of the
                // bad-status warning (tabStatusBad is suppressed while muted).
                Kirigami.Icon {
                    anchors.right: tabSurface.right
                    anchors.top: tabSurface.top
                    anchors.rightMargin: 4
                    anchors.topMargin: 4
                    width: 11
                    height: 11
                    visible: root.providerMuted(providerTab.providerKey)
                    source: "audio-volume-muted"
                    isMask: true
                    color: root.mutedTextColor(0.7)
                }

                MouseArea {
                    id: tabMouse
                    Accessible.role: Accessible.Button
                    Accessible.name: i18n("%1 tab", root.tabLabel(providerTab.providerKey))
                    Accessible.description: i18n("Switch to the %1 provider tab", root.tabLabel(providerTab.providerKey))

                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    activeFocusOnTab: true
                    onClicked: (mouse) => {
                        root.costDrawerOpen = false;
                        root.providerRequested(providerTab.providerKey);
                    }
                    Keys.onPressed: (event) => {
                        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                            event.accepted = true;
                            root.costDrawerOpen = false;
                            root.providerRequested(providerTab.providerKey);
                        }
                    }
                }

                // Visible keyboard-focus indicator — matches tabSurface's rounding,
                // invisible unless tabMouse actually has keyboard focus.
                Rectangle {
                    anchors.fill: tabSurface
                    radius: tabSurface.radius
                    color: "transparent"
                    border.width: 2
                    border.color: root.accentColor()
                    visible: tabMouse.activeFocus
                }
            }
        }
    }
}
