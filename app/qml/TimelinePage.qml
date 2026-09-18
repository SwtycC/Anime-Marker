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

    // 本地观看记录（原始，按集）
    property var localEntries: typeof library !== "undefined" && library
                               ? library.timeline(500) : []
    // Bangumi「看过」收藏（merged 模式下使用）
    property var bangumiItems: typeof library !== "undefined" && library
                               ? library.inProgress : []

    signal subjectClicked(int subjectId)

    /// 按动漫聚合后的条目列表（含 merged 模式的 Bangumi 条目）
    ///
    /// **踩坑（重要）：这个属性必须是「绑定」，不能被外部赋值。**
    /// 早期 Main.qml 在切到动态页时写了
    ///     `timelinePage.entries = library.timeline(500)`
    /// 以为是在"刷新数据"，实际上 QML 里给一个有绑定的属性赋值会
    /// **永久断开该绑定**，导致后续数据源变化不再触发重算。
    /// 正确做法：外部只改 `localEntries`（数据源），见 reload()。
    property var entries: buildEntries()

    /// 分组后的结果（按日期 + 可选的「Bangumi 看过」组）
    property var groups: buildGroups(entries)

    /// 是否正在拉取 Bangumi 看过列表（merged 模式下刷新时）
    readonly property bool busy: typeof inprogress !== "undefined" && inprogress
                                 ? inprogress.running : false

    /// 重新拉取本地观看记录（由外部调用，不碰 entries 本身）
    function reload() {
        if (typeof library !== "undefined" && library)
            localEntries = library.timeline(500)
    }

    /// 切换内容来源。切到 merged 且缓存为空时自动拉一次，
    /// 避免用户切过来看到空白还得再点一次刷新。
    function setSource(v) {
        if (v !== "local" && v !== "merged")
            return
        var changed = (source !== v)
        source = v
        if (v === "merged" && changed && bangumiItems.length === 0)
            pullBangumi()
    }

    /// 触发一次 Bangumi 收藏拉取（QThread，完成后
    /// InProgressBridge 回调 library.reloadInProgress()，
    /// 进而让 bangumiItems 的绑定重算）
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

    /// 把「按集」的原始记录**按动漫聚合**，merged 时再追加 Bangumi 条目。
    ///
    /// 聚合规则：
    /// - 同一 subjectId 只保留一条
    /// - `watchedAt` 取该动漫**最近一次**观看时间（决定排序位置）
    /// - `epIndex` 取最近看的那一集序号（展示"看到第几集"）
    /// - `watchedEpCount` 为该动漫的已看集数（展示"已看 N 集"）
    ///
    /// 原始列表已按 watchedAt 倒序，因此首次遇到某部动漫时
    /// 那一条就是它最新的记录，直接采信即可。
    function buildEntries() {
        var seen = {}
        var out = []
        for (var i = 0; i < localEntries.length; i++) {
            var e = localEntries[i]
            var key = String(e.subjectId)
            if (seen[key] !== undefined) {
                // 已收录：只累加集数，保留首条（最新）的时间与集号
                out[seen[key]].watchedEpCount += 1
                continue
            }
            seen[key] = out.length
            out.push({
                "subjectId": e.subjectId,
                "subjectName": e.subjectName,
                "epIndex": e.epIndex,
                "epTitle": e.epTitle,
                "watchedAt": e.watchedAt,
                "watchedEpCount": 1,
                "isInProgress": false
            })
        }

        // merged：追加 Bangumi 看过条目（不聚合，每条独立）
        if (source === "merged") {
            for (var j = 0; j < bangumiItems.length; j++) {
                var it = bangumiItems[j]
                out.push({
                    // 未入库时没有本地条目可跳，subjectId = 0
                    "subjectId": it.localSubjectId,
                    "subjectName": it.title,
                    "epIndex": it.epStatus,
                    "epTitle": "",
                    "watchedAt": "",
                    "watchedEpCount": 0,
                    "isInProgress": true,
                    "totalEps": it.totalEps,
                    "inLibrary": it.inLibrary
                })
            }
        }
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
                        { "label": "本地 + Bangumi 看过", "value": "merged" }
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
                                color: rowMouse.containsMouse ? Theme.hoverFill
                                                              : "transparent"

                                Behavior on color { ColorAnimation { duration: Theme.durFast } }

                                Row {
                                    anchors.fill: parent
                                    anchors.leftMargin: Theme.spacingMd
                                    anchors.rightMargin: Theme.spacingMd
                                    spacing: Theme.spacingSm

                                    // 序号徽标：
                                    // 已看记录显示最近看的那一集「EP11」，
                                    // Bangumi 条目显示「看过」（ep_status 可能为 0，
                                    // 显示「EP0」没有意义）
                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Math.max(28, epLabel.implicitWidth + 10)
                                        height: 20
                                        radius: Theme.radiusSm
                                        color: modelData.isInProgress
                                               ? Theme.warningColor
                                               : Theme.accentSoft

                                        Text {
                                            id: epLabel
                                            anchors.centerIn: parent
                                            text: modelData.isInProgress
                                                  ? "看过"
                                                  : "EP" + root.fmtIndex(modelData.epIndex)
                                            color: modelData.isInProgress
                                                   ? "#FFFFFF"
                                                   : Theme.accent
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

                                    // 中列：
                                    // - 已看记录 → 已看集数
                                    // - Bangumi 条目 → 总集数 + 是否入库
                                    Text {
                                        id: epCountLabel
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Math.max(implicitWidth, 56)
                                        text: modelData.isInProgress
                                              ? root.inProgressCaption(modelData)
                                              : "已看 " + modelData.watchedEpCount + " 集"
                                        color: Theme.textSecondary
                                        font.pixelSize: Theme.fontSm
                                    }

                                    // 右列：
                                    // - 已看记录 → 相对时间
                                    // - Bangumi 条目 → 看到第几集
                                    Text {
                                        id: timeLabel
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: modelData.isInProgress
                                              ? root.inProgressProgress(modelData)
                                              : root.fmtTime(modelData.watchedAt)
                                        color: Theme.textTertiary
                                        font.pixelSize: Theme.fontSm
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
                                    // Bangumi 条目未入库时（subjectId == 0）无处可跳，
                                    // 用箭头光标提示"这一行不可点"
                                    readonly property bool clickable:
                                        modelData.isInProgress
                                        ? modelData.subjectId > 0 : true
                                    cursorShape: clickable ? Qt.PointingHandCursor
                                                           : Qt.ArrowCursor
                                    onClicked: {
                                        if (clickable)
                                            root.subjectClicked(modelData.subjectId)
                                    }
                                }
                            }
                        }
                    }
                }
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
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.source === "merged"
                          ? "暂无内容 —— 可点右上角「刷新」拉取 Bangumi 看过列表"
                          : "播放剧集并在 PotPlayer 中看完后，这里会留下记录"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontMd
                }
            }
        }
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

    /// 把 ISO 时间串转成「今天 / 昨天 / YYYY-MM-DD」
    function dayLabel(iso) {
        var d = parseDate(iso)
        if (!d)
            return "未知日期"
        var today = new Date()
        var diff = Math.floor((startOfDay(today) - startOfDay(d)) / 86400000)
        if (diff === 0)
            return "今天"
        if (diff === 1)
            return "昨天"
        if (diff === 2)
            return "前天"
        return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
    }

    /// 相对时间：刚刚 / N 分钟前 / HH:MM / MM-DD
    function fmtTime(iso) {
        var d = parseDate(iso)
        if (!d)
            return ""
        var now = new Date()
        var sec = (now - d) / 1000
        if (sec < 60)
            return "刚刚"
        if (sec < 3600)
            return Math.floor(sec / 60) + " 分钟前"
        if (startOfDay(d).getTime() === startOfDay(now).getTime())
            return pad2(d.getHours()) + ":" + pad2(d.getMinutes())
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
    /// 已看记录部分同时说明「部数」与「原始记录条数」：因为列表按动漫
    /// 聚合，只说"条"会让人误以为同一部的其他集被漏掉了。
    function headerText() {
        var localCount = entries.length - (source === "merged" ? bangumiItems.length : 0)
        var parts = []
        if (localCount > 0)
            parts.push("已看 " + localCount + " 部（原始 "
                       + localEntries.length + " 条记录）")
        else
            parts.push("暂无观看记录")
        if (source === "merged")
            parts.push("Bangumi 看过 " + bangumiItems.length + " 部")
        return parts.join(" · ")
    }

    /// Bangumi 条目的中列文案：总集数 + 是否已入库
    function inProgressCaption(item) {
        var parts = []
        if (item.totalEps > 0)
            parts.push("共 " + item.totalEps + " 集")
        parts.push(item.inLibrary ? "本地已有" : "未入库")
        return parts.join(" · ")
    }

    /// Bangumi 条目的右列文案：看到第几集
    function inProgressProgress(item) {
        if (!item.epIndex || item.epIndex <= 0)
            return "尚未开始"
        return "看到第 " + fmtIndex(item.epIndex) + " 集"
    }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }
}
