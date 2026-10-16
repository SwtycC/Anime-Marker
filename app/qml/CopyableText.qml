import QtQuick
import QtQuick.Controls

// 可复制的展示文本（详情页各文字用）。
//
// **为什么用 `TextEdit` 而不是 `Text`**（关键，实测查证）：
// Qt 6 的 `Text` **完全没有选择 / 复制能力**。实测枚举 `QQuickText`
// 的属性与方法，只有 `text` / `textFormat` / `baselineOffset` 这类
// 基础项 —— **既没有 `selectByMouse`、也没有 `textInteractionFlags`、
// `selectionStart`，连 `select()` 方法都不存在**。所以"文本可选中 /
// 可复制"只能用 `TextEdit`（或 TextArea）实现。
//
// **怎么让 `TextEdit` 看起来像静态文本**（否则会带出编辑器观感）：
//   - `readOnly: true`             不可编辑（但**仍可获得焦点**，见下）
//   - `cursorVisible: false`       不画竖线光标
//   - `selectByMouse: true`        鼠标划选（**这个属性 TextEdit 上有**）
//   - `selectionColor` 跟主题走     默认的蓝底在浅色主题下太跳
//   - 外层不画任何"聚焦描边"：本组件没有依赖 `activeFocus` 的视觉，
//     所以拿到焦点在观感上看不出来（这正是我们想要的）。
//
// **踩坑：`activeFocusOnPress: false` 会让 Ctrl+C 失效**（实测反馈
// "Ctrl+C（原生）无法复制"）：`Ctrl+C` 要作用于"当前有焦点的那个项"，
// 关掉它后键盘焦点始终在外面，按 Ctrl+C 没有任何东西响应。
// 所以这里**不关**（保持默认 `true`）。
//
// **`TextEdit` 没有 `elide` / `maximumLineCount` / `lineHeight`**
// （那是 `Text` 的附加属性，实测逐个探测确认）。超长文本靠 `clip`
// + 外层给的宽度裁切，换行由 `wrapMode` 控制。
//
// 对外接口与 `Text` 尽可能对齐（text / color / font / wrapMode…），
// 使用方改一行类型名即可，其余代码不动。
//
// 复制方式：**划选后按 Ctrl+C**（`TextEdit` 原生支持）。
//
// **别指望在它上面叠点击手势**：`TextEdit` 会在按下时拿到**独占 grab**，
// 于是任何被动 grab 的 `TapHandler` 都不会收到 `tapped()`（Qt 的有意设计，
// 详见 DetailPage 的 folderRow 那段实测说明）。要"点一下就触发某个动作"
// 的地方，用普通 `Text` + `MouseArea`，别用本组件。
Item {
    id: root

    // ---- 文本与外观 ----
    property alias text: editor.text
    property alias color: editor.color
    property alias font: editor.font
    property alias wrapMode: editor.wrapMode
    property alias textFormat: editor.textFormat
    property alias horizontalAlignment: editor.horizontalAlignment
    property alias verticalAlignment: editor.verticalAlignment
    property alias selectedTextColor: editor.selectedTextColor
    property alias selectionColor: editor.selectionColor

    /// 是否允许鼠标划选。
    property alias selectable: editor.selectByMouse

    /// 选中内容变化时发一次（使用方可据此做提示等）。
    signal selectionChanged(string selected)

    implicitWidth: editor.implicitWidth
    implicitHeight: editor.implicitHeight

    TextEdit {
        id: editor
        anchors.fill: parent

        readOnly: true
        // **不要写 `activeFocusOnPress: false`** —— 那会让 Ctrl+C 失效，
        // 原因见文件头。点一下就聚焦，观感上仍然像静态文本，因为
        // 组件不根据 activeFocus 画任何东西，且 cursorVisible 为 false。
        cursorVisible: false
        selectByMouse: true
        // 焦点移走后保留选中高亮：用户常"先选中、再去点复制按钮"，
        // 选中态消失会让人以为选丢了。
        persistentSelection: true

        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText

        // 默认不换行（超长靠外层宽度裁切）；使用方按需改成 Wrap
        wrapMode: TextEdit.NoWrap
        clip: true

        onSelectedTextChanged: root.selectionChanged(selectedText)
    }

    HoverHandler {
        id: hover
        // 悬停在可划选文字上时给 I 字形（与文本框一致的暗示）
        cursorShape: Qt.IBeamCursor
    }

    /// 选中整段（供外部调用，如快捷键）。
    function selectAll() {
        editor.selectAll()
    }
}
