import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 详情页：左封面 + 右信息与集数列表。
//
// 数据全部通过 library 桥接层同步获取（本地 SQLite，无需异步）。
Item {
    id: root

    signal backRequested()

    property int subjectId: 0
    property var subject: ({})
    property var episodes: []
    property var siblings: []

    function load(sid) {
        root.subjectId = sid
        root.subject = library.subject(sid)
        root.episodes = library.episodes(sid)
        root.siblings = library.series_siblings(sid)
        epFlick.contentY = 0
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.pagePadding
        spacing: Theme.spacingLg

        // ---- 顶部：返回 + 重新匹配 ----
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            AppButton {
                text: "← 返回"
                onClicked: root.backRequested()
            }

            Item { Layout.fillWidth: true }

            AppButton {
                text: "重新匹配"
                onClicked: root.rematchRequested(root.subjectId)
            }
        }

        // ---- 中部：左封面 + 右信息 ----
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Theme.spacingXl

            // 封面
            Rectangle {
                Layout.preferredWidth: 240
                Layout.preferredHeight: Math.round(240 * Theme.posterRatio)
                Layout.alignment: Qt.AlignTop
                color: Theme.surfaceAlt
                border.width: Theme.lineThin
                border.color: Theme.border
                radius: Theme.radiusMd
                clip: true

                Image {
                    id: detailCover
                    anchors.fill: parent
                    source: root.subject.coverUrl || ""
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    visible: status === Image.Ready && source != ""
                }

                Text {
                    anchors.centerIn: parent
                    visible: !detailCover.visible
                    text: "无封面"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                }
            }

            // 信息 + 集数
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Theme.spacingSm

                Text {
                    Layout.fillWidth: true
                    text: root.subject.title || ""
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontXl
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                    elide: Text.ElideRight
                    maximumLineCount: 2
                }

                Text {
                    Layout.fillWidth: true
                    visible: (root.subject.name || "") !== ""
                    text: root.subject.name || ""
                    color: Theme.textSecondary
                    font.pixelSize: Theme.fontMd
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: root.buildMeta()
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                    elide: Text.ElideRight
                }

                // 同系列切换
                Row {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm
                    visible: root.siblings.length > 0

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "同系列："
                        color: Theme.textTertiary
                        font.pixelSize: Theme.fontSm
                    }

                    Repeater {
                        model: root.siblings
                        delegate: Rectangle {
                            required property var modelData
                            width: sibLabel.implicitWidth + Theme.spacingMd * 2
                            height: 26
                            radius: Theme.radiusSm
                            color: sibMouse.containsMouse ? Theme.accentSoft
                                                          : Theme.fade(Theme.accentSoft)
                            border.width: Theme.lineThin
                            border.color: sibMouse.containsMouse ? Theme.accent : Theme.border

                            Behavior on color { ColorAnimation { duration: Theme.durFast } }

                            Text {
                                id: sibLabel
                                anchors.centerIn: parent
                                text: root.shortName(modelData.title)
                                color: Theme.textPrimary
                                font.pixelSize: Theme.fontSm
                            }

                            MouseArea {
                                id: sibMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.load(modelData.id)
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: Theme.lineThin
                    color: Theme.border
                }

                // ---- 集数列表 ----
                Flickable {
                    id: epFlick
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    contentWidth: width
                    contentHeight: epColumn.implicitHeight
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded }

                    Column {
                        id: epColumn
                        width: epFlick.width
                        spacing: Theme.lineThin

                        Repeater {
                            model: root.episodes

                            delegate: Rectangle {
                                required property var modelData

                                width: epColumn.width
                                height: 40
                                color: epMouse.containsMouse ? Theme.hoverFillStrong
                                                             : Theme.fade(Theme.hoverFillStrong)

                                Behavior on color { ColorAnimation { duration: Theme.durFast } }

                                Row {
                                    anchors.fill: parent
                                    anchors.leftMargin: Theme.spacingMd
                                    anchors.rightMargin: Theme.spacingMd
                                    spacing: Theme.spacingMd

                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 46
                                        text: root.fmtIndex(modelData.epIndex)
                                        color: Theme.textTertiary
                                        font.pixelSize: Theme.fontSm
                                    }

                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: parent.width - 46 - markLabel.width
                                               - Theme.spacingMd * 3
                                        text: modelData.title
                                        color: modelData.watched
                                             ? Theme.textTertiary : Theme.textPrimary
                                        font.pixelSize: Theme.fontMd
                                        elide: Text.ElideRight
                                    }

                                    Text {
                                        id: markLabel
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: modelData.watched ? "✓" : ""
                                        color: Theme.successColor
                                        font.pixelSize: Theme.fontMd
                                    }
                                }

                                Rectangle {
                                    anchors.bottom: parent.bottom
                                    width: parent.width
                                    height: Theme.lineThin
                                    color: Theme.border
                                    opacity: 0.6
                                }

                                MouseArea {
                                    id: epMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    onClicked: root.playEpisode(modelData.id)
                                }
                            }
                        }

                        // 空状态
                        Text {
                            width: epColumn.width
                            height: 80
                            visible: root.episodes.length === 0
                            text: "没有集数"
                            color: Theme.textTertiary
                            font.pixelSize: Theme.fontMd
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        // 底部留白，避免最后一行被悬浮导航遮挡
                        Item {
                            width: 1
                            height: Theme.navContentGutter
                        }
                    }
                }
            }
        }
    }

    // ---- 信号（由 Main.qml 接）----
    signal playEpisode(int episodeId)
    signal rematchRequested(int subjectId)

    // ---- 文案辅助 ----
    function buildMeta() {
        var parts = []
        if (root.subject.totalEps > 0)
            parts.push("共 " + root.subject.totalEps + " 集")
        if (root.subject.folderPath)
            parts.push(root.subject.folderPath)
        if (root.subject.matchState === "pending")
            parts.push("⚠ 匹配待确认，请点右上角「重新匹配」")
        else if (root.subject.matchState === "manual")
            parts.push("已手动指定")
        return parts.join(" · ")
    }

    function fmtIndex(v) {
        // 12 → "12"，12.5 → "12.5"（去掉多余的 .0）
        return (Math.round(v * 100) / 100).toString()
    }

    /// 同系列按钮上的短名：去掉与系列名重复的部分
    function shortName(full) {
        var series = root.subject.seriesName || ""
        if (series && full.indexOf(series) === 0) {
            var rest = full.substring(series.length).replace(/^[\s\-_·:：]+/, "")
            if (rest.length > 0)
                return rest
        }
        return full
    }
}
