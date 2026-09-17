import QtQuick

// 区块标题：一条 1px 分隔线 + 标题文字。
// 用于设置页把表单分组（Bangumi / 路径 / 扫描 / qBittorrent / RSS / 界面）。
Item {
    id: root

    property string title: ""
    property string hint: ""

    implicitHeight: column.implicitHeight

    Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: Theme.spacingSm

        // 顶部 1px 分隔线（首个区块可设 lineVisible=false）
        property bool lineVisible: true

        Rectangle {
            width: parent.width
            height: Theme.lineThin
            color: Theme.border
            visible: column.lineVisible
        }

        Text {
            text: root.title
            color: Theme.textPrimary
            font.pixelSize: Theme.fontLg
            font.weight: Font.DemiBold
        }

        Text {
            visible: root.hint !== ""
            text: root.hint
            color: Theme.textTertiary
            font.pixelSize: Theme.fontSm
            wrapMode: Text.WordWrap
            width: parent.width
        }
    }
}
