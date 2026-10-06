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

Item {
    id: root

    property string text: ""
    property bool checked: false

    /// 形态："switch"（默认，胶囊开关）/ "checkbox"（方形勾选框）。
    ///
    /// 为什么不做成两个组件：两者的**文字排版、禁用态、flash 高亮、
    /// toggled 信号**完全一样，只有"那个方块长什么样"不同 ——
    /// 拆成两个文件会让上面这几样各留一份、迟早漂移。
    property string variant: "switch"

    /// 是否方框形态（内部派生，别在外部写）。
    readonly property bool isCheckbox: variant === "checkbox"

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
    //
    // `switch` 形态：36×20 胶囊 + 14 滑块 + 3 内边距（原有尺寸，不动）。
    // `checkbox` 形态：18×18 方框（与正文 14px 的字高相称 —— 跟开关的
    //   高度接近，两种形态在列表里并排时基线一致，不会一高一矮）。
    readonly property int trackW: isCheckbox ? 18 : 36
    readonly property int trackH: isCheckbox ? 18 : 20
    readonly property int thumbSize: 14
    readonly property int thumbPad: 3
    //: 方框圆角（对应 CSS 的 --checkbox-border-radius: 5px，按尺寸缩放成 4）
    readonly property int boxRadius: 4

    implicitWidth: trackW + (text === "" ? 0 : Theme.spacingMd + label.implicitWidth)
    implicitHeight: Math.max(trackH, label.implicitHeight)

    // ---- 方框形态（variant === "checkbox"）----
    //
    // 造型移植自 uiverse.io 的 `.ui-checkbox`（Galahhad）：白底细边框的方框，
    // 勾选时填主题色 + 打勾**带一点回弹**地缩放出现（原 CSS 的
    // `cubic-bezier(0.12, 0.4, 0.29, 1.46)` 就是这种"过冲"曲线，QML 里
    // 对应 `Easing.OutBack`）。
    //
    // 同一时刻只显示这一套或下面那套开关（`visible` 互斥）——
    // 比"用属性把尺寸/圆角在两种形态间切来切去"稳：后者容易出现
    // 中间态（动画途中改圆角/尺寸会看到形变）。
    Item {
        id: box
        anchors.verticalCenter: parent.verticalCenter
        visible: root.isCheckbox
        width: root.trackW
        height: root.trackH

        Rectangle {
            id: boxBg
            anchors.fill: parent
            radius: root.boxRadius
            // 未选中：白底（跟随面板底色，深色主题下自动变深）
            // 悬停：描边先变主题色（对应 CSS 的 :hover）
            // 选中：**整体填主题色**（对应 CSS 的 :checked）
            color: root.checked ? Theme.accent : Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: root.checked ? Theme.accent
                        : (trackMouse.containsMouse ? Theme.accent
                                                    : Theme.border)

            Behavior on color { ColorAnimation { duration: Theme.durFast } }
            Behavior on border.color { ColorAnimation { duration: Theme.durFast } }
        }

        // 勾：两段线组成的 "✓"，勾选时从 0 缩放出现。
        //
        // **用 Canvas 而不是旋转的 Rectangle**（改过一版）：两条 Rectangle
        // 各自 `rotation` 再拼，拐点处要靠调 x/y 手对齐 —— 换个字号/尺寸
        // 就容易错位（第一版渲染出来勾偏左、两笔没接上）。
        // Canvas 直接按比例画两段折线，`lineCap: round` 天然有圆头，
        // 几何只跟画布尺寸相关、不依赖手工凑坐标。
        Canvas {
            id: checkMark
            anchors.centerIn: parent
            width: 12
            height: 12
            // 中心缩放（默认原点在左上角，会像"从角落长出来"）
            transformOrigin: Item.Center
            scale: root.checked ? 1.0 : 0.0
            opacity: root.checked ? 1.0 : 0.0

            // 颜色随主题变（浅色主题色 → 深勾，深色 → 白勾）
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = Theme.accentText
                ctx.lineWidth = 2
                ctx.lineCap = "round"
                ctx.lineJoin = "round"
                // 折线：左侧起笔 → 底部拐点 → 右上收笔（按画布比例）
                ctx.beginPath()
                ctx.moveTo(width * 0.22, height * 0.52)
                ctx.lineTo(width * 0.43, height * 0.73)
                ctx.lineTo(width * 0.80, height * 0.30)
                ctx.stroke()
            }
            // 主题色变了要重画（否则换了主题仍是旧勾色）
            Connections {
                target: Theme
                function onAccentTextChanged() { checkMark.requestPaint() }
            }

            // OutBack = CSS 那条带过冲的 cubic-bezier（"蹦"一下出现）
            Behavior on scale {
                NumberAnimation {
                    duration: Theme.durNormal
                    easing.type: Easing.OutBack
                    easing.overshoot: 2.0
                }
            }
            Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
        }
    }

    // ---- 开关形态（variant === "switch"，默认）----
    Rectangle {
        id: track
        anchors.verticalCenter: parent.verticalCenter
        visible: !root.isCheckbox
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
