import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 详情页：左封面 + 右信息与集数列表。
//
// 数据全部通过 library 桥接层同步获取（本地 SQLite，无需异步）。
Item {
    id: root

    signal backRequested()
    signal statusMessage(string text)

    property int subjectId: 0
    property var subject: ({})
    property var episodes: []
    property var siblings: []
    // 外部 SVG 图标的目录基地址（与 NavIcon 同一来源，见 QmlApp 注入）
    property string iconsBase: typeof iconsBaseUrl !== "undefined"
                               ? iconsBaseUrl : ""

    // ---- 标签状态 ----
    // tagRows：展示用（deleted 项过滤掉）；编辑态单独存 editApi / editUser，
    // 不直接改 tagRows —— 「取消」时丢弃即可，无需回滚。
    property var tagRows: []
    property bool editingTags: false
    property var editApi: []        // [{name, deleted}]
    property var editUser: []       // [string]
    property string newTagText: ""

    function load(sid) {
        root.subjectId = sid
        root.subject = library.subject(sid)
        root.episodes = library.episodes(sid)
        root.siblings = library.series_siblings(sid)
        root.resetTagState()
        // 存量条目自动补拉标签（异步，不阻塞界面；已有 tag 时是空操作）。
        // 失败经状态栏提示；下次进入本页会自动重试，无需手动入口。
        if (root.tagRows.length === 0 && (root.subject.bangumiId || 0) > 0
                && typeof library !== "undefined" && library)
            library.requestTagFetch(root.subjectId)
        epFlick.contentY = 0
    }

    /// 从桥接层重取 tag 并复位编辑态（打开页面 / 保存成功后调用）
    function resetTagState() {
        if (typeof library !== "undefined" && library)
            root.tagRows = library.subjectTags(root.subjectId)
        else
            root.tagRows = []
        root.editingTags = false
        root.editApi = []
        root.editUser = []
        root.newTagText = ""
    }

    /// 展示用的 tag（去掉被删除的接口 tag）
    function activeTags() {
        var out = []
        for (var i = 0; i < root.tagRows.length; i++)
            if (!root.tagRows[i].deleted)
                out.push(root.tagRows[i].name)
        return out
    }

    /// 进入编辑态：从当前数据复制一份草稿
    ///
    /// **踩坑（push 不触发绑定重算）**：`property var` 数组只有**整体
    /// 赋值**才会发出变更通知；`root.editApi.push(...)` 是原地修改，
    /// Repeater 的 `model: root.editApi` 绑定不会重算 —— 先赋空数组再
    /// 逐个 push，结果就是编辑面板打开了（editingTags 通知了），
    /// chip 却永远空白。必须先在局部变量里组装好，最后一次性赋值。
    /// （toggleApiTag / addUserTag 等函数同理，都是"组装 → 整体赋值"。）
    function startEditTags() {
        var api = []
        var user = []
        for (var i = 0; i < root.tagRows.length; i++) {
            var t = root.tagRows[i]
            if (t.isApi)
                api.push({ "name": t.name, "deleted": t.deleted === true })
            else
                user.push(t.name)
        }
        root.editApi = api
        root.editUser = user
        root.editingTags = true
    }

    /// 编辑态点接口 tag：切换删除 / 恢复（不立即写库，确定时统一提交）
    function toggleApiTag(index) {
        var next = []
        for (var i = 0; i < root.editApi.length; i++) {
            var t = root.editApi[i]
            if (i === index)
                t.deleted = !t.deleted
            next.push(t)
        }
        root.editApi = next
    }

    /// 编辑态点用户 tag：直接从草稿移除（用户加的没有"恢复"一说）
    function removeUserTag(index) {
        var next = []
        for (var i = 0; i < root.editUser.length; i++)
            if (i !== index)
                next.push(root.editUser[i])
        root.editUser = next
    }

    /// 把输入框里的文字添加为用户 tag（去重、去空白）
    function addUserTag() {
        var name = root.newTagText.trim()
        if (!name)
            return
        for (var i = 0; i < root.editUser.length; i++)
            if (root.editUser[i].toLowerCase() === name.toLowerCase())
                return          // 已存在，静默忽略
        var next = root.editUser.slice()
        next.push(name)
        root.editUser = next
        root.newTagText = ""
    }

    /// 确定：把草稿提交给桥接层
    function confirmTags() {
        if (typeof library === "undefined" || !library)
            return
        var r = library.saveSubjectTags(root.subjectId, root.editApi, root.editUser)
        root.statusMessage(r.message || (r.ok ? "标签已保存" : "保存失败"))
        if (r.ok === true)
            root.resetTagState()
    }

    // 点击页面空白处：让输入框交出键盘焦点（恢复原状）。
    //
    // **为什么需要**：QML 里点击"别的控件"会移走焦点，但点击**空白区域**
    // 不会 —— 没有任何东西接管焦点，TextInput 的 activeFocus 一直保持，
    // 表现为"边框和浮起的「标签」停在激活态"。
    // TapHandler 非独占（DragThreshold 策略）：不影响 Flickable 拖拽，
    // 点在按钮 / 集数行等独占 grab 的控件上时自动让位（不触发）。
    TapHandler {
        onTapped: root.forceActiveFocus()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.pagePadding
        spacing: Theme.spacingLg

        // ---- 顶部：返回 + 更换海报 + 重新匹配 ----
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            AppButton {
                text: "← 返回"
                onClicked: root.backRequested()
            }

            Item { Layout.fillWidth: true }

            AppButton {
                text: "更换海报"
                onClicked: root.posterRequested(root.subjectId)
            }

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

                // ---- 标签区（展示态 / 编辑态二选一）----
                //
                // 编辑交互（点击 chip 本身，不放 × 按钮 —— chip 已是最小
                // 可点单元，再加 × 会挤在 24px 高的小块里难以点中）：
                //   接口 tag：点击 = 删除↔恢复切换，删除后置灰 + 删除线；
                //             确定 时只提交删除标记，tag 本体留在库里，
                //             再次编辑仍能看到（可复活）。
                //   用户 tag：点击 = 从草稿移除（确定后物理删除，无恢复）。
                //
                // 外观：胶囊形（radius = 高度一半），与常见的 tag 样式一致。
                //
                // 可见性：tagRows 非空才显示。进入无 tag 的条目时自动补拉，
                // 拉取期间这里什么都不显示（不闪「获取标签」按钮）——
                // 自动补拉每次进入都会重试，无需手动重试入口。
                Flow {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm
                    visible: !root.editingTags && root.tagRows.length > 0

                    // 展示 chip（可能为空：tag 全被删光时只剩「修改」按钮，
                    // 保证用户仍能进编辑恢复）
                    Repeater {
                        model: root.activeTags()

                        Rectangle {
                            required property var modelData
                            width: tagText.implicitWidth + Theme.spacingMd * 2
                            height: 24
                            radius: height / 2          // 胶囊形
                            color: Theme.surfaceAlt
                            border.width: Theme.lineThin
                            border.color: Theme.border

                            Text {
                                id: tagText
                                anchors.centerIn: parent
                                text: modelData
                                color: Theme.textSecondary
                                font.pixelSize: Theme.fontXs
                            }
                        }
                    }

                    // 编辑入口（tagRows 非空即显示 —— 即使 tag 全被删光，
                    // 也要能进编辑恢复）。
                    // 用 PillActionButton：纯钢笔图标、直径与 tag chip 同高
                    // （24px），这样它在一排胶囊里不破坏行高；悬停浮起并变
                    // 主题色，与静态的 tag 明显区分。
                    PillActionButton {
                        bodyHeight: 24      // 与 tag chip 高度一致
                        text: "修改标签"     // 悬停提示（图标本身无文字）
                        iconSource: root.iconsBase !== ""
                                    ? root.iconsBase + "pen.svg" : ""
                        onClicked: root.startEditTags()
                    }
                }

                // 编辑态：接口 tag（含置灰）+ 用户 tag + 添加 + 确定/取消
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm
                    visible: root.editingTags

                    Text {
                        Layout.fillWidth: true
                        text: "点击标签切换删除/恢复；灰色删除线的是已删除、确定后不再展示，再次编辑时可恢复。"
                        color: Theme.textTertiary
                        font.pixelSize: Theme.fontXs
                        wrapMode: Text.WordWrap
                    }

                    Flow {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        // 接口 tag（保留原始顺序，删过的置灰可复活）
                        Repeater {
                            model: root.editApi

                            Rectangle {
                                required property var modelData
                                required property int index
                                width: editTagText.implicitWidth + Theme.spacingMd * 2
                                height: 24
                                radius: height / 2          // 胶囊形
                                color: modelData.deleted ? "transparent"
                                                         : Theme.surfaceAlt
                                border.width: Theme.lineThin
                                border.color: Theme.border
                                opacity: modelData.deleted ? 0.55 : 1.0

                                Text {
                                    id: editTagText
                                    anchors.centerIn: parent
                                    // 已删除的加删除线，明示"确定后不再展示"
                                    text: modelData.name
                                    font.pixelSize: Theme.fontXs
                                    font.strikeout: modelData.deleted
                                    color: modelData.deleted ? Theme.textTertiary
                                                             : Theme.textSecondary
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.toggleApiTag(index)
                                }
                            }
                        }

                        // 用户 tag（点击即从草稿移除）
                        Repeater {
                            model: root.editUser

                            Rectangle {
                                required property var modelData
                                required property int index
                                width: userTagText.implicitWidth
                                       + userTagX.implicitWidth + Theme.spacingMd * 2
                                height: 24
                                radius: height / 2          // 胶囊形
                                color: Theme.accentSoft
                                border.width: Theme.lineThin
                                border.color: Theme.accent

                                Row {
                                    anchors.centerIn: parent
                                    spacing: 4

                                    Text {
                                        id: userTagText
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: modelData
                                        color: Theme.accent
                                        font.pixelSize: Theme.fontXs
                                    }

                                    Text {
                                        id: userTagX
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: "✕"
                                        color: Theme.textTertiary
                                        font.pixelSize: Theme.fontXs
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.removeUserTag(index)
                                }
                            }
                        }
                    }

                    // 添加新 tag
                    //
                    // 输入框：浮动标签样式（移植自 uiverse.io
                    // alexruix/slippery-snail-18，用户提供的原版 CSS）：
                    //   - 「标签」两字常驻框内当占位（原版 translateY(1rem)）；
                    //   - 聚焦或已有输入（原版 input:valid）时浮到上边框
                    //     （原版 translateY(-50%) scale(0.8)），并用自身
                    //     背景色遮出"嵌在边框上"的断口（原版 #212121）；
                    //   - 边框灰 → 主题色（原版 #9e9e9e → #1a73e8），
                    //     过渡 150ms（原版 cubic-bezier(0.4,0,0.2,1)）。
                    Row {
                        spacing: Theme.spacingSm

                        Rectangle {
                            id: tagField
                            width: 180
                            height: 36
                            radius: 12                   // 原版 border-radius: 1rem
                            color: "transparent"         // 原版 background: none
                            border.width: tagInput.activeFocus
                                          ? Theme.lineThick : Theme.lineThin
                            border.color: tagInput.activeFocus
                                          ? Theme.accent : Theme.border

                            // 浮起条件：聚焦或非空（原版 :focus / :valid）
                            readonly property bool floated:
                                tagInput.activeFocus || root.newTagText !== ""

                            Behavior on border.color {
                                ColorAnimation { duration: Theme.durFast }
                            }

                            TextInput {
                                id: tagInput
                                anchors.fill: parent
                                anchors.leftMargin: Theme.spacingMd
                                anchors.rightMargin: Theme.spacingMd
                                verticalAlignment: TextInput.AlignVCenter
                                color: Theme.textPrimary
                                font.pixelSize: Theme.fontSm
                                clip: true
                                text: root.newTagText
                                onTextEdited: root.newTagText = tagInput.text
                                onAccepted: root.addUserTag()
                                // Esc = 失焦恢复原状（比点空白更顺手）
                                Keys.onEscapePressed: tagInput.focus = false
                            }

                            Text {
                                id: tagLabel
                                x: tagField.floated ? 10 : Theme.spacingMd
                                y: tagField.floated ? -7
                                                    : (tagField.height - height) / 2
                                text: "标签"
                                color: tagField.floated
                                       ? Theme.accent : Theme.textTertiary
                                font.pixelSize: tagField.floated
                                                ? Theme.fontXs : Theme.fontSm

                                Behavior on x {
                                    NumberAnimation { duration: Theme.durFast }
                                }
                                Behavior on y {
                                    NumberAnimation { duration: Theme.durFast }
                                }
                                Behavior on color {
                                    ColorAnimation { duration: Theme.durFast }
                                }

                                // 浮起时垫一层页面底色，遮出"嵌在边框上"的断口
                                // （原版给 label 加 background-color + padding；
                                //   z:-1 的子项画在父项文字之下、边框之上）
                                Rectangle {
                                    anchors.fill: parent
                                    anchors.margins: -3
                                    color: Theme.windowBg
                                    visible: tagField.floated
                                    z: -1
                                }
                            }
                        }

                        AppButton {
                            height: 36
                            text: "添加"
                            onClicked: root.addUserTag()
                        }
                    }

                    // 确定 / 取消
                    Row {
                        spacing: Theme.spacingMd

                        AppButton {
                            text: "确定"
                            variant: "primary"
                            onClicked: root.confirmTags()
                        }

                        AppButton {
                            text: "取消"
                            onClicked: root.resetTagState()
                        }
                    }
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

                    // 点击列表空白处 → 编辑中的输入框交出焦点。
                    // 与 SettingsPage 同一套机制：铺满整个内容高度、z:-1
                    // 压到集数行之下，onPressed 先清焦点再 accepted=false
                    // 把事件交还 Flickable（拖拽滚动不受影响）。
                    MouseArea {
                        width: epFlick.width
                        height: epFlick.contentHeight
                        z: -1
                        onPressed: function (mouse) {
                            root.forceActiveFocus()
                            mouse.accepted = false
                        }
                    }

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
    signal posterRequested(int subjectId)

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
