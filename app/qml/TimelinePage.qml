import QtQuick
import QtQuick.Controls

// 动态页：按时间倒序的观看记录，支持两种内容来源。
//
// 内容来源由页面内的分段按钮控制（**不放在设置页**）：
//   local  —— 只显示本地观看记录（`library.timeline()`，纯离线）
//   merged —— 本地记录 + Bangumi 逐集标记
//
// 为什么不放设置页：这是"看当前页面"的临时视图偏好，切换后应立刻见效。
// 放设置页需要"改完→点保存→切页"，多两步且容易让人以为按钮没生效。
//
// 呈现规则：
// - `local` 模式**按动漫聚合**：同一部即使看了多集也只占一行，行内显示
//   「已看 N 集」（本地记录是同一次观看会话产生的，聚合更易读）。
// - `merged` 模式**逐集一行**：Bangumi 记录的核心价值就是"每集什么时候
//   看的"，聚合会把这个信息丢掉；本地记录同样逐集展示。
// - 两个来源都带 `watchedAt`，混在一起**按时间倒序**，并按日期归入
//   今天 / 昨天 / 本周 / … 等分组。
// - 列表默认只渲染首屏 N 条（`ep_timeline_count`），其余靠滚动到底或
//   「加载更多」追加（见 extraPages / pageStep）。**数据是全量同步的**，
//   显示上限只影响渲染，不影响拉取。
//
// 分组/聚合实现放在 QML 侧：纯展示逻辑，调整规则不必动后端。
Item {
    id: root

    // 内容来源：local | merged（页面内切换，不持久化）
    property string source: "local"

    // 本地观看记录（原始，按集）——由本软件播放并自动标记的记录
    property var localEntries: typeof library !== "undefined" && library
                               ? library.timeline(500) : []
    // Bangumi 集级观看记录（F20）—— 逐集、带标记时间
    //
    // 这是 merged 模式的**首选数据源**：它能给出"第几集是什么时候看的"，
    // 对应 Bangumi 网页上的「看过 ep.5 TV取材 · 5天12小时前」。
    // 数据由 InProgressBridge 的第二阶段并发拉取写入（最近 N 部，
    // N 可在设置页调整；N=0 时本列表为空）。
    property var bangumiEpisodes: typeof library !== "undefined" && library
                                  ? library.watchedEpisodes : []
    // Bangumi 动漫级收藏（**只用于判断"有没有拉过"**，不直接渲染 ——
    // 早期曾用它做"集级为空时退回动漫级展示"的兜底，但那会让
    // "设了 N=5 却看到 149 条"，与设置语义冲突，已移除）
    property var bangumiItems: typeof library !== "undefined" && library
                               ? library.inProgress : []

    signal subjectClicked(int subjectId)

    // 页面内的提示（Main.qml 转成状态栏消息，同 SubscriptionPage / SettingsPage）
    signal statusMessage(string text)

    /// 当前鼠标悬停在哪一行（行的 rowKey）。
    ///
    /// **为什么 hover 状态要放在页面而不是 delegate 里**（实测 bug）：
    /// 列表的 model 是 JS 数组，数据一变（刷新、切来源、改设置）整批
    /// delegate 会被销毁重建，delegate 内的 `containsMouse` 随之丢失 ——
    /// 表现是"悬停高亮闪一下就没了"，而且**不移动鼠标就不会恢复**
    /// （新建出来的 MouseArea 要等下一次鼠标事件才知道指针在它上面）。
    /// 提到页面级后，重建出来的新 delegate 读同一个值，高亮自动续上。
    property string hoveredKey: ""

    /// 行的稳定标识：跨 delegate 重建保持不变。
    /// `episodeId` 两种来源都有（本地=episodes.id，Bangumi=bangumi_ep_id），
    /// 各自唯一但属于不同 ID 空间，故加来源前缀区分。
    function rowKey(item) {
        return (item.isBangumi ? "b" : "l") + item.episodeId + "-" + item.subjectId
    }

    /// 已额外加载的页数（0 = 只显示首屏 `epLimit()` 条）。
    ///
    /// **为什么记"页数"而不是"已显示条数"**：条数得初始化、还得在设置变化时
    /// 手工同步，很容易漏（改完设置页面不跟着变）。记页数则每次都由
    /// `epLimit() + extraPages * pageStep` **现算**，设置一改立刻生效。
    property int extraPages: 0

    /// 每页追加多少条。至少 50 —— 首屏条数被设成 5 时，一次只加 5 条
    /// 点起来太累。
    readonly property int pageStep: Math.max(50, epLimit())

    /// 应用条数上限**之前**的完整列表（含 merged 模式的 Bangumi 条目）
    ///
    /// **踩坑（重要）：这个属性必须是「绑定」，不能被外部赋值。**
    /// 早期 Main.qml 在切到动态页时写了
    ///     `timelinePage.entries = library.timeline(500)`
    /// 以为是在"刷新数据"，实际上 QML 里给一个有绑定的属性赋值会
    /// **永久断开该绑定**，导致后续数据源变化不再触发重算。
    /// 正确做法：外部只改 `localEntries`（数据源），见 reload()。
    property var allEntries: buildEntries()

    /// 实际渲染的列表 = 首屏 N 条 + 已加载的页
    ///
    /// 拆成 `allEntries` + `applyLimit()` 两步，是为了能算出"还有多少条
    /// 没显示"（见 hasMore）—— 否则用户只看到"条数变少了"，无法判断是
    /// 设置生效了还是数据没拉到。
    property var entries: applyLimit(allEntries)

    /// 还有多少条没显示（>0 时列表底部出现「加载更多」）
    readonly property int hiddenByLimit: allEntries.length - entries.length
    readonly property bool hasMore: hiddenByLimit > 0
    readonly property int totalCount: allEntries.length

    /// 拍平成分组列表（组标题 + 数据行混排），给 ListView 当 model
    property var flatItems: flattenGroups(entries)

    /// 是否正在拉取 Bangumi 看过列表（merged 模式下刷新时）
    readonly property bool busy: typeof inprogress !== "undefined" && inprogress
                                 ? inprogress.running : false

    /// 重新拉取本地观看记录（由外部调用，不碰 entries 本身）
    function reload() {
        extraPages = 0                     // 刷新后回到首屏
        if (typeof library !== "undefined" && library)
            localEntries = library.timeline(500)
        if (typeof library !== "undefined" && library)
            library.reloadWatchedEpisodes()
    }

    /// 切换内容来源。切到 merged 且两个 Bangumi 数据源都为空时自动拉一次，
    /// 避免用户切过来看到空白还得再点一次刷新。
    function setSource(v) {
        if (v !== "local" && v !== "merged")
            return
        var changed = (source !== v)
        source = v
        extraPages = 0                     // 换数据源 → 收回首屏
        if (v === "merged" && changed
                && bangumiItems.length === 0 && bangumiEpisodes.length === 0)
            pullBangumi()
    }

    /// 数据源变化（刷新、同步落库、保存设置）→ 收回首屏。
    ///
    /// 为什么挂在 `watchedEpsChanged` 上：`epLimit()` 走的是
    /// `settingsBridge.getAll()`（Slot 调用，**不构成绑定依赖**），所以
    /// "设置里把显示条数改小"这件事本身不会让 `entries` 重算 —— 靠的正是
    /// 保存设置 → `applyEpisodeCount()` → `reloadWatchedEpisodes()` 这条链路。
    /// 在这里复位，改小之后页面才会立刻从"已加载 200 条"收回到新条数。
    ///
    /// 副作用（可接受）：全量同步期间每批都会回调一次，页面若停在很靠下的
    /// 位置会被拉回首屏；但那时列表本来就只有几十条，用户基本都在顶部。
    Connections {
        target: typeof library !== "undefined" && library ? library : null

        function onWatchedEpsChanged() {
            root.extraPages = 0
        }
    }

    /// 触发一次 Bangumi 拉取（两阶段：收藏列表 → 最近 N 部的集级记录）。
    /// 完成后 InProgressBridge 会依次回调
    /// `library.reloadInProgress()` 与 `library.reloadWatchedEpisodes()`，
    /// 进而让 bangumiItems / bangumiEpisodes 的绑定重算。
    function pullBangumi() {
        if (typeof inprogress !== "undefined" && inprogress && !inprogress.running)
            inprogress.refresh()
    }

    /// 点「刷新」按钮：
    /// - local 模式：只重读本地记录（瞬时完成）
    /// - merged 模式：额外拉一次 Bangumi 看过列表
    function requestRefresh() {
        reload()
        if (source === "merged")
            pullBangumi()
    }

    /// 构造渲染列表。两个来源的**粒度不同**，因此合并不聚合：
    ///
    /// - `local`  模式：本地记录按动漫聚合（同一部只占一行）
    /// - `merged` 模式：优先用 Bangumi **集级**记录（逐集一行，
    ///   因为它的核心价值就是"每集什么时候看的"，聚合会丢掉这个信息）；
    ///   若集级记录为空（用户设了 N=0 或尚未拉取），退回动漫级展示。
    ///
    /// 本地记录与 Bangumi 记录都带 `watchedAt`，因此可以**混在一起
    /// 按时间倒序**，而不是把 Bangumi 那部分整体堆在末尾。
    function buildEntries() {
        var out = []
        var seen = {}

        // ---- 1. 本地播放记录（按动漫聚合）----
        for (var i = 0; i < localEntries.length; i++) {
            var e = localEntries[i]
            var key = String(e.subjectId)
            if (seen[key] !== undefined) {
                out[seen[key]].watchedEpCount += 1
                continue
            }
            seen[key] = out.length
            out.push({
                // episodeId 必须带上：rowKey() 用它做行的稳定标识（悬停高亮靠它）
                "episodeId": e.episodeId,
                "subjectId": e.subjectId,
                "subjectName": e.subjectName,
                "epIndex": e.epIndex,
                "epTitle": e.epTitle,
                "watchedAt": e.watchedAt,
                "watchedEpCount": 1,
                "isBangumi": false,
                "isInProgress": false
            })
        }

        // ---- 2. Bangumi 集级记录（merged 模式）----
        //
        // **不做"动漫级兜底"**。早期实现是：集级为空时退回展示全部收藏
        // （149 部动漫级条目），但那样有两个问题：
        //   ① 与"集级记录条数 = N"的语义冲突 —— 用户设了 N=5，
        //      却看到 149 条，会以为设置没生效；
        //   ② 动漫级条目没有时间，只能堆在末尾，破坏时间线观感。
        // 现在改为：无集级数据时展示一句解释（见 emptyHint()），
        // 告诉用户"为什么是空的、怎么调整"。
        if (source === "merged") {
            for (var j = 0; j < bangumiEpisodes.length; j++) {
                var be = bangumiEpisodes[j]
                out.push({
                    // 同上：rowKey() 依赖 episodeId
                    "episodeId": be.episodeId,
                    "subjectId": be.subjectId,
                    "subjectName": be.subjectName,
                    "epIndex": be.epIndex,
                    "epTitle": be.epTitle,
                    "watchedAt": be.watchedAt,
                    "watchedEpCount": 1,
                    "isBangumi": true,
                    "isInProgress": false,
                    "inLibrary": be.inLibrary
                })
            }
        }

        // ---- 3. 按时间倒序（无时间的排最后）----
        out.sort(function (a, b) {
            var ta = a.watchedAt || ""
            var tb = b.watchedAt || ""
            if (ta && !tb) return -1
            if (!ta && tb) return 1
            if (!ta && !tb) return 0
            return ta < tb ? 1 : (ta > tb ? -1 : 0)   // 字符串倒序 = 时间倒序
        })
        return out
    }

    // ============ 页头（固定，不随列表滚动）============
    //
    // 旧版页头放在 Flickable 里，会跟着列表一起滚走。改成 ListView 后特意
    // 留在外面：列表越长越需要"随时能点刷新 / 切来源"。
    Column {
        id: headerBox
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Theme.pagePadding
        spacing: Theme.spacingMd

        // ---- 标题行：标题 + 状态 + 来源切换 + 刷新 ----
        Row {
            width: parent.width
            spacing: Theme.spacingMd

            Column {
                width: parent.width - sourceSeg.width - refreshBtn.width
                       - Theme.spacingMd * 2
                spacing: 2

                Text {
                    text: "动态"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontXl
                    font.weight: Font.DemiBold
                }

                Text {
                    width: parent.width
                    text: root.headerText()
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                    elide: Text.ElideRight
                }
            }

            // 内容来源切换（页面内即时生效，不写配置）
            SegmentedControl {
                id: sourceSeg
                objectName: "timelineSourceSeg"
                anchors.verticalCenter: parent.verticalCenter
                options: [
                    { "label": "仅本地", "value": "local" },
                    { "label": "本地 + Bangumi", "value": "merged" }
                ]
                currentValue: root.source
                onSelected: function (value) {
                    root.setSource(value)
                }
            }

            AppButton {
                id: refreshBtn
                anchors.verticalCenter: parent.verticalCenter
                text: root.busy ? "刷新中…" : "刷新"
                enabled: !root.busy
                onClicked: root.requestRefresh()
            }
        }

        Rectangle {
            width: parent.width
            height: Theme.lineThin
            color: Theme.border
        }
    }

    // ============ 记录列表 ============
    //
    // **为什么用 ListView 而不是 Flickable + Column + Repeater**：Repeater
    // 不做 delegate 回收，model 一变就把当前全部行销毁重建 —— 每次
    // 「加载更多」都要重付一遍，行数上千后明显卡（本账号全量同步后约
    // 1000+ 条）。ListView 的开销只与**可见窗口**成正比。
    ListView {
        id: listView
        objectName: "timelineList"
        anchors.top: headerBox.bottom
        anchors.topMargin: Theme.spacingMd
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: Theme.pagePadding
        anchors.rightMargin: Theme.pagePadding
        clip: true
        model: root.flatItems
        spacing: Theme.lineThin
        boundsBehavior: Flickable.StopAtBounds
        // 底部留出导航浮层的高度，否则最后几行会被挡住
        bottomMargin: Theme.navContentGutter

        ScrollBar.vertical: AppScrollBar {
            id: vbar
            policy: ScrollBar.AsNeeded
        }

        // 滚到底自动加载下一页（页脚的按钮是显式兜底 —— 自动加载在某些
        // 触控板/滚轮节奏下可能连触发多次，有个按钮用户心里更有底）
        onAtYEndChanged: {
            if (atYEnd && root.hasMore)
                root.loadMore()
        }

        delegate: Column {
            id: cell
            required property var modelData

            // modelData = { item, headerLabel }（见 flattenGroups）
            width: listView.width - (vbar.visible ? vbar.width : 0)
            spacing: Theme.lineThin

            // 组标题（今天 / 昨天 / 日期）—— 只在该组第一行上方出现
            Text {
                width: cell.width
                visible: modelData.headerLabel !== ""
                text: modelData.headerLabel
                topPadding: Theme.spacingSm
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
                font.weight: Font.DemiBold
            }

            // 单条记录
            Rectangle {
                id: rowRect
                width: cell.width
                height: 48
                                // 高亮 = 两个信号的**或**，缺一不可：
                                //   containsMouse —— 活着的那一行（鼠标确实在它上面）
                                //   hoveredKey    —— 跨 delegate 重建的"粘性"状态
                                //
                                // 只用 containsMouse：delegate 一重建高亮就丢（原 bug）。
                                // 只用 hoveredKey：delegate 销毁时旧 MouseArea 可能补一个
                                // exited，把新 delegate 刚写进去的同一个 key 清掉 ——
                                // 两者取或之后，任一条成立都亮，重建前后都不断。
                                //
                                // 静止态用 `Theme.fade(...)` 而不是 `"transparent"`：
                                // 后者是黑色透明，渐变时会先扫过一段深灰（详见 Theme.fade 的说明）
                                color: (rowMouse.containsMouse
                                        || root.hoveredKey === root.rowKey(modelData))
                                       ? Theme.hoverFillStrong
                                       : Theme.fade(Theme.hoverFillStrong)

                                Behavior on color { ColorAnimation { duration: Theme.durFast } }

                                Row {
                                    anchors.fill: parent
                                    anchors.leftMargin: Theme.spacingMd
                                    anchors.rightMargin: Theme.spacingMd
                                    spacing: Theme.spacingSm

                                    // 序号徽标：本地与 Bangumi 都用「EP8」
                                    // （二者都是"某一集"，区分靠中列/来源）
                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Math.max(28, epLabel.implicitWidth + 10)
                                        height: 20
                                        radius: Theme.radiusSm
                                        color: Theme.accentSoft

                                        Text {
                                            id: epLabel
                                            anchors.centerIn: parent
                                            text: "EP" + root.fmtIndex(modelData.item.epIndex)
                                            color: Theme.accent
                                            font.pixelSize: Theme.fontXs
                                        }
                                    }

                                    // 动漫名
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Math.round((parent.width - 40) * 0.42)
                                        text: modelData.item.subjectName
                                        color: Theme.textPrimary
                                        font.pixelSize: Theme.fontMd
                                        elide: Text.ElideRight
                                    }

                                    // 中列：本地聚合记录显示「已看 N 集」，
                                    //       Bangumi 集级记录显示集标题
                                    Text {
                                        id: epCountLabel
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Math.max(implicitWidth, 56)
                                        text: modelData.item.isBangumi
                                              ? (modelData.item.epTitle || "")
                                              : "已看 " + modelData.item.watchedEpCount + " 集"
                                        color: Theme.textTertiary
                                        font.pixelSize: Theme.fontSm
                                        elide: Text.ElideRight
                                    }

                                    // 右列：相对时间（Bangumi 记录额外标注来源）
                                    Row {
                                        anchors.verticalCenter: parent.verticalCenter
                                        spacing: Theme.spacingXs

                                        Rectangle {
                                            anchors.verticalCenter: parent.verticalCenter
                                            visible: modelData.item.isBangumi === true
                                            width: bgmLabel.implicitWidth + 8
                                            height: 16
                                            radius: 2
                                            color: "transparent"
                                            border.width: Theme.lineThin
                                            border.color: Theme.border

                                            Text {
                                                id: bgmLabel
                                                anchors.centerIn: parent
                                                text: "bgm"
                                                color: Theme.textTertiary
                                                font.pixelSize: Theme.fontXs
                                            }
                                        }

                                        Text {
                                            id: timeLabel
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: root.fmtTime(modelData.item.watchedAt)
                                            color: Theme.textTertiary
                                            font.pixelSize: Theme.fontSm
                                        }
                                    }
                                }

                                Rectangle {
                                    anchors.bottom: parent.bottom
                                    width: parent.width
                                    height: Theme.lineThin
                                    color: Theme.border
                                    opacity: 0.5
                                }

                                MouseArea {
                                    id: rowMouse
                                    anchors.fill: parent
                                    hoverEnabled: true

                                    onEntered: root.hoveredKey = root.rowKey(modelData.item)
                                    onExited: {
                                        // 只清自己那一行：delegate 重建时可能出现
                                        // "新的 entered 先于旧的 exited" 的次序
                                        if (root.hoveredKey === root.rowKey(modelData.item))
                                            root.hoveredKey = ""
                                    }

                                    // Bangumi 条目未入库时（subjectId == 0）没有本地详情，
                                    // 用箭头光标提示"这一行不可点"
                                    readonly property bool clickable: modelData.item.subjectId > 0
                                    cursorShape: clickable ? Qt.PointingHandCursor
                                                           : Qt.ArrowCursor
                                    onClicked: {
                                        if (clickable)
                                            root.subjectClicked(modelData.item.subjectId)
                                        else
                                            // 未入库 → 没有本地详情页，明确告诉用户
                                            // （早期是静默 return，被当成"点了没反应"的 bug）
                                            root.statusMessage(
                                                "「" + modelData.item.subjectName
                                                + "」未入库，无法打开详情")
                                    }
                                }
            }
        }

        // ---- 页脚：加载更多 / 已显示全部 ----
        //
        // 用 ListView 的 footer（而不是列表外面另放一块），这样它跟着列表
        // 一起滚动，最后一行下方不会凭空多出一段空白。
        footer: Column {
            width: listView.width
            spacing: Theme.spacingSm

            Item { width: 1; height: Theme.spacingMd }

            AppButton {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: root.hasMore
                text: "加载更多（还有 " + root.hiddenByLimit + " 条）"
                onClicked: root.loadMore()
            }

            Text {
                width: parent.width
                visible: root.entries.length > 0 && !root.hasMore
                text: "已显示全部 " + root.totalCount + " 条"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                horizontalAlignment: Text.AlignHCenter
            }

            Item { width: 1; height: Theme.spacingSm }
        }
    }

    // ---- 空状态（覆盖在列表区域）----
    Column {
        anchors.top: headerBox.bottom
        anchors.topMargin: Theme.spacingMd
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: Theme.pagePadding
        anchors.rightMargin: Theme.pagePadding
        spacing: Theme.spacingSm
        visible: root.entries.length === 0

        Item { width: 1; height: 60 }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "还没有观看记录"
            color: Theme.textSecondary
            font.pixelSize: Theme.fontLg
        }

        Text {
            width: parent.width
            text: root.emptyHint()
            color: Theme.textTertiary
            font.pixelSize: Theme.fontMd
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            lineHeight: 1.5
        }
    }

    /// 空状态说明文案
    ///
    /// 现在同步范围是**全部收藏状态**（想看 / 在看 / 看过 / 搁置 / 抛弃），
    /// 所以"空的"只可能是两种情况：还没同步过，或者这个账号确实没标过任何
    /// 单集。（旧版受"最近 N 部"限制，还会因为"最近几部恰好只有剧场版"
    /// 而空 —— 那个坑随着早停机制一起消失了。）
    function emptyHint() {
        if (source !== "merged")
            return "播放剧集并在 PotPlayer 中看完后，这里会留下记录"
        if (bangumiItems.length > 0)
            return "还没有逐集记录\n"
                 + "动态页收录的是**逐集**标记（Bangumi 网页上「看过 ep.5」那种）；"
                 + "只标了整部番状态、没标过单集的条目不会出现在这里。\n"
                 + "点右上角「刷新」同步一次（首次会拉取全部收藏，约十几秒）。"
        return "暂无内容 —— 点右上角「刷新」同步 Bangumi 观看记录"
    }

    /// 首屏条数上限（N = `ep_timeline_count`），超出部分靠「加载更多」展开。
    ///
    /// **为什么整页截取而不是只截 Bangumi 那部分**：设置项叫「条数」，
    /// 用户设 5 时期待页面上一共 5 条；只截 Bangumi 的话，本地记录一叠加
    /// 又会超过 5，等于设置没生效。
    ///
    /// 规则：
    /// - **仅 merged 模式生效**。「仅本地」是纯离线视图，与 Bangumi 同步
    ///   范围无关。
    /// - `N <= 0`（= 关闭逐集记录）时**只藏 Bangumi 那部分**，本地记录
    ///   照常显示 —— 设成 0 不等于把本地时间线也清掉。
    /// - 传入的 list 已按时间倒序，`slice(0, cap)` 即"最近的 cap 条"。
    /// - cap = 首屏 N + 已加载页数 × 每页条数（见 extraPages / pageStep）。
    function applyLimit(list) {
        if (source !== "merged")
            return list
        var n = epLimit()
        if (n <= 0) {
            // 逐集记录被关闭（N=0）：只藏掉 Bangumi 那部分，本地记录照常显示。
            // **不删数据** —— 表里的历史要完整保留，用户把 N 调回来就能立刻
            // 看到（旧版在这里清表，重新开启时得联网重拉一遍才行）。
            return list.filter(function (e) { return !e.isBangumi })
        }
        var cap = n + extraPages * pageStep
        return list.length <= cap ? list : list.slice(0, cap)
    }

    /// 追加一页（滚动到底自动触发，也是「加载更多」按钮的动作）
    function loadMore() {
        if (hasMore)
            extraPages += 1
    }

    /// 「集级记录条数」的数值形态；取不到或非法时返回 0（= 不设上限）。
    ///
    /// 注意 `getAll()` 是 Slot 调用，**不构成绑定依赖** —— 这里的值能刷新，
    /// 靠的是配置保存后 InProgressBridge 回调 `reloadWatchedEpisodes()`，
    /// 使 `watchedEpisodes` 变化 → `allEntries` 重算 → 上层绑定跟着重算。
    function epLimit() {
        if (typeof settingsBridge === "undefined" || !settingsBridge)
            return 0
        var v = settingsBridge.getAll()
        if (!v || v["bangumi.ep_timeline_count"] === undefined)
            return 0
        var n = parseInt(v["bangumi.ep_timeline_count"], 10)
        return (isNaN(n) || n < 0) ? 0 : n
    }

    /// 读取「集级记录条数」设置，仅用于文案展示
    function epCountSetting() {
        if (typeof settingsBridge === "undefined" || !settingsBridge)
            return "N"
        var v = settingsBridge.getAll()
        return (v && v["bangumi.ep_timeline_count"] !== undefined)
                ? v["bangumi.ep_timeline_count"] : "N"
    }

    // ---- 分组：按「最近观看日期」切分成组，并拍平成 ListView 的 model ----
    //
    // 输出每项 `{item, headerLabel}`：`headerLabel` 非空表示"这一行是某组的
    // 第一行，上方要画组标题"，其余行为空串。
    //
    // 旧版这里还有一个 `isInProgress` → 单独归到末尾「Bangumi 看过」组的
    // 分支：两个数据源（localEntries / bangumiEpisodes）都写死
    // `isInProgress: false`，那条路径从未生效过，随本次改造一并清掉
    // （顺带解决"Bangumi 条目没有时间只能堆末尾"的问题 —— 现在所有记录
    // 都有真实时间，一律按日期归组）。
    function flattenGroups(list) {
        if (!list || list.length === 0)
            return []
        var out = []
        var curLabel = ""
        for (var i = 0; i < list.length; i++) {
            var item = list[i]
            var label = dayLabel(item.watchedAt)
            // 组标题并进"该组第一行"，而不是在 model 里插独立的标题项 ——
            // 后者要用 DelegateChooser / Loader 才能分派两种 delegate，
            // 而这里的行本来就高度可变，并进来最省事，视觉完全一致。
            var first = (i === 0 || label !== curLabel)
            out.push({
                "item": item,
                "headerLabel": first ? label : ""
            })
            curLabel = label
        }
        return out
    }

    /// 把 ISO 时间串转成组名。
    ///
    /// **分组粒度按"距今多久"分档**，而不是一律用年月日：
    /// 集级记录（Bangumi 逐集）往往集中在近期，若每天都单独成组，
    /// 会出现大量只含 1~2 条的碎片组（实测 207 条记录产生 156 个组，
    /// 滚动时全是标题、几乎没有内容）。
    ///
    /// 分档规则（**周以周一为界**）：
    ///   今天 / 昨天 / 前天   —— 精确到天，符合"最近在看什么"的关注点
    ///   本周 / 上周          —— 自然周
    ///   本月 / 上月          —— 自然月（已归入本周/上周的日子不重复计）
    ///   更早                 —— 按月归档（YYYY-MM 月）
    ///
    /// **踩坑（标签不能"随口一估"）**：早期用的是"距今天数"粗分档
    /// （3~6 天=本周内、7~13 天=一周前、14~29 天=本月内），结果
    /// **13 天前的那条被标成"一周前"**（差了一倍），被用户当成显示错误
    /// 反馈上来。问题不在数据，在标签口径：既然写"一周前"，就得真的是
    /// 一周左右。现在改用自然周/月，标签的含义与日历一致，
    /// 不再需要用户换算"这一档到底覆盖几天"。
    ///
    /// 分档数量与原先相当（最多 7 档 + 每月一档），不会产生碎组。
    function dayLabel(iso) {
        var d = parseDate(iso)
        if (!d)
            return "未知日期"
        var today = new Date()
        var d0 = startOfDay(d)
        var days = Math.floor((startOfDay(today) - d0) / 86400000)
        if (days <= 0)
            return "今天"
        if (days === 1)
            return "昨天"
        if (days === 2)
            return "前天"

        // 本周：本周一 00:00 起（3 天前及更早里落在本周内的那些；
        // 未来时刻在上面的 days <= 0 已被归入"今天"）
        var thisWeek = weekStart(today)
        if (d0.getTime() >= thisWeek.getTime())
            return "本周"
        // 上周：上周一 ~ 本周日
        if (d0.getTime() >= addDays(thisWeek, -7).getTime())
            return "上周"

        // 本月：与今天同一个月，且未被本周/上周覆盖（即"本月早些时候"）。
        // 注意必须在"上周"之后判断 —— 跨月的那一周可能整周都属于上月。
        if (d.getFullYear() === today.getFullYear()
                && d.getMonth() === today.getMonth())
            return "本月"
        // 上月：比"上个月的 1 号"更近的，只可能是上月（本月/本周/上周均已返回）
        if (d0.getTime() >= monthStart(today, 1).getTime())
            return "上月"

        return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + " 月"
    }

    /// 所在自然周的周一 00:00（JS 的 getDay()：周日=0，故先 +6 再取模）
    function weekStart(d) {
        var s = startOfDay(d)
        var back = (s.getDay() + 6) % 7
        return new Date(s.getFullYear(), s.getMonth(), s.getDate() - back)
    }

    /// 日期加减天数（Date 构造器会自动处理跨月/跨年）
    function addDays(d, n) {
        return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)
    }

    /// n 个月前的那个月的 1 号
    function monthStart(d, n) {
        return new Date(d.getFullYear(), d.getMonth() - n, 1)
    }

    /// 相对时间：刚刚 / N 分钟前 / N 小时前 / HH:MM / MM-DD
    ///
    /// 之所以补上"N 小时前"：集级记录常见的情况是"今天早些时候看的"，
    /// 直接给 `HH:MM` 需要用户自己换算，不如相对时间直观。
    function fmtTime(iso) {
        var d = parseDate(iso)
        if (!d)
            return ""
        var now = new Date()
        var sec = (now - d) / 1000
        if (sec < 0)
            return pad2(d.getHours()) + ":" + pad2(d.getMinutes())
        if (sec < 60)
            return "刚刚"
        if (sec < 3600)
            return Math.floor(sec / 60) + " 分钟前"
        if (startOfDay(d).getTime() === startOfDay(now).getTime())
            return Math.floor(sec / 3600) + " 小时前"
        return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
    }

    function startOfDay(d) {
        return new Date(d.getFullYear(), d.getMonth(), d.getDate())
    }

    /// 解析 ISO 串。
    /// 注意：Python 的 `datetime.isoformat()` 可能带 "+08:00" 时区偏移，
    /// JS 的 Date 能识别；但不带时区时会被当成本地时间 —— 这里统一按
    /// 本地时间处理（软件只在单机用，误差可接受，且避免了时区换算的复杂度）。
    function parseDate(iso) {
        if (!iso)
            return null
        var d = new Date(iso)
        if (isNaN(d.getTime()))
            return null
        return d
    }

    function pad2(n) {
        return n < 10 ? "0" + n : "" + n
    }

    function fmtIndex(v) {
        return (Math.round(v * 100) / 100).toString()
    }

    /// 标题下的说明文字
    ///
    /// 按当前模式给出不同口径的统计：
    /// - local：本地记录聚合后的**部数** + 原始条数
    /// - merged：本地条数 + Bangumi 逐集条数 + "已显示 / 全部"（区分口径，
    ///   否则"900 条"会让人以为本地也看了这么多）
    ///
    /// **不报"同步了多少部"**：那是设置项口径（为什么只有这些条），写在这里
    /// 会和「显示条数」并排出现两个含义不同的数字，看着像自相矛盾（实测反馈）。
    function headerText() {
        var parts = []
        if (source === "local") {
            if (entries.length > 0)
                parts.push("已看 " + entries.length + " 部（原始 "
                           + localEntries.length + " 条记录）")
            else
                parts.push("暂无观看记录")
            return parts.join(" · ")
        }

        // merged：区分"本地播放"与"Bangumi 逐集"两个来源的条数
        if (localEntries.length > 0)
            parts.push("本地 " + localEntries.length + " 条")
        if (bangumiEpisodes.length > 0)
            parts.push("Bangumi 逐集 " + bangumiEpisodes.length + " 条")
        else
            parts.push("未同步逐集记录")
        // 列表被截取时说清口径（还有多少条要靠「加载更多」展开）——
        // 否则"只显示 20 条"看起来像数据丢了
        if (hiddenByLimit > 0)
            parts.push("已显示 " + entries.length + " / " + totalCount + " 条")
        return parts.join(" · ")
    }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        listView.contentY = Math.max(
            0, Math.min(y, listView.contentHeight - listView.height))
    }
}
