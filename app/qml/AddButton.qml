import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import QtQuick.Shapes

// 圆形「重新扫描」按钮：描边圆环 + 循环箭头图标，悬停时旋转一周。
//
// 视觉基底移植自 uiverse 的 Add New 按钮（`hover:rotate-90 duration-300`），
// 但图标从「加号」换成**循环箭头**并加大旋转量：
//   原版                                 → 本实现
//   外圈圆 stroke-width 1.5               → ring（ShapePath 画圆）
//   加号两条 stroke-width 1.5              → refresh.svg（循环箭头）
//   group-hover:fill-zinc-800             → 悬停淡入 Theme.accentSoft 填充
//   hover:rotate-90 duration-300          → rotation 0 → 360，durSlow 加长
//   颜色 stroke-zinc-400                  → 常态 textTertiary、悬停 accent
//
// **为什么旋转量从 90° 改成 360°**：旋转是"刷新/重扫"的通用隐喻，转满
// 一圈比转 90° 更像"重新跑一遍"；图标是循环箭头，转到任意角度都自洽
// （加号转 90° 才有意义，箭头不会）。
//
// 渲染链与 NavIcon 一致：Image(source) → MultiEffect(染色)。
// 图标 SVG 的描边必须是白色（见 resources/icons/README 的踩坑记录）。
Item {
    id: root

    property int size: 28
    /// 图标地址（由 Python 注入的 iconsBaseUrl 拼接）
    property string iconsBase: typeof iconsBaseUrl !== "undefined"
                               ? iconsBaseUrl : ""
    /// 常态色
    property color baseColor: Theme.textTertiary
    /// 持续旋转模式（作**进度指示器**用：扫描进行中在按钮旁转圈）。
    /// 开启时不可交互、不显示环内填充，只是匀速转。
    property bool spinning: false

    signal clicked()

    implicitWidth: size
    implicitHeight: size

    readonly property bool _hovered: mouse.containsMouse
    readonly property bool _pressed: mouse.pressed
    readonly property color _color: root.spinning ? Theme.accent
                                : (_pressed || _hovered) ? Theme.accent
                                : root.baseColor

    // 悬停旋转一周（原版 90°；本图标是循环箭头，转满一圈更贴合"重扫"语义）。
    // `spinning` 模式改用连续旋转动画覆盖（见下方 RotationAnimation）。
    // 时长收到 350ms（原 600ms —— 实测反馈"动画效果可以加快"）
    rotation: root.spinning ? 0 : (root._hovered ? 360 : 0)
    Behavior on rotation {
        enabled: !root.spinning
        NumberAnimation { duration: 350; easing.type: Easing.InOutCubic }
    }

    // 进度指示模式：匀速无限旋转（700ms 一圈，比原 1s 更"勤快"）
    RotationAnimation on rotation {
        running: root.spinning
        from: 0
        to: 360
        duration: 700
        loops: Animation.Infinite
    }

    // 环内填充：常态透明，悬停淡入（原版 group-hover:fill-zinc-800 的等价）。
    // 进度指示模式（spinning）不显示 —— 那是个状态图标，不是可点按钮。
    Rectangle {
        anchors.fill: parent
        radius: width / 2
        color: Theme.accentSoft
        opacity: (root._hovered && !root.spinning) ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
    }

    // 外圈圆环（描边式，与图标同色）
    Shape {
        anchors.fill: parent
        antialiasing: true        // 旋转时斜边不发毛

        ShapePath {
            strokeColor: root._color
            strokeWidth: 1.5
            fillColor: "transparent"
            startX: root.size / 2
            startY: 0.75
            PathAngleArc {
                centerX: root.size / 2
                centerY: root.size / 2
                radiusX: root.size / 2 - 0.75
                radiusY: root.size / 2 - 0.75
                startAngle: -90
                sweepAngle: 180
            }
            PathAngleArc {
                centerX: root.size / 2
                centerY: root.size / 2
                radiusX: root.size / 2 - 0.75
                radiusY: root.size / 2 - 0.75
                startAngle: 90
                sweepAngle: 180
            }
        }
    }

    // 循环箭头图标（染色）
    Item {
        id: iconBox
        anchors.centerIn: parent
        width: Math.round(root.size * 0.58)
        height: width

        Image {
            id: iconImage
            anchors.fill: parent
            visible: false                  // 仅作 MultiEffect 的 source
            source: root.iconsBase !== ""
                    ? root.iconsBase + "refresh.svg" : ""
            sourceSize.width: 64            // 放大解码，缩小后边缘更干净
            sourceSize.height: 64
            fillMode: Image.PreserveAspectFit
            asynchronous: false
        }

        MultiEffect {
            anchors.fill: parent
            visible: iconImage.status === Image.Ready
            source: iconImage
            colorization: 1.0
            // 与圆环同色（跳变，MultiEffect 的该属性不走 Behavior）
            colorizationColor: root._color
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        // 进度指示模式不响应交互（不拦截下层点击、不弹提示）
        enabled: root.enabled && !root.spinning
        onClicked: root.clicked()

        ToolTip.visible: containsMouse
        ToolTip.delay: 600
        ToolTip.text: "重新扫描该条目"
    }
}
