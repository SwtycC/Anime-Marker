import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 应用主窗口（QML 版）。
//
// 结构：
//   ApplicationWindow
//    ├─ ColumnLayout（占满）
//    │   ├─ StackLayout      五页内容（自适应高度）
//    │   └─ StatusBar        底部状态栏（真正占布局，高 28px）
//    └─ NavBar（overlay）    悬浮于内容之上，不占布局空间
//
// 两个容易混淆的"底部元素"：
//   - StatusBar 是**真正的底栏**，进布局流，把内容区高度顶掉 28px
//   - NavBar 是 **overlay**，浮在内容区底部，不挤压内容
// 二者位置不冲突：导航距内容区底边 navBottomMargin(20)，
// 内容区底边又在 StatusBar 之上。
//
// 导航栏定位方式：不用 anchors.bottom（那会进普通布局流），
// 而是手动计算 x/y，从而真正做到"浮在内容之上且不挤压内容"。
ApplicationWindow {
    id: window

    // 初始尺寸由 Python 算好后注入（`windowStartup`，见 QmlApp.run 的说明）。
    //
    // **为什么不写死**（踩坑：启动瞬间下半截黑边）：窗口一旦 `visible: true`
    // 就会立刻渲染首帧，而 Python 侧改尺寸是在 QML 加载**之后**才做的。
    // 早期这里写死 1120×720、随后被改到 860 高，新长出来的那 140px
    // 来不及绘制 → 下半截先显示为黑色（实测反馈："打开的一瞬间下半部分有黑边"）。
    // 现在尺寸随主题一起在 load 之前注入，首帧就是最终尺寸。
    //
    // 兜底：注入缺失时（如组件被单独加载、或注入那段被误改）回退到
    // 原先的一组安全值，`_fit_window_to_columns()` 仍会在加载后纠正。
    readonly property var _startupSize:
        (typeof windowStartup !== "undefined" && windowStartup)
        ? windowStartup
        : ({ "width": 1120, "height": 720, "x": -1, "y": -1 })

    width: _startupSize.width
    height: _startupSize.height
    // 位置也一起注入（Python 那边按屏幕可用区居中算好）。
    // 为负 = "拿不到屏幕信息"，此时不设 x/y，交给窗口管理器自己摆。
    x: _startupSize.x >= 0 ? _startupSize.x : x
    y: _startupSize.y >= 0 ? _startupSize.y : y
    minimumWidth: 900
    minimumHeight: 560
    visible: true
    title: "Anime Marker"

    // 窗口底色。
    // 注意：ApplicationWindow 的 `color` 只影响 window 自身，其 contentItem
    // 仍可能带默认浅色背景 —— 表现在**滚动条透明轨道区域**会透出浅灰
    // （实测右边缘像素 #f3f3f3，视觉上是深色界面右侧一条白边）。
    // 因此再给 `background` 显式铺一层同色底（background 位于 contentItem 之下）。
    color: Theme.windowBg
    background: Rectangle { color: Theme.windowBg }

    // ---- 当前页索引（供外部桥接层读写）----
    property int currentPage: 0

    // 详情页的「来源页」：从哪进来，退出时就退回哪
    //
    // 详情页在**内层** browseStack 的第 1 格，而海报墙 / 收藏页 / 动态页是外层
    // pageStack 的平级页。所以"返回"要恢复**两处位置**：
    //   内层 browseStack → 0（回到海报墙那一格）
    //   外层 currentPage → 当初进来的那一页
    // 早期只写了内层，于是无论从哪进详情，退出后都落到海报墙（实测反馈）。
    //
    // 入口处统一写 `detailOriginPage = window.currentPage`（而不是写死 0/1/2），
    // 这样以后新增入口也不用改返回逻辑。
    property int detailOriginPage: 0

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ============ 内容区 ============
        // 海报墙与详情页共用一层子栈：详情不是独立的一级导航页，
        // 从海报墙点进去、返回后仍回到海报墙（与旧版 MainWindow.browse_stack 一致）。
        //
        // **踩坑（两层栈：改了内层却看不见）**：详情页在**内层** `browseStack` 里，
        // 而动态页 / 收藏页是外层 `pageStack` 的**平级页**。从它们那里只写
        //     browseStack.currentIndex = 1
        // 是**完全没有反应**的 —— 外层还停在原来的页面，内层切到哪都看不见。
        // 海报墙不受影响（外层本来就在 browseStack 上），所以这个 bug 只在
        // "从动态页 / 收藏页进详情"时暴露（实测反馈："入库的动漫点击没反应"）。
        // 正确写法是两句一起写：
        //     browseStack.currentIndex = 1   // 内层：显示详情页
        //     window.currentPage = 0         // 外层：切回 browseStack 所在的那一层
        // 之所以赋给 `window.currentPage` 而不是 `pageStack.currentIndex`：
        // 后者本身是 `currentIndex: window.currentPage` 的绑定，
        // 直接赋值会**永久断开该绑定**（同类坑见 TimelinePage 里 entries 的注释）。
        StackLayout {
            id: pageStack
            objectName: "pageStack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: window.currentPage

            StackLayout {
                id: browseStack
                objectName: "browseStack"
                currentIndex: 0

                PosterWallPage {
                    id: wallPage
                    onSubjectClicked: function (subjectId) {
                        detailOriginPage = window.currentPage    // 记住来源页
                        detailPage.load(subjectId)
                        browseStack.currentIndex = 1
                    }
                }

                DetailPage { id: detailPage; objectName: "detailPage" }
            }

            InProgressPage {
                id: inProgressPage
                // 在看页「详情」按钮 / 整行点击 → 复用海报墙的详情页
                onSubjectClicked: function (subjectId) {
                    detailOriginPage = window.currentPage    // 必须在切页**之前**记
                    detailPage.load(subjectId)
                    browseStack.currentIndex = 1
                    // **必须同时把外层栈切到 browseStack**（见下方踩坑）
                    window.currentPage = 0
                }
                // 未入库的行点不出详情 —— 由页面发提示
                onStatusMessage: function (text) {
                    statusBar.setMessage(text, 5000)
                }
                // 「下一集」→ 交给播放器（与详情页同一条路径）
                onPlayEpisode: function (episodeId) {
                    if (typeof player !== "undefined" && player)
                        player.playEpisode(episodeId)
                }
            }

            TimelinePage {
                id: timelinePage
                objectName: "timelinePage"
                // 「上传」→ 打开补传小窗（见 UploadDialog.qml）
                onUploadRequested: uploadDialog.open()
                onSubjectClicked: function (subjectId) {
                    if (subjectId <= 0)
                        return
                    detailOriginPage = window.currentPage    // 必须在切页**之前**记
                    detailPage.load(subjectId)
                    browseStack.currentIndex = 1
                    // **必须同时把外层栈切到 browseStack**（见下方踩坑）
                    window.currentPage = 0
                }
                // 未入库的行点不出详情 —— 由页面发提示（"未入库，无法打开详情"）
                onStatusMessage: function (text) {
                    statusBar.setMessage(text, 5000)
                }
            }

            SubscriptionPage {
                id: subscriptionPage
                onStatusMessage: function (text) {
                    statusBar.setMessage(text, 5000)
                }
            }

            SettingsPage     { id: settingsPage; objectName: "settingsPage" }
        }

        // ============ 底部状态栏（真正占布局）============
        StatusBar {
            id: statusBar
            objectName: "statusBar"
            Layout.fillWidth: true
            version: appVersion
            message: "就绪"
        }
    }

    // 切页时的按需刷新
    //
    // 页面索引：0=海报墙/详情  1=收藏  2=动态  3=订阅  4=设置
    //
    // 所有跨上下文调用都做存在性判断：`library` / `rss` 等是 Python 注入的
    // 上下文属性，单独加载本文件（组件预览、静态检查、诊断脚本）时并不存在，
    // 直接调用会抛 TypeError 并中断整个 onCurrentPageChanged 的后续逻辑。
    onCurrentPageChanged: {
        // 设置页：重新读取配置（避免外部改动后显示旧值）
        if (currentPage === 4) {
            settingsPage.refresh()
            return
        }
        // 动态页：时间线不是 Property（library.timeline() 是普通 Slot），
        // 不会自动通知，因此每次进入都重取一次，保证刚看完的集能立刻出现。
        //
        // 注意：**必须调 timelinePage.reload()**，不能写
        // `timelinePage.entries = ...` —— 后者会断开 entries 的绑定
        // （详见 TimelinePage.qml 的注释）。
        if (currentPage === 2) {
            timelinePage.reload()
            return
        }
        // 订阅页：刷新订阅源列表（可能在别处改动过）
        if (currentPage === 3) {
            if (typeof rss !== "undefined" && rss)
                rss.reload()
            return
        }
        // 收藏页：切进来时刷新一下缓存状态（不自动联网，
        // 拉取仍需用户点「刷新」按钮 —— 避免进页面就发请求）
        if (currentPage === 1) {
            if (typeof library !== "undefined" && library)
                library.reloadInProgress()
        }
    }

    // 详情页返回海报墙
    //
    // 性能：这里**不能**调 library.reload()。
    // relaod 会把缓存标记为脏（_dirty=true）并 emit subjectsChanged，
    // 触发 Repeater 销毁并重建**全部**卡片 —— 条目多时（上百个）
    // 每题都要重新解码封面、重建 QQuickItem，表现为「返回海报墙卡顿 1~2 秒」。
    // 详情页的操作（播放、标记看过）不会改变海报墙需要的字段
    // （标题 / 封面 / 集数），因此直接切回即可，无需刷新。
    Connections {
        target: detailPage
        function onBackRequested() {
            browseStack.currentIndex = 0
            window.currentPage = detailOriginPage   // 回到当初进来的那一页
        }
    }

    // ============ 详情页 → 播放 / 重新匹配 ============
    Connections {
        target: detailPage

        function onPlayEpisode(episodeId) {
            if (typeof player !== "undefined" && player)
                player.playEpisode(episodeId)
        }

        function onRematchRequested(subjectId) {
            var subj = typeof library !== "undefined" && library
                       ? library.subject(subjectId) : null
            matchDialog.open(subjectId, subj ? subj.title : "")
        }

        function onPosterRequested(subjectId) {
            var subj = typeof library !== "undefined" && library
                       ? library.subject(subjectId) : null
            posterDialog.open(subjectId, subj ? subj.title : "")
        }

        // 标签编辑的提示 → 底部浮条。
        //
        // 为什么走浮条而不是状态栏：状态栏在窗口最底部、字小色淡，用户
        // 注意力在标签行上，看不到；浮条浮在内容上方且能变琥珀色
        // （warn=true，如"该标签已存在"），一眼可见。
        function onStatusMessage(text, warn) {
            banner.show(text, warn)
        }
    }

    // 标签在别处变更（自动补拉完成 / 外部修改）→ 刷新详情页数据。
    //
    // **注意**：这里必须用 `reloadTagRows()`（只重取数据）而不是
    // `resetTagState()`（重取 + 退出编辑态）。
    // 因为 addUserTag / toggleApiTag 每次操作都会写库 → 后端 emit
    // tagsChanged → 若这里调 resetTagState，编辑态会被**立刻踢出**
    // （实测：点一次 ✔ 加完 tag，编辑面板就自己关了）。
    Connections {
        target: typeof library !== "undefined" && library ? library : null
        function onTagsChanged(subjectId) {
            if (detailPage.subjectId === subjectId)
                detailPage.reloadTagRows()
        }
        function onStatusMessage(text) {
            banner.show(text)
        }
    }

    // ============ 上传对话框（本地观看记录 → Bangumi）============
    UploadDialog {
        id: uploadDialog

        // 上传成功后动态页要重取（传过的集会从「仅本地」变成带 bgm 标记）
        onVisibleChanged: {
            if (!visible)
                timelinePage.reload()
        }
    }

    // ============ 手动匹配对话框 ============
    MatchDialog {
        id: matchDialog

        // 应用成功后刷新详情页与海报墙
        onApplied: function (subjectId) {
            detailPage.load(subjectId)
            library.reload()
            statusBar.setMessage("已手动指定 Bangumi 条目", 5000)
        }
    }

    // ============ 更换海报对话框 ============
    PosterDialog {
        id: posterDialog

        // 更换/恢复成功后：详情页重取（新海报立即生效）。海报墙不用在这里
        // reload —— LibraryBridge._notify_cover_changed 已发 subjectsChanged，
        // QML 会自动重取 subjects。
        onApplied: function (subjectId, message) {
            if (detailPage.subjectId === subjectId)
                detailPage.load(subjectId)
            statusBar.setMessage(message, 5000)
        }

        // 关窗时补一次刷新。
        //
        // 为什么需要：小窗是**独立顶层窗口**，编辑期间用户可能把它拖到一边
        // 继续看主窗口 —— 此时 detailPage 的封面已经过期（仍是旧图），
        // 而 onApplied 只在操作成功那一瞬间刷新过。关窗补刷一次，保证
        // 「关掉小窗后看到的一定是当前海报」。
        // 用 subjectId 而不是 detailPage.subjectId 判定：小窗可能在用户
        // 切到别的条目之后才关闭，此时不该动详情页。
        onVisibleChanged: {
            if (!visible && detailPage.subjectId === subjectId && subjectId > 0)
                detailPage.load(subjectId)
        }
    }

    // ============ 播放 / 监控 → 状态栏 ============
    Connections {
        target: typeof player !== "undefined" && player ? player : null

        function onMessage(text) {
            statusBar.setMessage(text, 6000)
        }

        function onFailed(msg) {
            statusBar.setMessage("播放失败：" + msg, 8000)
        }

        function onWatched(episodeId) {
            // 自动标记成功后刷新详情页集数列表（打勾状态）
            if (detailPage.subjectId > 0)
                detailPage.load(detailPage.subjectId)
            // 动态页的数据源不是 Property，这里显式重取一次
            // （本次新增了一条观看记录）。注意用 reload() 而不是赋值 entries。
            timelinePage.reload()
        }

        function onProgressChanged(episodeId, progress) {
            // 播放中：把进度显示在状态栏，替代旧的进度条语义
            if (progress > 0)
                statusBar.setMessage(
                    "播放中 " + Math.round(progress * 100) + "%")
        }
    }

    // 鼠标后侧键（XButton1）= "返回上一层"，两处语义：
    //   详情页       → 回到进来的那一页（同 onBackRequested）
    //   海报墙 · 搜索中 → 退出搜索，回到整墙（同浏览器的"返回"退出搜索页）
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.BackButton

        readonly property bool onDetail: window.currentPage === 0
                                         && browseStack.currentIndex === 1
        readonly property bool onWallSearch: window.currentPage === 0
                                             && browseStack.currentIndex === 0
                                             && wallPage.searchActive

        // 只在"确实能返回"时拦截，否则会吃掉正常点击 / 抢别的按键语义。
        //
        // 两个条件都必须带 `window.currentPage === 0`：browseStack 只在外层第 0 页
        // 可见，从详情页用底部导航切到别的页时 currentIndex 仍停在 1，
        // 只判 currentIndex 会在"设置页按后侧键"这类场景里误触发（实测踩到）。
        enabled: onDetail || onWallSearch

        onClicked: {
            if (onDetail) {
                // 不调 library.reload()，避免重建全部卡片
                browseStack.currentIndex = 0
                window.currentPage = detailOriginPage   // 同 onBackRequested
            } else if (wallPage.filterOpen) {
                // 筛选面板开着 → 先收面板。**不能顺手把搜索词也清了**：
                // 用户只是开了下面板，关键词还得留着。
                wallPage.closeFilterPanel()
            } else {
                wallPage.clearSearch()
            }
        }
    }

    // ============ 悬浮胶囊导航（overlay）============
    NavBar {
        id: nav
        objectName: "navBar"

        // 关键：不用 anchors，手动定位 → 不占据布局空间。
        // NavBar 自身含 _shadowPad 阴影留白，定位时要把这部分补偿回来，
        // 使「胶囊实体」距**内容区底边**（= 状态栏顶边）正好 navBottomMargin。
        // 注意：不能再用 window.height 作为基准 —— 底部多了 28px 状态栏，
        // 直接用窗口高会把胶囊压在状态栏上。
        readonly property int _pad: 12
        x: Math.round((window.width - width) / 2)
        y: Math.round(pageStack.height - height - Theme.navBottomMargin + _pad)

        currentIndex: window.currentPage
        onItemClicked: function (index) {
            window.currentPage = index
        }

        // 入场淡入：用 NumberAnimation 主动播放，而不是
        // `opacity: 0` + `Component.onCompleted: opacity = 1` ——
        // 后者在窗口尚未显示时完成，且赋值会破坏绑定，实测停在 0 导致整条导航不可见。
        opacity: 1
        NumberAnimation on opacity {
            from: 0
            to: 1
            duration: Theme.durSlow
            easing.type: Easing.OutCubic
            running: true
        }
    }

    // 窗口尺寸变化时，导航栏自动重新居中（x/y 绑定已保证，此处仅确保层级）
    onWidthChanged: nav.z = 100
    Component.onCompleted: nav.z = 100

    // ============ 扫描 → 状态栏 ============
    Connections {
        target: typeof scanner !== "undefined" && scanner ? scanner : null

        function onProgressChanged(current, total) {
            statusBar.setProgress(current, total)
        }

        function onLogMessage(msg) {
            statusBar.setMessage(msg)
        }

        function onFinished(matched, pending) {
            statusBar.stopProgress()
            statusBar.setMessage(
                "扫描完成：匹配 " + matched + " 个，待确认 " + pending + " 个", 8000)
        }

        function onFailed(msg) {
            statusBar.stopProgress()
            statusBar.setMessage("扫描失败：" + msg, 8000)
        }
    }

    // ============ 设置页消息 → 状态栏 ============
    Connections {
        target: settingsPage
        function onStatusMessage(text) {
            statusBar.setMessage(text, 5000)
        }
    }

    // ============ 在看列表拉取 → 状态栏 ============
    Connections {
        target: typeof inprogress !== "undefined" && inprogress ? inprogress : null

        function onMessage(text) {
            statusBar.setMessage(text, 5000)
        }

        function onFailed(msg) {
            statusBar.setMessage("在看列表：" + msg, 8000)
        }
    }

    // ---- 对外接口（供 Python 桥接层调用）----
    function gotoPage(index) {
        window.currentPage = index
    }

    /**
     * 应用主题（界面加载完成后由 Python 侧调用，做一次复核）。
     *
     * 参数：
     *   isDark       —— true 深色 / false 白色简约
     *   accentColor  —— 主题色 "#RRGGBB"
     *
     * 说明：Theme 是 QML 单例，Python 无法直接拿到实例，故从这里转发。
     * **首帧的主题不靠这里** —— 那时已经画出去了；启动主题由 Python 在加载
     * QML 之前注入 `themeStartup`，Theme 单例创建时就已初始化（见 Theme.qml
     * 的 applyStartupTheme 与 QmlApp.run 里的说明）。
     */
    function applyTheme(isDark, accentColor) {
        Theme.dark = isDark
        if (accentColor && accentColor.length > 0)
            Theme.applyAccent(accentColor)
    }

    /** 读取当前主题状态（供桥接层回写配置时参考）。 */
    function currentTheme() {
        return { "dark": Theme.dark, "accent": String(Theme.accentSource) }
    }

    /** 诊断用：直接设置状态栏进度与日志（截图核对用）。 */
    function debugSetProgress(current, total, message) {
        statusBar.setProgress(current, total)
        if (message !== undefined)
            statusBar.setMessage(message)
    }

    /** 诊断用：滚动设置页到指定位置（截图核对用）。 */
    function debugScrollSettings(y) {
        settingsPage.scrollTo(y)
    }

    /** 诊断用：按控件 id 设置设置页的值（自动化测试用）。 */
    function debugSetField(fieldId, value) {
        return settingsPage.debugSet(fieldId, value)
    }

    /** 诊断用：触发表单保存，返回是否成功。 */
    function debugSaveSettings() {
        return settingsPage.save()
    }

    /** 诊断用：滚动海报墙到指定位置。 */
    function debugScrollWall(y) {
        wallPage.scrollTo(y)
    }

    /** 诊断用：给海报墙搜索框填关键词（截图核对过滤结果用）。返回命中条数。 */
    function debugSearchWall(text) {
        wallPage.searchText = text
        return wallPage.matchCount
    }

    /** 诊断用：打开指定条目的详情页（截图核对用）。 */
    function debugOpenDetail(subjectId) {
        detailOriginPage = window.currentPage
        detailPage.load(subjectId)
        browseStack.currentIndex = 1
        window.currentPage = 0
        return detailPage.subjectId
    }

    /** 诊断用：打开更换海报小窗（截图核对用）。 */
    function debugOpenPosterDialog(subjectId, title) {
        posterDialog.open(subjectId, title || "")
        return posterDialog.visible
    }

    /** 诊断用：导出设置页的行布局几何，便于核对 FormRow 是否正常撑开。 */
    function debugSettings() {
        var rows = []
        function findRows(item, depth) {
            if (depth > 8 || !item || !item.children)
                return
            for (var i = 0; i < item.children.length; i++) {
                var c = item.children[i]
                var tn = c.toString()
                if (tn.indexOf("FormRow") >= 0) {
                    rows.push({
                        "label": c.label,
                        "y": Math.round(c.y),
                        "w": Math.round(c.width),
                        "h": Math.round(c.height)
                    })
                }
                findRows(c, depth + 1)
            }
        }
        findRows(settingsPage, 0)
        // 顺带导出设置页里的 ColumnLayout 宽度链，定位宽度在哪一层断掉
        var widths = []
        function findColumns(item, depth) {
            if (depth > 6 || !item || !item.children)
                return
            for (var i = 0; i < item.children.length; i++) {
                var c = item.children[i]
                var tn = c.toString()
                if (tn.indexOf("ColumnLayout") === 0 || tn.indexOf("QQuickFlickable") === 0) {
                    widths.push({
                        "type": tn.indexOf("Flickable") === 0 ? "Flickable" : "ColumnLayout",
                        "w": isNaN(c.width) ? -1 : Math.round(c.width),
                        "implicitW": isNaN(c.implicitWidth) ? -1 : Math.round(c.implicitWidth)
                    })
                }
                findColumns(c, depth + 1)
            }
        }
        findColumns(settingsPage, 0)
        return {
            "settingsPageHeight": Math.round(settingsPage.height),
            "widths": widths,
            "rowCount": rows.length,
            "rows": rows
        }
    }

    /** 诊断用：导出海报墙 Flow 的几何数据，便于脚本核对布局。 */
    function debugWall() {
        var out = []
        var kids = wallPage.children
        // 逐层找到 Flow
        function findFlow(item) {
            if (!item || !item.children)
                return null
            for (var i = 0; i < item.children.length; i++) {
                var c = item.children[i]
                if (c.toString().indexOf("QQuickFlow") === 0)
                    return c
                var r = findFlow(c)
                if (r)
                    return r
            }
            return null
        }
        var flow = findFlow(wallPage)
        if (!flow)
            return { "error": "Flow not found" }
        var cards = []
        for (var j = 0; j < flow.children.length; j++) {
            var cd = flow.children[j]
            if (cd.width === undefined)
                continue
            cards.push({ "x": Math.round(cd.x), "y": Math.round(cd.y),
                         "w": Math.round(cd.width), "h": Math.round(cd.height) })
        }
        return {
            "flowWidth": Math.round(flow.width),
            "flowHeight": Math.round(flow.height),
            "flowImplicitHeight": Math.round(flow.implicitHeight),
            "spacing": Theme.posterSpacing,
            "cards": cards
        }
    }

    /** 诊断用：导出 Theme 的关键派生色，便于脚本/日志核对。 */
    function debugTheme() {
        return {
            "dark": Theme.dark,
            "accent": String(Theme.accent),
            "accentSoft": String(Theme.accentSoft),
            "accentText": String(Theme.accentText),
            "accentHover": String(Theme.accentHover),
            "hoverFill": String(Theme.hoverFill),
            "surfaceBg": String(Theme.surfaceBg),
            "windowBg": String(Theme.windowBg),
            "textPrimary": String(Theme.textPrimary),
            "accentIsLight": Theme.isLight(Theme.accentSource)
        }
    }

    // ---- 主题改动持久化 ----
    // 注意：必须由「用户操作」触发写入，不能用 Theme.onDarkChanged，
    // 否则启动时 applyTheme() 赋值会立刻把配置覆盖一遍。
    Connections {
        target: settingsPage

        function onAccentPicked(value) {
            persistTheme()
        }

        function onThemeModePicked(isDark) {
            persistTheme()
        }
    }

    function persistTheme() {
        if (typeof themeBridge === "undefined" || !themeBridge)
            return
        themeBridge.saveThemeMode(Theme.dark ? "dark" : "light")
        themeBridge.saveAccent(String(Theme.accentSource))
    }

    // ---- 设置页状态提示（底部浮条，3 秒后自动消失）----
    Connections {
        target: settingsPage
        function onStatusMessage(text) {
            banner.show(text)
        }
    }

    Rectangle {
        id: banner
        objectName: "statusBanner"

        // 提示条有两种语气：
        //   normal —— 主题色底（"已保存"这类中性/成功信息）
        //   warn   —— 琥珀色底（"该标签已存在"这类需要注意但不致命的提示）
        // 用 `warn` 而不是 `dangerColor`：重复添加不是错误，红字会显得过重。
        property bool warn: false

        // 位置：**窗口顶部居中**，浮在内容之上，不占布局空间。
        //
        // 位置取舍（三次调整，记下结论）：
        //   ① 最初在底部导航正上方 —— 与高对比的胶囊导航挤成一团，
        //      既抢注意力、又像是导航的一部分（实测反馈"和导航栏重叠"）。
        //   ② 搬到右上角 —— 会压住详情页右上角的「更换海报 / 重新匹配」
        //      按钮（实测截图确认）。各页面右上角基本都有操作按钮，
        //      这里并不空。
        //   ③ 现在放在**顶部居中** —— 该区域在各页面都是空白：标题左对齐、
        //      操作按钮靠右，中间这条带子没人占用。
        //
        // 为什么不贴顶：贴着窗口边缘会有"被裁切"的观感，留 `_topMargin`
        // 让它落在标题行上方的空白带里。
        readonly property int _pad: 12
        readonly property int _topMargin: 14
        x: Math.round((window.width - width) / 2)
        y: _topMargin

        width: bannerText.implicitWidth + Theme.spacingXl * 2
        height: 38
        radius: Theme.radiusSm
        // 琥珀色不放进 Theme：只在提示条这一处用，加进主题表反而增加
        // 维护面（且 warningColor 是为深色背景调过的，做底色偏暗）
        color: banner.warn
               ? (Theme.dark ? "#3A2E12" : "#FFF4D6")
               : Theme.accentSoft
        border.width: Theme.lineThin
        border.color: banner.warn
                      ? (Theme.dark ? "#8A6D1F" : "#E0B84C")
                      : Theme.accent
        opacity: 0
        visible: opacity > 0.01
        z: 200

        // 出现时从上方轻微滑入（配合顶部位置的"弹出"观感）
        transform: Translate {
            y: banner.opacity > 0.5 ? 0 : -8
            Behavior on y {
                NumberAnimation { duration: Theme.durNormal; easing.type: Easing.OutCubic }
            }
        }

        Behavior on opacity { NumberAnimation { duration: Theme.durNormal } }

        Text {
            id: bannerText
            anchors.centerIn: parent
            color: banner.warn
                   ? (Theme.dark ? "#F0D48A" : "#7A5B08")
                   : Theme.accent
            font.pixelSize: Theme.fontMd

            Behavior on color { ColorAnimation { duration: Theme.durNormal } }
        }

        Timer {
            id: bannerTimer
            // 停留 3 秒后淡出（淡出动画本身再加 durNormal=200ms）。
            // 用 restart() 而非 start()：连续触发时重新计时，避免第二条
            // 提示被前一条的计时器提前关掉。
            interval: 3000
            onTriggered: banner.opacity = 0
        }

        /// 显示提示。`warn` 为 true 时用琥珀色（重复 / 需注意）。
        function show(text, warn) {
            banner.warn = warn === true
            bannerText.text = text
            banner.opacity = 1
            bannerTimer.restart()
        }
    }

    // ---- 扫描进度反馈：写进窗口标题（QML 无状态栏，暂用标题承载）----
    Connections {
        target: typeof scanner !== "undefined" && scanner ? scanner : null

        function onRunningChanged() {
            if (scanner.running)
                banner.show("开始扫描…")
        }

        function onLogMessage(msg) {
            banner.show(msg)
        }

        function onFinished(matched, pending) {
            banner.show("扫描完成：匹配 " + matched + " 个，待确认 " + pending + " 个")
            library.reload()
        }

        function onFailed(msg) {
            banner.show("扫描失败：" + msg)
        }
    }
}
