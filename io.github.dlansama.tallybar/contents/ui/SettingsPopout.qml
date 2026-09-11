// Settings flyout — a separate floating glass window beside the widget (same
// macOS-submenu pattern as the cost popout). Extracted verbatim from
// FullRepresentation.qml; `root` is the FullRepresentation root (all the helper
// functions/colours/signals live there) and `anchorItem` is the invisible 1x1
// left-edge anchor KWin places this window against. Auto-closes on focus-out.
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.core as PlasmaCore

PlasmaCore.PopupPlasmaWindow {
    id: settingsPopout

    // The FullRepresentation root (helpers, colours, config state, signals) and the
    // invisible left-edge anchor this window is placed against — both injected by the
    // parent so the block below stays a verbatim copy of the original inline version.
    property var root
    property Item anchorItem

    // Item 5: free-entry monthly budget. The presets row stays; a "Custom…" chip reveals a
    // numeric field. budgetIsCustom is true when the saved value isn't one of the presets —
    // that case used to leave NO chip selected.
    readonly property var budgetPresets: [0, 50, 100, 250, 500]
    readonly property bool budgetIsCustom: root.monthlyBudgetValue() > 0
                                           && budgetPresets.indexOf(root.monthlyBudgetValue()) < 0
    property bool budgetEditing: false

    visualParent: anchorItem
    popupDirection: Qt.LeftEdge
    floating: true
    animated: true
    margin: root.drawerGap
    width: settingsCard.implicitWidth + leftPadding + rightPadding
    height: settingsCard.implicitHeight + topPadding + bottomPadding
    visible: root.settingsDrawerOpen

    onActiveChanged: {
        if (!active)
            root.settingsDrawerOpen = false;
    }

    mainItem: Item {
        id: settingsCard

        implicitWidth: root.settingsWidth
        // Content-sized (+ 14px top/bottom margins), CAPPED to the screen: a popout taller
        // than the screen makes PopupPlasmaWindow collapse to just the header (observed live
        // 2026-07-06 when the mute row pushed it over 1080p). Overflow scrolls instead.
        implicitHeight: Math.min(settingsColumn.implicitHeight + 28,
                                 (Screen.desktopAvailableHeight > 0 ? Screen.desktopAvailableHeight : 1000) - 48)
        focus: true

        Keys.onEscapePressed: (event) => {
            root.settingsDrawerOpen = false;
            event.accepted = true;
        }

        Flickable {
            id: settingsScroller

            anchors.fill: parent
            contentWidth: width
            contentHeight: settingsColumn.implicitHeight + 28
            interactive: contentHeight > height
            clip: true
            boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: settingsColumn

            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 14
            spacing: 11

            // While a config write is in flight, disable + dim every control so a click
            // can't be silently swallowed by writeConfig's `if (configSaving) return`
            // (correctness is preserved either way — no double-write — but the deleted
            // interval picker's "control is busy" affordance, enabled:!configSaving + a
            // dim, is restored here once for all the toggles rather than per-MouseArea).
            // Escape (settingsCard.Keys) and click-away (window onActiveChanged) still
            // dismiss the popout, so the close × being briefly inert is harmless.
            enabled: !root.configSaving
            opacity: root.configSaving ? 0.55 : 1.0

            // --- Header: title + saving/error status + close --------------------
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 22

                Text {
                    Layout.fillWidth: true
                    color: root.primaryTextColor()
                    font.family: root.displayFont
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                    text: i18n("Settings")
                }

                Text {
                    visible: root.configSaving
                    color: root.mutedTextColor(0.6)
                    font.family: root.uiFont
                    font.pixelSize: 11
                    text: i18n("Saving…")
                }

                Text {
                    visible: !root.configSaving && root.configError.length > 0
                    color: "#ff665d"
                    font.family: root.uiFont
                    font.pixelSize: 11
                    text: i18n("Save failed")
                }

                Text {
                    Layout.preferredWidth: 22
                    Layout.preferredHeight: 22
                    color: closeSettingsMouse.containsMouse ? root.primaryTextColor() : root.mutedTextColor(0.7)
                    font.family: root.uiFont
                    font.pixelSize: 21
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    text: "×"

                    MouseArea {
                        id: closeSettingsMouse
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: root.settingsDrawerOpen = false
                        Accessible.role: Accessible.Button
                        Accessible.name: i18n("Close settings")
                    }
                }
            }

            // --- Panel icon shows ----------------------------------------------
            Text {
                color: root.mutedTextColor(0.55)
                font.family: root.uiFont
                font.pixelSize: 10
                font.weight: Font.DemiBold
                text: i18n("PANEL ICON SHOWS")
            }

            Row {
                Layout.fillWidth: true
                spacing: 6

                Repeater {
                    model: [{
                        "m": "percent", "l": i18n("Bars")
                    }, {
                        "m": "reset", "l": i18n("Reset")
                    }, {
                        "m": "cost", "l": i18n("Cost")
                    }, {
                        "m": "pace", "l": i18n("Pace")
                    }]

                    delegate: Rectangle {
                        required property var modelData

                        property bool sel: root.panelModeValue() === modelData.m

                        width: (root.settingsWidth - 28 - 18) / 4
                        height: 30
                        radius: 7
                        color: sel ? root.accentTintColor(0.22) : (pmMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04))
                        border.width: sel ? 1 : 0
                        border.color: root.accentTintColor(0.4)

                        Text {
                            anchors.centerIn: parent
                            color: parent.sel ? root.primaryTextColor() : root.primaryTextColor(0.7)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            text: modelData.l
                        }

                        MouseArea {
                            id: pmMouse
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: root.configChangeRequested({
                                "panelDisplayMode": modelData.m
                            })
                            Accessible.role: Accessible.RadioButton
                            Accessible.name: i18n("Panel icon shows %1", modelData.l)
                            Accessible.checked: parent.sel
                        }
                    }
                }
            }

            // --- Refresh interval (moved here from the main footer) -------------
            Text {
                color: root.mutedTextColor(0.55)
                font.family: root.uiFont
                font.pixelSize: 10
                font.weight: Font.DemiBold
                text: i18n("REFRESH EVERY")
            }

            Row {
                Layout.fillWidth: true
                spacing: 5

                Repeater {
                    model: root.refreshIntervalOptions

                    delegate: Rectangle {
                        required property var modelData

                        property bool sel: Number(modelData) === root.effectiveRefreshInterval()

                        width: (root.settingsWidth - 28 - 20) / 5
                        height: 30
                        radius: 7
                        color: sel ? root.accentTintColor(0.22) : (riMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04))
                        border.width: sel ? 1 : 0
                        border.color: root.accentTintColor(0.4)

                        Text {
                            anchors.centerIn: parent
                            color: parent.sel ? root.primaryTextColor() : root.primaryTextColor(0.7)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            text: modelData + "m"
                        }

                        MouseArea {
                            id: riMouse
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: root.selectRefreshInterval(Number(modelData))
                            Accessible.role: Accessible.RadioButton
                            Accessible.name: i18n("Refresh every %1 minutes", modelData)
                            Accessible.checked: parent.sel
                        }
                    }
                }
            }

            // --- Provider visibility -------------------------------------------
            Text {
                color: root.mutedTextColor(0.55)
                font.family: root.uiFont
                font.pixelSize: 10
                font.weight: Font.DemiBold
                text: i18n("SHOW PROVIDERS")
            }

            Row {
                Layout.fillWidth: true
                spacing: 6

                Repeater {
                    model: root.allProviderKeys

                    delegate: Rectangle {
                        required property var modelData

                        property bool act: root.providerEnabled(modelData)

                        width: (root.settingsWidth - 28 - 18) / root.allProviderKeys.length
                        height: 28
                        radius: 7
                        color: act ? root.tabAccentTintColor(modelData, 0.24) : (provMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04))
                        border.width: act ? 1 : 0
                        border.color: root.tabAccentTintColor(modelData, 0.45)

                        Text {
                            anchors.centerIn: parent
                            color: parent.act ? root.primaryTextColor() : root.primaryTextColor(0.55)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            text: root.providerShortLabel(modelData)
                        }

                        MouseArea {
                            id: provMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            // The protected last-on provider reads as non-deselectable.
                            cursorShape: (parent.act && root.providersValue().length <= 1) ? Qt.ArrowCursor : Qt.PointingHandCursor
                            onClicked: root.toggleProvider(modelData)
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: i18n("Show %1", root.providerShortLabel(modelData))
                            Accessible.checked: parent.act
                        }
                    }
                }
            }

            // --- Mute alerts per provider (Feature 7) --------------------------
            Text {
                color: root.mutedTextColor(0.55)
                font.family: root.uiFont
                font.pixelSize: 10
                font.weight: Font.DemiBold
                text: i18n("MUTE ALERTS")
            }

            Row {
                Layout.fillWidth: true
                spacing: 6

                Repeater {
                    model: root.allProviderKeys

                    delegate: Rectangle {
                        required property var modelData

                        // Muted reads as the "active" (highlighted) chip here.
                        property bool act: root.providerMuted(modelData)

                        width: (root.settingsWidth - 28 - 18) / root.allProviderKeys.length
                        height: 28
                        radius: 7
                        color: act ? root.accentTintColor(0.24) : (muteMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04))
                        border.width: act ? 1 : 0
                        border.color: root.accentTintColor(0.45)

                        Text {
                            anchors.centerIn: parent
                            color: parent.act ? root.primaryTextColor() : root.primaryTextColor(0.55)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            text: root.providerShortLabel(modelData)
                        }

                        MouseArea {
                            id: muteMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.toggleMuted(modelData)
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: i18n("Mute %1 alerts", root.providerShortLabel(modelData))
                            Accessible.checked: parent.act
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 2
                Layout.preferredHeight: 1
                color: root.separatorColor(0.12)
            }

            // --- Notifications on/off + test ------------------------------------
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 24
                spacing: 10

                Text {
                    Layout.fillWidth: true
                    color: root.primaryTextColor(0.92)
                    font.family: root.uiFont
                    font.pixelSize: 13
                    text: i18n("Usage notifications")
                }

                Rectangle {
                    Layout.preferredWidth: 50
                    Layout.preferredHeight: 24
                    radius: 7
                    opacity: root.notificationsEnabledValue() ? 1.0 : 0.4
                    color: testMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.12) : Qt.rgba(1, 1, 1, 0.06)
                    border.width: 1
                    border.color: Qt.rgba(1, 1, 1, 0.1)

                    Text {
                        anchors.centerIn: parent
                        color: root.primaryTextColor(0.82)
                        font.family: root.uiFont
                        font.pixelSize: 11
                        text: i18n("Test")
                    }

                    MouseArea {
                        id: testMouse
                        anchors.fill: parent
                        enabled: root.notificationsEnabledValue()
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: root.testNotificationRequested()
                        Accessible.role: Accessible.Button
                        Accessible.name: i18n("Send a test notification")
                    }
                }

                Rectangle {
                    id: notifToggle

                    property bool on: root.notificationsEnabledValue()

                    width: 40
                    height: 22
                    radius: 11
                    color: notifToggle.on ? root.accentTintColor(0.55) : Qt.rgba(1, 1, 1, 0.12)

                    Behavior on color {
                        ColorAnimation { duration: 140 }
                    }

                    Rectangle {
                        width: 18
                        height: 18
                        radius: 9
                        y: 2
                        x: notifToggle.on ? parent.width - width - 2 : 2
                        color: "#f5f6f8"

                        Behavior on x {
                            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.configChangeRequested({
                            "notificationsEnabled": !notifToggle.on
                        })
                        Accessible.role: Accessible.CheckBox
                        Accessible.name: i18n("Usage notifications")
                        Accessible.checked: notifToggle.on
                    }
                }
            }

            // --- Alert thresholds ----------------------------------------------
            Text {
                color: root.mutedTextColor(0.55)
                font.family: root.uiFont
                font.pixelSize: 10
                font.weight: Font.DemiBold
                opacity: root.notificationsEnabledValue() ? 1.0 : 0.4
                text: i18n("ALERT WHEN USAGE PASSES")
            }

            Row {
                Layout.fillWidth: true
                spacing: 6
                opacity: root.notificationsEnabledValue() ? 1.0 : 0.4

                Repeater {
                    model: [80, 90, 95, 100]

                    delegate: Rectangle {
                        required property int modelData

                        property bool act: root.thresholdActive(modelData)

                        width: (root.settingsWidth - 28 - 18) / 4  // fill the row like the segmented controls above
                        height: 28
                        radius: 7
                        color: act ? root.accentTintColor(0.22) : (thMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04))
                        border.width: act ? 1 : 0
                        border.color: root.accentTintColor(0.4)

                        Text {
                            anchors.centerIn: parent
                            color: parent.act ? root.primaryTextColor() : root.primaryTextColor(0.6)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            text: modelData + "%"
                        }

                        MouseArea {
                            id: thMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            enabled: root.notificationsEnabledValue()
                            // The protected last-on threshold reads as non-deselectable.
                            cursorShape: (parent.act && root.thresholdsValue().length <= 1) ? Qt.ArrowCursor : Qt.PointingHandCursor
                            onClicked: root.toggleThreshold(modelData)
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: i18n("Alert at %1 percent usage", modelData)
                            Accessible.checked: parent.act
                        }
                    }
                }
            }

            // --- Monthly cost budget -------------------------------------------
            Text {
                color: root.mutedTextColor(0.55)
                font.family: root.uiFont
                font.pixelSize: 10
                font.weight: Font.DemiBold
                text: i18n("MONTHLY BUDGET")
            }

            Row {
                Layout.fillWidth: true
                spacing: 5

                Repeater {
                    model: [{ "v": 0, "l": i18n("Off") }, { "v": 50, "l": "$50" },
                            { "v": 100, "l": "$100" }, { "v": 250, "l": "$250" },
                            { "v": 500, "l": "$500" }]

                    delegate: Rectangle {
                        required property var modelData

                        property bool sel: root.monthlyBudgetValue() === modelData.v

                        width: (root.settingsWidth - 28 - 20) / 5
                        height: 30
                        radius: 7
                        color: sel ? root.accentTintColor(0.22) : (budgetMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04))
                        border.width: sel ? 1 : 0
                        border.color: root.accentTintColor(0.4)

                        Text {
                            anchors.centerIn: parent
                            color: parent.sel ? root.primaryTextColor() : root.primaryTextColor(0.7)
                            font.family: root.uiFont
                            font.pixelSize: 11
                            text: modelData.l
                        }

                        MouseArea {
                            id: budgetMouse
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: {
                                settingsPopout.budgetEditing = false;
                                root.configChangeRequested({ "monthlyBudget": modelData.v });
                            }
                            Accessible.role: Accessible.RadioButton
                            Accessible.name: modelData.v === 0 ? i18n("No monthly budget") : i18n("Monthly budget %1", modelData.l)
                            Accessible.checked: parent.sel
                        }
                    }
                }
            }

            // Item 5: Custom… chip + inline numeric field. The chip reads selected when a
            // non-preset value is saved (and shows it), fixing the "no chip selected" bug.
            Row {
                Layout.fillWidth: true
                spacing: 8

                Rectangle {
                    id: customBudgetChip

                    property bool sel: settingsPopout.budgetIsCustom

                    width: 84
                    height: 30
                    radius: 7
                    color: sel ? root.accentTintColor(0.22) : (customChipMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04))
                    border.width: (sel || settingsPopout.budgetEditing) ? 1 : 0
                    border.color: root.accentTintColor(0.4)

                    Text {
                        anchors.centerIn: parent
                        color: parent.sel ? root.primaryTextColor() : root.primaryTextColor(0.7)
                        font.family: root.uiFont
                        font.pixelSize: 11
                        text: settingsPopout.budgetIsCustom ? ("$" + root.monthlyBudgetValue()) : i18n("Custom…")
                    }

                    MouseArea {
                        id: customChipMouse
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: {
                            settingsPopout.budgetEditing = true;
                            budgetField.text = settingsPopout.budgetIsCustom ? String(root.monthlyBudgetValue()) : "";
                            budgetField.forceActiveFocus();
                        }
                        Accessible.role: Accessible.Button
                        Accessible.name: i18n("Set a custom monthly budget")
                    }
                }

                Rectangle {
                    // The editor: visible while editing or when a custom value is already set.
                    width: root.settingsWidth - 28 - 84 - 8
                    height: 30
                    radius: 7
                    visible: settingsPopout.budgetEditing || settingsPopout.budgetIsCustom
                    color: Qt.rgba(1, 1, 1, 0.06)
                    border.width: budgetField.activeFocus ? 1 : 0
                    border.color: root.accentTintColor(0.4)

                    Text {
                        id: dollarSign
                        anchors.left: parent.left
                        anchors.leftMargin: 9
                        anchors.verticalCenter: parent.verticalCenter
                        text: "$"
                        color: root.primaryTextColor(0.6)
                        font.family: root.uiFont
                        font.pixelSize: 12
                    }

                    TextInput {
                        id: budgetField
                        anchors.left: dollarSign.right
                        anchors.leftMargin: 3
                        anchors.right: parent.right
                        anchors.rightMargin: 9
                        anchors.verticalCenter: parent.verticalCenter
                        color: root.primaryTextColor()
                        font.family: root.uiFont
                        font.pixelSize: 12
                        selectByMouse: true
                        clip: true
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        validator: DoubleValidator { bottom: 0; top: 1000000; decimals: 2; notation: DoubleValidator.StandardNotation }
                        text: settingsPopout.budgetIsCustom ? String(root.monthlyBudgetValue()) : ""

                        function commit() {
                            // DoubleValidator accepts the locale group separator (e.g. "1,234")
                            // but JS parseFloat stops at the first non-digit/"." character, so
                            // strip group separators first — otherwise "1,234" silently saves as 1.
                            const v = parseFloat(budgetField.text.replace(/,/g, ""));
                            if (!isNaN(v) && v >= 0 && v <= 1000000)
                                root.configChangeRequested({ "monthlyBudget": v });
                            settingsPopout.budgetEditing = false;
                        }

                        onAccepted: budgetField.commit()
                        onActiveFocusChanged: {
                            if (!activeFocus && settingsPopout.budgetEditing)
                                budgetField.commit();
                        }
                    }
                }
            }
        }
        }
    }
}
