import QtQuick
import QtQuick.Controls

// 阶段 1 占位页：仅用于验证路由、导航与主题，后续阶段将被真实页面替换。
Item {
    id: root

    property string title: "页面"
    property string subtitle: ""

    Column {
        anchors.centerIn: parent
        spacing: Theme.spacingMd

        // 页面标题
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: root.title
            color: Theme.textPrimary
            font.pixelSize: Theme.fontXl
            font.weight: Font.DemiBold
        }

        // 副标题
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            visible: root.subtitle !== ""
            text: root.subtitle
            color: Theme.textSecondary
            font.pixelSize: Theme.fontMd
        }

        // 说明卡片
        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 320
            height: 96
            radius: Theme.radiusMd
            color: Theme.surfaceAlt
            border.width: 1
            border.color: Theme.border

            Text {
                anchors.centerIn: parent
                width: parent.width - Theme.spacingLg * 2
                text: "阶段 1 骨架占位页\n后续阶段将替换为真实内容"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                lineHeight: 1.5
            }
        }
    }
}
