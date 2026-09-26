import QtQuick
import QtQuick.Controls

// 线性输入框：1px 描边，聚焦时描边变主题色（不改变底色，保持"线性"观感）。
Rectangle {
    id: root

    property alias text: input.text
    property string placeholder: ""
    property bool echoPassword: false

    /// 只读：不可编辑、不可获得焦点。
    ///
    /// 视觉上同时做三件事（缺一用户就看不出"这栏不能改"）：
    ///   ① 底色用 `surfaceAlt`（比输入框浅一档）
    ///   ② 文字用 `textSecondary`（降级）
    ///   ③ 去掉聚焦描边效果（既然拿不到焦点）
    /// 光标形状也改回箭头，避免给出"可点"的错误暗示。
    property bool readOnly: false

    signal accepted()
    signal edited()

    implicitWidth: 200
    implicitHeight: 34
    radius: Theme.radiusSm
    color: root.readOnly ? Theme.surfaceAlt : Theme.surfaceBg
    border.width: Theme.lineThin
    border.color: root.readOnly
                  ? Theme.border
                  : (input.activeFocus ? Theme.accent : Theme.border)

    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

    TextInput {
        id: input
        anchors.fill: parent
        anchors.leftMargin: Theme.spacingMd
        anchors.rightMargin: Theme.spacingMd
        verticalAlignment: TextInput.AlignVCenter
        color: root.readOnly ? Theme.textSecondary : Theme.textPrimary
        font.pixelSize: Theme.fontMd
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText
        echoMode: root.echoPassword ? TextInput.Password : TextInput.Normal
        clip: true
        // readOnly 用 activeFocusOnPress 拦住"点一下就聚焦"；
        // 注意**不能用 enabled: false** —— 那样文字会整片变淡到几乎看不清，
        // 而这栏的内容（API 地址）恰恰是要让用户看清楚的。
        activeFocusOnPress: !root.readOnly
        readOnly: root.readOnly

        onAccepted: root.accepted()
        onTextChanged: root.edited()

        MouseArea {
            // 只读时把点击吃掉，否则事件会穿透到下面的 TextInput
            anchors.fill: parent
            enabled: root.readOnly
            cursorShape: Qt.ArrowCursor
        }

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
