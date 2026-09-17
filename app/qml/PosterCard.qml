import QtQuick
import QtQuick.Controls

// 海报卡片：固定尺寸、封面等比缩放居中、标题单行省略。
//
// 尺寸与旧版 PosterCard（app/ui/widgets.py）保持一致：宽 × 1.4 为封面区，
// 文本区固定 56px，确保迁移前后视觉一致。
Rectangle {
    id: root

    property int subjectId: 0
    property string title: ""
    property string meta: ""
    property string coverUrl: ""
    property string matchState: "auto"    // auto | manual | pending
    property int posterWidth: Theme.posterWidth

    signal clicked(int subjectId)

    readonly property int coverHeight: Math.round(posterWidth * Theme.posterRatio)
    readonly property bool _hovered: hoverArea.containsMouse

    width: posterWidth
    height: coverHeight + Theme.posterTextHeight

    // 卡片形状：**只有下方两个角是圆角，上方两角为直角**。
    //
    // 为什么这样设计（踩坑记录）：
    // 上方两角若做圆角，圆角处就必然露出卡片底色，深色海报上会出现
    // 刺眼的白色缺口（实测 Re:Zero 等深色封面尤其明显）。尝试过的三条路：
    //   ① MultiEffect + 圆角 maskSource —— 封面**整张吃掉**。
    //      三种写法（maskSource 为 visible:false / opacity:0 / 底层可见）
    //      全部失败：前两种整图消失，第三种遮罩反相只留角上一丝。
    //      根因是 layer 与 maskSource 在同帧内互相依赖，结果不可预期。
    //   ② 背景色补角覆盖 —— 几何正确，但固有色差无法消除（即上面的矛盾）。
    //   ③ 让封面溢出后被圆角容器裁 —— Qt 的 `clip` 只支持轴对齐矩形，
    //      纯 QML 无法裁圆角，需要 ShaderEffect 或额外模块。
    // 因此上方两角取直角（封面自然铺满），下方两角保留圆角，
    // 兼顾"无缺口"与"卡片不呆板"。
    topLeftRadius: 0
    topRightRadius: 0
    bottomLeftRadius: Theme.radiusMd
    bottomRightRadius: Theme.radiusMd
    color: Theme.surfaceBg
    border.width: Theme.lineThin
    border.color: _hovered ? Theme.accent : Theme.border

    // 开 clip 是为了让文本区/悬停高亮等子项在**下方圆角**处被裁掉，
    // 否则子项会盖住圆角、露出尖角。
    clip: true

    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

    // 悬停轻微上浮，增强"可点击"暗示。
    // 注意：不用 `y: hovered ? -3 : 0`，因为 y 由 Flow 布局掌管，
    // 直接改 y 会与布局打架；改用 transform 位移。
    transform: Translate {
        y: root._hovered ? -3 : 0
        Behavior on y { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic } }
    }

    Column {
        anchors.fill: parent
        spacing: 0

        // ---- 封面区 ----
        // 封面铺满卡片上半部分，不做任何圆角/裁切。
        //
        // 设计决定（踩坑记录）：曾尝试「上圆下方」的圆角封面，但存在
        // 一个无法回避的矛盾 —— 只要有圆角，圆角处就必然露出卡片底色，
        // 深色海报上会出现刺眼的白色缺口（实测 Re:Zero 等深色封面尤其明显）。
        //
        // 尝试过的三条路：
        //   ① MultiEffect + 圆角 maskSource —— 封面**整张消失**。
        //      三种写法（maskSource 为 visible:false / opacity:0 / 底层可见）
        //      全部失败：前两种整图被裁掉，第三种遮罩反相只留角上一丝。
        //      根因是 layer 与 maskSource 在同帧内互相依赖，结果不可预期。
        //   ② 背景色补角覆盖 —— 几何正确，但固有色差无法消除（即上面的矛盾）。
        //   ③ 让封面溢出后被圆角容器裁 —— Qt 的 `clip` 只能裁轴对齐矩形，
        //      纯 QML 无法裁圆角，需要 ShaderEffect 或额外模块。
        //
        // 最终选择直角：干净、无副作用，与「白色简约线性」的整体风格一致。
        Item {
            id: coverArea
            width: parent.width
            height: root.coverHeight

            // 占位底（无封面时显示）
            Rectangle {
                id: coverPlaceholder
                anchors.fill: parent
                color: Theme.surfaceAlt
            }

            Image {
                id: coverImage
                anchors.fill: parent
                source: root.coverUrl
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: true
                visible: status === Image.Ready && root.coverUrl !== ""
            }

            // 无封面占位：居中短横线（与旧版 _make_placeholder 视觉一致）
            Rectangle {
                anchors.centerIn: parent
                width: Math.round(parent.width * 0.4)
                height: Theme.lineThin
                color: Theme.borderStrong
                visible: !coverImage.visible
            }

            // 匹配状态标记
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.margins: Theme.spacingSm
                visible: root.matchState !== "auto"
                width: badgeLabel.implicitWidth + Theme.spacingMd
                height: 20
                radius: Theme.radiusSm
                color: root.matchState === "pending" ? Theme.warningColor : Theme.successColor

                Text {
                    id: badgeLabel
                    anchors.centerIn: parent
                    text: root.matchState === "pending" ? "待匹配" : "手动"
                    color: "#FFFFFF"
                    font.pixelSize: Theme.fontXs
                }
            }
        }

        // ---- 文本区 ----
        Column {
            width: parent.width
            height: root.height - root.coverHeight
            topPadding: 6
            leftPadding: 10
            rightPadding: 10
            spacing: 2

            Text {
                width: parent.width - parent.leftPadding - parent.rightPadding
                text: root.title
                color: Theme.textPrimary
                font.pixelSize: Theme.fontMd
                elide: Text.ElideRight
                maximumLineCount: 1
            }

            Text {
                width: parent.width - parent.leftPadding - parent.rightPadding
                text: root.meta
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                elide: Text.ElideRight
                maximumLineCount: 1
            }
        }
    }

    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked(root.subjectId)

        ToolTip.visible: containsMouse && root.title.length > 0
        ToolTip.delay: 600
        ToolTip.text: root.title
    }
}
