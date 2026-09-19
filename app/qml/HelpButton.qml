import QtQuick
import QtQuick.Controls

// 帮助按钮：圆形描边 + 居中感叹号，点击由调用方打开说明弹窗。
//
// 为什么自绘而不用 QtQuick Controls 的 Button：需要精确控制「圆圈 + 感叹号」
// 的视觉比例，且要和「线性」风格的其他控件保持一致（1px 描边、无阴影）。
//
// 尺寸：默认 20×20 的小圆（放在输入框行内不抢视线）。
// root 的 implicitWidth/Height 就是圆本身的直径，不额外留边距 ——
// 这样调用方用 `parent.width - helpBtn.width - spacing` 算输入框宽度时，
// 结果与视觉严格一致（早期版本 root 与圆同为 34，显得偏大且把输入框挤歪）。
Item {
    id: root

    property string tooltip: "查看说明"

    signal clicked()

    implicitWidth: 20
    implicitHeight: 20

    readonly property bool _hovered: mouse.containsMouse

    Rectangle {
        anchors.fill: parent
        radius: width / 2          // 正圆
        color: root._hovered ? Theme.hoverFill : Theme.fade(Theme.hoverFill)
        border.width: Theme.lineThin
        border.color: root._hovered ? Theme.accent : Theme.border

        Behavior on color { ColorAnimation { duration: Theme.durFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

        // 感叹号：上竖条 + 下圆点，比文字 "!" 的视觉居中更好控制
        Column {
            anchors.centerIn: parent
            spacing: 2

            Rectangle {
                anchors.horizontalCenter: parent.horizontalCenter
                width: 1.5
                height: 7
                radius: 0.75
                color: root._hovered ? Theme.accent : Theme.textSecondary
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
            }

            Rectangle {
                anchors.horizontalCenter: parent.horizontalCenter
                width: 2
                height: 2
                radius: 1
                color: root._hovered ? Theme.accent : Theme.textSecondary
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
            }
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()

        ToolTip.visible: containsMouse && root.tooltip !== ""
        ToolTip.delay: 600
        ToolTip.text: root.tooltip
    }
}
