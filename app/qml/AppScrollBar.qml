import QtQuick
import QtQuick.Controls

// 主题化滚动条。
//
// 为什么需要它：QtQuick Controls 的默认 ScrollBar 在深色界面里
// 会渲染出**浅灰轨道**（实测右边缘像素 #f0f0f0），视觉上是
// 「深色界面右侧贴了一条白边」。默认样式不受 `ApplicationWindow.color`
// 影响，必须显式覆盖 background / contentItem。
//
// 用法：
//     ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded }
ScrollBar {
    id: root

    // 外观参数（与 Theme 联动）
    readonly property int barWidth: 10
    readonly property int minHandle: 32
    readonly property bool horizontal_: orientation === Qt.Horizontal

    padding: 2
    policy: ScrollBar.AsNeeded

    // ---- 轨道：必须是「显式透明的 Rectangle」----
    // 写成 `background: Item {}` 不行 —— QtQuick Controls 的样式仍会
    // 给 ScrollBar 画一条浅灰轨道（实测右侧 10px 宽 #f3f3f3，就是 barWidth 的宽度）。
    // 必须用 Rectangle 且显式 color: "transparent" 才能覆盖掉样式默认绘制。
    background: Rectangle {
        implicitWidth: root.horizontal_ ? 40 : root.barWidth
        implicitHeight: root.horizontal_ ? root.barWidth : 40
        color: "transparent"
    }

    // ---- 滑块 ----
    contentItem: Rectangle {
        implicitWidth: root.horizontal_ ? 40 : root.barWidth
        implicitHeight: root.horizontal_ ? root.barWidth : 40
        radius: width / 2
        color: root.pressed
             ? Theme.accent
             : root.hovered
               ? (Theme.dark ? "#4A5464" : "#AFB7C4")
               : (Theme.dark ? "#3A4250" : "#C6CCD6")
        opacity: root.active || root.hovered ? 1.0 : 0.6

        Behavior on color { ColorAnimation { duration: Theme.durFast } }
        Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
    }
}
