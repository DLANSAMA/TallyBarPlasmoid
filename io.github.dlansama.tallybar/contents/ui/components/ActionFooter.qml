import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: actionFooter

    property var root

    Layout.fillWidth: true
    Layout.preferredHeight: root.actionFooterHeight()
    spacing: 0

    // Cross-provider "All AI" spend — the snapshot's per-provider cost totals summed
    // (the popup body is per-provider; this is the only all-providers figure). Shows
    // the 30-day total (so it adds up to each tab's "Last 30 days"), or month-to-date
    // vs budget when a monthly budget is set. Height mirrored in actionFooterHeight().
    Item {
        id: allAiRow

        property var totals: root.allAiTotals()
        property real budget: root.monthlyBudgetValue()

        Layout.fillWidth: true
        Layout.preferredHeight: visible ? 27 : 0
        visible: allAiRow.totals.has

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 1
            color: root.separatorColor(0.14)
        }

        RowLayout {
            anchors.fill: parent
            anchors.topMargin: 1
            spacing: 8

            Text {
                Layout.alignment: Qt.AlignVCenter
                color: root.mutedTextColor(0.72)
                font.family: root.uiFont
                font.pixelSize: 11
                font.weight: Font.DemiBold
                text: i18n("All AI")
            }

            Item { Layout.fillWidth: true }

            Text {
                Layout.alignment: Qt.AlignVCenter
                color: root.primaryTextColor(0.9)
                elide: Text.ElideRight
                font.family: root.uiFont
                font.pixelSize: 11
                horizontalAlignment: Text.AlignRight
                textFormat: Text.PlainText
                text: {
                    const t = allAiRow.totals;
                    // Default: the 30-day total (sums each provider's "Last 30 days", so
                    // it adds up to the tabs). With a monthly budget set, switch to
                    // month-to-date vs that budget — the window the budget alert tracks.
                    if (allAiRow.budget > 0) {
                        const pct = Math.round(t.mtd / allAiRow.budget * 100);
                        return i18n("%1 of %2 this month · %3%", root.compactUsd(t.mtd), root.compactUsd(allAiRow.budget), pct);
                    }
                    return i18n("%1 · last 30 days", root.compactUsd(t.m30));
                }
            }
        }
    }

    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 1
        color: root.separatorColor(0.14)
    }

    Item {
        id: quickActionStrip

        Layout.fillWidth: true
        Layout.preferredHeight: 44

        Row {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.verticalCenter: parent.verticalCenter
            height: 34
            spacing: 16

            Repeater {
                model: root.quickActionRows()

                delegate: Item {
                    id: quickAction

                    required property int index
                    required property var modelData

                    width: 38
                    height: parent.height
                    QQC2.ToolTip.delay: 450
                    QQC2.ToolTip.text: quickAction.modelData.label
                    QQC2.ToolTip.visible: quickActionMouse.containsMouse

                    Rectangle {
                        anchors.fill: parent
                        radius: 8
                        // macOS toolbar/Control-Center style: borderless at rest,
                        // a soft rounded highlight appears only on hover.
                        color: quickActionMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.12) : "transparent"
                        border.width: 0

                        Behavior on color {
                            ColorAnimation {
                                duration: 120
                                easing.type: Easing.OutCubic
                            }
                        }
                    }

                    Kirigami.Icon {
                        anchors.centerIn: parent
                        width: 16
                        height: 16
                        source: quickAction.modelData.icon
                        color: quickActionMouse.containsMouse ? root.accentColor() : root.primaryTextColor(0.78)
                    }

                    MouseArea {
                        id: quickActionMouse
                        Accessible.role: Accessible.Button
                        Accessible.name: modelData.label
                        Accessible.description: i18n("Execute quick action: %1", modelData.label)

                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        activeFocusOnTab: true
                        onClicked: {
                            root.triggerAction(quickAction.modelData);
                        }
                        Keys.onPressed: (event) => {
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                event.accepted = true;
                                root.triggerAction(quickAction.modelData);
                            }
                        }
                    }

                    // Visible keyboard-focus indicator, invisible unless quickActionMouse has focus.
                    Rectangle {
                        anchors.fill: parent
                        radius: 8
                        color: "transparent"
                        border.width: 2
                        border.color: root.accentColor()
                        visible: quickActionMouse.activeFocus
                    }
                }
            }
        }
    }

    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 1
        color: root.separatorColor(0.12)
    }

    Repeater {
        model: root.systemActionRows()

        delegate: Item {
            id: actionRow

            required property int index
            required property var modelData

            Layout.fillWidth: true
            Layout.preferredHeight: root.actionRowHeight(actionRow.modelData, actionRow.index)

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: 1
                visible: Boolean(actionRow.modelData.separator)
                color: root.separatorColor(0.14)
            }

            Rectangle {
                anchors.fill: parent
                anchors.leftMargin: -2
                anchors.rightMargin: -2
                radius: 5
                color: actionMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.12) : Qt.rgba(1, 1, 1, 0)
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 0
                anchors.rightMargin: 4
                spacing: 10

                Kirigami.Icon {
                    visible: String(actionRow.modelData.icon || "").length > 0
                    Layout.preferredWidth: visible ? 18 : 0
                    Layout.preferredHeight: 18
                    Layout.alignment: Qt.AlignVCenter
                    source: actionRow.modelData.icon
                    color: actionMouse.containsMouse ? root.accentColor() : root.primaryTextColor(0.84)
                }

                Text {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    color: root.primaryTextColor()
                    elide: Text.ElideRight
                    font.family: root.uiFont
                    font.pixelSize: 13
                    textFormat: Text.PlainText
                    text: actionRow.modelData.label
                }
            }

            MouseArea {
                id: actionMouse
                Accessible.role: Accessible.Button
                Accessible.name: actionRow.modelData.label
                Accessible.description: i18n("Activate %1", actionRow.modelData.label)

                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                activeFocusOnTab: true
                onClicked: (mouse) => {
                    root.triggerAction(actionRow.modelData);
                }
                Keys.onPressed: (event) => {
                    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                        event.accepted = true;
                        root.triggerAction(actionRow.modelData);
                    }
                }
            }

            // Visible keyboard-focus indicator, invisible unless actionMouse has focus.
            Rectangle {
                anchors.fill: parent
                anchors.leftMargin: -2
                anchors.rightMargin: -2
                radius: 5
                color: "transparent"
                border.width: 2
                border.color: root.accentColor()
                visible: actionMouse.activeFocus
            }
        }
    }
}
