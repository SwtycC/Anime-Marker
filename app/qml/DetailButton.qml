import QtQuick
import QtQuick.Controls
import QtQuick.Effects

// 详情页顶部操作按钮：胶囊白底 + 柔和投影，悬停变主题色并拉开字距，
// 按下时下沉（投影收回）。
//
// 视觉移植自 uiverse.io by barisdogansutcu 的 button：
//   原版（CSS）                          → 本实现
//   border-radius: 50px                  → radius: height / 2（胶囊）
//   background-color: white              → Theme.surfaceBg（亮/暗自适应）
//   box-shadow: rgb(0 0 0 / 5%) 0 0 8px  → 下压两层低透明度圆角矩形
//   letter-spacing: 1.5px                → font.letterSpacing: 1.5
//   text-transform: uppercase            → 中文无大小写，忽略
//   hover: letter-spacing 3px            → font.letterSpacing: 3
//   hover: background hsl(261 80% 48%)   → Theme.accent
//   hover: color white                   → Theme.accentText（按亮度自动取）
//   hover: box-shadow rgb(93 24 220)     → 主题色投影（用 accent，不写死紫）
//   active: translateY(10px) + 无投影     → 下沉 2px + 投影淡出
//
// **为什么不扩展 AppButton**：那个是"线性风格"基础按钮，全项目 30+ 处
// 在引用（表单、对话框、订阅页…），改它会让整个界面按钮一起变形。
// 这里是详情页顶部的专属样式（也更"重"），独立成组件风险最低。
Rectangle {
    id: root

    property string text: ""
    /// 图标（SVG 地址）。非空且 text 为空时进入**纯图标模式**：
    /// 宽度收窄为与高度相近的胶囊，内容换成染色图标。
    /// 地址由调用方用注入的 iconsBaseUrl 拼好（与 NavIcon 同源）。
    property string iconSource: ""
    /// 悬停提示文案（纯图标模式下承载语义）。
    ///
    /// **必须在这里实现**：早期调用方在按钮上**再叠一层 MouseArea** 来
    /// 承载 ToolTip，结果那层 MouseArea 抢走了 hover 事件 —— 按钮内部的
    /// `mouse.containsMouse` 恒为 false，悬停变色/字距动画全部失效
    /// （实测反馈"鼠标悬停的重扫按钮上没动画效果"）。
    /// 由组件自己挂 ToolTip 就不会有这个问题：同一个 MouseArea 既判定
    /// hover 又提供提示。
    property string tooltip: ""
    /// 悬停/按下时的强调色（默认主题色；传 dangerColor 可做危险操作）
    property color accentColor: Theme.accent

    signal clicked()

    /// 纯图标模式（无文字、有图标）
    readonly property bool _iconOnly: text === "" && iconSource !== ""
    /// 图标高度占按钮高度的比例
    property real iconRatio: 0.5

    implicitWidth: root._iconOnly
                   ? implicitHeight + Theme.spacingSm      // 近正方形胶囊
                   : label.implicitWidth + Theme.spacingXl * 2
    implicitHeight: 36
    radius: height / 2
    opacity: enabled ? 1.0 : 0.45

    readonly property bool _hovered: mouse.containsMouse
    readonly property bool _pressed: mouse.pressed

    // 压在强调色上的文字色（按底色亮度自动取黑或白）
    readonly property color _onAccent: Theme.isLight(root.accentColor)
                                       ? "#1A1A1A" : "#FFFFFF"

    // ---- 背景 ----
    color: root._pressed || root._hovered ? root.accentColor : Theme.surfaceBg
    border.width: Theme.lineThin
    border.color: root._hovered ? root.accentColor : Theme.border

    // 动画节奏：原版 `transition: all .5s ease` 偏慢 → 收到 140ms
    // （两轮实测反馈"动画效果可以加快"，180ms 仍嫌慢）。
    // 140 略高于 durFast(120)：够快、又能看清"颜色推入"的过程。
    readonly property int animDur: 140

    Behavior on color { ColorAnimation { duration: root.animDur } }
    Behavior on border.color { ColorAnimation { duration: root.animDur } }

    // ---- 投影（两层，模拟 box-shadow）----
    //
    // 用两层低透明度圆角矩形而不是 MultiEffect：与 PillActionButton /
    // SearchPill 同样的取舍（见 NavBar.qml 的踩坑记录 —— PySide6 下
    // `MultiEffect { source: 某Item }` 会崩进程）。
    //
    // 常态：中性灰投影；悬停：染成强调色（对应原版 hover 的紫色光晕）；
    // 按下：淡出（原版 active 把 box-shadow 收到 0）。
    Repeater {
        model: 2

        Rectangle {
            required property int index
            anchors.horizontalCenter: body.horizontalCenter
            y: body.y + 3 + index * 3
            width: body.width
            height: body.height
            radius: body.radius
            color: root._hovered ? root.accentColor : "#000000"
            opacity: root._pressed ? 0
                     : root._hovered ? 0.26 / (index + 1)
                     : (Theme.dark ? 0.30 : 0.09) / (index + 1)
            z: -1 - index

            Behavior on opacity { NumberAnimation { duration: root.animDur } }
            Behavior on color { ColorAnimation { duration: root.animDur } }
        }
    }

    // ---- 本体（按下时下沉，投影留在原处 → 视觉上"按进去了"）----
    Rectangle {
        id: body
        anchors.fill: parent
        radius: parent.radius
        color: root.color
        border.width: root.border.width
        border.color: root.border.color

        // 原版 active 是 translateY(10px)，这里按按钮尺寸取 2px
        // （原版按钮高 ~50px，10px 是 1/5；这里 36px 取 2px 比例接近）
        y: root._pressed ? 2 : 0
        Behavior on y {
            NumberAnimation { duration: 70; easing.type: Easing.OutCubic }
        }

        Text {
            id: label
            anchors.centerIn: parent
            visible: !root._iconOnly
            text: root.text
            color: root._hovered || root._pressed
                   ? root._onAccent : Theme.textPrimary
            font.pixelSize: Theme.fontMd
            // letter-spacing：常态 1.5px、悬停 3px（原版 1.5 → 3）
            font.letterSpacing: root._hovered ? 3 : 1.5

            Behavior on color { ColorAnimation { duration: root.animDur } }
            // 字距动画：QML 的 real 属性可直接 Behavior
            Behavior on font.letterSpacing {
                NumberAnimation { duration: root.animDur }
            }
        }

        // ---- 纯图标模式：染色图标（渲染链同 NavIcon）----
        Item {
            id: iconBox
            anchors.centerIn: parent
            visible: root._iconOnly
            width: Math.round(root.height * root.iconRatio)
            height: width

            Image {
                id: btnIcon
                anchors.fill: parent
                visible: false                 // 仅作 MultiEffect 的 source
                source: root.iconSource
                sourceSize.width: 64           // 放大解码，缩小后边缘更干净
                sourceSize.height: 64
                fillMode: Image.PreserveAspectFit
                asynchronous: false
            }

            MultiEffect {
                anchors.fill: parent
                visible: btnIcon.status === Image.Ready
                source: btnIcon
                colorization: 1.0
                // 悬停/按下时用反色（压在主题色底上），常态用正文色。
                // MultiEffect 的该属性不走 Behavior（跳变），与本体底色
                // 的渐变同时发生，肉眼几乎察觉不到。
                colorizationColor: (root._hovered || root._pressed)
                                   ? root._onAccent : Theme.textPrimary
            }
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        enabled: root.enabled
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()

        // 提示直接挂在这个 MouseArea 上（不要再叠一层，理由见 tooltip 说明）
        ToolTip.visible: root.tooltip !== "" && containsMouse
        ToolTip.delay: 500
        ToolTip.text: root.tooltip
    }
}
