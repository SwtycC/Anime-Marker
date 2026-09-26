import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects
import QtQuick.Window      // Screen.devicePixelRatio（封面解码尺寸用）

// 手动匹配对话框（原 ui/match_dialog.py 的 QML 版）。
//
// 流程：
//   打开 → 自动用「本地条目名」作为关键词搜一次 → 候选按匹配分排序展示
//   → 用户选定 → matcher.apply(bangumiId) 写入 match_state='manual'
//
// 结果分档着色（与旧版一致）：
//   - 相关（score >= 60）      ：正常显示
//   - 低分（0 < score < 60）   ：次要文字色
//   - 被否决（<= -1000）       ：更淡 + 标「不相关」，但仍**可选中**
//     （用户可能确实要一部名字不同的作品，不做硬拦截）
Window {
    id: dlg

    // ---- 入参 ----
    property int subjectId: 0
    property string subjectTitle: ""

    // ---- 状态 ----
    property var results: []
    property int selectedIndex: -1
    property bool searching: false

    // 应用成功后通知外部刷新
    signal applied(int subjectId)

    width: 760
    height: 560
    minimumWidth: 640
    minimumHeight: 420
    modality: Qt.ApplicationModal
    title: "重新匹配 Bangumi 条目"
    color: Theme.windowBg
    flags: Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint

    /// 打开对话框（外部调用）
    function open(subjectId, subjectTitle) {
        dlg.subjectId = subjectId
        dlg.subjectTitle = subjectTitle
        dlg.selectedIndex = -1
        dlg.results = []
        keywordField.text = typeof matcher !== "undefined" && matcher
                            ? matcher.defaultKeyword(subjectId) : ""
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
        doSearch()
    }

    function doSearch() {
        if (typeof matcher === "undefined" || !matcher)
            return
        dlg.searching = true
        matcher.search(dlg.subjectId, keywordField.text)
    }

    // ================= 顶部：说明 + 搜索行 =================
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.pagePadding
        spacing: Theme.spacingMd

        // ---- 当前条目 ----
        Column {
            Layout.fillWidth: true
            spacing: 2

            Text {
                width: parent.width
                text: dlg.subjectTitle
                color: Theme.textPrimary
                font.pixelSize: Theme.fontLg
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                maximumLineCount: 1
            }
            Text {
                width: parent.width
                text: "从 Bangumi 搜索结果中指定正确条目"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
            }
        }

        // ---- 搜索行 ----
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            AppTextField {
                id: keywordField
                Layout.fillWidth: true
                placeholder: "搜索关键词"
                onAccepted: dlg.doSearch()
            }

            AppButton {
                text: "搜索"
                variant: "primary"
                enabled: !dlg.searching
                onClicked: dlg.doSearch()
            }
        }

        // ---- 状态行 ----
        Text {
            Layout.fillWidth: true
            text: dlg.searching ? "搜索中…"
                : (dlg.results.length > 0
                   ? ("共 " + dlg.results.length + " 条结果（按匹配分排序）")
                   : "没有结果")
            color: Theme.textTertiary
            font.pixelSize: Theme.fontSm
        }

        // ---- 结果列表 ----
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: Theme.border
            radius: Theme.radiusMd
            clip: true

            ListView {
                id: resultList
                anchors.fill: parent
                anchors.margins: 1
                clip: true
                model: dlg.results
                spacing: 0
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: AppScrollBar { }

                delegate: Rectangle {
                    required property var modelData
                    required property int index

                    readonly property bool isSelected: dlg.selectedIndex === index
                    readonly property bool isRejected: modelData.rejected === true
                    readonly property bool isLow: modelData.lowScore === true

                    width: resultList.width
                    // 82 = 原先 64 + 别名行（18）。加高是为了让别名有一整行
                    // 的位置，不必挤在元信息那行里被 elide 掉。
                    height: 82
                    color: isSelected ? Theme.accentSoft
                         : rowMouse.containsMouse ? Theme.hoverFillStrong
                         : Theme.fade(Theme.hoverFillStrong)

                    Behavior on color { ColorAnimation { duration: Theme.durFast } }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Theme.spacingMd
                        anchors.rightMargin: Theme.spacingMd
                        spacing: Theme.spacingMd

                        // 缩略图
                        Rectangle {
                            Layout.preferredWidth: 36
                            Layout.preferredHeight: 48
                            Layout.alignment: Qt.AlignVCenter
                            color: Theme.surfaceAlt
                            radius: Theme.radiusSm
                            clip: true

                            Image {
                                id: thumb
                                anchors.fill: parent
                                source: modelData.coverUrl || ""
                                fillMode: Image.PreserveAspectCrop
                                asynchronous: true
                                visible: status === Image.Ready

                                // 36×48 的缩略图是 1200+ 宽原图的 34 倍缩小，
                                // 不给 sourceSize 会糊成一团（顺带省下解码内存）
                                sourceSize.width: Math.round(width * Screen.devicePixelRatio * 2)
                                sourceSize.height: Math.round(height * Screen.devicePixelRatio * 2)
                            }
                        }

                        // 名称 + 元信息
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            Text {
                                Layout.fillWidth: true
                                text: (dlg.selectedIndex === index ? "● " : "")
                                      + modelData.title
                                      + (modelData.name && modelData.name !== modelData.title
                                         ? "  ／  " + modelData.name : "")
                                color: isRejected ? Theme.textTertiary
                                     : isLow ? Theme.textSecondary
                                     : Theme.textPrimary
                                font.pixelSize: Theme.fontMd
                                elide: Text.ElideRight
                                maximumLineCount: 1
                            }

                            Text {
                                Layout.fillWidth: true
                                text: (modelData.year || "----")
                                      + " · " + modelData.totalEps + " 集"
                                      + " · bgm " + modelData.bangumiId
                                      + " · 匹配分 "
                                      + (isRejected ? "不相关" : modelData.score)
                                      + "（" + modelData.reason + "）"
                                color: Theme.textTertiary
                                font.pixelSize: Theme.fontXs
                                elide: Text.ElideRight
                                maximumLineCount: 1
                            }

                            // 别名：用俗称搜到的候选，靠这行才能确认"就是这部"
                            // （如「未闻花名」↔「我们仍未知道那天所看见的花的名字。」）
                            Text {
                                Layout.fillWidth: true
                                visible: !!modelData.aliases
                                text: "别名：" + (modelData.aliases || "")
                                color: Theme.textTertiary
                                font.pixelSize: Theme.fontXs
                                elide: Text.ElideRight
                                maximumLineCount: 1
                            }
                        }
                    }

                    // 底部 1px 分隔
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
                        onClicked: dlg.selectedIndex = index
                        onDoubleClicked: {
                            dlg.selectedIndex = index
                            dlg.applySelected()
                        }
                    }
                }
            }
        }

        // ---- 底部按钮 ----
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Text {
                Layout.fillWidth: true
                text: {
                    if (dlg.selectedIndex < 0)
                        return "双击可快速应用"
                    return "已选择：" + dlg.results[dlg.selectedIndex].title
                }
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                elide: Text.ElideRight
            }

            AppButton {
                text: "取消"
                onClicked: dlg.close()
            }

            AppButton {
                text: "使用选中条目"
                variant: "primary"
                enabled: dlg.selectedIndex >= 0
                onClicked: dlg.applySelected()
            }
        }
    }

    // ================= 动作 =================
    function applySelected() {
        if (dlg.selectedIndex < 0 || typeof matcher === "undefined" || !matcher)
            return
        var item = dlg.results[dlg.selectedIndex]
        if (!item)
            return
        if (matcher.apply(item.bangumiId)) {
            dlg.applied(dlg.subjectId)
            dlg.close()
        }
    }

    // ================= 桥接信号 =================
    Connections {
        target: typeof matcher !== "undefined" && matcher ? matcher : null

        function onSearchFinished(results) {
            dlg.searching = false
            dlg.results = results
            dlg.selectedIndex = results.length > 0 ? 0 : -1
        }

        function onSearchStarted() {
            dlg.searching = true
            dlg.results = []
            dlg.selectedIndex = -1
        }

        function onFailed(msg) {
            dlg.searching = false
            console.log("手动匹配失败:", msg)
        }

        function onApplied(subjectId, nameCn) {
            console.log("手动匹配成功:", subjectId, nameCn)
        }
    }
}
