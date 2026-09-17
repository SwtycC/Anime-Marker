import QtQuick
import QtQuick.Controls

// 动态页（阶段 7）：按时间倒序的观看记录。
//
// 数据来源：`library.timeline()`（episodes JOIN subjects，取 watched=1）。
// 纯本地查询，无需网络，因此直接同步取数（不走 QThread）。
//
// 呈现方式（对应旧版 timeline_page.py）：
// - 按「日期」分组：今天 / 昨天 / 具体日期，组间有分隔标题
// - 每条显示：集序号 + 动漫名 + 集标题 + 相对时间
// - 点击跳详情页
//
// 分组实现：QML 侧用一次遍历把扁平列表切成 [[组名, [条目...]], ...]。
// 之所以放 QML 而不是 Python，是因为分组规则纯属展示逻辑，
// 以后想改成「按周分组」不必动后端。
Item {
    id: root

    property var entries: typeof library !== "undefined" && library
                          ? library.timeline(500) : []

    signal subjectClicked(int subjectId)

    // 懒计算的分组结果：entries 变化时重算
    property var groups: buildGroups(entries)

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

            // ---- 标题 ----
            Column {
                width: parent.width
                spacing: 2

                Text {
                    text: "动态"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontXl
                    font.weight: Font.DemiBold
                }

                Text {
                    text: root.entries.length > 0
                          ? "共 " + root.entries.length + " 条观看记录"
                          : "暂无观看记录"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
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

                                    // 集序号徽标
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

                                    // 集标题（可能为空）
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: parent.width - 40 - Math.round((parent.width - 40) * 0.42)
                                               - timeLabel.width - Theme.spacingSm * 2
                                        text: modelData.epTitle
                                        color: Theme.textTertiary
                                        font.pixelSize: Theme.fontSm
                                        elide: Text.ElideRight
                                    }

                                    // 相对时间
                                    Text {
                                        id: timeLabel
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: root.fmtTime(modelData.watchedAt)
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
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.subjectClicked(modelData.subjectId)
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
                    text: "播放剧集并在 PotPlayer 中看完后，这里会留下记录"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontMd
                }
            }
        }
    }

    // ---- 分组：把扁平列表按日期切成 [ {label, items}, ... ] ----
    function buildGroups(list) {
        if (!list || list.length === 0)
            return []
        var out = []
        var curLabel = ""
        var curItems = []
        for (var i = 0; i < list.length; i++) {
            var label = dayLabel(list[i].watchedAt)
            if (label !== curLabel) {
                if (curItems.length > 0)
                    out.push({ "label": curLabel, "items": curItems })
                curLabel = label
                curItems = []
            }
            curItems.push(list[i])
        }
        if (curItems.length > 0)
            out.push({ "label": curLabel, "items": curItems })
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

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }
}
