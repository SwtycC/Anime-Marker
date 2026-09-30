import QtQuick
import QtQuick.Controls   // FontMetrics 在 QtQuick.Controls 下（Qt6 亦可来自 QtQuick）

// 右下角悬浮动作按钮（样式移植自 uiverse.io by vinodjangid07）。
//
// 由「返回顶部」按钮泛化而来：同一个组件通过 `icon` / `label` 复用，
// 避免第二、第三个按钮各抄一份动画代码（两份迟早漂移）。
//
// 原版 CSS 的行为拆解：
//   静止：50×50 圆形，实色底，`box-shadow: 0 0 0 4px rgba(180,160,255,.25)`
//         —— 一圈**不带偏移**的实心光环
//   悬停：宽度 50 → 104、圆角 50%（圆）→ 24（胶囊）；
//         图标 `translateY(-200%)` 向上飞出；
//         `::before` 的文字从 `font-size: 0` 长到可见、浮现出来
//   —— 即"圆钮横向展开成胶囊、文字从下方顶出、图标上飞让位"。
//
// 两个**必须注意**的实现点（都是踩过的坑）：
//   ① 圆角**不能用 `height/2`**：悬停时宽度变了，若圆角仍绑 height/2，
//      两端还是圆的、变不成胶囊。必须显式在 24 与 半高 之间切换。
//   ② 图标/文字的定位**不能用 `anchors.centerIn`**：anchors 的优先级高于
//      `x`/`y` 赋值，会让位移动画**永远不生效**（表现是"悬停后图标还留在
//      按钮里"）。这里一律用 x/y 手算居中，位移才动得起来。
Item {
    id: root

    /// 图标种类（透传给 NavIcon，如 "up" / "upBold" / "plus"）
    property string icon: "upBold"
    /// 悬停时展开显示的文案
    property string label: "返回顶部"

    signal clicked()

    // 尺寸：静止 50、悬停 104（窄胶囊）
    readonly property int collapsedSize: 50
    readonly property int expandedSize: 104
    /// 箭头/图标尺寸
    readonly property int iconSize: 20

    // ---- 展开 / 收回 的速度（**进出不对称**，用户需求）----
    //
    // 需求："鼠标离开按钮时，淡描边返回速度比按钮返回速度慢"。
    // 即：鼠标移出后，**按钮本体先缩回，外圈淡描边随后才慢慢收回去**，
    // 留下一点"余韵"。进入时仍要跟手（快了才不显得迟钝）。
    //
    // 实现：`Behavior` 只能给一个时长、**无法区分方向**，所以这里不用
    // Behavior，改成"显式动画 + 用 hovered 当开关"：
    //   hover 进入 → 走 inDur（快）
    //   hover 离开 → 本体走 outDur（较快）、描边走 haloOutDur（略慢）
    // 三条动画各自 `running` 绑定到对应方向，属性保持普通赋值。
    // **注**：注释里的毫秒数要与 Theme 里的真实取值一致 —— 这两处曾因
    // "凭记忆写注释"而对不上（Theme.durFast 实际是 120，注释却写 140），
    // 排查"动画时长不对"时被误导过。改 Theme 时记得同步这里。
    readonly property int inDur: Theme.durNormal              // = 200ms 进入
    readonly property int outDur: Theme.durFast               // = 120ms 本体收回
    // 描边收回：**仍比本体慢**（保留"本体先缩、描边随后收拢"的层次），
    // 但比初版的 400ms 快 —— 实测反馈"可以再快点"，400ms 的滞后感偏拖沓。
    // 取 240ms：比本体的 140ms 慢 100ms，看得出先后，又不拖泥带水。
    //
    // **踩坑**：这里一度被写成 200（= 与本体同速），于是"描边收回慢"的效果
    // 整个消失，但代码注释仍写着 240 —— 注释与值不符，看代码根本发现不了。
    // 那次是靠"读动画运行时 duration"才定位到的（运行时读到 200 ≠ 预期 240）。
    readonly property int haloOutDur: 240
    /// 按钮**外面那圈淡描边**的厚度（4px）。
    ///
    /// 它对应原版 CSS 的 `box-shadow: 0 0 0 4px rgba(...)` —— 一圈**不偏移**、
    /// 半透明的外扩光晕。QML 没有 box-shadow，做法是在按钮本体外面**再套
    /// 一个稍大的圆**（每侧大这么多），填半透明主题色，让本体盖住中间，
    /// 露出来的部分就是那圈描边。
    ///
    /// **它同时也是"留白"**：本组件的根项比按钮本体每侧都大这么多，
    /// 所以外部用 `anchors` 贴边时，算出来的间距会比"看到的间距"多 8px
    /// （两侧各 4）。需要在外部把这两份减掉，见 PosterWallPage 的说明。
    readonly property int haloWidth: 4

    readonly property bool hovered: mouse.containsMouse

    implicitWidth: expandedSize + haloWidth * 2
    implicitHeight: collapsedSize + haloWidth * 2

    // 描边的**两个端点尺寸**（显式动画的 from/to 用）。
    //
    // 与本体尺寸的关系恒定是"每侧多 haloWidth"：
    //   本体 50  → 描边 58（+8）
    //   本体 104 → 描边 112（+8）
    // 做成独立属性是为了让动画有**固定的端点值**，不再依赖任何"动画中的值"。
    readonly property int haloCollapsedW: collapsedSize + haloWidth * 2   // 58
    readonly property int haloExpandedW: expandedSize + haloWidth * 2     // 112
    readonly property int haloCollapsedH: collapsedSize + haloWidth * 2   // 58

    // 图标与文字的**端点位置**（显式动画的 from/to 用）。
    //
    // 图标：静止时在本体里居中；悬停时上飞到"本体半高 + 图标高"，
    //       保证整体越过顶边完全移出（见文件头关于"箭头留在按钮里"的说明）。
    // 文字：静止时压在中心下方 30px（被 clip 裁掉）；悬停时回到垂直居中。
    //
    // 都写成"相对本体高度"的算式，这样改 collapsedSize 时端点自动跟随。
    readonly property int iconRestY: Math.round((collapsedSize - iconSize) / 2)
    readonly property int iconFlownY: -(collapsedSize / 2 + iconSize)
    readonly property int captionShownY: Math.round(
        (collapsedSize - captionHeight) / 2)
    readonly property int captionHiddenY: captionShownY + 30
    // 文字实际高度（字号由 Theme 决定，这里测一次供上面算居中用）
    readonly property int captionHeight: captionMetrics.height
    FontMetrics {
        id: captionMetrics
        font.pixelSize: Theme.fontMd
        font.weight: Font.DemiBold
    }

    // ---- 按钮外那圈淡描边（对应 CSS 的 box-shadow 扩散环）----
    //
    // **踩坑（实测"完全看不到这圈"）**：这里原先写的是
    // `color: Theme.fade(Theme.accent)` —— 但 `Theme.fade()` 的定义是
    /// `Qt.rgba(r, g, b, 0)`，即 **alpha 恒为 0（完全透明）**。它的用途是
    // "颜色动画的起始态"（保持 RGB 不变、只让 alpha 从 0 涨上去，避免
    // 渐变色扫过脏色，见 Theme.fade 的说明），**不能拿来做可见的填充色**。
    // 结果就是这圈描边一直是全透明的，无论怎么调宽度/间距都看不见。
    //
    // 现在改为**显式给 alpha**：对齐原版 CSS 的 `rgba(180,160,255,.253)`
    // —— 约 25% 不透明度。用主题色而不是写死紫色（需求"颜色跟随主题色"），
    // 因此这一圈会随主题色一起变。
    Rectangle {
        id: halo
        // 锚到**根项**而不是 `body`：body 的宽度在动画中，`centerIn: body`
        // 会让描边的位置也跟着抖（虽然居中时水平方向看不出，但语义上仍是
        // "依赖动画中的值"）。根项尺寸固定，居中更稳。
        anchors.centerIn: parent
        // **尺寸直接由 hovered 推出，而不是跟着 `body.width` 算**。
        //
        // **踩坑（实测"描边进入慢半拍"）**：原先写的是
        //     width: body.width + root.haloWidth * 2
        // 即"描边尺寸 = 本体的**动画中**宽度 + 4"。本体的 `Behavior` 一跑，
        // 这个绑定每帧都重算，而描边自己的 `Behavior` 又被这个"一直在变的
        // 目标值"反复触发 —— 结果描边永远在追一个移动靶，起步天生滞后半拍
        // （即使两侧时长都设成 200ms，观感也不同步）。
        //
        // 现在改为**与本体同源**：本体宽 = hovered ? expanded : collapsed，
        // 描边直接用同一个表达式 + 两端留白。这样两侧从**同一时刻、同一目标**
        // 开始做动画，真正同步；进入/收回的速度差仍由各自的 Behavior 控制。
        width: root.haloExpandedW
        height: root.haloCollapsedH
        radius: width / 2
        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b,
                       root.hovered ? 0.35 : 0.25)

        // 描边尺寸：用**显式动画**驱动，而不是 `Behavior on width`。
        //
        // **为什么不用 Behavior**（踩坑，实测"收回速度设置不生效"）：
        // 原先 `width: (hovered ? expanded : collapsed) + haloWidth*2` 再配
        // `Behavior { duration: hovered ? inDur : haloOutDur }` —— 理论上收回
        // 该走 240ms，但实测本体与描边**同在 ~130ms 到位**，240ms 完全没生效。
        // 原因是这个 `Behavior` 的动画目标属性本身被写成了"依赖 hovered 的
        // 表达式"：hovered 一变，绑定立刻给 width 一个新值，Behavior 的介入
        // 与绑定求值的先后不稳定，时长参数也就不可靠了。
        //
        // 改用显式动画后时序完全确定：两个方向各一条，`running` 绑到对应
        // 方向，`from/to` 写死端点值。
        NumberAnimation {
            target: halo
            property: "width"
            running: root.hovered
            from: root.haloCollapsedW
            to: root.haloExpandedW
            duration: root.inDur
            easing.type: Easing.OutCubic
        }
        NumberAnimation {
            target: halo
            property: "width"
            running: !root.hovered
            from: root.haloExpandedW
            to: root.haloCollapsedW
            // **比本体慢**（140ms）：保留"本体先缩、描边随后收拢"的层次
            duration: root.haloOutDur
            easing.type: Easing.OutCubic
        }
        // 深浅也跟着（悬停稍深 0.35 / 静止 0.25），收回时同样慢一档
        ColorAnimation {
            target: halo
            property: "color"
            running: !root.hovered
            to: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.25)
            duration: root.haloOutDur
        }
        ColorAnimation {
            target: halo
            property: "color"
            running: root.hovered
            to: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.35)
            duration: root.inDur
        }
    }

    // ---- 按钮本体 ----
    Rectangle {
        id: body
        anchors.centerIn: parent
        height: root.collapsedSize
        width: root.hovered ? root.expandedSize : root.collapsedSize
        // 静止 = 正圆（半高）；悬停 = 24 圆角（胶囊端）。不能用 height/2，
        // 理由见文件头 ①。
        radius: root.hovered ? 24 : height / 2
        color: root.hovered ? Theme.accentHover : Theme.accent
        clip: true

        // 本体：**收回更快**（outDur），比描边先到位 —— 与需求一致。
        //
        // **同样改成显式动画**（原先是 `Behavior on width` + `duration:
        // hovered ? inDur : outDur`）。原因与描边那侧一样：`width` 的绑定
        // 依赖 `hovered`，而 `Behavior` 的 `duration` 也依赖 `hovered`，
        // 两者在同一帧求值的先后不稳，实测出现过"收回时长不生效、与描边
        // 完全同步"的现象（读运行时 duration 才确认）。
        // 显式动画把两个方向的时长彻底写死，不再有任何间接依赖。
        NumberAnimation {
            target: body
            property: "width"
            running: root.hovered
            from: root.collapsedSize
            to: root.expandedSize
            duration: root.inDur
            easing.type: Easing.OutCubic
        }
        NumberAnimation {
            target: body
            property: "width"
            running: !root.hovered
            from: root.expandedSize
            to: root.collapsedSize
            duration: root.outDur
            easing.type: Easing.OutCubic
        }
        NumberAnimation {
            target: body
            property: "radius"
            running: root.hovered
            from: root.collapsedSize / 2
            to: 24
            duration: root.inDur
            easing.type: Easing.OutCubic
        }
        NumberAnimation {
            target: body
            property: "radius"
            running: !root.hovered
            from: 24
            to: root.collapsedSize / 2
            duration: root.outDur
            easing.type: Easing.OutCubic
        }
        ColorAnimation {
            target: body
            property: "color"
            running: root.hovered
            to: Theme.accentHover
            duration: root.inDur
        }
        ColorAnimation {
            target: body
            property: "color"
            running: !root.hovered
            to: Theme.accent
            duration: root.outDur
        }

        // 图标：悬停时整体向上飞出（让位给文字）
        // 不用 anchors.centerIn —— 理由见文件头 ②。
        Item {
            id: iconHost
            width: root.iconSize
            height: root.iconSize
            x: Math.round((parent.width - width) / 2)
            // 飞到"本体半高 + 图标高"，保证整体越过顶边（与按钮高解耦）
            y: root.hovered
               ? -(root.collapsedSize / 2 + height)
               : Math.round((parent.height - height) / 2)

            // 与本体同步：进入 inDur、收回 outDur（比描边快）。
            // 同样用显式动画（不用 Behavior + hovered 三元时长，理由见本体处）。
            NumberAnimation {
                target: iconHost
                property: "y"
                running: root.hovered
                from: root.iconRestY
                to: root.iconFlownY
                duration: root.inDur
                easing.type: Easing.OutCubic
            }
            NumberAnimation {
                target: iconHost
                property: "y"
                running: !root.hovered
                from: root.iconFlownY
                to: root.iconRestY
                duration: root.outDur
                easing.type: Easing.OutCubic
            }

            NavIcon {
                anchors.fill: parent
                kind: root.icon
                color: Theme.accentText
            }
        }

        // 文字：悬停时从下方移入并显现
        // 同样不用 anchors.centerIn —— 理由见文件头 ②。
        Text {
            id: caption
            text: root.label
            color: Theme.accentText
            font.pixelSize: Theme.fontMd
            font.weight: Font.DemiBold
            opacity: root.hovered ? 1 : 0

            x: Math.round((parent.width - width) / 2)
            y: root.hovered
               ? Math.round((parent.height - height) / 2)
               : Math.round((parent.height - height) / 2) + 30

            // 同样改成显式动画（理由见本体处）
            NumberAnimation {
                target: caption
                property: "y"
                running: root.hovered
                from: root.captionHiddenY
                to: root.captionShownY
                duration: root.inDur
                easing.type: Easing.OutCubic
            }
            NumberAnimation {
                target: caption
                property: "y"
                running: !root.hovered
                from: root.captionShownY
                to: root.captionHiddenY
                duration: root.outDur
                easing.type: Easing.OutCubic
            }
            NumberAnimation {
                target: caption
                property: "opacity"
                running: root.hovered
                from: 0
                to: 1
                duration: root.inDur
            }
            NumberAnimation {
                target: caption
                property: "opacity"
                running: !root.hovered
                from: 1
                to: 0
                duration: root.outDur
            }
        }
    }

    // 命中区只覆盖**本体**（不含光环）：光环是纯装饰，
    // 若把它也算进命中区，两个按钮相邻时悬停区会互相重叠、
    // 出现"鼠标在一个按钮上、另一个也被判定为悬停"（用户明确要求避免）。
    MouseArea {
        id: mouse
        anchors.fill: body
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
