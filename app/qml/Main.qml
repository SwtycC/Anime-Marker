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

    width: 1120
    height: 720
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

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ============ 内容区 ============
        // 海报墙与详情页共用一层子栈：详情不是独立的一级导航页，
        // 从海报墙点进去、返回后仍回到海报墙（与旧版 MainWindow.browse_stack 一致）。
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
                        detailPage.load(subjectId)
                        browseStack.currentIndex = 1
                    }
                }

                DetailPage { id: detailPage }
            }

            InProgressPage {
                id: inProgressPage
                // 在看页「详情」按钮 → 复用海报墙的详情页
                onSubjectClicked: function (subjectId) {
                    detailPage.load(subjectId)
                    browseStack.currentIndex = 1
                }
            }

            TimelinePage {
                id: timelinePage
                onSubjectClicked: function (subjectId) {
                    detailPage.load(subjectId)
                    browseStack.currentIndex = 1
                }
            }
            SubscriptionPage { }
            SettingsPage     { id: settingsPage }
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

    // 切到设置页时重新读取配置（避免外部改动后显示旧值）
    onCurrentPageChanged: {
        if (currentPage === 4)
            settingsPage.refresh()
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
            // 动态页的数据源是 library.timeline()，不是 Property，
            // 因此这里显式重取一次（本次新增了一条观看记录）。
            timelinePage.entries = library.timeline(500)
        }

        function onProgressChanged(episodeId, progress) {
            // 播放中：把进度显示在状态栏，替代旧的进度条语义
            if (progress > 0)
                statusBar.setMessage(
                    "播放中 " + Math.round(progress * 100) + "%")
        }
    }

    // 鼠标后侧键（XButton1）在详情页时返回海报墙
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.BackButton
        // 只在详情页拦截，否则会吃掉正常点击
        enabled: browseStack.currentIndex === 1
        onClicked: {
            // 同上：不调 library.reload()，避免重建全部卡片
            browseStack.currentIndex = 0
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
     * 应用主题（启动时由 Python 侧读取 config.ini 后调用）。
     *
     * 参数：
     *   isDark       —— true 深色 / false 白色简约
     *   accentColor  —— 主题色 "#RRGGBB"
     *
     * 说明：Theme 是 QML 单例，Python 无法直接拿到实例，故从这里转发。
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

        // 浮在内容之上、导航栏上方，不占布局空间
        readonly property int _pad: 12
        x: Math.round((window.width - width) / 2)
        y: Math.round(window.height - height - Theme.navBottomMargin
                      - Theme.navPillHeight - 12)

        width: bannerText.implicitWidth + Theme.spacingXl * 2
        height: 38
        radius: Theme.radiusSm
        color: Theme.accentSoft
        border.width: Theme.lineThin
        border.color: Theme.accent
        opacity: 0
        visible: opacity > 0.01
        z: 200

        Behavior on opacity { NumberAnimation { duration: Theme.durNormal } }

        Text {
            id: bannerText
            anchors.centerIn: parent
            color: Theme.accent
            font.pixelSize: Theme.fontMd
        }

        Timer {
            id: bannerTimer
            interval: 3000
            onTriggered: banner.opacity = 0
        }

        function show(text) {
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
