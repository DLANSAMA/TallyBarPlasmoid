import QtQuick
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

// The desktop style paints ToolTip.text in a Label and never sets textFormat
// (Qt Basic's Text doesn't either). There is no textFormat attached property,
// and "<img src>" in that string is fetched on hover.
// → tests/test_qml_text_format.py::test_dynamic_tooltips_are_plain_text
QQC2.ToolTip {
    id: tip

    contentItem: Item {
        implicitWidth: Math.min(label.maxTextLength, label.contentWidth)
        implicitHeight: label.implicitHeight

        Text {
            id: label

            readonly property real maxTextLength: Kirigami.Units.gridUnit * 14

            text: tip.text
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            font: tip.font
            // Kirigami.Theme throws in this applet, so use the tooltip palette role.
            color: tip.palette.toolTipText

            onLineLaidOut: (line) => {
                if (line.implicitWidth > label.maxTextLength)
                    line.width = label.maxTextLength
            }
        }
    }
}
