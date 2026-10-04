import QtQuick
import QtQuick.Controls

// 线性按钮：1px 描边 + 透明/实色底，无阴影、无明显圆角（简约线性风格）。
//
// 三态：
//   primary —— 主题色实底 + 反色文字（主操作，如「保存」「添加」）
//   normal  —— 透明底 + 描边（次要操作，如「浏览…」「删除」）
//   ghost   —— 无描边，仅悬停有底色（用于图标按钮旁）
//
// 用法：
//   AppButton { text: "保存"; variant: "primary"; onClicked: ... }
Rectangle {
    id: root

    property string text: ""
    // primary | normal | ghost | danger
    //
    // danger：实底 + 红色，用于不可逆的确认按钮（如「删除」）。
    property string variant: "normal"
    property bool enabledState: enabled
    property bool hovered: mouseArea.containsMouse
    property bool pressed: mouseArea.pressed

    /// 紧凑模式：更小的内边距与高度，用于**塞进标题行/工具条**里的小按钮
    /// （如「下载器 · 保存位置」标题右侧的「重新扫描」）。
    ///
    /// 为什么做成开关而不是让调用方覆写 `implicitHeight`（踩坑）：
    /// 直接写 `implicitHeight: 24` 只改了外框，里面 `label` 的字号与
    /// 居中都不变 —— 字会几乎贴满边框，看着比原来还"挤"。
    /// 这里连内边距和字号一起缩，才是真正的"小一号"。
    property bool compact: false

    signal clicked()

    implicitWidth: label.implicitWidth
                   + (root.compact ? Theme.spacingMd : Theme.spacingLg) * 2
    implicitHeight: root.compact ? 24 : 34
    radius: Theme.radiusSm
    // 禁用态：整体降透明度，避免"看起来能点却点不动"
    opacity: enabled ? 1.0 : 0.45

    readonly property bool _isPrimary: variant === "primary"
    readonly property bool _isDanger: variant === "danger"
    readonly property bool _isGhost: variant === "ghost"

    // 拆成多个 readonly 绑定，而不是一个 JS 代码块。
    // 原因：`Behavior on color` 会接管该属性的绑定，若原先绑的是
    // `color: { if (...) return Theme.accent ... }` 这种代码块，
    // 主题色变化时不会重新求值（表现为"换主题色后主按钮仍是旧色"）。
    // 用纯三元表达式绑定可让依赖关系被正确追踪。
    readonly property color _primaryColor: root.pressed ? Theme.accentPressed
                                         : root.hovered ? Theme.accentHover
                                         : Theme.accent
    // 红色实底：按下更深、悬停更亮。
    // 亮度用 Qt.darker/lighter 从 dangerColor 派生，不再往主题表里塞
    // 三个"危险色"变量 —— 那种颜色只有确认按钮用得上。
    readonly property color _dangerColor: root.pressed
                                          ? Qt.darker(Theme.dangerColor, 1.15)
                                          : root.hovered
                                            ? Qt.lighter(Theme.dangerColor, 1.12)
                                            : Theme.dangerColor
    // 静止态的"无色"一律用 Theme.fade(同色)，不要用 "transparent"：
    // 后者是黑色透明，ColorAnimation 逐分量插值时会先扫过一段深色（见 Theme.fade）
    readonly property color _normalColor: root.pressed ? Theme.pressedFill
                                        : root.hovered ? Theme.hoverFill
                                        : Theme.fade(Theme.hoverFill)

    color: root._isPrimary ? _primaryColor
         : root._isDanger  ? _dangerColor
         : root._isGhost   ? (root.hovered ? Theme.hoverFill
                                           : Theme.fade(Theme.hoverFill))
         : _normalColor

    border.width: root._isGhost ? 0 : Theme.lineThin
    border.color: (root._isPrimary || root._isDanger)
                  ? Theme.fade(Theme.border) : Theme.border

    Behavior on color { ColorAnimation { duration: Theme.durFast } }
    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        // 实底按钮（主题色 / 红色）用反色文字，保证对比度
        color: (root._isPrimary || root._isDanger)
               ? Theme.accentText : Theme.textPrimary
        font.pixelSize: root.compact ? Theme.fontXs : Theme.fontMd
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        enabled: root.enabled
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
