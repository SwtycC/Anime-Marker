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
    property string variant: "normal"     // primary | normal | ghost
    property bool enabledState: enabled
    property bool hovered: mouseArea.containsMouse
    property bool pressed: mouseArea.pressed

    signal clicked()

    implicitWidth: label.implicitWidth + Theme.spacingLg * 2
    implicitHeight: 34
    radius: Theme.radiusSm
    // 禁用态：整体降透明度，避免"看起来能点却点不动"
    opacity: enabled ? 1.0 : 0.45

    readonly property bool _isPrimary: variant === "primary"
    readonly property bool _isGhost: variant === "ghost"

    // 拆成多个 readonly 绑定，而不是一个 JS 代码块。
    // 原因：`Behavior on color` 会接管该属性的绑定，若原先绑的是
    // `color: { if (...) return Theme.accent ... }` 这种代码块，
    // 主题色变化时不会重新求值（表现为"换主题色后主按钮仍是旧色"）。
    // 用纯三元表达式绑定可让依赖关系被正确追踪。
    readonly property color _primaryColor: root.pressed ? Theme.accentPressed
                                         : root.hovered ? Theme.accentHover
                                         : Theme.accent
    // 静止态的"无色"一律用 Theme.fade(同色)，不要用 "transparent"：
    // 后者是黑色透明，ColorAnimation 逐分量插值时会先扫过一段深色（见 Theme.fade）
    readonly property color _normalColor: root.pressed ? Theme.pressedFill
                                        : root.hovered ? Theme.hoverFill
                                        : Theme.fade(Theme.hoverFill)

    color: root._isPrimary ? _primaryColor
         : root._isGhost   ? (root.hovered ? Theme.hoverFill
                                           : Theme.fade(Theme.hoverFill))
         : _normalColor

    border.width: root._isGhost ? 0 : Theme.lineThin
    border.color: root._isPrimary ? Theme.fade(Theme.border) : Theme.border

    Behavior on color { ColorAnimation { duration: Theme.durFast } }
    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: root._isPrimary ? Theme.accentText : Theme.textPrimary
        font.pixelSize: Theme.fontMd
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
