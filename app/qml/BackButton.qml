import QtQuick
import QtQuick.Controls
import QtQuick.Effects

// 圆形返回按钮：双层圆环交叉淡入 + 缩放，中间箭头随悬停左移动效。
//
// 视觉移植自 uiverse.io by karthik092726122003 的 `.styled-wrapper .button`：
//   原版（CSS）                          → 本实现
//   :before  内环 border 3px black       → ringInner（细环，常态）
//   :after   外环 border 4px #599a53     → ringOuter（粗环，悬停态）
//   hover 内环 scale(0.7) + opacity 0    → ringInner 收缩并淡出
//   hover 外环 scale(1.3) → scale(1)     → ringOuter 从放大态归位并显现
//   .button-box translateX(-69px)        → arrowBox 悬停左移（箭头位移动效）
//   fill: #f0eeef（箭头色）              → Theme.accent（跟随主题色）
//
// **颜色一律取 Theme.accent**：原版的两个环色（黑 / #599a53）都换成主题色，
// 两环交叉时是"细环收缩、粗环张开归位"的层次变化，不会两个色块打架。
//
// 为什么不用 AppButton：那是胶囊矩形按钮（带底色的常规主按钮），而这是
// 纯描边的圆形图标按钮，形态与交互都不同，强行加参数会让 AppButton
// 变成"什么都能配"的组件。
Item {
    id: root

    /// 直径（原版 76px；这里按界面密度压到 40，与导航按钮同量级）
    property int size: 40
    /// 环线宽（原版 3 / 4）
    property real innerWidth: 2
    property real outerWidth: 2.5
    /// 箭头图标地址（由 Python 注入的 iconsBaseUrl 拼接，见 NavIcon）
    property string iconsBase: typeof iconsBaseUrl !== "undefined"
                               ? iconsBaseUrl : ""
    /// 图标直径占按钮直径的比例（原版 30/76 ≈ 0.39）
    property real iconRatio: 0.44
    /// 常态颜色（鼠标未移入时）。默认跟随正文色，悬停时统一变主题色。
    ///
    /// 亮色主题下 `Theme.textPrimary` 就是近黑（#1A1D23），暗色主题下是
    /// 近白 —— 用主题色令牌而不是写死 `#000000`，才能在两套主题里都保住
    /// 对比度（暗色界面上的纯黑圆环会看不见）。
    /// 需要"无论如何都是黑"时显式传 `restColor: "#000000"`。
    property color restColor: Theme.textPrimary

    /// 当前生效的颜色：悬停 → 主题色；否则 → restColor
    readonly property color _color: _hovered ? Theme.accent : restColor

    signal clicked()

    implicitWidth: size
    implicitHeight: size

    readonly property bool _hovered: mouse.containsMouse

    // ---- 内环（常态显示，悬停时收缩淡出）----
    Rectangle {
        id: ringInner
        anchors.centerIn: parent
        width: root.size
        height: root.size
        radius: width / 2
        color: "transparent"
        border.width: root.innerWidth
        border.color: root._color
        opacity: root._hovered ? 0 : 1
        // 悬停收缩到 0.7（原版 :hover:before）
        scale: root._hovered ? 0.7 : 1

        Behavior on border.color { ColorAnimation { duration: Theme.durNormal } }
        Behavior on opacity {
            NumberAnimation {
                duration: 400
                easing.type: Easing.OutQuart   // cubic-bezier(0.165,0.84,0.44,1)
            }
        }
        Behavior on scale {
            NumberAnimation {
                duration: 500
                easing.type: Easing.InOutCubic // cubic-bezier(0.25,0.46,0.45,0.94)
            }
        }
    }

    // ---- 外环（常态放大且透明，悬停时归位显现）----
    Rectangle {
        id: ringOuter
        anchors.centerIn: parent
        width: root.size
        height: root.size
        radius: width / 2
        color: "transparent"
        border.width: root.outerWidth
        border.color: root._color
        opacity: root._hovered ? 1 : 0
        // 常态 1.3 倍（原版 :after 的 scale(1.3)），悬停回到 1
        scale: root._hovered ? 1 : 1.3

        Behavior on border.color { ColorAnimation { duration: Theme.durNormal } }
        Behavior on opacity {
            NumberAnimation {
                duration: 400
                easing.type: Easing.OutQuart
            }
        }
        Behavior on scale {
            NumberAnimation {
                duration: 500
                easing.type: Easing.InOutCubic
            }
        }
    }

    // ---- 箭头：SVG 图标 + 染色（原版 .button-box 的位移动效在这里）----
    //
    // 渲染链与 NavIcon 一致：Image 作 source → MultiEffect 染色。
    // **SVG 的 fill 必须是白色**（踩坑记录见 NavIcon.qml）：colorization 的
    // 实际算法是 `结果色 = colorizationColor × 源亮度`，源是黑色时亮度为 0，
    // 无论传什么主题色染出来都是纯黑 —— 这正是"鼠标没移上去时箭头是黑色"
    // 的原因。arrow-left.svg 已把 fill 改成 #FFFFFF 当"白色蒙版"。
    Item {
        id: arrowBox
        anchors.centerIn: parent
        width: Math.round(root.size * root.iconRatio)
        height: width
        // 悬停左移：原版是整条 .button-box 平移（为无限滚动做的，
        // 位移量等于一个按钮宽）。这里只有一个图标，取一个"看得见但
        // 不跑出圆环"的小位移，表达"返回/后退"的方向感。
        //
        // **必须用 transform 而不是 `x:`**（实测踩坑）：本项用了
        // `anchors.centerIn: parent`，anchors 会**接管 x** —— 再写
        // `x: ... + (hovered ? -2 : 0)` 会被锚定值覆盖，位移完全不生效
        // （表现为"箭头没有进行位移"）。这与 PosterCard 悬停上浮
        // 用 Translate 而非 `y:` 是同一个原因。
        transform: Translate {
            x: root._hovered ? -2 : 0
            Behavior on x {
                NumberAnimation {
                    duration: 500
                    easing.type: Easing.InOutCubic
                }
            }
        }

        Image {
            id: arrowImage
            anchors.fill: parent
            visible: false                 // 仅作 MultiEffect 的 source
            source: root.iconsBase !== ""
                    ? root.iconsBase + "arrow-left.svg" : ""
            sourceSize.width: 64           // 放大解码，缩小后边缘更干净
            sourceSize.height: 64
            fillMode: Image.PreserveAspectFit
            asynchronous: false
        }

        MultiEffect {
            anchors.fill: parent
            visible: arrowImage.status === Image.Ready
            source: arrowImage
            colorization: 1.0
            // 常态 restColor（近黑）→ 悬停主题色。
            // 注意 MultiEffect 的 colorizationColor 是普通属性、不走
            // Behavior，所以这里是"跳变"而非渐变（与 PillActionButton
            // 里同样的取舍：本体描边在渐变，肉眼几乎察觉不到）。
            colorizationColor: root._color
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()

        ToolTip.visible: containsMouse
        ToolTip.delay: 600
        ToolTip.text: "返回"
    }
}
