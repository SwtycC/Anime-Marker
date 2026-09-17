import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 订阅页（阶段 7）：RSS 订阅源管理与下载记录。
//
// 本阶段范围：订阅源 CRUD + 绑定本地条目 + 查看下载记录。
// **不含**实际轮询下载 —— 那需要 qBittorrent WebAPI 客户端与 RSS 解析器，
// 数据库表与配置项已就绪，留待后续接入（详见 bridges/rss.py 顶部说明）。
//
// 交互设计：
// - 顶部「添加订阅」展开一个内联表单（比弹窗轻，且不打断阅读）
// - 每条订阅显示：名称 / 地址 / 绑定条目 / 判新规则 / 下载统计 / 启用开关
// - 行尾操作：绑定条目、编辑、删除
Item {
    id: root

    // 防御性写法：上下文属性在独立加载本文件时不存在（同 PosterWallPage）
    property var sources: typeof rss !== "undefined" && rss ? rss.sources : []
    property var downloads: typeof rss !== "undefined" && rss ? rss.downloads : []
    // 可绑定的本地条目（有 bangumi_id 的才算）
    property var linkableSubjects: typeof library !== "undefined" && library
                                   ? library.subjects.filter(function (s) {
                                         return s.bangumiId > 0
                                     })
                                   : []

    property bool addFormVisible: false
    property int editingId: 0          // 0 = 新增模式
    property int bindingId: 0          // 正在选绑定条目的订阅 ID
    property bool showDownloads: false

    signal statusMessage(string text)

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

            // ---- 标题行 ----
            Row {
                width: parent.width
                spacing: Theme.spacingMd

                Column {
                    width: parent.width - addBtn.width - downloadBtn.width
                           - Theme.spacingMd * 2
                    spacing: 2

                    Text {
                        text: "订阅"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontXl
                        font.weight: Font.DemiBold
                    }

                    Text {
                        width: parent.width
                        text: root.sources.length > 0
                              ? root.sources.length + " 个订阅源"
                              : "尚无订阅源，点右侧「添加订阅」开始"
                        color: Theme.textTertiary
                        font.pixelSize: Theme.fontSm
                        elide: Text.ElideRight
                    }
                }

                AppButton {
                    id: downloadBtn
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.showDownloads ? "隐藏记录" : "下载记录"
                    onClicked: root.showDownloads = !root.showDownloads
                }

                AppButton {
                    id: addBtn
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.addFormVisible ? "取消" : "添加订阅"
                    onClicked: {
                        root.addFormVisible = !root.addFormVisible
                        if (root.addFormVisible) {
                            root.editingId = 0
                            nameField.text = ""
                            urlField.text = ""
                            ruleBox.currentValue = "new_only"
                        }
                    }
                }
            }

            Rectangle {
                width: parent.width
                height: Theme.lineThin
                color: Theme.border
            }

            // ---- 新增 / 编辑表单 ----
            Rectangle {
                width: parent.width
                height: formColumn.implicitHeight + Theme.spacingLg * 2
                visible: root.addFormVisible
                color: Theme.surfaceAlt
                border.width: Theme.lineThin
                border.color: Theme.border
                radius: Theme.radiusMd

                Column {
                    id: formColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: Theme.spacingLg
                    spacing: Theme.spacingSm

                    Text {
                        text: root.editingId > 0 ? "编辑订阅" : "添加订阅"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontMd
                        font.weight: Font.DemiBold
                    }

                    FormRow {
                        width: parent.width
                        label: "名称"
                        AppTextField {
                            id: nameField
                            width: parent.width
                            placeholderText: "留空则用域名（如 dmhy）"
                        }
                    }

                    FormRow {
                        width: parent.width
                        label: "订阅地址"
                        AppTextField {
                            id: urlField
                            width: parent.width
                            placeholderText: "https://example.com/rss.xml"
                        }
                    }

                    FormRow {
                        width: parent.width
                        label: "判新规则"
                        // 用分段选择器而非下拉框：只有两个固定选项，
                        // 且能让用户一眼看到另一个选项是什么（详见 SegmentedControl）
                        SegmentedControl {
                            id: ruleBox
                            width: parent.width
                            options: [
                                { "label": "只下新集", "value": "new_only" },
                                { "label": "全部下载", "value": "all" }
                            ]
                            currentValue: "new_only"
                        }
                    }

                    Row {
                        anchors.right: parent.right
                        spacing: Theme.spacingSm

                        AppButton {
                            text: "保存"
                            variant: "primary"
                            onClicked: root.saveForm()
                        }
                    }
                }
            }

            // ---- 订阅源列表 ----
            Column {
                id: listColumn
                width: parent.width
                spacing: Theme.spacingSm

                Repeater {
                    model: root.sources

                    delegate: Rectangle {
                        required property var modelData

                        width: listColumn.width
                        height: cardColumn.implicitHeight + Theme.spacingMd * 2
                        color: Theme.surfaceBg
                        border.width: Theme.lineThin
                        border.color: Theme.border
                        radius: Theme.radiusMd

                        Column {
                            id: cardColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.margins: Theme.spacingMd
                            spacing: Theme.spacingXs

                            // 第一行：名称 + 绑定 + 开关 + 操作
                            Row {
                                width: parent.width
                                spacing: Theme.spacingSm

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: Math.min(implicitWidth, parent.width * 0.4)
                                    text: modelData.name
                                    color: modelData.enabled ? Theme.textPrimary
                                                             : Theme.textTertiary
                                    font.pixelSize: Theme.fontMd
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }

                                // 绑定状态标签
                                Rectangle {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: modelData.subjectName !== ""
                                    width: bindLabel.implicitWidth + Theme.spacingMd
                                    height: 20
                                    radius: Theme.radiusSm
                                    color: Theme.accentSoft

                                    Text {
                                        id: bindLabel
                                        anchors.centerIn: parent
                                        text: "→ " + modelData.subjectName
                                        color: Theme.accent
                                        font.pixelSize: Theme.fontXs
                                        elide: Text.ElideRight
                                    }
                                }

                                Item { width: 1; height: 1; Layout.fillWidth: true }

                                CheckBoxLine {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: "启用"
                                    checked: modelData.enabled
                                    onToggled: function (v) {
                                        if (typeof rss !== "undefined" && rss)
                                            rss.setEnabled(modelData.id, v)
                                    }
                                }
                            }

                            // 第二行：地址
                            Text {
                                width: parent.width
                                text: modelData.url
                                color: Theme.textTertiary
                                font.pixelSize: Theme.fontSm
                                elide: Text.ElideMiddle
                            }

                            // 第三行：规则 + 统计 + 错误
                            Row {
                                width: parent.width
                                spacing: Theme.spacingMd

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.ruleLabel
                                    color: Theme.textSecondary
                                    font.pixelSize: Theme.fontSm
                                }

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: modelData.downloadCount > 0
                                    text: "下载 " + modelData.downloadCount
                                          + "（完成 " + modelData.completedCount
                                          + (modelData.failedCount > 0
                                             ? " · 失败 " + modelData.failedCount : "")
                                          + "）"
                                    color: modelData.failedCount > 0
                                           ? Theme.dangerColor : Theme.textTertiary
                                    font.pixelSize: Theme.fontSm
                                }

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: parent.width - x
                                    visible: modelData.lastError !== ""
                                    text: "⚠ " + modelData.lastError
                                    color: Theme.dangerColor
                                    font.pixelSize: Theme.fontSm
                                    elide: Text.ElideRight
                                }
                            }

                            // 第四行：操作按钮
                            Row {
                                spacing: Theme.spacingSm
                                topPadding: Theme.spacingXs

                                AppButton {
                                    text: modelData.subjectName !== ""
                                          ? "改绑定" : "绑定条目"
                                    onClicked: {
                                        root.bindingId = modelData.id
                                        bindDialog.open()
                                    }
                                }

                                AppButton {
                                    text: "编辑"
                                    onClicked: {
                                        root.editingId = modelData.id
                                        nameField.text = modelData.name
                                        urlField.text = modelData.url
                                        ruleBox.currentValue = modelData.rule
                                        root.addFormVisible = true
                                    }
                                }

                                AppButton {
                                    text: "删除"
                                    variant: "normal"
                                    onClicked: root.confirmDelete(modelData)
                                }
                            }
                        }
                    }
                }
            }

            // ---- 空状态 ----
            Column {
                width: parent.width
                height: 180
                visible: root.sources.length === 0
                spacing: Theme.spacingSm

                Item { width: 1; height: 40 }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "还没有订阅源"
                    color: Theme.textSecondary
                    font.pixelSize: Theme.fontLg
                }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "添加 RSS 订阅后，可以把更新与本地媒体库联动"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontMd
                }
            }

            // ---- 下载记录 ----
            Column {
                width: parent.width
                spacing: Theme.spacingSm
                visible: root.showDownloads

                Rectangle {
                    width: parent.width
                    height: Theme.lineThin
                    color: Theme.border
                }

                Text {
                    topPadding: Theme.spacingSm
                    text: "下载记录"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontLg
                    font.weight: Font.DemiBold
                }

                Text {
                    visible: root.downloads.length === 0
                    text: "暂无下载记录（轮询下载功能尚未接入）"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                }

                Repeater {
                    model: root.downloads

                    delegate: Rectangle {
                        required property var modelData

                        width: parent.width
                        height: 36
                        color: "transparent"

                        Row {
                            anchors.fill: parent
                            spacing: Theme.spacingMd

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 60
                                text: "EP" + root.fmtIndex(modelData.epIndex)
                                color: Theme.accent
                                font.pixelSize: Theme.fontSm
                            }

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - 60 - statusLabel.width
                                       - Theme.spacingMd * 2
                                text: modelData.torrentTitle
                                color: Theme.textPrimary
                                font.pixelSize: Theme.fontSm
                                elide: Text.ElideRight
                            }

                            Text {
                                id: statusLabel
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData.statusLabel
                                color: Theme.textTertiary
                                font.pixelSize: Theme.fontSm
                            }
                        }
                    }
                }
            }
        }
    }

    // ---- 绑定条目弹窗 ----
    Dialog {
        id: bindDialog
        modal: true
        anchors.centerIn: parent
        width: 420
        padding: Theme.spacingLg
        title: "绑定本地条目"

        background: Rectangle {
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: Theme.border
            radius: Theme.radiusMd
        }

        contentItem: Column {
            spacing: Theme.spacingSm

            Text {
                text: "选择该订阅对应的本地动漫："
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
            }

            Flickable {
                width: parent.width
                height: 260
                clip: true
                contentHeight: bindList.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                    id: bindList
                    width: parent.width
                    spacing: Theme.lineThin

                    Repeater {
                        model: root.linkableSubjects

                        delegate: Rectangle {
                            required property var modelData

                            width: bindList.width
                            height: 32
                            color: bindMouse.containsMouse ? Theme.hoverFill
                                                           : "transparent"

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.left: parent.left
                                anchors.leftMargin: Theme.spacingSm
                                width: parent.width - Theme.spacingLg
                                text: modelData.title
                                color: Theme.textPrimary
                                font.pixelSize: Theme.fontSm
                                elide: Text.ElideRight
                            }

                            MouseArea {
                                id: bindMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (typeof rss !== "undefined" && rss)
                                        rss.linkSubject(root.bindingId, modelData.id)
                                    bindDialog.close()
                                }
                            }
                        }
                    }
                }
            }

            Text {
                visible: root.linkableSubjects.length === 0
                text: "没有已匹配 Bangumi 的条目，请先扫描媒体库"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
            }

            Row {
                anchors.right: parent.right

                AppButton {
                    text: "解除绑定"
                    visible: root.currentBindingName() !== ""
                    onClicked: {
                        if (typeof rss !== "undefined" && rss)
                            rss.linkSubject(root.bindingId, 0)
                        bindDialog.close()
                    }
                }
            }
        }
    }

    // ---- 删除确认弹窗 ----
    Dialog {
        id: deleteDialog
        modal: true
        anchors.centerIn: parent
        width: 360
        padding: Theme.spacingLg
        title: "删除订阅"

        property int targetId: 0
        property string targetName: ""

        background: Rectangle {
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: Theme.border
            radius: Theme.radiusMd
        }

        contentItem: Column {
            spacing: Theme.spacingMd

            Text {
                width: parent.width
                text: "确定删除订阅「" + deleteDialog.targetName + "」？\n"
                      + "其下载记录也会一并删除。"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
            }

            Row {
                anchors.right: parent.right
                spacing: Theme.spacingSm

                AppButton {
                    text: "取消"
                    onClicked: deleteDialog.close()
                }

                AppButton {
                    text: "删除"
                    variant: "normal"
                    onClicked: {
                        if (typeof rss !== "undefined" && rss)
                            rss.removeSource(deleteDialog.targetId)
                        deleteDialog.close()
                    }
                }
            }
        }
    }

    // ---- 逻辑 ----
    function saveForm() {
        if (typeof rss === "undefined" || !rss)
            return
        var rule = ruleBox.currentValue
        if (root.editingId > 0)
            rss.updateSource(root.editingId, nameField.text, urlField.text,
                             rule, true)
        else
            rss.addSource(nameField.text, urlField.text, rule)
        root.addFormVisible = false
        root.editingId = 0
    }

    function confirmDelete(item) {
        deleteDialog.targetId = item.id
        deleteDialog.targetName = item.name
        deleteDialog.open()
    }

    function currentBindingName() {
        for (var i = 0; i < root.sources.length; i++) {
            if (root.sources[i].id === root.bindingId)
                return root.sources[i].subjectName
        }
        return ""
    }

    function fmtIndex(v) {
        return (Math.round(v * 100) / 100).toString()
    }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }

    // 桥接层消息 → 状态栏
    Connections {
        target: typeof rss !== "undefined" && rss ? rss : null
        function onMessage(text) { root.statusMessage(text) }
        function onFailed(msg) { root.statusMessage(msg) }
    }
}
