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
        dlg.selectedId = source.saveSubjectId || 0
        dlg.query = ""
        searchField.text = ""
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
        // 打开就算一次预览（让用户立刻看到当前规则的效果）
        dlg.triggerPreview()
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

    function selectedSubject() {
        for (var i = 0; i < dlg.subjects.length; i++)
            if (dlg.subjects[i].id === dlg.selectedId)
                return dlg.subjects[i]
        return null
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
            // **为什么把重新扫描放在这里**（实测需求）：用户在预览里看到
            // "将下载 10 集"，但自己目录里明明已有其中 3 集 —— 原因是查重
            // 第①层读的是**数据库 `episodes` 表**，只有**扫描过**才会写进去。
            // 目录里后来新增/改名/换组的文件不会自动进库，于是被当成新集。
            // 用户在这个弹窗里能直接看到目录，正好在这里给一个补救入口，
            // 不用退回详情页再点一次扫描。
            // 标题行：文字与按钮**垂直居中对齐**。
            //
            // **为什么给 Row 一个显式高度并让子项 centerIn**（实测反馈
            // "上下边框与保存位置上下边界对齐"）：Row 的高度由最高的子项
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
            // **为什么不在这里拼字符串**（实测需求："可以在这段文字的
            // 下一行写清楚目录在哪，像其他已存在条目一样"）：以前"没有
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
                    onEdited: dlg.query = searchField.text.trim().toLowerCase()
                }

                AppButton {
                    id: clearBtn
                    text: "不指定"
                    onClicked: {
                        dlg.selectedId = 0
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
                                    onClicked: dlg.selectedId = modelData.id
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


}
