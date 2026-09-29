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

    // 禁用态：整体降透明度 + 不再响应鼠标。
    //
    // 用途：**联动置灰** —— 如设置页「附加内容独立编号」在「附加内容显示」
    // 关闭时无效（外层关了，编号方式无从谈起），此时要把这一行灰掉，
    // 而不是留一个"点得动却没效果"的开关。
    //
    // 两个细节：
    //   ① 透明度加在组件**根项**上：本组件在 FormRow 里被拉伸到整行宽，
    //      但透明是逐像素的，视觉范围仍只有"轨道 + 文字"那块；
    //   ② MouseArea 要显式 `enabled: root.enabled` —— 只设 opacity 的话
    //      MouseArea 照旧吃事件、开关照样能拨（"看起来灰、点得动"）。
    opacity: enabled ? 1.0 : 0.45
    Behavior on opacity { NumberAnimation { duration: Theme.durFast } }

    signal toggled(bool checked)

    /// 触发一次"高亮闪烁"（供外部定位提示用，如设置页的 revealField）。
    ///
    /// **为什么把高亮画在组件内部**（踩坑）：本组件在 `FormRow` 里会被
    /// 拉伸到整行宽（400+），外部代码无论用 `width` / `implicitWidth` /
    /// `childrenRect` / `FontMetrics` 都很难稳定地算出"轨道 + 文字"那段
    /// 真实视觉范围（父级布局会把这些值一并拉宽）。而框在组件**内部**
    /// 就完全没有这个问题 —— 高亮矩形直接锚在 `track` 与 `label` 上，
    /// 天然贴合本体，且随主题/字号变化自动跟随。
    function flash() {
        flashAnim.restart()
    }

    /// 高亮边框：覆盖「轨道 + 文字」这段真实视觉范围。
    ///
    /// **不用 anchors 跨项锚定**（实测踩坑）：`anchors.left: track.left`
    /// 与 `anchors.right: label.right` 指向不同的兄弟项时，QML 无法推出
    /// 宽度（会退回 implicit），框最终只有轨道那么大。
    /// 这里直接用 `track` 与 `label` 的位置/宽度**算出**几何，
    /// 不依赖 anchors 的自动推导。
    Rectangle {
        id: flashBox
        x: track.x - flashPad
        y: -flashPad
        width: (label.visible ? label.x + label.width : track.x + track.width)
               - track.x + flashPad * 2
        height: root.height + flashPad * 2
        radius: Theme.radiusMd
        color: "transparent"
        border.color: Theme.accent
        border.width: Theme.lineThick
        opacity: 0
        visible: opacity > 0.01
        z: 1

        readonly property int flashPad: 8

        SequentialAnimation {
            id: flashAnim
            NumberAnimation {
                target: flashBox
                property: "opacity"
                to: 1
                duration: 120
            }
            PauseAnimation { duration: 1000 }
            NumberAnimation {
                target: flashBox
                property: "opacity"
                to: 0
                duration: 400
            }
        }
    }

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
        // 禁用时**必须显式关掉**（见根项 opacity 处的说明）：否则只是
        // "看起来灰"，鼠标移上去仍有悬停高亮、点击仍会翻转 checked。
        enabled: root.enabled
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            root.checked = !root.checked
            root.toggled(root.checked)
        }
    }
}
