import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 订阅页（阶段 7）：RSS 订阅源管理与下载记录。
//
// 本阶段范围：订阅源 CRUD + 绑定本地条目 + 查看下载记录。
// **不含**实际轮询下载 —— 那需要 qBittorrent WebAPI 客户端与 RSS 解析器，
// 数据库表与配置项已就绪，留待后续接入（详见 bridges/rss.py 顶部说明）。
//
// 交互设计：
// - 顶部「添加订阅」展开一个内联表单（比弹窗轻，且不打断阅读）
// - 每条订阅显示：名称 / 地址 / 绑定条目 / 判新规则 / 下载统计 / 启用开关
// - 行尾操作：绑定条目、编辑、删除
Item {
    id: root

    // 防御性写法：上下文属性在独立加载本文件时不存在（同 PosterWallPage）
    property var sources: typeof rss !== "undefined" && rss ? rss.sources : []
    property var downloads: typeof rss !== "undefined" && rss ? rss.downloads : []
    // 可绑定的本地条目（有 bangumi_id 的才算）
    property var linkableSubjects: typeof library !== "undefined" && library
                                   ? library.subjects.filter(function (s) {
                                         return s.bangumiId > 0
                                     })
                                   : []
    // 绑定弹窗里搜索框的关键词（存成小写，比较时不再重复转）
    property string bindQuery: ""
    // 过滤后的可绑定条目（绑定弹窗的列表用这个，不是 linkableSubjects）
    //
    // 匹配四个字段 + 别名，与海报墙的 textMatches 同一套口径（见
    // PosterWallPage.textMatches 的说明）：用户想搜的往往是俗称而不是
    // 正式全名，只比 title 会让「未闻花名」这类搜不到。
    //
    // 这里**故意把依赖写进属性而不是函数**：`property` 参与绑定追踪，
    // bindSearch.text 一变就会重算；写成函数调用的话 QML 不知道它依赖
    // 什么，只会求值一次，搜索框就"打不进字"。
    readonly property var filteredLinkableSubjects: {
        var q = root.bindQuery
        if (q === "")
            return root.linkableSubjects
        var out = []
        for (var i = 0; i < root.linkableSubjects.length; i++) {
            var s = root.linkableSubjects[i]
            if (root.subjectMatches(s, q))
                out.push(s)
        }
        return out
    }

    property bool addFormVisible: false
    property int editingId: 0          // 0 = 新增模式
    property int bindingId: 0          // 正在选绑定条目的订阅 ID
    property bool showDownloads: false
    // 轮询状态（来自 RssBridge 的 Property，检查中/上次结果）
    readonly property bool pollRunning: typeof rss !== "undefined" && rss
                                        ? rss.pollRunning : false
    readonly property string pollStatus: typeof rss !== "undefined" && rss
                                        ? rss.pollStatus : ""

    signal statusMessage(string text)

    Flickable {
        id: flick
        anchors.fill: parent
        clip: true
        contentWidth: width
        contentHeight: Math.max(
            Theme.pagePadding + content.implicitHeight
                + Theme.navContentGutter, height)
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.vertical: AppScrollBar {
            id: vbar
            policy: ScrollBar.AsNeeded
        }

        Column {
            id: content
            x: Theme.pagePadding
            y: Theme.pagePadding
            width: flick.width - Theme.pagePadding * 2
                   - (vbar.visible ? vbar.width : 0)
            spacing: Theme.spacingMd

            // ---- 标题行 ----
            Row {
                width: parent.width
                spacing: Theme.spacingMd

                // 宽度 = 行宽 − **所有**右侧按钮 − 按钮间距。
                //
                // 现在改成用 `childrenRect` 之外的方式：显式列出三个按钮。
                // **以后再加按钮必须同步这里**（或者改用一个 RowLayout +
                // Layout.fillWidth 让布局自己算，那就不会再漏）。
                Column {
                    width: parent.width - pollBtn.width - addBtn.width
                           - downloadBtn.width - Theme.spacingMd * 3
                    spacing: 2

                    Text {
                        text: "订阅"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontXl
                        font.weight: Font.DemiBold
                    }

                    // 状态行：条数 + **上次检查时间/结果**。
                    // 两段合成一行（见 RssBridge.pollStatus），检查中显示进度。
                    Text {
                        width: parent.width
                        text: {
                            var n = root.sources.length
                            var head = n > 0 ? n + " 个订阅源"
                                             : "尚无订阅源，点右侧「添加订阅」开始"
                            var st = root.pollStatus
                            return st ? head + " · " + st : head
                        }
                        color: root.pollRunning ? Theme.accent
                                                : Theme.textTertiary
                        font.pixelSize: Theme.fontSm
                        elide: Text.ElideRight
                    }
                }

                // 「立即检查」：手动触发一次轮询（不等定时器到点）。
                // 检查期间禁用 —— 后端也会拒绝并发（见 RssService.poll）。
                AppButton {
                    id: pollBtn
                    objectName: "rssPollBtn"
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.pollRunning ? "检查中…" : "立即检查"
                    enabled: !root.pollRunning
                    onClicked: {
                        if (typeof rss !== "undefined" && rss)
                            rss.requestPoll()
                    }
                }

                AppButton {
                    id: downloadBtn
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.showDownloads ? "隐藏记录" : "下载记录"
                    onClicked: {
                        root.showDownloads = !root.showDownloads
                        // 打开记录时顺手拉一次 qBittorrent 进度（异步、
                        // 失败静默）—— 否则进度要等下一次轮询才更新
                        if (root.showDownloads && typeof rss !== "undefined" && rss)
                            rss.refreshTorrents()
                    }
                }

                AppButton {
                    id: addBtn
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.addFormVisible ? "取消" : "添加订阅"
                    onClicked: {
                        root.addFormVisible = !root.addFormVisible
                        // 现在只切换 addFormVisible，编辑状态不受影响。
                        if (root.addFormVisible)
                            addForm.setValues("", "", "new_only")
                    }
                }
            }

            Rectangle {
                width: parent.width
                height: Theme.lineThin
                color: Theme.border
            }

            // ---- 新增表单（只用于「新增」，编辑时表单在卡片下方内联展开）----
            //
            // 顶部这份只服务新增。
            SubscriptionForm {
                id: addForm
                objectName: "subAddForm"
                width: parent.width
                // **只看 addFormVisible**，不再要求 editingId === 0 ——
                // 新增与编辑是两个独立的面板，可以同时在屏幕上（见
                // addBtn 的说明）。
                visible: root.addFormVisible
                title: "添加订阅"
                ruleValue: "new_only"
                onSubmitted: function (name, url, rule) {
                    root.saveForm(0, name, url, rule)
                }
                onCancelled: {
                    root.addFormVisible = false
                }
            }

            // ---- 订阅源列表 ----
            Column {
                id: listColumn
                width: parent.width
                spacing: Theme.spacingSm

                Repeater {
                    model: root.sources

                    delegate: Rectangle {
                        required property var modelData

                        readonly property bool editing: root.editingId === modelData.id

                        width: listColumn.width
                        height: cardColumn.implicitHeight + Theme.spacingMd * 2
                        color: Theme.surfaceBg
                        border.width: Theme.lineThin
                        // 编辑中的卡片描边换成主题色：一眼看出在改哪一条
                        border.color: editing ? Theme.accent : Theme.border
                        radius: Theme.radiusMd

                        Column {
                            id: cardColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.margins: Theme.spacingMd
                            spacing: Theme.spacingXs

                            // 第一行：名称 + 绑定 + 开关 + 操作
                            Row {
                                width: parent.width
                                spacing: Theme.spacingSm

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: Math.min(implicitWidth, parent.width * 0.4)
                                    text: modelData.name
                                    color: modelData.enabled ? Theme.textPrimary
                                                             : Theme.textTertiary
                                    font.pixelSize: Theme.fontMd
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }

                                // 绑定状态标签
                                Rectangle {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: modelData.subjectName !== ""
                                    width: bindLabel.implicitWidth + Theme.spacingMd
                                    height: 20
                                    radius: Theme.radiusSm
                                    color: Theme.accentSoft

                                    Text {
                                        id: bindLabel
                                        anchors.centerIn: parent
                                        text: "→ " + modelData.subjectName
                                        color: Theme.accent
                                        font.pixelSize: Theme.fontXs
                                        elide: Text.ElideRight
                                    }
                                }

                                // ---- 弹簧：把「启用」推到最右侧 ----
                                //
                                // **踩坑**：这里原来是
                                //     Item { width: 1; height: 1; Layout.fillWidth: true }
                                // —— 那是从 **RowLayout** 里抄来的写法，而本行是
                                // **`Row`**。`Layout.fillWidth` 属于 QtQuick.Layouts
                                // 的附加属性，**在 Row 里完全无效**（Row 不认
                                // Layout 属性，只会用子项的 width）。
                                // 于是这个 Item 永远只有 1px 宽，"启用"就紧贴在
                                // 绑定标签屁股后面，看着偏左。
                                //
                                // Row 里做弹簧要用**显式宽度**：占满"前面已用的
                                // 宽度"之外的全部空间。用 `parent.width` 减去
                                // 之前所有子项的隐式宽度与间距（`children[0]` 是
                                // 名称 Text，但它的 width 被 `Math.min(...)` 限制过，
                                // 直接取它的实际 width 才对）。
                                // 采用 `x + width` 的实时算法最容易跟住布局：
                                // 弹簧 Item 放在倒数第二，它的 x 就是前面内容的
                                // 右边缘，因此 width = 父宽 − x − 右侧开关宽度 − 间距。
                                Item {
                                    id: spring
                                    height: 1
                                    width: Math.max(0,
                                                    parent.width - x
                                                    - enableBox.width
                                                    - Theme.spacingSm)
                                }

                                CheckBoxLine {
                                    id: enableBox
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: "启用"
                                    checked: modelData.enabled
                                    onToggled: function (v) {
                                        if (typeof rss !== "undefined" && rss)
                                            rss.setEnabled(modelData.id, v)
                                    }
                                }
                            }

                            // 第二行：地址
                            Text {
                                width: parent.width
                                text: modelData.url
                                color: Theme.textTertiary
                                font.pixelSize: Theme.fontSm
                                elide: Text.ElideMiddle
                            }

                            // 第三行：规则 + 统计 + 错误
                            Row {
                                width: parent.width
                                spacing: Theme.spacingMd

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.ruleLabel
                                    color: Theme.textSecondary
                                    font.pixelSize: Theme.fontSm
                                }

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: modelData.downloadCount > 0
                                    text: "下载 " + modelData.downloadCount
                                          + "（完成 " + modelData.completedCount
                                          + (modelData.failedCount > 0
                                             ? " · 失败 " + modelData.failedCount : "")
                                          + "）"
                                    color: modelData.failedCount > 0
                                           ? Theme.dangerColor : Theme.textTertiary
                                    font.pixelSize: Theme.fontSm
                                }

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: parent.width - x
                                    visible: modelData.lastError !== ""
                                    text: "⚠ " + modelData.lastError
                                    color: Theme.dangerColor
                                    font.pixelSize: Theme.fontSm
                                    elide: Text.ElideRight
                                }
                            }

                            // 第四行：操作按钮
                            Row {
                                spacing: Theme.spacingSm
                                topPadding: Theme.spacingXs

                                // 「下载器」放在三个按钮**左边**（需求明确指定）。
                                // 它管的是"下载行为"（过滤词 / 保存到哪个目录），
                                // 与右边三个"订阅管理"动作（绑定/编辑/删除）
                                // 性质不同，所以单独放前面，靠间距区分开。
                                AppButton {
                                    objectName: "subDownloaderBtn"
                                    text: "下载器"
                                    onClicked: downloaderDialog.open(modelData)
                                }

                                AppButton {
                                    text: modelData.subjectName !== ""
                                          ? "改绑定" : "绑定条目"
                                    onClicked: {
                                        root.bindingId = modelData.id
                                        // 清掉上次的搜索词：否则再打开时列表
                                        // 被旧关键词过滤着，用户会以为
                                        // "条目都不见了"
                                        root.bindQuery = ""
                                        bindSearch.text = ""
                                        // 新建条目那一栏也清空：否则会带着
                                        // 上一个订阅输入的名字
                                        newSubjectField.text = ""
                                        bindDialog.open()
                                    }
                                }

                                // 「编辑 / 取消」：同一颗按钮负担两个语义。
                                // 编辑中时文案变「取消」—— 需求明确要求
                                // "显示后，该动漫栏的编辑变成取消"。
                                AppButton {
                                    objectName: "subEditBtn"
                                    text: parent.parent.editing ? "取消" : "编辑"
                                    onClicked: {
                                        if (parent.parent.editing) {
                                            root.editingId = 0
                                            return
                                        }
                                        root.editingId = modelData.id
                                        // **不要在这里收掉「添加」表单**
                                        // （同 addBtn 处的踩坑说明）：两者是
                                        // 独立面板，同时打开没有任何问题。
                                        // 早期写 `addFormVisible = false`，
                                        // 于是点「编辑」会把用户正填着的新增
                                        // 表单一起关掉（数据白填）。
                                        //
                                        // 用 setValues 预填（而不是直接改子项
                                        // id）—— 表单已抽成组件，外部只能通过
                                        // 它的方法填值。
                                        editForm.setValues(modelData.name,
                                                           modelData.url,
                                                           modelData.rule)
                                    }
                                }

                                AppButton {
                                    text: "删除"
                                    variant: "normal"
                                    onClicked: root.confirmDelete(modelData)
                                }
                            }

                            // ---- 编辑表单：**内联在被编辑的卡片下方** ----
                            //
                            // 放在这里（卡片 Column 的末尾）而不是页面顶部，
                            // 用户点哪条就改哪条，视线不用跳。
                            SubscriptionForm {
                                id: editForm
                                width: parent.width
                                visible: parent.parent.editing
                                title: "编辑订阅"
                                onSubmitted: function (name, url, rule) {
                                    root.saveForm(modelData.id, name, url, rule)
                                }
                                onCancelled: {
                                    root.editingId = 0
                                }
                            }
                        }
                    }
                }
            }

            // ---- 空状态 ----
            Column {
                width: parent.width
                height: 180
                visible: root.sources.length === 0
                spacing: Theme.spacingSm

                Item { width: 1; height: 40 }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "还没有订阅源"
                    color: Theme.textSecondary
                    font.pixelSize: Theme.fontLg
                }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "添加 RSS 订阅后，可以把更新与本地媒体库联动"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontMd
                }
            }

            // ---- 下载记录 ----
            Column {
                width: parent.width
                spacing: Theme.spacingSm
                visible: root.showDownloads

                Rectangle {
                    width: parent.width
                    height: Theme.lineThin
                    color: Theme.border
                }

                Text {
                    topPadding: Theme.spacingSm
                    text: "下载记录"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontLg
                    font.weight: Font.DemiBold
                }

                Text {
                    width: parent.width
                    visible: root.downloads.length === 0
                    // 文案随"有没有订阅"变化：早期固定写"轮询下载功能尚未
                    // 接入"，而**功能接上之后那句话就成了假话**。
                    // 现在按是否有订阅源给不同引导。
                    text: root.sources.length === 0
                          ? "还没有订阅源，添加并启用后会自动检查新集"
                          : "暂无下载记录 —— 点右上角「立即检查」试一次"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                    wrapMode: Text.WordWrap
                }

                Repeater {
                    model: root.downloads

                    delegate: Rectangle {
                        required property var modelData

                        width: parent.width
                        // 高度按"有没有附加行"算：进度条一行 +27px，
                        // 失败原因一行也 +27px（两者可能同时出现）
                        height: 36
                                + (modelData.progress >= 0 ? 20 : 0)
                                + (modelData.lastError !== "" ? 20 : 0)
                        color: "transparent"

                        Row {
                            anchors.fill: parent
                            spacing: Theme.spacingMd

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 60
                                text: "EP" + root.fmtIndex(modelData.epIndex)
                                color: Theme.accent
                                font.pixelSize: Theme.fontSm
                            }

                            // 标题 + 进度条（竖排）
                            Column {
                                anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - 60 - rightPart.width
                                       - Theme.spacingMd * 2
                                spacing: 3

                                Text {
                                    width: parent.width
                                    text: modelData.torrentTitle
                                    color: Theme.textPrimary
                                    font.pixelSize: Theme.fontSm
                                    elide: Text.ElideRight
                                }

                                // 进度条：**只在拿到真实进度时显示**
                                // （progress < 0 表示没查到，见 _load_downloads），
                                // 否则画一条 0% 的空条会让人以为"卡住了"
                                Row {
                                    width: parent.width
                                    spacing: Theme.spacingXs
                                    visible: modelData.progress >= 0

                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: parent.width - pctText.width
                                               - Theme.spacingXs
                                        height: 4
                                        radius: height / 2
                                        color: Theme.surfaceAlt

                                        Rectangle {
                                            width: Math.round(parent.width
                                                   * Math.max(0, Math.min(
                                                         1, modelData.progress)))
                                            height: parent.height
                                            radius: height / 2
                                            color: modelData.progress >= 1
                                                   ? Theme.successColor
                                                   : Theme.accent
                                            Behavior on width {
                                                NumberAnimation {
                                                    duration: Theme.durFast
                                                }
                                            }
                                        }
                                    }

                                    Text {
                                        id: pctText
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: Math.round(modelData.progress * 100)
                                              + "%"
                                        color: Theme.textTertiary
                                        font.pixelSize: Theme.fontXs
                                    }
                                }

                                // 失败原因（v11 落库）：**直接显示在记录上**，而不是让用户去翻日志。
                                Text {
                                    width: parent.width
                                    visible: modelData.lastError !== ""
                                    text: "⚠ " + modelData.lastError
                                    color: Theme.dangerColor
                                    font.pixelSize: Theme.fontXs
                                    elide: Text.ElideRight
                                }
                            }

                            // 右侧：状态 + 「下发」按钮
                            Row {
                                id: rightPart
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: Theme.spacingSm

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.stateLabel !== ""
                                          ? modelData.stateLabel
                                          : modelData.statusLabel
                                    color: modelData.status === "failed"
                                           ? Theme.dangerColor : Theme.textTertiary
                                    font.pixelSize: Theme.fontSm
                                }

                                // 「下发」（pending → 推送 qBittorrent）
                                // 只对待确认的记录显示 —— 已下发的再点一次
                                // 只会重复添加同一个种子
                                AppButton {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: modelData.canPush
                                    text: "下发"
                                    onClicked: {
                                        if (typeof rss !== "undefined" && rss)
                                            rss.requestPush(modelData.id)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // ---- 绑定条目弹窗 ----
    Dialog {
        id: bindDialog
        objectName: "bindDialog"        // 诊断/探针用
        modal: true
        anchors.centerIn: parent
        width: 420
        padding: Theme.spacingLg
        title: "绑定本地条目"

        /// 把推断出的名称写进「新建条目」输入框。
        ///
        /// **为什么要绕这一层**：
        /// `newSubjectField` 声明在下面 `contentItem` 的 Column 内部，
        /// 那个 id **对文件根作用域不可见** —— 直接在末尾的
        /// `Connections.onTitleSuggested` 里写 `newSubjectField.text = name`
        /// 会**静默失效**。
        /// 而 `bindDialog` 这个 id 在根作用域可见，所以在它上面暴露一个
        /// 方法，外层通过 `bindDialog.setName(name)` 调用即可 ——
        /// 方法体在 Dialog 内部求值，那里能看到 `newSubjectField`。
        function setName(value) {
            newSubjectField.text = value || ""
        }

        background: Rectangle {
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: Theme.border
            radius: Theme.radiusMd
        }

        contentItem: Column {
            spacing: Theme.spacingSm

            Text {
                text: "选择该订阅对应的本地动漫："
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
            }

            // ---- 搜索框----
            //
            // 为什么必须有：可绑定条目是**整个媒体库**（所有匹配到 Bangumi
            // 的条目），一次铺开要滚很久才能找到目标。
            // 输入关键词即时过滤，比滚动快得多。
            //
            // 匹配范围用 `searchText`：后端已经把它拼成
            // 「标题 + 原名 + 别名」的长串（见 library._subject_to_dict 的
            // aliases 说明），所以这里一个 indexOf 就能同时按中文名、
            // 日文名、英文别名搜 —— 不必在 QML 里拆字段。
            AppTextField {
                id: bindSearch
                objectName: "bindSearchField"
                width: parent.width
                placeholder: "输入关键词筛选（支持别名）"
                // 写进 root.bindQuery（小写）供 filteredLinkableSubjects 用；
                // 直接绑 `text: root.bindQuery` 会形成双向循环，所以单向写。
                onEdited: root.bindQuery = bindSearch.text.trim().toLowerCase()
            }

            Flickable {
                width: parent.width
                height: 260
                clip: true
                contentHeight: bindList.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                    id: bindList
                    width: parent.width
                    spacing: Theme.lineThin

                    Repeater {
                        model: root.filteredLinkableSubjects

                        delegate: Rectangle {
                            required property var modelData

                            width: bindList.width
                            height: 32
                            color: bindMouse.containsMouse ? Theme.hoverFill
                                                           : "transparent"

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.left: parent.left
                                anchors.leftMargin: Theme.spacingSm
                                width: parent.width - Theme.spacingLg
                                text: modelData.title
                                color: Theme.textPrimary
                                font.pixelSize: Theme.fontSm
                                elide: Text.ElideRight
                            }

                            MouseArea {
                                id: bindMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (typeof rss !== "undefined" && rss)
                                        rss.linkSubject(root.bindingId, modelData.id)
                                    bindDialog.close()
                                }
                            }
                        }
                    }
                }
            }

            // 空态分两种，文案必须不同（否则用户不知道是"没条目"还是"搜不到"）：
            //   完全没有可绑定条目 → 提示去扫描媒体库
            //   有条目但被搜索词过滤光了 → 提示换个关键词
            Text {
                width: parent.width
                visible: root.filteredLinkableSubjects.length === 0
                text: root.linkableSubjects.length === 0
                      ? "没有已匹配 Bangumi 的条目，请先扫描媒体库"
                      : "没有匹配「" + bindSearch.text + "」的条目"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
            }

            // ---- 新建条目（「全部下载」场景，需求 C）----
            //
            // 背景：判新规则选「全部下载」时，订阅源往往**还没有对应的本地
            // 条目**（那正是"全部下下来"的用意），所以需要一个"从订阅源
            // 直接建条目"的入口 —— 否则用户得先去别处建好条目才能绑定。
            //
            // 需求要点：名称从订阅源获取后自动填入、可手动改；要匹配 Bangumi；
            // 没 Token 则跳过匹配；一个订阅源一个条目。
            Rectangle {
                width: parent.width
                height: Theme.lineThin
                color: Theme.border
            }

            Text {
                width: parent.width
                text: "找不到想要的条目？可以从该订阅源新建一个："
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
            }

            Row {
                width: parent.width
                spacing: Theme.spacingSm

                AppTextField {
                    id: newSubjectField
                    objectName: "newSubjectNameField"
                    width: parent.width - fetchBtn.width - createBtn.width
                           - Theme.spacingSm * 2
                    placeholder: "条目名（可先「获取」再改）"
                }

                AppButton {
                    id: fetchBtn
                    text: "获取"
                    onClicked: {
                        if (typeof rss === "undefined" || !rss)
                            return
                        var src = root.sourceById(root.bindingId)
                        if (!src || src.url === "") {
                            root.statusMessage("该订阅还没填地址，请先编辑补充")
                            return
                        }
                        rss.suggestSubjectName(src.url)
                    }
                }

                AppButton {
                    id: createBtn
                    text: "新建并绑定"
                    variant: "primary"
                    enabled: newSubjectField.text.trim() !== ""
                    onClicked: {
                        if (typeof rss === "undefined" || !rss)
                            return
                        // **不要写 `root.matchBox`**：
                        // `matchBox` 是本组件作用域里的一个 id，而 `root`
                        // （SubscriptionPage）上**没有**这个属性 ——
                        // `root.matchBox` 求值为 undefined，再取 `.checked`
                        // 就抛：
                        //     TypeError: Cannot read property 'checked' of undefined
                        // 该异常发生在 onClicked 内，会**中断整个处理函数** ——
                        // `createSubjectFromSource` 根本没被调用、弹窗也没关，
                        // 表现就是"点了「新建并绑定」完全没反应"。
                        // QML 的 id 在**同一组件内**（含嵌套子项）直接可见，
                        // 去掉前缀即可。
                        rss.createSubjectFromSource(
                            root.bindingId, newSubjectField.text.trim(),
                            matchBox.checked)
                        bindDialog.close()
                    }
                }
            }

            CheckBoxLine {
                id: matchBox
                objectName: "matchOnCreateBox"
                // 默认勾上（用户要求"需要匹配"）；没 Token 时后端会跳过并提示，
                // 这里不禁用 —— 免得用户以为"匹配坏了"
                checked: true
                text: "新建时匹配 Bangumi（拉取封面、集数、tag）"
            }

            Row {
                anchors.right: parent.right
                spacing: Theme.spacingSm

                AppButton {
                    text: "解除绑定"
                    visible: root.currentBindingName() !== ""
                    onClicked: {
                        if (typeof rss !== "undefined" && rss)
                            rss.linkSubject(root.bindingId, 0)
                        bindDialog.close()
                    }
                }
            }
        }
    }

    // ---- 下载器弹窗（标题过滤 + 保存到指定条目目录）----
    RssDownloaderDialog {
        id: downloaderDialog
        objectName: "rssDownloaderDialog"
    }

    // ---- 删除确认弹窗 ----
    Dialog {
        id: deleteDialog
        modal: true
        anchors.centerIn: parent
        width: 360
        padding: Theme.spacingLg
        title: "删除订阅"

        property int targetId: 0
        property string targetName: ""

        background: Rectangle {
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: Theme.border
            radius: Theme.radiusMd
        }

        contentItem: Column {
            spacing: Theme.spacingMd

            Text {
                width: parent.width
                text: "确定删除订阅「" + deleteDialog.targetName + "」？\n"
                      + "其下载记录也会一并删除。"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
            }

            Row {
                anchors.right: parent.right
                spacing: Theme.spacingSm

                AppButton {
                    text: "取消"
                    onClicked: deleteDialog.close()
                }

                AppButton {
                    text: "删除"
                    // 不可逆操作用**红色**（与媒体库删除动漫一致）：
                    // 原先写 "normal"（中性灰），危险动作看起来太"温和"，
                    // 用户容易顺手点下去
                    variant: "danger"
                    onClicked: {
                        if (typeof rss !== "undefined" && rss)
                            rss.removeSource(deleteDialog.targetId)
                        deleteDialog.close()
                    }
                }
            }
        }
    }

    // ---- 逻辑 ----
    /// 保存表单（新增或更新）。
    ///
    /// **参数由调用方传入**（表单组件的 submitted 信号带过来），不再自己去
    /// 读 `nameField.text` —— 表单已抽成 `SubscriptionForm`，它的内部 id
    /// 对外不可见（QML 的 id 不是公开属性）。这样新增与编辑共用同一个保存
    /// 逻辑，只有 `id` 不同。
    function saveForm(id, name, url, rule) {
        if (typeof rss === "undefined" || !rss)
            return
        if (id > 0)
            rss.updateSource(id, name, url, rule, true)
        else
            rss.addSource(name, url, rule)
        root.addFormVisible = false
        root.editingId = 0
    }

    function confirmDelete(item) {
        deleteDialog.targetId = item.id
        deleteDialog.targetName = item.name
        deleteDialog.open()
    }

    function currentBindingName() {
        for (var i = 0; i < root.sources.length; i++) {
            if (root.sources[i].id === root.bindingId)
                return root.sources[i].subjectName
        }
        return ""
    }

    /// 按 id 取订阅源（「新建条目」里需要它的 url 去抓名称）
    function sourceById(id) {
        for (var i = 0; i < root.sources.length; i++) {
            if (root.sources[i].id === id)
                return root.sources[i]
        }
        return null
    }

    function fmtIndex(v) {
        return (Math.round(v * 100) / 100).toString()
    }

    /// 关键词是否命中该条目（`q` 已小写化）。
    /// 字段口径与海报墙一致：标题 / 原名 / 中文名 / 系列名 / 别名。
    function subjectMatches(item, q) {
        if (!item)
            return false
        function has(v) {
            return String(v === undefined || v === null ? "" : v)
                       .toLowerCase().indexOf(q) >= 0
        }
        return has(item.title) || has(item.name) || has(item.nameCn)
            || has(item.seriesName) || has(item.aliases)
    }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }

    // 桥接层消息 → 状态栏
    Connections {
        target: typeof rss !== "undefined" && rss ? rss : null
        function onMessage(text) { root.statusMessage(text) }
        function onFailed(msg) { root.statusMessage(msg) }
        // 推断出的番名 → 自动填入「名称」框。
        //
        // **三个地方都要填**（：`suggestSubjectName` 有**三个**调用方 ——
        //   ① 添加订阅表单的「获取」  → addForm
        //   ② 编辑订阅表单的「获取」  → editForm
        //   ③ 绑定弹窗里的「获取」    → newSubjectField
        // 信号本身不带"是谁发起的"，所以这里**全部写一遍**：同一时刻
        // 只有一处可见，写另外两处没有副作用（表单下次打开会被
        // `setValues` 重置；`newSubjectField` 也在打开绑定弹窗时清空）。
        //
        // 早期这里只写了前两个，于是③那条路径抓到了名字（状态栏也提示
        // "已获取名称：…"）却**填不进输入框** —— 用户以为"获取"没生效。
        //
        // ---- 逐个 try，绝不让一个失败拖垮后面的 ----
        //
        // **致命踩坑**：`editForm` 声明在
        // 下面 `Repeater` 的 **delegate 内部**（每个订阅卡片各一份）。这意味着：
        //   ① 它在 delegate 作用域里 —— 根作用域的 Connections 访问不到；
        //   ② **一个订阅都没有时，delegate 根本不会被创建**，`editForm`
        //      就是 `undefined`。
        // 于是 `editForm.setNameOnly(name)` 抛 `TypeError`，**中断整个
        // handler** —— 排在它后面的 `bindDialog.setName()` 永远执行不到。
        // 现象正是用户报的："小窗获取依旧没有生效"（状态栏却正常显示
        // "已获取名称"，因为那是另一个信号 onMessage 干的）。
        //
        // 修法：**每个目标各自 try 包裹**。填不上某一个不影响其余，
        // 语义上也确实如此 —— 三个目标互不依赖，一个不存在不该拖垮全部。
        function onTitleSuggested(sourceId, name) {
            try { addForm.setNameOnly(name) } catch (e) { /* 表单还没建 */ }
            try { editForm.setNameOnly(name) } catch (e) { /* delegate 未创建 */ }
            // 必须走 bindDialog.setName()，不能直接写 `newSubjectField.text`
            // —— 那个 id 对根作用域不可见（见 bindDialog 里的说明）。
            try { bindDialog.setName(name) } catch (e) { /* 同上 */ }
        }
    }

    // 切进本页时拉一次 qBittorrent 进度。
    //
    // **为什么需要**：下载进度是"实时变化的量"，而本页的数据源
    // （`library`/`rss` 的 Property）只在变更时通知 —— 用户切到别的页
    // 看了一会儿再回来，进度还停在离开时的值（可能已从 30% 变成 90%）。
    onVisibleChanged: {
        if (visible && typeof rss !== "undefined" && rss)
            rss.refreshTorrents()
    }

    // ---- 下载进度自动刷新----
    //
    // **只在"记录面板打开 且 本页可见"时运行**（running 绑定那两个条件）：
    // 不这么收紧的话，用户在别的页面看番时也会每 5 秒去问一次
    // qBittorrent，纯属浪费（而且日志里会一直有请求痕迹）。
    //
    // 刷新本身是**异步**的（`_TorrentStatusWorker`），不会卡界面；
    // 若上一次还没回来，`refreshTorrents` 内部会直接跳过本次触发
    // （见那里的重入保护），所以定时器设短一点也不会堆积请求。
    Timer {
        id: progressTimer
        interval: 5000
        repeat: true
        running: root.showDownloads && root.visible
        onTriggered: {
            if (typeof rss !== "undefined" && rss)
                rss.refreshTorrents()
        }
    }
}
