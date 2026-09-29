import QtQuick
import QtQuick.Controls

// 红色警告弹层：用于"配置校验失败"这类需要用户立刻注意的错误。
//
// 为什么不用现有的浮条（Main.qml 的 banner）：
//   banner 是**窗口级**的，且颜色绑定在正常的主题色淡底上；而设置页的
//   校验失败（如 Token 无效）需要在**当前页面**立刻可见、且语义是"错误"
//   （红色），与 banner 的"提示/警告（琥珀）"两档分级是两回事。
//
// 位置固定在本组件所在页面底部居中（调用方用 anchors 摆放），
// 3 秒后自动淡出；点一下可立即关闭。
Rectangle {
    id: root

    /// 错误正文（显示后自动启动计时）
    property string text: ""

    /// 显示一条错误（自动重启计时）
    function show(msg) {
        root.text = msg
        root.opacity = 1
        hideTimer.restart()
    }

    /// 立即关闭
    function dismiss() {
        hideTimer.stop()
        root.opacity = 0
    }

    signal dismissed()

    implicitWidth: Math.min(560, msgText.implicitWidth + Theme.spacingXl * 2)
    implicitHeight: 40
    radius: Theme.radiusSm

    // 红色系：亮色主题用浅红底 + 深红字；暗色主题用暗红底 + 亮红字。
    // 与 banner 的琥珀色（warn）保持一致的"底色淡、文字实"层次。
    color: Theme.dark ? "#3A1A1D" : "#FDECEE"
    border.width: Theme.lineThin
    border.color: Theme.dark ? "#7A2E33" : "#F3AFB6"

    opacity: 0
    visible: opacity > 0.01
    z: 300

    Behavior on opacity { NumberAnimation { duration: Theme.durNormal } }

    Row {
        anchors.centerIn: parent
        spacing: Theme.spacingSm

        // 感叹号圆标（不引图标资源：这一类提示只此一处用）
        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: 16
            height: 16
            radius: width / 2
            color: Theme.dangerColor

            Text {
                anchors.centerIn: parent
                text: "!"
                color: "#FFFFFF"
                font.pixelSize: Theme.fontXs
                font.weight: Font.Bold
            }
        }

        Text {
            id: msgText
            anchors.verticalCenter: parent.verticalCenter
            text: root.text
            color: Theme.dark ? "#FF9AA2" : Theme.dangerColor
            font.pixelSize: Theme.fontMd
            elide: Text.ElideRight
            width: Math.min(implicitWidth, 480)
        }
    }

    Timer {
        id: hideTimer
        interval: 4000
        onTriggered: root.dismiss()
    }

    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.dismiss()
    }

    onOpacityChanged: {
        if (opacity < 0.01 && visible === false)
            root.dismissed()
    }
}
