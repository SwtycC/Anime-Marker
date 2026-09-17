import QtQuick
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
Item {
    id: root

    property string label: ""
    property string hint: ""
    property int labelWidth: 132
    property int rowSpacing: Theme.spacingLg

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
        Column {
            Layout.preferredWidth: root.labelWidth
            Layout.maximumWidth: root.labelWidth
            Layout.alignment: Qt.AlignTop
            spacing: 2

            Text {
                width: root.labelWidth
                text: root.label
                color: Theme.textSecondary
                font.pixelSize: Theme.fontMd
                elide: Text.ElideRight
            }

            Text {
                width: root.labelWidth
                visible: root.hint !== ""
                text: root.hint
                color: Theme.textTertiary
                font.pixelSize: Theme.fontXs
                wrapMode: Text.WordWrap
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
}
