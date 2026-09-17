import QtQuick
import QtQuick.Controls

// 在看页（阶段 7）：Bangumi「动画 · 在看」列表 + 本地关联。
//
// 数据来源：`library.inProgress`（读本地缓存表 inprogress_cache）。
// 拉取动作由 `inprogress.refresh()` 触发（QThread + 信号），
// 完成后 LibraryBridge 会 emit inProgressChanged，本页自动重算。
//
// 设计要点（对应旧版 inprogress_page.py）：
// - **离线可用**：缓存表是唯一数据源，断网时照常展示上次结果
// - **本地关联**：有同 bangumi_id 的条目才给「详情」按钮，避免指错
// - 进度用「已看/总集数」的两段式小进度条表达，比纯文字更直观
Item {
    id: root

    // 防御性写法：上下文属性在独立加载本文件时不存在（同 PosterWallPage）
    property var items: typeof library !== "undefined" && library
                        ? library.inProgress : []
    property var meta: typeof library !== "undefined" && library
                       ? library.inProgressMeta() : ({ "count": 0, "stale": true })
    property bool busy: typeof inprogress !== "undefined" && inprogress
                        ? inprogress.running : false

    signal subjectClicked(int subjectId)

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

            // ---- 标题行：标题 + 状态 + 刷新按钮 ----
            Row {
                width: parent.width
                spacing: Theme.spacingMd

                Column {
                    width: parent.width - refreshBtn.width - Theme.spacingMd
                    spacing: 2

                    Text {
                        text: "在看"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontXl
                        font.weight: Font.DemiBold
                    }

                    Text {
                        width: parent.width
                        text: root.statusText()
                        color: root.meta.stale ? Theme.warningColor
                                               : Theme.textTertiary
                        font.pixelSize: Theme.fontSm
                        elide: Text.ElideRight
                    }
                }

                AppButton {
                    id: refreshBtn
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.busy ? "拉取中…" : "刷新"
                    enabled: !root.busy
                    onClicked: {
                        if (typeof inprogress !== "undefined" && inprogress)
                            inprogress.refresh()
                    }
                }
            }

            Rectangle {
                width: parent.width
                height: Theme.lineThin
                color: Theme.border
            }

            // ---- 列表 ----
            Column {
                id: listColumn
                width: parent.width
                spacing: Theme.lineThin

                Repeater {
                    model: root.items

                    delegate: Rectangle {
                        required property var modelData

                        width: listColumn.width
                        height: 76
                        color: mouse.containsMouse ? Theme.hoverFill
                                                   : "transparent"

                        Behavior on color { ColorAnimation { duration: Theme.durFast } }

                        Row {
                            anchors.fill: parent
                            anchors.margins: Theme.spacingSm
                            spacing: Theme.spacingMd

                            // 封面缩略图（在线 URL 或本地缓存）
                            Rectangle {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 44
                                height: 62
                                color: Theme.surfaceAlt
                                border.width: Theme.lineThin
                                border.color: Theme.border
                                clip: true

                                Image {
                                    id: thumb
                                    anchors.fill: parent
                                    source: modelData.coverUrl || ""
                                    fillMode: Image.PreserveAspectCrop
                                    asynchronous: true
                                    cache: true
                                    visible: status === Image.Ready
                                }
                            }

                            // 标题 + 进度
                            Column {
                                anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - 44 - tail.width
                                       - Theme.spacingMd * 2
                                spacing: Theme.spacingXs

                                Text {
                                    width: parent.width
                                    text: modelData.title
                                    color: Theme.textPrimary
                                    font.pixelSize: Theme.fontMd
                                    elide: Text.ElideRight
                                    maximumLineCount: 1
                                }

                                // 日文原名（与中文名不同才显示）
                                Text {
                                    width: parent.width
                                    visible: modelData.nameCn !== ""
                                             && modelData.name !== modelData.nameCn
                                    text: modelData.name
                                    color: Theme.textTertiary
                                    font.pixelSize: Theme.fontSm
                                    elide: Text.ElideRight
                                    maximumLineCount: 1
                                }

                                // 进度条 + 文字
                                Row {
                                    spacing: Theme.spacingSm

                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 120
                                        height: 5
                                        radius: 2.5
                                        color: Theme.surfaceAlt
                                        border.width: Theme.lineThin
                                        border.color: Theme.border

                                        Rectangle {
                                            width: Math.round(parent.width
                                                              * root.epPercent(modelData))
                                            height: parent.height
                                            radius: parent.radius
                                            color: Theme.accent
                                        }
                                    }

                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: root.epText(modelData)
                                        color: Theme.textSecondary
                                        font.pixelSize: Theme.fontSm
                                    }
                                }
                            }

                            // 尾部：本地关联状态 / 详情按钮
                            Row {
                                id: tail
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: Theme.spacingSm

                                Rectangle {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: modelData.inLibrary
                                    width: libLabel.implicitWidth + Theme.spacingMd
                                    height: 20
                                    radius: Theme.radiusSm
                                    color: Theme.accentSoft

                                    Text {
                                        id: libLabel
                                        anchors.centerIn: parent
                                        text: "已入库"
                                        color: Theme.accent
                                        font.pixelSize: Theme.fontXs
                                    }
                                }

                                AppButton {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: modelData.inLibrary
                                    text: "详情"
                                    onClicked: root.subjectClicked(
                                                   modelData.localSubjectId)
                                }
                            }
                        }

                        // 底部分隔线
                        Rectangle {
                            anchors.bottom: parent.bottom
                            width: parent.width
                            height: Theme.lineThin
                            color: Theme.border
                            opacity: 0.6
                        }

                        MouseArea {
                            id: mouse
                            anchors.fill: parent
                            hoverEnabled: true
                            // 让「详情」按钮优先接收点击，避免被整行吃掉
                            propagateComposedEvents: true
                            onClicked: function (m) {
                                if (modelData.inLibrary)
                                    root.subjectClicked(modelData.localSubjectId)
                                m.accepted = false
                            }
                        }
                    }
                }
            }

            // ---- 空状态 ----
            Column {
                width: parent.width
                height: 200
                visible: root.items.length === 0
                spacing: Theme.spacingSm

                Item { width: 1; height: 60 }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.busy ? "正在拉取…" : "暂无在看条目"
                    color: Theme.textSecondary
                    font.pixelSize: Theme.fontLg
                }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.busy
                          ? ""
                          : "点右上角「刷新」从 Bangumi 拉取在看列表"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontMd
                }
            }
        }
    }

    // ---- 文案辅助 ----
    function statusText() {
        if (root.busy)
            return "正在从 Bangumi 拉取…"
        if (root.meta.count === 0)
            return "离线缓存为空"
        var age = root.meta.ageSeconds
        var when = age < 0 ? "未知时间"
                 : age < 60 ? "刚刚更新"
                 : age < 3600 ? Math.floor(age / 60) + " 分钟前更新"
                 : Math.floor(age / 3600) + " 小时前更新"
        return root.meta.count + " 部 · " + when
               + (root.meta.stale ? "（数据较旧，建议刷新）" : "")
    }

    function epPercent(item) {
        if (!item.totalEps || item.totalEps <= 0)
            return 0
        return Math.max(0, Math.min(1, item.epStatus / item.totalEps))
    }

    function epText(item) {
        if (!item.totalEps || item.totalEps <= 0)
            return "已看 " + item.epStatus + " 集"
        return item.epStatus + " / " + item.totalEps + " 集"
    }

    // 数据变化：滚动回顶部
    Connections {
        target: typeof library !== "undefined" && library ? library : null
        function onInProgressChanged() {
            flick.contentY = 0
        }
    }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }
}
