import QtQuick
import QtQuick.Controls

// 通用「确认」小窗（不可逆操作用）。
//
// 用法：
//     confirmDialog.ask(subjectId, "番名", "删除后……")
//     Connections { onConfirmed: function (id) { ... } }
//
// **为什么单独做一个而不是用 QtQuick.Controls 的 MessageDialog**：
// 本项目其余小窗（UploadDialog / AddAnimeDialog / PosterDialog）都是
// `Window` + 自绘背景，走的是同一套视觉（Theme.surfaceBg / radiusMd /
// 主题色按钮）。用系统风格的 MessageDialog 会在界面里"突出来"一块。
//
// 返回值走信号而不是 `ask()` 的返回 —— QML 的对话框是异步的，
// 同步返回"用户点了哪个"做不到（与 ScannerBridge.validate 拆两步同一个道理）。
Window {
    id: dlg

    /// 待确认对象的 id（由调用方解释，本组件只负责原样回传）
    property int targetId: 0
    /// 正文（说明后果）
    property string message: ""
    /// 标题
    property string caption: "确认操作"
    /// 确认按钮文案
    property string acceptText: "删除"
    /// 是否为危险操作（确认按钮用红色）
    property bool danger: true

    signal confirmed(int targetId)
    signal cancelled()

    // 高度：正文在上、按钮贴底，中间留出呼吸空间。
    // 220 够放两行正文 + 一行补充说明 + 按钮行（实测截图核对过）。
    width: 460
    height: 220
    minimumWidth: 380
    minimumHeight: 190
    modality: Qt.ApplicationModal
    title: caption
    color: Theme.windowBg
    flags: Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint

    /// 打开小窗（外部调用）
    function ask(id, text, msg) {
        dlg.targetId = id
        // 标题带上番名更容易认（用户可能开着小窗又切去看别的）
        dlg.caption = "确认删除"
        dlg.message = msg !== undefined && msg !== ""
                       ? msg
                       : ("确定要从媒体库中删除「" + (text || "该条目")
                          + "」吗？")
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
    }

    // ---- 正文（只占上方，高度由内容决定）----
    //
    // **不用 anchors.fill**（踩坑，实测截图）：fill 会把 Column 拉满整个
    // 窗口高度，而 Column 的 `spacing` 只在**子项之间**生效 —— 于是
    // 最下方那排按钮与正文之间被"撑"出很大一段空白，看着离得很远。
    Column {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Theme.spacingXl
        spacing: Theme.spacingMd

        Text {
            id: msgText
            width: parent.width
            text: dlg.message
            color: Theme.textPrimary
            font.pixelSize: Theme.fontMd
            lineHeight: 1.4
            wrapMode: Text.WordWrap
        }

        // 补充说明：磁盘文件不受影响。**必须说清**，否则用户会担心
        // "删了会不会把番剧文件也删掉"。
        Text {
            width: parent.width
            text: "只从媒体库移除记录，磁盘上的视频文件不会被删除。"
            color: Theme.textTertiary
            font.pixelSize: Theme.fontSm
            wrapMode: Text.WordWrap
        }
    }

    // ---- 操作按钮：**钉在小窗下沿**（用户诉求："靠近小窗的下边"）----
    //
    // 与正文**分开声明**、锚到小窗自身（而不是留在上面的 Column 里）：
    // 对话框的常规版式是"正文在上、按钮贴底"，这样不同长度的正文都不会
    // 让按钮的位置上下跳动，观感更整齐。
    //
    // 曾经把它放在 Column 里紧贴正文，问题是：
    //   - 正文短时按钮浮在中间，下面留一大片空（实测截图）；
    //   - 用 fill 撑开 Column 又会把间距全挤到按钮上方（更早那一版）。
    // 锚底就同时避开了这两个问题。
    Row {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Theme.spacingXl
        spacing: Theme.spacingMd

        AppButton {
            objectName: "confirmCancelBtn"
            text: "取消"
            onClicked: {
                dlg.close()
                dlg.cancelled()
            }
        }

        AppButton {
            objectName: "confirmAcceptBtn"
            text: dlg.acceptText
            variant: dlg.danger ? "danger" : "primary"
            onClicked: {
                var id = dlg.targetId
                dlg.close()
                dlg.confirmed(id)
            }
        }
    }
}
