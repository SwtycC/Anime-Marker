import QtQuick
import QtQuick.Controls

// 「添加动漫」小窗：选择一个**单个动漫目录**加入媒体库，可选是否匹配 Bangumi，
// 添加完成后自动进入该动漫的详情页。
//
// 流程：
//   点「浏览…」→ settingsBridge.pickDirectory() → 回填路径
//   点「添加」→ scanner.addFolder(path, 是否匹配)
//              → 后端单目录扫描（复用 ScanWorker，only_folder=该目录）
//              → 成功：scanner.subjectAdded(subjectId) → 本窗关闭 + 外部跳详情
//              → 失败：scanner.failed(msg) → 本窗内红字提示
//
// **为什么要在选目录时做"父目录"校验**（用户明确提出的诉求）：
// 用户可能直接选到 `F:/动漫` 这种"装着一堆番的根目录"。若照单全收，后端会
// 把它下面的所有番当成**一个**条目（关键词退化成"动漫"，匹配到毫不相干的
// 东西），完全违背"添加单个动漫"的用意。
// 因此这里在**前端**先做一次轻量提示（真正拦截在后端 addFolder，见那里
// 的说明）—— 后端也会拒绝，前端提示只是为了"不用等一个来回"。
//
// 「是否匹配」的默认值取决于有没有填 Token：
//   有 Token → 勾选（走正常匹配）
//   没 Token → **取消勾选且禁用**（用户要求"没填 Token 就不匹配"），
//              此时只把目录里的视频作为本地条目入库，状态为「已手动指定」
Window {
    id: dlg

    /// 当前选择的目录
    property string folder: ""
    /// 是否做 Bangumi 匹配（无 Token 时强制 false）
    property bool doMatch: true
    /// 是否已填 Token（决定 doMatch 能否勾选）
    property bool hasToken: false
    /// 提交中（禁用按钮、防连点）
    property bool busy: false
    /// 窗内提示（错误红字 / 进行中说明）
    property string status: ""
    property bool statusIsError: false

    /// 用户确认添加（携带目录与是否匹配，由 Main.qml 转发给后端）
    signal submitted(string folder, bool match)

    width: 560
    height: 300
    minimumWidth: 480
    minimumHeight: 280
    modality: Qt.ApplicationModal
    title: "添加动漫"
    color: Theme.windowBg
    flags: Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint

    /// 打开小窗（外部调用）
    function open() {
        dlg.folder = ""
        dlg.busy = false
        dlg.status = ""
        dlg.statusIsError = false
        // 有 Token 才默认勾选匹配（用户要求：没填 Token 就不匹配）。
        //
        // 必须调 `getValue`（**@Slot**）而不是 `value` —— 后者是普通 Python
        // 方法，QML 调不动（异常在下面这个三元里被吞掉，表现为 hasToken
        // 恒为 false，即"明明填了 Token 却被当成没填"）。见 SettingsBridge
        // 里 getValue 的说明。
        dlg.hasToken = (typeof settingsBridge !== "undefined" && settingsBridge)
                       ? String(settingsBridge.getValue("bangumi.token", "")).trim() !== ""
                       : false
        dlg.doMatch = dlg.hasToken
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
    }

    /// 后端回报的结果：成功/失败都由此收尾
    function onResult(ok, message) {
        dlg.busy = false
        if (!ok) {
            dlg.status = message
            dlg.statusIsError = true
        }
        // 成功时由外部立刻 close() 并跳详情页，这里不额外处理
    }

    Column {
        anchors.fill: parent
        anchors.margins: Theme.spacingXl
        spacing: Theme.spacingMd

        Text {
            width: parent.width
            text: "选择动漫文件夹"
            color: Theme.textPrimary
            font.pixelSize: Theme.fontLg
            font.weight: Font.DemiBold
        }

        Text {
            width: parent.width
            text: "请选中「某一部番自己的文件夹」（里面直接就是视频文件，"
                  + "或按季分子文件夹）。\n"
                  + "不要选媒体库根目录 —— 那会把下面所有番混成一条。"
            color: Theme.textSecondary
            font.pixelSize: Theme.fontSm
            lineHeight: 1.4
            wrapMode: Text.WordWrap
        }

        // ---- 路径行：输入框 + 浏览 ----
        Row {
            width: parent.width
            spacing: Theme.spacingSm

            AppTextField {
                id: pathField
                objectName: "addAnimePathField"
                width: parent.width - browseBtn.width - Theme.spacingSm
                text: dlg.folder
                placeholder: "点右侧「浏览…」选择动漫文件夹"
                onEdited: dlg.folder = pathField.text
            }

            AppButton {
                id: browseBtn
                objectName: "addAnimeBrowseBtn"
                text: "浏览…"
                onClicked: {
                    if (typeof settingsBridge === "undefined" || !settingsBridge)
                        return
                    var p = settingsBridge.pickDirectory(dlg.folder)
                    if (p) {
                        dlg.folder = p
                        dlg.status = ""
                        dlg.statusIsError = false
                    }
                }
            }
        }

        // ---- 是否匹配 ----
        //
        // **无 Token 时必须显示为"关"**（用户要求"没填 Token 就不匹配"）：
        // 早期只把 `enabled` 设成 false 而 `checked` 仍绑着 doMatch 的初值，
        // 视觉上开关停在"开"的位置、只是变灰 —— 用户会以为匹配是开启的。
        // 现在 checked 直接绑 `dlg.hasToken && dlg.doMatch`：无 Token 时
        // 恒为关，语义与行为一致。
        CheckBoxLine {
            id: matchBox
            objectName: "addAnimeMatchBox"
            checked: dlg.hasToken && dlg.doMatch
            enabled: dlg.hasToken
            text: dlg.hasToken
                  ? "加入后自动匹配 Bangumi（拉取封面、集数标题、tag）"
                  : "未填写 Access Token，只能加入本地（不匹配）"
            onToggled: function (checked) { dlg.doMatch = checked }
        }

        // ---- 提示行（错误红字 / 进行中） ----
        Text {
            id: statusText
            objectName: "addAnimeStatus"
            width: parent.width
            visible: dlg.status !== ""
            text: dlg.status
            color: dlg.statusIsError ? Theme.dangerColor : Theme.textSecondary
            font.pixelSize: Theme.fontSm
            wrapMode: Text.WordWrap
        }

        Item { width: 1; height: Theme.spacingSm }

        // ---- 操作 ----
        Row {
            anchors.right: parent.right
            spacing: Theme.spacingMd

            AppButton {
                objectName: "addAnimeCancelBtn"
                text: "取消"
                enabled: !dlg.busy
                onClicked: dlg.close()
            }

            AppButton {
                objectName: "addAnimeConfirmBtn"
                text: dlg.busy ? "添加中…" : "添加"
                variant: "primary"
                enabled: !dlg.busy && dlg.folder.trim() !== ""
                onClicked: {
                    if (dlg.folder.trim() === "")
                        return
                    dlg.busy = true
                    dlg.status = dlg.doMatch ? "正在匹配并加入…" : "正在加入…"
                    dlg.statusIsError = false
                    dlg.submitted(dlg.folder.trim(), dlg.doMatch)
                }
            }
        }
    }
}
