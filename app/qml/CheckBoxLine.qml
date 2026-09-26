import QtQuick

// 开关（toggle switch）：胶囊轨道 + 圆形滑块，选中时整条轨道变主题色。
//
// 移植自 uiverse.io 的 `.theme-checkbox`（纯 CSS 实现：轨道用
// `linear-gradient(to right, 灰 50%, 深 50%)` + `background-size: 205%`，
// 靠 `background-position` 在 0 ↔ 100% 之间切换来"移动"轨道底色；
// 滑块是 `::before`，`left` 在 0.438em ↔ (100% - 2.25em - 0.438em) 之间动）。
//
// **为什么不用 QML 的 Switch**：QtQuick Controls 的 Switch 自带平台风格的
// 阴影与高光，与界面的"1px 线性"观感不搭；且它的尺寸/圆角不好压到这么小。
// 自绘能精确控制成"轨道 36×20、滑块 14、内边距 3"这套与字号匹配的比例。
//
// 与旧版（方形 + Canvas 打勾）的差别：**只保留开关形态**。
// 那个方框打勾在设置页里与"多选"的语义撞车（读者会以为可以多选），
// 而这里全部是"开/关"型配置，用开关更准确。
Item {
    id: root

    property string text: ""
    property bool checked: false

    signal toggled(bool checked)

    // ---- 尺寸（与 Theme.fontMd 的 14px 正文视觉重量匹配）----
    readonly property int trackW: 36
    readonly property int trackH: 20
    readonly property int thumbSize: 14
    readonly property int thumbPad: 3

    implicitWidth: trackW + (text === "" ? 0 : Theme.spacingMd + label.implicitWidth)
    implicitHeight: Math.max(trackH, label.implicitHeight)

    Rectangle {
        id: track
        anchors.verticalCenter: parent.verticalCenter
        width: root.trackW
        height: root.trackH
        radius: height / 2                    // 胶囊
        // 未选中：浅灰轨道（对应 CSS 的 #efefef 那半）
        // 选中：主题色（对应 CSS 切到 #2a2a2a 那半，这里换成主题色）
        color: root.checked ? Theme.accent
             : trackMouse.containsMouse ? Theme.hoverFillStrong
             : Theme.surfaceAlt
        border.width: Theme.lineThin
        border.color: root.checked ? Theme.accent : Theme.border

        Behavior on color { ColorAnimation { duration: Theme.durNormal } }
        Behavior on border.color { ColorAnimation { duration: Theme.durNormal } }

        // 滑块：左 ↔ 右。用 x 动画而非 left，避免与 anchors 混用。
        Rectangle {
            id: thumb
            width: root.thumbSize
            height: root.thumbSize
            radius: width / 2
            anchors.verticalCenter: parent.verticalCenter
            // 关闭时贴左内边距，打开时贴右内边距
            x: root.checked
               ? track.width - width - root.thumbPad
               : root.thumbPad
            // 滑块恒为白色：在浅灰轨道上靠 1px 描边区分，在主题色轨道上
            // 靠对比度区分 —— 比"跟着轨道变色"更稳（主题色可能很深/很浅）
            color: "#FFFFFF"
            border.width: Theme.lineThin
            border.color: root.checked ? Theme.fade(Theme.accent)
                                       : Theme.border

            Behavior on x { NumberAnimation { duration: Theme.durNormal; easing.type: Easing.OutCubic } }
            Behavior on border.color { ColorAnimation { duration: Theme.durNormal } }
        }
    }

    Text {
        id: label
        anchors.left: track.right
        anchors.leftMargin: Theme.spacingMd
        anchors.verticalCenter: parent.verticalCenter
        visible: root.text !== ""
        text: root.text
        color: Theme.textPrimary
        font.pixelSize: Theme.fontMd
    }

    MouseArea {
        id: trackMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            root.checked = !root.checked
            root.toggled(root.checked)
        }
    }
}
