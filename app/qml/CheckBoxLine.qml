import QtQuick

// 线性复选框：1px 描边方框，选中时填充主题色 + 白勾。
// 用 Canvas 画勾，避免引入额外资源文件。
Item {
    id: root

    property string text: ""
    property bool checked: false

    signal toggled(bool checked)

    implicitWidth: box.width + (text === "" ? 0 : Theme.spacingMd + label.implicitWidth)
    implicitHeight: Math.max(box.height, label.implicitHeight)

    Rectangle {
        id: box
        anchors.verticalCenter: parent.verticalCenter
        width: 18
        height: 18
        radius: Theme.radiusSm
        // 未勾选时用同色透明：勾选/取消时不再扫过一段深色（见 Theme.fade）
        color: root.checked ? Theme.accent : Theme.fade(Theme.accent)
        border.width: Theme.lineThin
        border.color: root.checked ? Theme.accent
                    : boxMouse.containsMouse ? Theme.borderStrong
                    : Theme.border

        Behavior on color { ColorAnimation { duration: Theme.durFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

        Canvas {
            id: tick
            anchors.fill: parent
            visible: root.checked
            opacity: root.checked ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Theme.durFast } }

            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = Theme.accentText
                ctx.lineWidth = 2
                ctx.lineCap = "round"
                ctx.lineJoin = "round"
                // 打勾：从左中 → 下中 → 右上（按控件尺寸等比）
                var w = width, h = height
                ctx.beginPath()
                ctx.moveTo(w * 0.26, h * 0.52)
                ctx.lineTo(w * 0.43, h * 0.70)
                ctx.lineTo(w * 0.76, h * 0.32)
                ctx.stroke()
            }

            // 主题色变化时重画（勾的颜色取自 accentText）
            Connections {
                target: Theme
                function onAccentSourceChanged() { tick.requestPaint() }
                function onDarkChanged() { tick.requestPaint() }
            }
        }
    }

    Text {
        id: label
        anchors.left: box.right
        anchors.leftMargin: Theme.spacingMd
        anchors.verticalCenter: parent.verticalCenter
        visible: root.text !== ""
        text: root.text
        color: Theme.textPrimary
        font.pixelSize: Theme.fontMd
    }

    MouseArea {
        id: boxMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            root.checked = !root.checked
            root.toggled(root.checked)
        }
    }
}
