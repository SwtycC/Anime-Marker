import QtQuick
import QtQuick.Controls
import QtQuick.Effects

// 悬浮胶囊导航栏。
//
// 设计要点（对应需求）：
// - 悬浮在内容之上、固定在底部、不占据布局空间（由父级用 anchors 定位，不进 Layout）
// - 胶囊形：左右两端为半圆（radius = 高度/2）
// - 不透明实色背景 + 轻阴影，保证按钮清晰可读且与内容分层
// - 圆形按钮，水平居中排列，外边界为完整圆形
// - 悬停 / 按下 / 选中三态视觉反馈 + 平滑过渡动画
Item {
    id: root

    // 当前选中项索引
    property int currentIndex: 0
    // 外部 SVG 图标的目录基地址（见 NavIcon.iconsBase）
    property string iconsBase: typeof iconsBaseUrl !== "undefined"
                               ? iconsBaseUrl : ""
    // 按钮定义：[{ kind, label }]
    property var items: [
        { "kind": "grid", "label": "海报墙" },
        { "kind": "play", "label": "在看" },
        { "kind": "clock", "label": "动态" },
        { "kind": "rss", "label": "订阅" },
        { "kind": "gear", "label": "设置" }
    ]

    signal itemClicked(int index)

    // 尺寸必须显式设置：Item 不会自动采用 implicit 尺寸（那是给 Layout 用的），
    // 否则 width/height 为 0，内部 anchors.centerIn 会按 0 尺寸居中 → 胶囊被裁切。
    // 这里连同四周留白一起算，给阴影留出绘制空间。
    readonly property int _shadowPad: 12
    width: row.width + Theme.spacingMd * 2 + _shadowPad * 2
    height: Theme.navPillHeight + _shadowPad * 2

    // ---- 胶囊主体 ----
    Rectangle {
        id: pill
        anchors.centerIn: parent

        width: row.width + Theme.spacingMd * 2
        height: Theme.navPillHeight
        radius: height / 2                       // 两端半圆
        color: Theme.surfaceBg
        border.width: Theme.lineThin
        border.color: Theme.border

        Row {
            id: row
            anchors.centerIn: parent
            spacing: Theme.spacingSm

            Repeater {
                model: root.items

                delegate: Item {
                    id: btnHost
                    required property var modelData
                    required property int index

                    readonly property bool selected: root.currentIndex === index
                    readonly property bool hovered: hoverArea.containsMouse
                    // 把 label 落到本地只读属性：
                    // 附着的 ToolTip 是 window 级单例，直接绑 modelData.label 时
                    // 作用域不可靠，实测会显示成别的控件的 tooltip 内容
                    // （被 PosterCard / 状态栏的文字污染）。
                    readonly property string label: modelData && modelData.label
                                                    ? modelData.label : ""

                    width: Theme.navButtonSize
                    height: Theme.navButtonSize

                    // 选中态底色：强调色圆形，随索引切换平滑滑动由选中项自身动画承担
                    Rectangle {
                        anchors.fill: parent
                        radius: width / 2
                        // 静止态用同色透明：用 "transparent"（黑色透明）淡出时会扫过深灰
                        color: btnHost.selected ? Theme.accent
                             : btnHost.hovered  ? Theme.hoverFill
                             : Theme.fade(Theme.hoverFill)

                        // 选中 / 悬停切换时轻微缩放，增强反馈
                        scale: btnHost.selected ? 1.0
                             : btnHost.hovered  ? 1.06
                             : 1.0

                        Behavior on color { ColorAnimation { duration: Theme.durFast } }
                        Behavior on scale { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic } }
                    }

                    NavIcon {
                        anchors.centerIn: parent
                        width: Theme.navButtonSize * 0.55
                        height: width
                        kind: btnHost.modelData.kind
                        iconsBase: root.iconsBase
                        color: btnHost.selected ? Theme.accentText
                             : btnHost.hovered  ? Theme.textPrimary
                             : Theme.textSecondary

                        Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    }

                    MouseArea {
                        id: hoverArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.itemClicked(btnHost.index)

                        // 悬停提示：绑定本地 label 属性，避免单例作用域问题
                        ToolTip.text: btnHost.label
                        ToolTip.delay: 600
                        ToolTip.visible: hoverArea.containsMouse
                    }
                }
            }
        }
    }

    // ---- 阴影层 ----
    // 踩坑记录（两次都失败，最终改用 CSS 式「多层描边」模拟）：
    // ① 给 pill 开 `layer.enabled` + MultiEffect(shadowEnabled) 会在胶囊上方
    //    渲染出一块**不透明白矩形**（layer 源纹理按不透明处理，与阴影叠加后实心）。
    // ② `MultiEffect { source: 某Item }` 在 PySide6 下会直接进程崩溃
    //    （退出码 0xC0000409），无论源是否 visible。
    // 这里改为**叠两层低透明度圆角矩形**模拟柔和阴影：
    // 成本低、无 GPU 特效依赖、在明暗两种主题下都稳定。
    Repeater {
        model: 3

        Rectangle {
            required property int index
            anchors.centerIn: pill
            anchors.verticalCenterOffset: 2 + index
            width: pill.width + index * 2
            height: pill.height + index * 2
            radius: height / 2
            color: "#000000"
            opacity: (Theme.dark ? 0.18 : 0.05) / (index + 1)
            z: -1 - index
        }
    }
}
