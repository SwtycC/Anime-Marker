import QtQuick
import QtQuick.Controls
import QtQuick.Effects

// 圆形图标操作按钮：白底 + 柔和投影 + 悬停浮起变色。
//
// 视觉移植自 uiverse.io by cssbuttons-io 的按钮，按本项目主题体系改写：
//   原版                          → 本实现
//   background-color: #fff        → Theme.surfaceBg（亮/暗主题自适应）
//   color: #000（文字）           → Theme.textPrimary（图标染色）
//   border-radius: 45px           → radius: height / 2（正圆）
//   box-shadow: 0 8px 15px ...    → 两层半透明圆角矩形模拟（见 _shadowPad）
//   hover: #23c483 + 上移 7px     → Theme.accent + 上移 2px
//   active: 上移 1px              → 上移 1px
//
// 原版按钮自带文字、尺寸很大（padding 1.3em 3em）。这里**只用图标**并把
// 直径压到与 tag chip 同高（24px），因为它要跟一排胶囊 tag 并排显示 ——
// 带文字的方块会把那一行撑变形。
//
// 为什么用多层矩形模拟投影而不是 MultiEffect：
// 见 NavBar.qml 的踩坑记录 —— `layer.enabled + MultiEffect(shadowEnabled)`
// 会渲染出实心白块，`MultiEffect { source: 某Item }` 在 PySide6 下会崩进程。
// 叠两层低透明度圆角矩形成本低、明暗主题下都稳定。
Item {
    id: root

    /// 无障碍标签 / 悬停提示文案（纯图标按钮没有可见文字）
    property string text: ""
    /// 强制交互态："" (跟随鼠标) / "hover" / "pressed"
    property string state: ""
    /// 图标（SVG 文件地址；为空则回落到 `glyph`）
    property string iconSource: ""
    /// 文字符号（如 ✔ / ×）：`iconSource` 为空时显示它。
    /// 用于"确定 / 取消"这类没有合适 SVG 的场景 —— 复用本组件的
    /// 圆形底 + 投影 + 悬停变色，视觉与图标按钮完全一致。
    property string glyph: ""
    property int iconSize: Math.round(root.bodyHeight * 0.5)
    /// 是否处于"语义危险"状态（如删除 / 取消）：悬停时用 dangerColor
    /// 而不是主题色，避免"随手点一下就退出编辑"被误当成主操作
    property bool danger: false
    property real hoverLift: 2        // 悬停上移像素（24px 圆不宜大动作）
    // 交互态：默认跟随鼠标；`state` 非空时强制指定（诊断截图 / 特殊场景用）
    property bool pressedState: state === "pressed"
                                || (state === "" && mouseArea.pressed)
    property bool hovered: state === "hover" || state === "pressed"
                           || (state === "" && mouseArea.containsMouse)

    signal clicked()

    // ---- 悬停 / 按下的派生配色 ----
    // 抽出来是因为底下有投影层、本体、图标三处都要用同一套色，
    // 各写一遍三元表达式容易改漏（且 MultiEffect 的颜色无法用 Behavior 动画）。
    readonly property color _hoverColor: root.danger ? Theme.dangerColor
                                                     : Theme.accent
    // 压在悬停底色上的文字 / 图标色（按底色亮度自动取黑或白）
    readonly property color _hoverFg: Theme.isLight(root._hoverColor)
                                      ? "#1A1A1A" : "#FFFFFF"

    // 投影留白：只在**底部**留，不在顶部留。
    //
    // **踩坑（与 tag 不在一条水平线上）**：最初上下各留 5px（总高 34 = 24+10），
    // 而 tag chip 高 24。`Flow` 是让子项**顶边对齐**，于是本体（从 y=5 起画）
    // 比同排的 chip 低了 5px，肉眼就是"按钮没对齐、掉下去了"。
    // 现在本体固定在 y=0，与 chip 顶边严格齐平；底部留出的空间供投影使用。
    //
    // 悬停上浮时本体会溢出到 y<0 —— Flow/Item 默认 `clip: false`，
    // 溢出的部分正常显示，不会被裁掉。
    readonly property int _shadowPad: 6

    /// 本体直径（正方形 + radius = 半高 → 正圆）。
    ///
    /// 默认取 **tag 的高度 24**，这样它在 Flow 里与周围的胶囊 tag
    /// **顶边对齐**（Flow 不支持垂直居中对齐，顶边对齐即同一水平线）；
    /// `bodyHeight` 可覆盖。
    property int bodyHeight: 24

    // **注意**：body 的尺寸必须由这里算出，不能让 body 自己 anchors.fill，
    // 也不能让本项去读 body 内部 Row 的 implicitWidth —— 那条依赖会成环
    // （实测表现为内容被裁、阴影盖住文字）。纯图标按钮只依赖固定值，无此问题。
    readonly property int _bodyWidth: root.bodyHeight

    implicitWidth: _bodyWidth
    implicitHeight: root.bodyHeight + _shadowPad

    // ---- 投影层（两层，越远越淡）----
    Repeater {
        model: 2

        Rectangle {
            required property int index
            anchors.horizontalCenter: body.horizontalCenter
            y: body.y + 5 + index * 2
            width: body.width
            height: body.height
            radius: height / 2
            color: root.hovered ? root._hoverColor : "#000000"
            opacity: root.hovered
                     ? (Theme.dark ? 0.30 : 0.22) / (index + 1)
                     : (Theme.dark ? 0.28 : 0.10) / (index + 1)
            z: -1 - index

            Behavior on opacity { NumberAnimation { duration: Theme.durNormal } }
            Behavior on color { ColorAnimation { duration: Theme.durNormal } }
        }
    }

    // ---- 按钮本体 ----
    Rectangle {
        id: body
        anchors.horizontalCenter: parent.horizontalCenter
        // 本体顶边固定在 y=0，与同排 tag chip 严格齐平（见 _shadowPad 的说明）。
        // 悬停上移 / 按下回落（原版 transform: translateY），向上溢出可显示。
        y: root.pressedState ? -1 : root.hovered ? -root.hoverLift : 0
        width: root._bodyWidth
        height: root.bodyHeight
        radius: height / 2                       // 圆形（正圆 = 半高）
        color: root.hovered ? root._hoverColor : Theme.surfaceBg
        border.width: Theme.lineThin
        border.color: root.hovered ? root._hoverColor : Theme.border

        // transition: all .3s ease（原版 300ms）
        Behavior on y { NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic } }
        Behavior on color { ColorAnimation { duration: Theme.durSlow } }
        Behavior on border.color { ColorAnimation { duration: Theme.durSlow } }

        // 纯图标（不再带文字标签）
        // 图标尺寸按本体比例给：24px 的圆里放 12px 图标留出呼吸感
        Image {
            id: btnIcon
            anchors.centerIn: parent
            visible: root.iconSource !== "" && status === Image.Ready
            source: root.iconSource
            sourceSize.width: 48
            sourceSize.height: 48
            width: root.iconSize
            height: root.iconSize
            fillMode: Image.PreserveAspectFit
            asynchronous: false
        }

        MultiEffect {
            anchors.fill: btnIcon
            visible: btnIcon.visible
            source: btnIcon
            colorization: 1.0
            // 注意：MultiEffect 的 colorizationColor 是普通属性、不走
            // Behavior，所以这里是"跳变"而非渐变。视觉上几乎察觉不到
            // （本体底色同时在渐变），不值得为它引入额外动画层。
            colorizationColor: root.hovered ? root._hoverFg : Theme.textPrimary
        }

        // 文字符号（iconSource 为空时使用，如 ✔ / ×）
        Text {
            anchors.centerIn: parent
            visible: root.iconSource === "" && root.glyph !== ""
            text: root.glyph
            color: root.hovered ? root._hoverFg : Theme.textPrimary
            font.pixelSize: Math.round(root.bodyHeight * 0.55)
            font.weight: Font.Medium

            Behavior on color { ColorAnimation { duration: Theme.durSlow } }
        }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: body
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()

        // 纯图标没有可见文字，用 ToolTip 承载语义
        // （绑定本地属性而非 root.text，避免 window 级单例的作用域问题）
        readonly property string tip: root.text
        ToolTip.text: tip
        ToolTip.delay: 600
        ToolTip.visible: tip !== "" && containsMouse
    }
}
