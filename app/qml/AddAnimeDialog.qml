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
    /// 「浏览…」对话框的起始目录（默认媒体库根，见 open()）
    property string pickStart: ""
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
        // 「浏览…」第一次打开时的**起始目录**：取设置里的媒体库根目录。
        //
        // **为什么要单独存一份**（踩坑）：
        // 早期直接把 `dlg.folder`（当前已选路径）当起始目录传下去 ——
        // 首次打开时它是空串，系统文件对话框就落在"上次访问的目录"
        // （实测反馈"第一次点浏览没有默认打开媒体库路径"）。
        // 用户选了值之后再点"浏览"，用刚选的路径当起点才是对的
        // （连续调整同一部番的路径时不用重新导航）。
        //
        // 媒体库路径可能配了多个（分号分隔），取**第一个**作起点。
        dlg.pickStart = (typeof settingsBridge !== "undefined" && settingsBridge)
                        ? String(settingsBridge.getValue("general.library_path", ""))
                              .split(";")[0].trim()
                        : ""
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

    // ---- 上方：标题 + 说明 + 路径 + 开关 + 状态 ----
    //
    // **不用 anchors.fill**（踩坑，与 ConfirmDialog 同一个问题）：
    // fill 会把 Column 拉满整个窗口高度，`spacing` 又只在子项之间生效，
    // 于是"状态行"到"按钮行"之间会被撑出一大段空白。
    // 现在正文区锚顶、按钮区锚底（见下方 Row），两者位置互不影响。
    Column {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
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
                    // 起始目录：**已选路径优先**（连续调整时不用重新导航），
                    // 首次打开则用媒体库根（见 open() 里的 pickStart 说明）
                    var start = dlg.folder !== "" ? dlg.folder : dlg.pickStart
                    var p = settingsBridge.pickDirectory(start)
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
        //
        // **用固定高度的容器兜住，而不是让 Text 自己 visible 切换**：
        // `visible: false` 会让该行高度**归零**，下面的内容整体上移一行 ——
        // 于是"添加前"与"添加中"两种状态下，开关的垂直位置差了一行
        // （实测两张截图对比很明显）。
        //
        // 这里外层 Item 恒定占一个行高（约 20px，够放一行 fontSm），
        // Text 在里面叠放；没有内容时只是不可见，**位置照旧**。
        Item {
            width: parent.width
            height: 20

            Text {
                id: statusText
                objectName: "addAnimeStatus"
                width: parent.width
                text: dlg.status
                visible: dlg.status !== ""
                color: dlg.statusIsError ? Theme.dangerColor
                                         : Theme.textSecondary
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
            }
        }
    }

    // ---- 操作按钮：**钉在小窗下沿** ----
    //
    // 与 ConfirmDialog 同一套版式（正文在上、按钮贴底）。这样：
    //   ① 状态行出现/消失时按钮**不会上下跳**（实测反馈"位置不一致"）；
    //   ② 正文长度变化（如错误信息折行）也不影响按钮位置。
    Row {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Theme.spacingXl
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
