import QtQuick
import QtQuick.Controls

// 线性输入框：1px 描边，聚焦时描边变主题色（不改变底色，保持"线性"观感）。
Rectangle {
    id: root

    property alias text: input.text
    property string placeholder: ""
    property bool echoPassword: false

    signal accepted()
    signal edited()

    implicitWidth: 200
    implicitHeight: 34
    radius: Theme.radiusSm
    color: Theme.surfaceBg
    border.width: Theme.lineThin
    border.color: input.activeFocus ? Theme.accent : Theme.border

    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

    TextInput {
        id: input
        anchors.fill: parent
        anchors.leftMargin: Theme.spacingMd
        anchors.rightMargin: Theme.spacingMd
        verticalAlignment: TextInput.AlignVCenter
        color: Theme.textPrimary
        font.pixelSize: Theme.fontMd
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText
        echoMode: root.echoPassword ? TextInput.Password : TextInput.Normal
        clip: true

        onAccepted: root.accepted()
        onTextChanged: root.edited()

        Text {
            anchors.fill: parent
            verticalAlignment: Text.AlignVCenter
            text: root.placeholder
            color: Theme.textTertiary
            font.pixelSize: Theme.fontMd
            visible: input.text.length === 0 && !input.activeFocus
        }
    }
}
