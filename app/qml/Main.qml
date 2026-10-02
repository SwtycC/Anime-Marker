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
                    // 右下角「添加动漫」悬浮按钮 → 打开小窗
                    onAddAnimeRequested: addAnimeDialog.open()
                }

                DetailPage {
                    id: detailPage
                    objectName: "detailPage"
                    // 「删除」→ 先弹确认框（不可逆操作，必须二次确认）
                    onDeleteRequested: function (subjectId) {
                        deleteDialog.ask(subjectId,
                                         detailPage.subject.title || "")
                    }
                }
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
        // `action` 非空（如 "settings"）时浮条可点击跳转（见 banner.show）。
        function onStatusMessage(text, warn, action) {
            banner.show(text, warn, action)
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

    // ============ 添加动漫对话框 ============
    //
    // 流程：小窗选目录 → 转发给 scanner.addFolder(path, match)
    //      → 后端单目录扫描入库
    //      → scanner.subjectAdded(id) → 关窗 + 进详情页（并刷新海报墙）
    //      → scanner.failed(msg)      → 窗内红字
    AddAnimeDialog {
        id: addAnimeDialog

        // 用户点了「添加」：把请求交给后端。
        //
        // 后端返回结构化结果（见 ScannerBridge.addFolder）：
        //   ok=true                        → 已在扫描，等 subjectAdded
        //   exists=true                    → 该番**已存在**：关窗 + 主题色提示
        //                                    （可点击跳转到该条目）
        //   message=<str>                  → 其它失败，窗内红字
        onSubmitted: function (folder, match) {
            if (typeof scanner === "undefined" || !scanner) {
                addAnimeDialog.onResult(false, "扫描服务不可用")
                return
            }
            var r = scanner.addFolder(folder, match)
            if (r && r.ok === true)
                return
            if (r && r.exists === true) {
                // 「已存在」不是错误：用**主题色**提示条（warn=false），
                // 并带上 action="subject" + 条目 id，点一下直达该动漫。
                addAnimeDialog.close()
                banner.show("「" + (r.name || "该动漫") + "」已在库中 · 点击查看",
                            false, "subject", r.subjectId || 0)
                return
            }
            addAnimeDialog.onResult(false,
                (r && r.message) ? r.message : "添加失败")
        }
    }

    // 「添加动漫」完成 → 关窗并直接进该条目的详情页
    //
    // 说明为什么用 `browseStack.currentIndex = 1` + `currentPage = 0`：
    // 与从海报墙点卡片进详情是**同一条路径**（详情页在内层子栈里，
    // 外层必须切回 browseStack 所在那一页，见文件头「两层栈」的说明）。
    Connections {
        target: typeof scanner !== "undefined" && scanner ? scanner : null
        function onSubjectAdded(subjectId) {
            if (!addAnimeDialog.visible)
                return                    // 不是本小窗发起的（如全量扫描）
            addAnimeDialog.close()
            if (subjectId > 0) {
                library.reload()          // 先刷新列表，详情页取到的数据才一致
                detailOriginPage = 0      // 从海报墙进来的语义
                detailPage.load(subjectId)
                browseStack.currentIndex = 1
                window.currentPage = 0
                statusBar.setMessage("已添加并打开详情", 5000)
            } else {
                statusBar.setMessage("未能添加该目录（详见日志）", 5000)
            }
        }
        function onFailed(msg) {
            if (addAnimeDialog.visible)
                addAnimeDialog.onResult(false, msg || "添加失败")
        }
        // 某条目**因网络原因**没匹配上 → 黄色提示说明原因。
        //
        // warn=true 用琥珀色：这不是"错误"（条目已入库、只是标签页为
        // 待确认），而是"需要知道但不致命"的提醒 —— 与「该标签已存在」
        // 同一档语义。
        //
        // `name` 参数仍保留在信号里（日志与将来可能的展开视图要用）。
        function onMatchFailed(name, reason) {
            // 去掉 reason 末尾可能带的" —— 已走代理 xx"之类补充（基类提示
            // 现在只保留到"是什么错"为止，这里不再额外拼接长尾）。
            banner.show("联网匹配失败：" + reason
                        + " —— 条目已加入待确认", true)
        }
    }

    // ============ 删除确认对话框 ============
    //
    // 「删除」是不可逆操作，必须先确认。确认后：
    //   ① 调 library.deleteSubject(id)（只删库记录，磁盘文件不动）
    //   ② 退出详情页回到海报墙（该卡片已消失，留在详情页会很怪）
    //   ③ 用提示条说明"磁盘文件未删除"，消除用户顾虑
    ConfirmDialog {
        id: deleteDialog
        objectName: "deleteDialog"
        danger: true
        acceptText: "删除"

        onConfirmed: function (subjectId) {
            if (typeof library === "undefined" || !library) {
                statusBar.setMessage("媒体库不可用，删除失败", 5000)
                return
            }
            var r = library.deleteSubject(subjectId)
            if (!r || r.ok !== true) {
                statusBar.setMessage((r && r.message) ? r.message : "删除失败",
                                     6000)
                return
            }
            // 退出详情页：先切回海报墙那一格，再回到当初进来的外层页
            detailPage.load(0)
            browseStack.currentIndex = 0
            window.currentPage = detailOriginPage
            // 删除成功用**主题色**提示（不是红色 —— 红色留给"失败"）
            banner.show(r.message || "已删除", false)
        }

        onCancelled: {
            // 取消不做任何事（详情页保持原样）
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
    // ============ 扫描 → 状态栏（**只管进度与过程日志**）============
    //
    // **分工约定**：
    //   状态栏  —— 进度 + 过程性日志（"共发现 N 个条目"、"✓ 已匹配…"）
    //   顶部提示条 —— 所有**结论性**消息（扫描完成 / 联网失败 / 已删除…），
    //                 并带颜色分级（主题色=正常、琥珀=需注意）
    //
    // **为什么这么分**（踩坑）：原先这里和下面那个 banner 的 Connections
    // 是**各接一遍同一个 scanner**，于是同一次事件被两个通道各报一次 ——
    // 用户看到"两条都在说超时"（其实一条来自状态栏、一条来自顶部提示条），
    // 而且措辞还不一样（状态栏写"匹配 N 个"、提示条写"已匹配/未匹配"）。
    // 结论只报一次，才不会自相矛盾。
    //
    // 状态栏适合承载"持续变化的过程量"（进度条 + 滚动日志），
    // 但它是 28px 的细条、容易被忽略，不适合放需要用户看到的结论。
    Connections {
        target: typeof scanner !== "undefined" && scanner ? scanner : null

        function onProgressChanged(current, total) {
            statusBar.setProgress(current, total)
        }

        // 过程日志照旧进状态栏（**结论性日志已在下方的 banner 通道拦截**，
        // 见那里 onLogMessage 的说明，同一条不会两边都弹）
        function onLogMessage(msg) {
            statusBar.setMessage(msg)
        }

        function onFinished(matched, pending) {
            // **不再在这里报"扫描完成：匹配 N 个…"** —— 结论统一由顶部
            // 提示条播报（见下方 banner 的 onFinished）。这里只收掉进度条。
            statusBar.stopProgress()
            // 详情页正开着时重取：单条目重扫（详情页「+」按钮）会改写集数
            // 序号/标题/匹配状态，不刷新的话用户看到的还是旧列表
            // （与 player.onWatched 的处理同理）。
            if (detailPage.subjectId > 0
                    && window.currentPage === 0
                    && browseStack.currentIndex === 1)
                detailPage.load(detailPage.subjectId)
        }

        function onFailed(msg) {
            // 同上：失败结论归顶部提示条，这里只收进度条
            statusBar.stopProgress()
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

    // ============ 顶部提示条（**可叠加多层**）============
    //
    // 为什么要做成"列表"而不是单个浮层（实测反馈）：
    // 「添加动漫」网络失败时会**连发两条**消息 ——
    //   ① 琥珀："联网匹配失败：连接超时…… —— 条目已加入待确认"（说明原因）
    //   ② 主题色："扫描完成：未匹配"（说明结果）
    // 单浮层实现下后一条会把前一条**覆盖**掉，用户根本看不到那句原因
    // （实测："在软件里没看到为什么超时的黄色提示框"）。
    // 现在按顺序**从上往下堆叠**：原因在上、结果在下，两条同时可见。
    //
    // **注意与状态栏的分工**（方案 ）：顶部提示条只放**结论**；
    // 过程日志（"共发现 N 个条目"、"✓ 已匹配…"）归状态栏，不要在这里
    // 再弹一遍 —— 否则同一次事件会出现两条措辞不同的提示。
    Item {
        id: banner

        // 每条消息：{ text, warn, action, subjectId }
        property var messages: []

        // 单条的高度与间距
        readonly property int _itemH: 38
        readonly property int _gap: 8
        readonly property int _topMargin: 14
        // 最多同时显示几条（超出时挤掉最旧的，避免刷屏盖满上半屏）
        readonly property int _maxItems: 3
        // 各条的停留时长：**统一 5 秒**（实测反馈"两个提示框时间都改为 5s"）。
        //
        // 早先琥珀警告给了 8 秒（理由是"要用户去看"），但实际用起来：
        // 网络失败时黄条与"扫描完成"同时出现，黄条赖着不走会挡住后面的
        // 提示，还让整屏一直有东西在闪。既然结论已经足够简短，
        // 两种语气统一即可（3 秒 → 5 秒，留足阅读时间）。
        readonly property int _infoLife: 5000
        readonly property int _warnLife: 5000

        // 整体宽度：取**最长那条的估算宽**（Text 未创建时用字数粗估，
        // 避免依赖 Repeater 的 delegate 是否已实例化）。
        //
        // 每字宽度按 fontMd 的中文全角估：Theme.fontMd ≈ 14px，
        // 中文字形接近字号宽度，故 `字数 × 字号 + 内边距` 足够贴近实际；
        // 略微偏大一点点也无妨（提示条宽一点比文字被截断好）。
        readonly property int _width: {
            var maxLen = 1
            for (var i = 0; i < messages.length; i++)
                maxLen = Math.max(maxLen, messages[i].text.length)
            return Math.min(window.width - 48,
                            maxLen * Theme.fontMd + Theme.spacingXl * 2)
        }

        width: _width
        height: Math.max(0, messages.length * _itemH
                         + Math.max(0, messages.length - 1) * _gap)
        x: Math.round((window.width - width) / 2)
        y: _topMargin
        z: 200

        /// 显示一条提示（**追加**到底部，不覆盖已有的）。
        ///
        /// `warn` 为 true 时用琥珀色（需注意但不致命）；
        /// `action` 传 "settings" → 点击跳设置页；
        /// 传 "subject" → 点击跳 `subjectId` 对应条目详情页。
        function show(text, warn, action, subjectId) {
            var list = banner.messages.slice()   // var 属性要整体赋值才会通知
            list.push({
                "text": text,
                "warn": warn === true,
                "action": (action === "settings" || action === "subject")
                          ? action : "",
                "subjectId": (action === "subject") ? (subjectId || 0) : 0,
                // 剩余存活时间（毫秒）。**警告类留更久**：它是要用户去
                // 处理的（如"检查代理设置"），一闪而过等于没说。
                "life": (warn === true) ? banner._warnLife : banner._infoLife
            })
            // 超过上限时丢掉**最旧**的（用户已经在看最新的了）
            while (list.length > banner._maxItems)
                list.shift()
            banner.messages = list
        }

        /// 立即清空全部提示
        function clear() {
            banner.messages = []
        }

        /// 移除指定下标的一条（越界忽略）
        function removeAt(index) {
            var list = banner.messages.slice()
            if (index < 0 || index >= list.length)
                return
            list.splice(index, 1)
            banner.messages = list
        }

        /// 执行某条的 action（"settings" 跳设置页 / "subject" 跳详情页）
        function runAction(msg) {
            if (!msg || msg.action === "")
                return
            if (msg.action === "settings") {
                window.gotoPage(4)      // 4 = 设置页（见页面索引表）
                // 不止切页，还要滚动到那个开关并高亮
                settingsPage.revealField("epAlignOrderBox")
            } else if (msg.action === "subject" && msg.subjectId > 0) {
                // 跳到该条目的详情页（与从海报墙点卡片同一条路径：
                // 内层子栈切到详情 + 外层切回 browseStack 所在页）
                detailOriginPage = 0
                detailPage.load(msg.subjectId)
                browseStack.currentIndex = 1
                window.currentPage = 0
            }
        }

        Column {
            anchors.fill: parent
            spacing: banner._gap

            Repeater {
                id: messageRepeater
                model: banner.messages

                delegate: Rectangle {
                    id: row
                    required property var modelData
                    required property int index

                    width: rowText.implicitWidth + Theme.spacingXl * 2
                    height: banner._itemH
                    radius: Theme.radiusSm
                    // 琥珀色不放进 Theme：只在这类提示用，加进主题表反而
                    // 增加维护面（且 warningColor 是给深色背景调的，做底色偏暗）
                    color: row.modelData.warn
                           ? (Theme.dark ? "#3A2E12" : "#FFF4D6")
                           : Theme.accentSoft
                    border.width: Theme.lineThin
                    border.color: row.modelData.warn
                                  ? (Theme.dark ? "#8A6D1F" : "#E0B84C")
                                  : Theme.accent

                    // 整体水平居中（Column 是左对齐的，这里各自居中）
                    x: Math.round((banner.width - width) / 2)

                    // 出现时轻微滑入 + 淡入（新加的那条会动画，旧的保持）
                    opacity: 1
                    transform: Translate {
                        y: 0
                        Behavior on y {
                            NumberAnimation { duration: Theme.durNormal
                                              easing.type: Easing.OutCubic }
                        }
                    }

                    Text {
                        id: rowText
                        anchors.centerIn: parent
                        text: row.modelData.text
                        color: row.modelData.warn
                               ? (Theme.dark ? "#F0D48A" : "#7A5B08")
                               : Theme.accent
                        font.pixelSize: Theme.fontMd
                    }

                    // 点击 → 执行该条的 action 并**移除这一条**
                    MouseArea {
                        anchors.fill: parent
                        // 只在带 action 时接管点击（否则不挡下层）
                        enabled: row.modelData.action !== ""
                        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                        onClicked: {
                            banner.runAction(row.modelData)
                            banner.removeAt(row.index)
                        }
                    }
                }
            }
        }

        // **每条各自倒计时**（不再"统一到点删最旧的"）。
        //
        // 为什么要这样（踩坑思路）：早期实现是"定时器每 3 秒删最旧那条"，
        // 于是「网络失败原因」（琥珀，先出现）会在结果提示出现后没多久
        // **先被删掉** —— 用户正想读原因，它却先没了，反而把不重要的
        // "扫描完成"留着。现在每条带自己的 `life`，到点各自退场
        // （普通信息与琥珀警告现在统一 5 秒，见上方 _infoLife/_warnLife）。
        Timer {
            id: bannerTimer
            interval: 500                     // 每 0.5 秒结算一次
            repeat: true
            running: banner.messages.length > 0
            onTriggered: {
                var list = banner.messages.slice()
                var kept = []
                for (var i = 0; i < list.length; i++) {
                    var m = list[i]
                    // 拷贝一份再改（var 数组里的对象就地改不发通知）
                    kept.push({
                        "text": m.text, "warn": m.warn,
                        "action": m.action, "subjectId": m.subjectId,
                        "life": m.life - bannerTimer.interval
                    })
                }
                // 全部过期就清空（避免留下 life 为负的死项）
                var alive = []
                for (var j = 0; j < kept.length; j++)
                    if (kept[j].life > 0)
                        alive.push(kept[j])
                banner.messages = alive
            }
        }
    }



    // ---- 扫描进度反馈：写进窗口标题（QML 无状态栏，暂用标题承载）----
    Connections {
        target: typeof scanner !== "undefined" && scanner ? scanner : null

        function onRunningChanged() {
            if (scanner.running)
                banner.show("开始扫描…")
        }

        // **过程日志不再进顶部提示条**（方案 A 分工）。
        //
        // 理由：过程日志是"滚动的流水"（"共发现 N 个条目"、"✓ 已匹配 X"、
        // "⚠ 联网匹配失败…"），它们属于**状态栏**（细条、常驻、可以一直刷新，
        // 见上面那个 Connections）。若同时往顶部提示条灌，会出现：
        //   ① 每次扫描刷出十几条提示条，把画面盖住；
        //   ② 网络失败时同时出现 [琥珀]"联网匹配失败：<原因>" 与
        //      [主题色]"⚠ 联网匹配失败：<原因>" 两条，说的是同一件事、
        //      底色还不同，用户以为出了两种问题（实测反馈"两条都在说超时"）。
        //
        // 顶部提示条只承载**结论**：扫描完成 / 联网失败 / 扫描失败
        // （分别见 onFinished / onMatchFailed / onFailed）。
        function onLogMessage(msg) {
            // 有意留空：分工调整后，过程日志归状态栏。
            // 保留这个空实现是为了让"这里接过 onLogMessage、但故意不播报"
            // 这件事在代码里可见，避免以后有人以为漏了而补回去。
        }

        // 扫描结果的统一播报（**只在这里播一次**）。
        //
        // 措辞分两种（用户要求）：
        //   单个目录（「添加动漫」/ 详情页重扫）：
        //       只报**一个**结论 —— "扫描完成：已匹配" 或 "扫描完成：未匹配"。
        //       **不带原因**：失败原因由那条黄色提示负责说明，这里复述
        //       一遍只会让两条提示内容重叠。
        //   全量扫描：沿用"匹配 N 个，待确认 M 个"（扫一整个库，
        //       用"个"计数才自然，也能一眼看出规模）。
        function onFinished(matched, pending) {
            if (scanner.singleScan) {
                banner.show(matched > 0 ? "扫描完成：已匹配"
                                        : "扫描完成：未匹配")
            } else {
                banner.show("扫描完成：匹配 " + matched
                            + " 个，待确认 " + pending + " 个")
            }
            library.reload()
        }

        function onFailed(msg) {
            banner.show("扫描失败：" + msg)
        }
    }
}
