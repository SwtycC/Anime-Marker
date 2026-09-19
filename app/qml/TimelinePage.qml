import QtQuick
import QtQuick.Controls

// 动态页：按时间倒序的观看记录，支持两种内容来源。
//
// 内容来源由页面内的分段按钮控制（**不放在设置页**）：
//   local  —— 只显示本地观看记录（`library.timeline()`，纯离线）
//   merged —— 本地记录 + Bangumi「看过」收藏（单独归组排在末尾）
//
// 为什么不放设置页：这是"看当前页面"的临时视图偏好，切换后应立刻见效。
// 放设置页需要"改完→点保存→切页"，多两步且容易让人以为按钮没生效。
//
// 呈现规则：
// - **按动漫聚合**：同一部动漫即使看了多集，也只占一行，
//   行内显示「已看 N 集」，并按"最近看的那一集的时间"排序。
//   这样"看过的动漫"一眼可见，不会被同一部的多集记录淹没。
// - Bangumi 条目（merged 模式）不参与聚合，也不按日期分组。
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

    /// 应用条数上限**之前**的完整列表（含 merged 模式的 Bangumi 条目）
    ///
    /// **踩坑（重要）：这个属性必须是「绑定」，不能被外部赋值。**
    /// 早期 Main.qml 在切到动态页时写了
    ///     `timelinePage.entries = library.timeline(500)`
    /// 以为是在"刷新数据"，实际上 QML 里给一个有绑定的属性赋值会
    /// **永久断开该绑定**，导致后续数据源变化不再触发重算。
    /// 正确做法：外部只改 `localEntries`（数据源），见 reload()。
    property var allEntries: buildEntries()

    /// 实际渲染的列表 = 完整列表按「集级记录条数」取最近 N 条
    ///
    /// 拆成 `allEntries` + `applyLimit()` 两步，是为了能算出"被截掉几条"
    /// （见 hiddenByLimit）—— 否则用户看到的只是"条数变少了"，
    /// 无法判断是设置生效了还是数据没拉到。
    property var entries: applyLimit(allEntries)

    /// 被上限截掉的条数（>0 时列表底部给一句说明）
    readonly property int hiddenByLimit: allEntries.length - entries.length

    /// 分组后的结果（按日期 + 可选的「Bangumi 看过」组）
    property var groups: buildGroups(entries)

    /// 是否正在拉取 Bangumi 看过列表（merged 模式下刷新时）
    readonly property bool busy: typeof inprogress !== "undefined" && inprogress
                                 ? inprogress.running : false

    /// 重新拉取本地观看记录（由外部调用，不碰 entries 本身）
    function reload() {
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
        if (v === "merged" && changed
                && bangumiItems.length === 0 && bangumiEpisodes.length === 0)
            pullBangumi()
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

            // ---- 分组列表 ----
            Repeater {
                model: root.groups

                delegate: Column {
                    required property var modelData

                    width: content.width
                    spacing: Theme.spacingSm

                    // 组标题（今天 / 昨天 / 日期）
                    Text {
                        topPadding: Theme.spacingSm
                        text: modelData.label
                        color: Theme.textSecondary
                        font.pixelSize: Theme.fontSm
                        font.weight: Font.DemiBold
                    }

                    // 组内条目
                    Column {
                        id: groupCol
                        width: parent.width
                        spacing: Theme.lineThin

                        Repeater {
                            model: modelData.items

                            delegate: Rectangle {
                                required property var modelData

                                width: groupCol.width
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
                                            text: "EP" + root.fmtIndex(modelData.epIndex)
                                            color: Theme.accent
                                            font.pixelSize: Theme.fontXs
                                        }
                                    }

                                    // 动漫名
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Math.round((parent.width - 40) * 0.42)
                                        text: modelData.subjectName
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
                                        text: modelData.isBangumi
                                              ? (modelData.epTitle || "")
                                              : "已看 " + modelData.watchedEpCount + " 集"
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
                                            visible: modelData.isBangumi === true
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
                                            text: root.fmtTime(modelData.watchedAt)
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

                                    onEntered: root.hoveredKey = root.rowKey(modelData)
                                    onExited: {
                                        // 只清自己那一行：delegate 重建时可能出现
                                        // "新的 entered 先于旧的 exited" 的次序
                                        if (root.hoveredKey === root.rowKey(modelData))
                                            root.hoveredKey = ""
                                    }

                                    // Bangumi 条目未入库时（subjectId == 0）没有本地详情，
                                    // 用箭头光标提示"这一行不可点"
                                    readonly property bool clickable: modelData.subjectId > 0
                                    cursorShape: clickable ? Qt.PointingHandCursor
                                                           : Qt.ArrowCursor
                                    onClicked: {
                                        if (clickable)
                                            root.subjectClicked(modelData.subjectId)
                                        else
                                            // 未入库 → 没有本地详情页，明确告诉用户
                                            // （早期是静默 return，被当成"点了没反应"的 bug）
                                            root.statusMessage(
                                                "「" + modelData.subjectName
                                                + "」未入库，无法打开详情")
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // ---- 上限提示（被截掉时才出现）----
            Text {
                width: parent.width
                topPadding: Theme.spacingSm
                visible: root.hiddenByLimit > 0
                text: "仅显示最近 " + root.epLimit() + " 条（另有 "
                      + root.hiddenByLimit + " 条未显示）"
                      + " · 可在「设置 → 集级记录条数」调大上限"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
            }

            // ---- 空状态 ----
            Column {
                width: parent.width
                height: 200
                visible: root.entries.length === 0
                spacing: Theme.spacingSm

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
        }
    }

    /// 空状态说明文案
    ///
    /// 前提：动态页展示的是**逐集**记录，而 Bangumi 单集的时间戳字段
    /// （`updated_at`）历史上出现过恒为 0，只能用动漫收藏级的时间兜底 —— 因此
    /// 「最近 N 部」里**没有标记过任何单集**的条目（剧场版、只标了整体
    /// 状态的番）不会产生记录。N 设得太小时可能一位都没有，这不是故障，
    /// 所以给一句解释 + 指引，避免用户以为功能坏了。
    function emptyHint() {
        if (source !== "merged")
            return "播放剧集并在 PotPlayer 中看完后，这里会留下记录"
        if (bangumiItems.length > 0)
            return "最近看过的几部里没有逐集标记\n"
                 + "动态页收录的是逐集标记的记录（整部番状态为看过 / 在看的动画），"
                 + "而最近更新的几部可能只标了整体状态（如剧场版）。\n"
                 + "可在「设置 → 动态显示条数」调大范围，或点「刷新」重新拉取。"
        return "暂无内容 —— 点右上角「刷新」拉取 Bangumi 看过记录"
    }

    /// 按「集级记录条数」截取**最近 N 条**（整页口径：本地 + Bangumi 合并后）。
    ///
    /// **为什么整页截取而不是只截 Bangumi 那部分**：设置项叫「条数」，
    /// 用户设 5 时期待页面上一共 5 条；只截 Bangumi 的话，本地记录一叠加
    /// 又会超过 5，等于设置没生效。
    ///
    /// 规则：
    /// - **仅 merged 模式生效**。「仅本地」是纯离线视图，与 Bangumi 抓取
    ///   范围无关（否则 N=0 时本地页面会被一并清空）。
    /// - `N <= 0`（= 关闭逐集记录）时**不截取**，此时页面只剩本地记录，
    ///   截成 0 条会把本地记录也误伤。
    /// - 传入的 list 已按时间倒序，`slice(0, n)` 即"最近 N 条"。
    function applyLimit(list) {
        var n = epLimit()
        if (source !== "merged" || n <= 0 || list.length <= n)
            return list
        return list.slice(0, n)
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

    // ---- 分组：按「最近观看日期」切分，Bangumi 条目单独成组排最后 ----
    //
    // Bangumi 条目没有 watchedAt，无法按日期归组；统一放进
    // 「Bangumi 看过」组并置于末尾，符合「本地已看在前、
    // Bangumi 收藏在后」的阅读顺序。
    function buildGroups(list) {
        if (!list || list.length === 0)
            return []
        var out = []
        var curLabel = ""
        var curItems = []
        var ipItems = []

        for (var i = 0; i < list.length; i++) {
            var item = list[i]
            if (item.isInProgress) {
                ipItems.push(item)
                continue
            }
            var label = dayLabel(item.watchedAt)
            if (label !== curLabel) {
                if (curItems.length > 0)
                    out.push({ "label": curLabel, "items": curItems })
                curLabel = label
                curItems = []
            }
            curItems.push(item)
        }
        if (curItems.length > 0)
            out.push({ "label": curLabel, "items": curItems })
        if (ipItems.length > 0)
            out.push({ "label": "Bangumi 看过", "items": ipItems })
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
    /// - merged：本地条数 + Bangumi 集级/动漫级条数（区分口径，
    ///   否则"149 条"会让人以为本地也看了这么多）
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
        //
        // **这里不报"抓取了多少部"**。N 兼作抓取范围（部）与显示上限（条），
        // 两个数又都由同一个设置决定，一起写会变成
        //     「Bangumi 逐集 344 条（来自最近 40 部） · 显示最近 40 条」
        // —— 两个 40 含义不同却并排出现，看着像自相矛盾（实测反馈）。
        // 抓取部数是"为什么只有 344 条"的解释，属设置项口径，
        // 写在设置页 hint 与技术文档里即可，页头只报用户看得见的两个数：
        // 拉到多少条、显示多少条。
        if (localEntries.length > 0)
            parts.push("本地 " + localEntries.length + " 条")
        if (bangumiEpisodes.length > 0)
            parts.push("Bangumi 逐集 " + bangumiEpisodes.length + " 条")
        else
            parts.push("未拉取逐集记录")
        // 截取生效时说清口径 —— 否则"只显示 5 条"看起来像数据丢了
        if (hiddenByLimit > 0)
            parts.push("显示最近 " + epLimit() + " 条")
        return parts.join(" · ")
    }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }
}
