import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: emptyStateSection

    property var root
    // Height of the VIEWPORT (the host's metricsScroller), passed in by the host. NOT
    // `parent.height`: the parent is the scroller's content column, whose height derives from
    // its children, so that collapses to the 120 floor and the message + action button bunch
    // up at the top instead of filling the body.
    property real availableHeight: 0

    Layout.fillWidth: true
    Layout.preferredHeight: Math.max(120, availableHeight)
    visible: root.providerLimits().length === 0 && !root.hasCostSection() && !root.hasExtraUsage()
    spacing: 12

    Text {
        id: emptyStateText

        // When the bad state carries an actionUrl, the message reads as a
        // link (accent colour + underline) and clicking it opens the sign-in page.
        readonly property string actionUrl: root.providerActionUrl()
        readonly property bool isLink: actionUrl.length > 0

        Layout.fillWidth: true
        Layout.fillHeight: true
        color: isLink ? root.accentColor() : root.mutedTextColor(0.68)
        font.family: root.uiFont
        font.pixelSize: 13
        font.underline: isLink && emptyStateLinkHover.hovered
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.WordWrap
        textFormat: Text.PlainText
        text: root.emptyStateMessage()

        Accessible.role: isLink ? Accessible.Button : Accessible.StaticText
        Accessible.name: text
        Accessible.description: isLink ? i18n("Open the sign-in page in your browser") : ""

        HoverHandler {
            id: emptyStateLinkHover
            enabled: emptyStateText.isLink
            cursorShape: Qt.PointingHandCursor
        }
        TapHandler {
            enabled: emptyStateText.isLink
            onTapped: root.openExternalUrlSafe(emptyStateText.actionUrl)
        }
    }

    Rectangle {
        id: unlockButton
        Layout.alignment: Qt.AlignHCenter
        Layout.preferredHeight: 32
        Layout.preferredWidth: unlockLabel.implicitWidth + 32
        Layout.bottomMargin: 8
        visible: root.emptyStateShowsUnlock()
        radius: 8
        color: unlockMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.18) : Qt.rgba(1, 1, 1, 0.10)
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, 0.16)

        Behavior on color {
            ColorAnimation { duration: 120; easing.type: Easing.OutCubic }
        }

        Text {
            id: unlockLabel
            anchors.centerIn: parent
            text: i18n("Unlock KWallet")
            color: root.primaryTextColor(0.92)
            font.family: root.uiFont
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }

        MouseArea {
            id: unlockMouse
            Accessible.role: Accessible.Button
            Accessible.name: i18n("Unlock KWallet")
            Accessible.description: i18n("Run a foreground refresh that prompts for the KWallet password")
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            hoverEnabled: true
            activeFocusOnTab: true
            onClicked: root.unlockWalletRequested()
            Keys.onPressed: (event) => {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                    event.accepted = true;
                    root.unlockWalletRequested();
                }
            }
        }

        // Visible keyboard-focus indicator, invisible unless unlockMouse has focus.
        Rectangle {
            anchors.fill: parent
            radius: unlockButton.radius
            color: "transparent"
            border.width: 2
            border.color: root.accentColor()
            visible: unlockMouse.activeFocus
        }
    }

    // Sign-in button: for missing-cookies / unauthorized, opens the
    // provider's login site in the browser so the user can land a fresh session
    // cookie, then refresh. Styled/positioned like the unlock button; the two are
    // mutually exclusive by status (never both visible).
    Rectangle {
        id: signInButton
        Layout.alignment: Qt.AlignHCenter
        Layout.preferredHeight: 32
        Layout.preferredWidth: signInLabel.implicitWidth + 32
        Layout.bottomMargin: 8
        visible: root.emptyStateShowsSignIn()
        radius: 8
        color: signInMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.18) : Qt.rgba(1, 1, 1, 0.10)
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, 0.16)

        Behavior on color {
            ColorAnimation { duration: 120; easing.type: Easing.OutCubic }
        }

        Text {
            id: signInLabel
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: i18n("Sign in to %1", root.providerLoginSite(root.selectedProvider))
            color: root.primaryTextColor(0.92)
            font.family: root.uiFont
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }

        MouseArea {
            id: signInMouse
            Accessible.role: Accessible.Button
            Accessible.name: i18n("Sign in to %1", root.providerLoginSite(root.selectedProvider))
            Accessible.description: i18n("Open the sign-in page in your browser, then refresh")
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            hoverEnabled: true
            activeFocusOnTab: true
            onClicked: Qt.openUrlExternally(root.providerLoginUrl(root.selectedProvider))
            Keys.onPressed: (event) => {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                    event.accepted = true;
                    Qt.openUrlExternally(root.providerLoginUrl(root.selectedProvider));
                }
            }
        }

        // Visible keyboard-focus indicator, invisible unless signInMouse has focus.
        Rectangle {
            anchors.fill: parent
            radius: signInButton.radius
            color: "transparent"
            border.width: 2
            border.color: root.accentColor()
            visible: signInMouse.activeFocus
        }
    }
}
