import QtQuick
import QtQuick.Controls
import QtQuick.Window      // Screen.devicePixelRatio（封面解码尺寸用）

// 在看页（阶段 7）：Bangumi「动画 · 在看」列表 + 本地关联。
//
// 页面语义是"我正在追的番"（导航栏那一项也写着「在看」）。
// 「看过」的番不在这里 —— 它们数量多（实测 149 部），时间线在「动态」页更合适。
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

    /// 未入库的行点不出详情 —— 由页面发提示（Main.qml 接到状态栏）
    signal statusMessage(string text)

    /// 请求播放某一集（由 Main.qml 转给 player.playEpisode，与详情页一致）
    signal playEpisode(int episodeId)

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
                        color: rowHover.hovered ? Theme.hoverFillStrong
                                                : Theme.fade(Theme.hoverFillStrong)

                        Behavior on color { ColorAnimation { duration: Theme.durFast } }

                        // ---- 整行点击区：必须声明在内容之前（层级最低）----
                        //
                        // QML 里**后声明的兄弟项在上层、优先接收事件**。早期这个
                        // MouseArea 写在最后（层级最高）盖住了「详情」按钮，于是靠
                        // `propagateComposedEvents: true` + `accepted = false`
                        // 把点击"漏"给下面的按钮。那个 hack 的副作用是致命的：
                        // 点击会**继续往更下层同步传播**，而按钮的处理函数在传播
                        // 途中就把页面切到了详情页 —— 同一次点击于是又落到了详情页
                        // 的集数行上（那行是点击即播放）→
                        // **「详情」一点就开始播放**（实测反馈）。
                        //
                        // 放回最前面后：按钮自己接收点击，空白处归本 MouseArea，
                        // 两条路互不干扰，不需要任何传播技巧。
                        // （Row / Text / Image 这类没有事件处理器的项不会吃掉点击，
                        //   所以放在下面照样能收到。）
                        MouseArea {
                            id: mouse
                            anchors.fill: parent
                            // 悬停状态交给 HoverHandler（见下），这里只负责点击
                            // 未入库的行没有本地详情可开，光标明确提示不可点
                            // （实测 11 部「在看」里通常有 3~4 部未入库）
                            cursorShape: modelData.inLibrary ? Qt.PointingHandCursor
                                                             : Qt.ArrowCursor
                            onClicked: {
                                if (modelData.inLibrary)
                                    root.subjectClicked(modelData.localSubjectId)
                                else
                                    root.statusMessage(
                                        "「" + modelData.title
                                        + "」未入库，无法打开详情")
                            }
                        }

                        // 整行悬停高亮。
                        // 用 HoverHandler 而不是 MouseArea.containsMouse：后者会被
                        // 「详情」按钮的 MouseArea 抢走（鼠标移到按钮上时整行高亮会掉），
                        // 而 HoverHandler 不参与这种独占，悬在按钮上整行依然亮。
                        HoverHandler {
                            id: rowHover
                        }

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

                                    // 44×62 的缩略图是 1200+ 宽原图的 28 倍缩小，
                                    // 不给 sourceSize 会糊成一团（顺带省下解码内存）
                                    sourceSize.width: Math.round(width * Screen.devicePixelRatio * 2)
                                    sourceSize.height: Math.round(height * Screen.devicePixelRatio * 2)
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

                                // 「下一集」：直接接着看那一集。
                                // 集号由后端按 Bangumi 的「已看到第 N 集」算出
                                // （见 LibraryBridge._next_episode），与左侧进度条
                                // 「5 / 12 集」同一口径。
                                //
                                // **按钮不隐藏**（实测要求）：播不了时把"为什么播不了"
                                // 报到状态栏 —— 没入库 / 本地已看到最新 / 集号数据异常，
                                // 三种情况的处置完全不同，藏掉按钮等于把信息也藏掉了。
                                AppButton {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: "下一集"
                                    onClicked: {
                                        if (modelData.nextEpisodeId > 0)
                                            root.playEpisode(modelData.nextEpisodeId)
                                        else
                                            root.statusMessage(
                                                "「" + modelData.title + "」"
                                                + (modelData.nextEpisodeHint
                                                   || "没有可播放的下一集"))
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
