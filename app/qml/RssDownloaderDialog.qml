import QtQuick
import QtQuick.Controls

// 「下载器」设置弹窗：左侧设置（标题过滤 + 保存位置），右侧**实时预览**。
//
// 需求：
//   「添加必须包含，必须不包含」
//   「如果目录，新建条目或者选择条目，就在媒体库目录下搜寻或新建目录」
//   「全部下载 = 获取订阅源中所有，再经过滤，下载」
//   「下载器必须点保存才能启用这个订阅的下载，相当于一个『启用』开关」
//   「下载器右侧添加预览，可以查看过滤后的下载的剧集」
//   「可以自动预览吗，填完过滤词实时出现」
//
// **预览为什么是异步 + 防抖**：预览要真抓一次 RSS 才能算出「本地已有的
// 会被跳过」，那是网络请求。用户每敲一个字都抓一次不合适（打字 5 个字符
// 就是 5 次请求，还会被站点限流），所以输入停止 700ms 后才触发
// （见 previewDebounce），且同一时刻只跑一个 worker（见 requestPreview）。
Window {
    id: dlg

    property int sourceId: 0
    property string sourceName: ""
    /// 该订阅此前是否保存过下载器（未保存 → 检查时只记录不下发，
    /// 界面上要给一句提示，见底部）
    property bool savedBefore: false

    /// 可选的本地条目（**全部条目**，不限于已匹配 Bangumi 的）
    property var subjects: typeof library !== "undefined" && library
                           ? library.subjects : []
    property string query: ""
    readonly property var filteredSubjects: {
        // 一律用 `query`（用户在搜索框里打的那个词）过滤，**与
        // `showingPicked` 无关**：选中条目后框里显示的是条目名，但
        // `query` 仍是上一轮的搜索词，列表因此保持那批结果不变。
        var q = dlg.query
        if (q === "")
            return dlg.subjects
        var out = []
        for (var i = 0; i < dlg.subjects.length; i++) {
            var s = dlg.subjects[i]
            if (dlg.matches(s, q))
                out.push(s)
        }
        return out
    }

    /// 当前选中的条目 id（0 = 不指定，用 qBittorrent 全局路径）
    property int selectedId: 0

    /// 上面那个搜索框此刻显示的是「已选中条目名」还是「用户在搜的词」。
    ///
    /// **为什么要区分**：选中后把条目名填进文本框，用户才看得出"我指定了
    /// 哪个"；但那个框同时兼着搜索 —— 直接填进去就等于把它当成了筛选词，
    /// 列表会被过滤成孤零零一行（甚至全名匹配不上而显示"无结果"），
    /// 反而更像坏了。
    ///
    /// 所以：**程序回填时置 `true`（只展示、不参与过滤）**；
    /// 用户一旦动手打字就置回 `false`（恢复成搜索词）。
    /// 见 `selectedSubjectName` / `filteredSubjects` / `searchField.onEdited`。
    property bool showingPicked: false

    /// 当前选中条目的显示名（未指定时为空串）。
    readonly property string selectedSubjectName: {
        var s = dlg.selectedSubject()
        return s ? (s.title || s.name || "") : ""
    }

    /// 内部闸门：`true` 表示**这次文本框变化是程序回填的**，不是用户输入。
    ///
    /// 给 `searchField.text` 赋值会触发 `onTextChanged` → `onEdited`，
    /// 那里会把 `showingPicked` 置回 false（"用户在搜"的语义）——
    /// 程序回填时不能被这样处理（见 pickSubject 的踩坑说明）。
    /// 用这个旗标把两种来源分开，比"赋值顺序上做文章"可靠。
    property bool _fillText: false

    /// 保存路径预览（后端算好：{path, note, exists}）。
    ///
    /// **由后端算**（`rss.savePathPreview`）—— 路径规则全在
    /// `RssMatcher.plan_save_path`，QML 复刻一遍会有两套规则。
    /// 依赖 `selectedId`：用户在列表里改选条目时自动重算。
    readonly property var savePathInfo: {
        if (typeof rss === "undefined" || !rss || dlg.selectedId === 0)
            return null
        return rss.savePathPreview(dlg.sourceId, dlg.selectedId)
    }

    /// 是否正在重新扫描（来自全局 scanner 桥接）。
    /// 只读绑定，用于禁用按钮 / 显示转圈。
    readonly property bool scanRunning: typeof scanner !== "undefined"
                                        && scanner
                                        ? scanner.running : false

    // 预览（来自 RssBridge 的 Property）
    readonly property var previewItems: typeof rss !== "undefined" && rss
                                        ? rss.previewItems : []
    readonly property bool previewRunning: typeof rss !== "undefined" && rss
                                           ? rss.previewRunning : false
    readonly property string previewSummary: typeof rss !== "undefined" && rss
                                             ? rss.previewSummary : ""

    width: 940
    height: 600
    minimumWidth: 780
    minimumHeight: 480
    modality: Qt.ApplicationModal
    title: "下载器 · " + dlg.sourceName
    color: Theme.windowBg
    flags: Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint

    function open(source) {
        dlg.sourceId = source.id
        dlg.sourceName = source.name || ""
        dlg.savedBefore = source.downloaderSaved === true
        includeField.text = source.mustInclude || ""
        excludeField.text = source.mustExclude || ""
        dlg.query = ""
        dlg.selectedId = dlg.pickOnOpen(source)
        // 有保存位置（自己存过的、或从绑定条目回落来的）就把条目名回填进
        // 框里 —— 否则每次打开都像"没指定过"。
        // 回填时置 showingPicked，让列表保持全量、不按这个名字过滤。
        dlg.showingPicked = dlg.selectedId !== 0
        dlg.setTextProgrammatically(
            dlg.showingPicked ? dlg.selectedSubjectName : "")
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
        // 记一行"打开了哪个订阅的下载器"（后端从库读订阅名 + 绑定条目）。
        // 必须**在触发预览之前**：预览会抓一次 RSS 并打出一批"按规则过滤"
        // 的日志，先记这行才能对上号。
        if (typeof rss !== "undefined" && rss)
            rss.logDownloaderOpened(dlg.sourceId)
        // 打开就算一次预览（让用户立刻看到当前规则的效果）
        dlg.triggerPreview()
    }

    /// 选中某个条目：记住 id、把名字回填进框，**列表保持上一轮的搜索结果**。
    ///
    /// **`query` 保留、不能清**：那个搜索词是用户
    /// 用来找条目的线索，选完还要接着用它比选别的季 —— 清掉就等于
    /// 把他刚打的字抹了。
    ///
    /// 回填条目名走 `setTextProgrammatically`（`_fillText` 闸门）：
    /// 直接赋值会触发 `onEdited`，把 `query` 覆盖成条目全名 ——
    /// 列表随即按全名过滤、只剩一行（正是要避免的）。
    function pickSubject(subject) {
        if (!subject)
            return
        dlg.selectedId = subject.id
        dlg.setTextProgrammatically(dlg.selectedSubjectName)
        // 回填的是条目名（不是搜索词），列表继续用 `query` 过滤
        dlg.showingPicked = true
    }

    /// 程序回填搜索框（不触发"用户开始搜索"的副作用）。见 pickSubject 说明。
    function setTextProgrammatically(text) {
        dlg._fillText = true
        searchField.text = text
        dlg._fillText = false
    }

    /// 清除选择（「不指定」按钮与手动开始搜索时调用）。
    function clearPick() {
        dlg.selectedId = 0
        dlg.showingPicked = false
    }

    /// 触发预览（内部走防抖）
    function triggerPreview() {
        if (typeof rss === "undefined" || !rss)
            return
        previewDebounce.restart()
    }

    /// 重新扫描**当前选中的条目**，让"本地已有几集"重新对齐磁盘。
    ///
    /// 为什么需要：查重第①层读的是数据库 `episodes` 表，
    /// 而它只在**扫描时**被写入。目录里后加的文件（换资源组、补集、
    /// 改名）不会自动进库 —— 用户看到"明明是已有的集，预览却说要下载"。
    /// 这里复用详情页那个 `scanner.startSubject()`（同一个入口，
    /// 不是另写一套扫描），行为与详情页「重新扫描该条目」完全一致。
    ///
    /// 扫完**自动重跑预览**（见下方 Connections），用户不用手动再点一次。
    function rescanSelected() {
        if (typeof scanner === "undefined" || !scanner)
            return
        if (dlg.selectedId === 0)
            return
        scanner.startSubject(dlg.selectedId)
    }

    function folderOf(subj) {
        return subj && (subj.folderPath || "") !== "" ? subj.folderPath : ""
    }

    function subjectById(id) {
        for (var i = 0; i < dlg.subjects.length; i++)
            if (dlg.subjects[i].id === id)
                return dlg.subjects[i]
        return null
    }

    function selectedSubject() {
        return dlg.subjectById(dlg.selectedId)
    }

    /// 打开弹窗时该预选哪个条目当保存位置。
    ///
    /// 优先用**上次保存过的保存位置**（`saveSubjectId`）；没有的话回落到
    /// **订阅绑定的那个条目**（`localSubjectId`）—— 「绑定哪个就下到谁的
    /// 目录」本来就是后端口径：`RssMatcher.plan_save_path` 第一件事就是
    /// `save_subject_id or local_subject_id`。所以旧版界面会出现自相矛盾的
    /// 一处：上面写着"未指定 —— 使用 qBittorrent 自己的保存路径"，后端却
    /// 老老实实按绑定条目算路径、把文件下进那部番的目录。
    ///
    /// 回落到绑定条目前**先确认它还在库里**：绑定的条目可能已被删除，
    /// 这时留 0（= 未指定），别让搜索框回填一个全库都查不到的名字。
    function pickOnOpen(source) {
        var saved = source.saveSubjectId || 0
        if (saved !== 0)
            return saved
        var bound = source.localSubjectId || 0
        return (bound !== 0 && dlg.subjectById(bound) !== null) ? bound : 0
    }

    function matches(item, q) {
        if (!item)
            return false
        function has(v) {
            return String(v === undefined || v === null ? "" : v)
                       .toLowerCase().indexOf(q) >= 0
        }
        return has(item.title) || has(item.name) || has(item.nameCn)
            || has(item.seriesName) || has(item.aliases)
    }

    /// 预览行的动作 → 文案与颜色
    function actionLabel(a) {
        return a === "download" ? "将下载"
             : a === "filtered" ? "已过滤" : "跳过"
    }
    function actionColor(a) {
        return a === "download" ? Theme.accent
             : a === "filtered" ? Theme.warningColor : Theme.textTertiary
    }

    // 重新扫描完成后**自动重跑预览**。
    //
    // 为什么必须重跑：预览里"跳过的集"完全取决于扫描结果，不重跑的话
    // 界面上还显示旧结论（"将下载 10 集"），用户会以为扫描没生效。
    //
    // 延后 300ms 再触发：`finished` 信号从扫描线程发出，而 LibraryBridge
    // 的 `subjects`（预览里读的目录信息）是在**扫描完成钩子**里刷新的。
    // 立刻重预览可能读到旧数据 —— 给主线程一个事件循环的余量。
    Connections {
        target: typeof scanner !== "undefined" && scanner ? scanner : null
        function onFinished(matched, pending) {
            if (dlg.visible)
                rerunTimer.restart()
        }
    }

    Timer {
        id: rerunTimer
        interval: 300
        repeat: false
        onTriggered: dlg.triggerPreview()
    }

    // 输入停止 700ms 后才真正去抓（见文件头说明）
    Timer {
        id: previewDebounce
        interval: 700
        repeat: false
        onTriggered: {
            if (typeof rss !== "undefined" && rss)
                rss.requestPreview(dlg.sourceId, includeField.text,
                                   excludeField.text)
        }
    }

    /// 取消所有输入框的焦点与文字选中（点空白处时调用）。
    ///
    /// **必须调 `AppTextField.blur()`，不能给外层设 `focus = false`**
    /// ：真正持有焦点的是组件**内部的 TextInput**，
    /// 聚焦描边读的也是 `input.activeFocus`；外层的 Rectangle 不是焦点项，
    /// 给它赋值等于什么都没做，描边照旧亮着。见 AppTextField.blur()。
    function clearInputFocus() {
        includeField.blur()
        excludeField.blur()
        searchField.blur()
    }

    // ---- 主体：左右两栏 ----
    Row {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: buttons.top
        anchors.margins: Theme.spacingXl
        anchors.bottomMargin: Theme.spacingMd
        spacing: Theme.spacingXl

        // ============ 左：设置 ============
        Column {
            id: leftCol
            width: Math.round(parent.width * 0.5) - Theme.spacingXl / 2
            spacing: Theme.spacingMd

            Text {
                text: "标题过滤"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontMd
                font.weight: Font.DemiBold
            }

            Text {
                width: parent.width
                text: "留空 = 不过滤。多个词用「,」分隔。"
                      + "必须包含：全部命中才下；必须不包含：命中任一即跳过。"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontXs
                wrapMode: Text.WordWrap
            }

            FormRow {
                width: parent.width
                label: "必须包含"
                AppTextField {
                    id: includeField
                    objectName: "downloaderIncludeField"
                    width: parent.width
                    placeholder: "如：简日, 1080p"
                    // 改完实时重算预览（防抖）
                    onEdited: dlg.triggerPreview()
                }
            }

            FormRow {
                width: parent.width
                label: "必须不包含"
                AppTextField {
                    id: excludeField
                    objectName: "downloaderExcludeField"
                    width: parent.width
                    placeholder: "如：繁体, 合集"
                    onEdited: dlg.triggerPreview()
                }
            }

            Rectangle {
                width: parent.width
                height: Theme.lineThin
                color: Theme.border
            }

            // 「保存位置」标题行 + 右侧「重新扫描」按钮。
            //
            // **为什么把重新扫描放在这里**：用户在预览里看到
            // "将下载 10 集"，但自己目录里明明已有其中 3 集 —— 原因是查重
            // 第①层读的是**数据库 `episodes` 表**，只有**扫描过**才会写进去。
            // 目录里后来新增/改名/换组的文件不会自动进库，于是被当成新集。
            // 用户在这个弹窗里能直接看到目录，正好在这里给一个补救入口，
            // 不用退回详情页再点一次扫描。
            // 标题行：文字与按钮**垂直居中对齐**。
            //
            // **为什么给 Row 一个显式高度并让子项 centerIn**：Row 的高度由最高的子项
            // 决定，而按钮（24）比文字（约 19）高 —— 若各自
            // `anchors.verticalCenter: parent.verticalCenter`，Row 会以
            // 按钮为基准撑高，文字看着偏上、整行又与相邻元素错位。
            // 这里把 Row 高度钉成**按钮的高度**，文字与按钮都以它居中，
            // 于是「保存位置」四个字与按钮边框上下都对齐。
            Row {
                width: parent.width
                height: rescanBtn.implicitHeight
                spacing: Theme.spacingSm

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "保存位置"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontMd
                    font.weight: Font.DemiBold
                }

                AppButton {
                    id: rescanBtn
                    objectName: "downloaderRescanBtn"
                    anchors.verticalCenter: parent.verticalCenter
                    // 小一号：这是标题行里的辅助操作，不该和底部的
                    // 「保存 / 取消」同等份量（compact 见 AppButton 说明）
                    compact: true
                    // 没选中条目就没得扫（此时用的是 qB 全局路径，
                    // 我们不知道对应哪个本地条目）
                    visible: dlg.selectedId !== 0
                    enabled: !dlg.scanRunning
                    text: dlg.scanRunning ? "扫描中…" : "重新扫描"
                    onClicked: dlg.rescanSelected()
                }

                AddButton {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: dlg.scanRunning
                    size: 14
                    spinning: true
                }
            }

            // 保存位置说明：**目录由后端算好**（rss.savePathPreview），
            // QML 只负责显示。
            //
            // **为什么不在这里拼字符串**：以前"没有
            // folder_path"的条目只说一句"媒体库根目录下新建「xx」"，
            // 用户看不到**具体路径**，也就不知道会不会和别的季分家。
            // 但路径规则（按系列归位、季子目录名、媒体库根）全在后端
            // `plan_save_path` 里 —— 在 QML 复刻一遍就会有两套规则，
            // 迟早出现"界面显示 F:\A、文件却下到 F:\B"。
            // 所以统一走后端：它算出 path + 现成文案，这里照抄即可。
            Text {
                width: parent.width
                text: {
                    if (dlg.selectedId === 0)
                        return "未指定 —— 使用 qBittorrent 自己的保存路径"
                    var info = dlg.savePathInfo
                    var note = info && info.note ? info.note : ""
                    if (note === "")
                        // 后端算不出（条目被删 / 媒体库未配置）
                        return "无法确定保存目录，将使用 qBittorrent 自己的路径"
                    return info.exists
                           ? note
                           : note + "\n（目录不存在，下发时自动创建）"
                }
                color: Theme.textSecondary
                font.pixelSize: Theme.fontXs
                lineHeight: 1.35
                wrapMode: Text.WordWrap
            }

            Row {
                width: parent.width
                spacing: Theme.spacingSm

                AppTextField {
                    id: searchField
                    objectName: "downloaderSubjectSearch"
                    width: parent.width - clearBtn.width - Theme.spacingSm
                    placeholder: "搜索本地条目（支持别名）"
                    // 用户一动手打字就**退出"已选中展示"态**，回到搜索语义：
                    //   - 保留 selectedId（选中没有取消，只是框里换成了搜索词）
                    //   - 用新输入的内容过滤列表
                    // 这样"选中后看着不满意、想改选"只需直接接着打字。
                    onEdited: {
                        // 程序回填（选中条目 / 打开弹窗）不算"用户在搜索"
                        if (dlg._fillText)
                            return
                        if (dlg.showingPicked)
                            dlg.showingPicked = false
                        dlg.query = searchField.text.trim().toLowerCase()
                    }
                }

                AppButton {
                    id: clearBtn
                    text: "不指定"
                    onClicked: {
                        // 语义是"不指定保存位置"（用 qBittorrent 全局路径），
                        // 顺带把搜索框清空、恢复成可搜索的空态。
                        dlg.clearPick()
                        dlg.query = ""
                        searchField.text = ""
                    }
                }
            }

            Rectangle {
                width: parent.width
                height: 150
                color: Theme.surfaceAlt
                border.width: Theme.lineThin
                border.color: Theme.border
                radius: Theme.radiusSm
                clip: true

                Flickable {
                    anchors.fill: parent
                    anchors.margins: 2
                    clip: true
                    contentHeight: subjList.implicitHeight
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded }

                    Column {
                        id: subjList
                        width: parent.width

                        Repeater {
                            model: dlg.filteredSubjects

                            delegate: Rectangle {
                                required property var modelData

                                readonly property bool picked:
                                    dlg.selectedId === modelData.id

                                width: subjList.width
                                height: 28
                                color: picked ? Theme.accentSoft
                                     : (subjMouse.containsMouse ? Theme.hoverFill
                                                                : "transparent")

                                Text {
                                    anchors.left: parent.left
                                    anchors.leftMargin: Theme.spacingSm
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: parent.width - Theme.spacingLg
                                    text: {
                                        var t = modelData.title || ""
                                        return dlg.folderOf(modelData) !== ""
                                               ? t : t + "（无目录，将新建）"
                                    }
                                    color: picked ? Theme.accent
                                                  : Theme.textPrimary
                                    font.pixelSize: Theme.fontSm
                                    font.weight: picked ? Font.DemiBold
                                                        : Font.Normal
                                    elide: Text.ElideRight
                                }

                                MouseArea {
                                    id: subjMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: dlg.pickSubject(modelData)
                                }
                            }
                        }

                        Text {
                            width: parent.width
                            height: 50
                            visible: dlg.filteredSubjects.length === 0
                            text: dlg.subjects.length === 0
                                  ? "媒体库里还没有条目"
                                  : "没有匹配「" + searchField.text + "」的条目"
                            color: Theme.textTertiary
                            font.pixelSize: Theme.fontSm
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }
        }

        // ============ 右：预览 ============
        Column {
            width: parent.width - leftCol.width - Theme.spacingXl
            height: parent.height
            spacing: Theme.spacingSm

            Row {
                width: parent.width
                spacing: Theme.spacingSm

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "预览"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontMd
                    font.weight: Font.DemiBold
                }

                AddButton {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: dlg.previewRunning
                    size: 16
                    spinning: true
                }
            }

            Text {
                width: parent.width
                text: dlg.previewSummary
                color: dlg.previewRunning ? Theme.accent : Theme.textSecondary
                font.pixelSize: Theme.fontSm
                elide: Text.ElideRight
            }

            Rectangle {
                width: parent.width
                height: parent.height - y
                color: Theme.surfaceAlt
                border.width: Theme.lineThin
                border.color: Theme.border
                radius: Theme.radiusSm
                clip: true

                Flickable {
                    anchors.fill: parent
                    anchors.margins: 2
                    clip: true
                    contentHeight: prevList.implicitHeight
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded }

                    Column {
                        id: prevList
                        width: parent.width

                        Repeater {
                            model: dlg.previewItems

                            delegate: Rectangle {
                                required property var modelData

                                width: prevList.width
                                height: 44
                                color: "transparent"

                                Row {
                                    anchors.fill: parent
                                    anchors.leftMargin: Theme.spacingSm
                                    anchors.rightMargin: Theme.spacingSm
                                    spacing: Theme.spacingSm

                                    // 状态标签（将下载/跳过/已过滤）
                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 52
                                        height: 18
                                        radius: Theme.radiusSm
                                        color: "transparent"
                                        border.width: Theme.lineThin
                                        border.color: dlg.actionColor(modelData.action)

                                        Text {
                                            anchors.centerIn: parent
                                            text: dlg.actionLabel(modelData.action)
                                            color: dlg.actionColor(modelData.action)
                                            font.pixelSize: Theme.fontXs
                                        }
                                    }

                                    Column {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: parent.width - 52 - Theme.spacingSm * 2
                                        spacing: 1

                                        Text {
                                            width: parent.width
                                            text: (modelData.ep > 0
                                                   ? "EP" + Math.round(
                                                         modelData.ep * 100) / 100
                                                     + "  "
                                                   : "")
                                                  + (modelData.title || "")
                                            color: modelData.action === "download"
                                                   ? Theme.textPrimary
                                                   : Theme.textTertiary
                                            font.pixelSize: Theme.fontXs
                                            elide: Text.ElideRight
                                        }

                                        Text {
                                            width: parent.width
                                            visible: (modelData.reason || "") !== ""
                                            text: modelData.reason || ""
                                            color: Theme.textTertiary
                                            font.pixelSize: Theme.fontXs
                                            elide: Text.ElideRight
                                        }
                                    }
                                }
                            }
                        }

                        Text {
                            width: parent.width
                            height: 80
                            visible: dlg.previewItems.length === 0
                                     && !dlg.previewRunning
                            text: dlg.previewSummary
                            color: Theme.textTertiary
                            font.pixelSize: Theme.fontSm
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }
        }
    }

    // ---- 底部按钮（锚底）----
    Row {
        id: buttons
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Theme.spacingXl
        spacing: Theme.spacingMd

        Text {
            anchors.verticalCenter: parent.verticalCenter
            visible: !dlg.savedBefore
            text: "保存后才会开始下载"
            color: Theme.warningColor
            font.pixelSize: Theme.fontXs
        }

        AppButton {
            objectName: "downloaderCancelBtn"
            text: "取消"
            onClicked: {
                if (typeof rss !== "undefined" && rss)
                    rss.clearPreview()
                dlg.close()
            }
        }

        AppButton {
            objectName: "downloaderSaveBtn"
            text: "保存"
            variant: "primary"
            onClicked: {
                if (typeof rss === "undefined" || !rss) {
                    dlg.close()
                    return
                }
                rss.setDownloader(dlg.sourceId, includeField.text,
                                  excludeField.text, dlg.selectedId)
                rss.clearPreview()
                dlg.close()
            }
        }
    }

    // ---- 点空白处 → 取消输入框的焦点 ----
    //
    // **必须声明在所有内容之后**（QML 里后声明 = 在上层）。
    //
    // **用 TapHandler 置顶**：它只在"没有任何其它 handler 消费这次点击"
    // 时才触发 —— 按钮 / 输入框 / 列表行自己的 handler 会先把它吃掉，
    // 所以不会误伤控件；真正落在空白处的点击才会走到 `onTapped`。
    TapHandler {
        acceptedButtons: Qt.LeftButton
        onTapped: dlg.clearInputFocus()
    }

}
