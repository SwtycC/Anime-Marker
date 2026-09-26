import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 表单行：左标签（固定宽度）+ 右侧内容。
//
// 用 labelWidth 统一各行标签宽度，保证输入框左边缘对齐
// （对应旧版 settings_page.py 的 ROW_INDENT 对齐需求）。
//
// 实现要点（踩坑）：
// - 必须用 RowLayout 而非 Row。Row 不会拉伸子项，且 Row 内的子项
//   一旦设置了 width 就会被 Row 的布局覆盖（Row 按 implicitWidth 排布），
//   导致里面的输入框宽度为 0、整行不可见。
// - 内容区用 `Layout.fillWidth: true` + Item 容器包裹 default 属性，
//   这样调用方写的 `AppTextField { width: parent.width }` 才能生效。
//
// **说明文字（`hint`）的呈现方式**：不再是标签下方的一行小字，而是标签
// 右侧的一个 `?` 圆钮，点开弹小窗显示 —— 与「全屏后等待」那一行同一套
// 交互（那边是显式手写的 `HelpButton`，这里把它内置进 `FormRow`）。
//
// 为什么改（实测）：小字挤在 132px 宽的标签列里得折行三四行，把整行撑高、
// 左侧一大片全是灰字，扫一眼分不出哪些是"标签"哪些是"补充说明"；
// 而真正的说明往往一句话说不完（如代理那条要解释"没填≠没走代理"、
// 以及分流规则的坑）。收进弹窗后：行高统一、界面干净，说明反而能写得更完整。
Item {
    id: root

    property string label: ""
    property string hint: ""
    property int labelWidth: 132
    property int rowSpacing: Theme.spacingLg

    /// `?` 钮被点击时**额外**要执行的动作（可选）。
    ///
    /// 默认行为是打开内置的 hint 弹窗；调用方若需要更丰富的内容
    /// （如 Access Token 那条要带一个可点击的"打开链接"按钮），
    /// 可以把自己的 Dialog 传进来 —— 此时**内置弹窗不再打开**。
    ///
    /// 为什么这样设计而不是让调用方自己画按钮：按钮的**位置**必须由
    /// FormRow 统一决定（锚在标签列右边缘），否则又会出现"某一行按钮
    /// 比别的行偏十几像素"的老问题（实测踩过三次）。
    property var helpAction: null

    default property alias contentChildren: contentHost.data

    implicitHeight: rowLayout.implicitHeight
    height: implicitHeight

    RowLayout {
        id: rowLayout
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: root.rowSpacing

        // ---- 标签列 ----
        // 关键：标签列必须同时限制 preferredWidth 与 maximumWidth，
        // 否则 RowLayout 会按"空间富余"把它撑开（表现为标签贴最左、
        // 输入框被挤到最右、中间一大片空白）。
        // 也不用 ColumnLayout —— 它在 RowLayout 里会与子项的 fillWidth 相互作用、
        // 反而把自身撑大；普通 Column 配合显式 width 更可控。
        // 标签列：文字靠左、`?` 钮**固定在最右侧**。
        //
        // **为什么用绝对定位而不是 Row**（踩坑）：Row 里 `?` 会紧跟在
        // 文字屁股后面，于是"代理"（两个字）与"动态显示条数"（六个字）
        // 的按钮 x 坐标完全不同 —— 一列扫下来按钮参差不齐（实测截图
        // 里正是这样）。这里改为：Text 锚左、HelpButton 锚右 &
        // **锚到整个标签列的右边缘**，于是所有行的 `?` 都在同一条
        // 垂直线上，与下方输入框的左边缘一起构成整齐的两列。
        Item {
            Layout.preferredWidth: root.labelWidth
            Layout.maximumWidth: root.labelWidth
            Layout.alignment: Qt.AlignTop
            implicitHeight: labelText.implicitHeight

            Text {
                id: labelText
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                // 给按钮留位置，避免长标签压到 `?` 上
                width: root.hint !== ""
                       ? parent.width - helpBtn.width - Theme.spacingXs
                       : parent.width
                text: root.label
                color: Theme.textSecondary
                font.pixelSize: Theme.fontMd
                elide: Text.ElideRight
            }

            // 有说明（或指定了自定义动作）才出现 —— 没有的行不留空位
            HelpButton {
                id: helpBtn
                visible: root.hint !== "" || root.helpAction !== null
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                tooltip: "查看说明"
                onClicked: {
                    // 指定了自定义 Dialog 就交给它，否则用内置弹窗
                    if (root.helpAction !== null)
                        root.helpAction.open()
                    else
                        hintDialog.open()
                }
            }
        }

        // ---- 内容列 ----
        // Item 承载调用方传入的子项；高度由子项决定
        Item {
            id: contentHost
            Layout.fillWidth: true
            Layout.minimumHeight: childrenRect.height
            implicitHeight: childrenRect.height
        }
    }

    // 说明弹窗。宽度留足以便长句自然折行，高度由内容撑开。
    //
    // **必须显式把 parent 挂到窗口**（踩坑）：`FormRow` 本身只有一行高
    // （`implicitHeight = rowLayout.implicitHeight`），若用
    // `anchors.centerIn: parent`，那个 `parent` 就是这一块行区域 ——
    // 弹窗会以"这一行"为中心，在整屏里看起来就偏到某一边（实测截图里
    // 它偏到了右下角）。挂到窗口后才真正居中。
    //
    // `Window.window` 是附加属性：向上找到该组件所属的 Window
    // （即 Main.qml 的主窗口）。理论上不会为 null，但为 null 时保持
    // 默认 parent（Popup 的常规行为），至少不会崩。
    Dialog {
        id: hintDialog
        modal: true
        parent: root.Window.window ? root.Window.window.contentItem : null
        anchors.centerIn: parent
        width: Math.min(520, root.Window.window ? root.Window.window.width - 80 : 520)
        padding: Theme.spacingXl
        title: root.label

        background: Rectangle {
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: Theme.border
            radius: Theme.radiusMd
        }

        contentItem: Column {
            width: parent.width
            spacing: Theme.spacingMd

            Text {
                width: parent.width
                text: root.hint
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
                lineHeight: 1.5
                wrapMode: Text.WordWrap
            }

            Row {
                anchors.right: parent.right

                AppButton {
                    text: "知道了"
                    variant: "primary"
                    onClicked: hintDialog.close()
                }
            }
        }
    }
}
