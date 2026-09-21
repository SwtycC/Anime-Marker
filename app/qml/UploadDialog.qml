import QtQuick
import QtQuick.Controls

// 「上传」小窗：把**本地看过、Bangumi 还没标**的集批量补传（F21）。
//
// 流程：open() → `library.pendingUploads()` 取清单 → 勾选（可一键全选）
//      → `inprogress.uploadEpisodes(ids)`（QThread，不卡界面）
//      → 完成消息经 `inprogress.message` 回来 → 重新拉清单（传过的会消失）
//
// **一行 = 一集**，并写清「动漫名 · EP7 妈妈」：早期是按动漫一行的
// 「碧蓝之海 第三季 · 1 集待传」，用户看不出待传的到底是哪一集 ✗
// （实测反馈："小窗口写清楚哪一集，以及单集名字"）。改成逐集后，
// 勾选单位也就是"这一集"，后端 `uploadEpisodes()` 按集处理 ✓
//
// **为什么放在动态页而不是「在看」页**：这条时间线才是"我看过什么"的完整
// 视图；而"补传"本身就是"让本地记录变成 bgm"的动作 —— 用户是在看到某一行
// 只有「本地」tag 时才想起来要传的。
//
// **勾选状态存在对话框的 `selected` 里（唯一真相）**，不放在各行的复选框上：
//   ① 那个组件点击时内部会 `checked = ...` 赋值，外部给的绑定会被永久断开 ✗；
//   ② ListView 只创建可见行的 delegate（虚拟化），把状态放行里的话
//      「全选」只能覆盖到看得见的那几行 ✗（实测：屏幕外的行既不被勾上、
//      也不计入已选数）。delegate 只在"创建时"和"集合变化时"各同步一次。
Window {
    id: dlg

    // ---- 状态 ----
    property var items: []              // library.pendingUploads() 的结果（逐集）
    property var selected: ({})         // episodeId -> true
    property int selectedCount: 0
    property int blockedCount: 0        // 缺集号、根本传不了的集数（只能提示）
    property bool uploading: false
    property string status: ""

    /// 勾选集合变了 → 各行的复选框据此把方框同步成当前状态。
    ///
    /// **为什么不是"对话框广播一个值、各行自己设"**（早期写法 ✗）：
    /// ListView **只创建可见行的 delegate**（虚拟化），广播只能覆盖到看得见
    /// 的那几行 —— 长列表下点「全选」，屏幕外的行既不会被勾上、也不计入
    /// 已选数 ✗（实测：点全选后计数仍是 0，因为测试环境下一个 delegate 都没建）。
    /// 现在**勾选状态存在对话框的 map 里**（唯一真相 ✓），delegate 在"创建时"
    /// 与"集合变化时"各同步一次 ✓。
    signal selectionChanged()

    width: 700
    height: 580
    minimumWidth: 560
    minimumHeight: 400
    modality: Qt.ApplicationModal
    title: "上传本地观看记录到 Bangumi"
    color: Theme.windowBg
    flags: Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint

    /// 打开小窗（外部调用）
    function open() {
        dlg.uploading = false
        dlg.status = ""
        dlg.reload()
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
    }

    /// 重新拉清单（打开时、以及每次上传完成后）
    function reload() {
        dlg.items = (typeof library !== "undefined" && library)
                    ? library.pendingUploads() : []
        dlg.blockedCount = (typeof library !== "undefined" && library)
                           ? library.blockedUploadCount() : 0
        dlg.selected = ({})
        dlg.selectedCount = 0
        dlg.selectionChanged()           // 让各行（若已创建）把勾去掉
    }

    /// 单行勾选/取消（由 delegate 的 onToggled 调用）
    function setChecked(episodeId, value) {
        if (value === true)
            dlg.selected[episodeId] = true
        else
            delete dlg.selected[episodeId]
        dlg.selectedCount = dlg.countSelected()
        dlg.selectionChanged()
    }

    /// 全选 / 取消全选（按钮调用）。
    /// **只改对话框里的集合**，不依赖任何 delegate 是否存在 ✓
    function checkAll(value) {
        var m = ({})
        if (value === true) {
            for (var i = 0; i < dlg.items.length; i++)
                m[dlg.items[i].episodeId] = true
        }
        dlg.selected = m
        dlg.selectedCount = (value === true) ? dlg.items.length : 0
        dlg.selectionChanged()
    }

    function countSelected() {
        var n = 0
        for (var k in dlg.selected)
            if (dlg.selected[k] === true)
                n += 1
        return n
    }

    function isChecked(episodeId) {
        return dlg.selected[episodeId] === true
    }

    function selectedIds() {
        var out = []
        for (var k in dlg.selected)
            if (dlg.selected[k] === true)
                out.push(parseInt(k, 10))
        return out
    }

    function doUpload() {
        var ids = dlg.selectedIds()
        if (ids.length === 0) {
            dlg.status = "先勾选要上传的集（可用「全选」）"
            return
        }
        if (typeof inprogress === "undefined" || !inprogress)
            return
        dlg.uploading = true
        dlg.status = ""
        var r = inprogress.uploadEpisodes(ids)
        if (r && r.message)
            dlg.status = r.message
        if (r && r.ok === false)         // 没起来（比如上一批还在跑）
            dlg.uploading = false
    }

    /// 集号格式化（与动态页一致：7 → "7"，7.5 → "7.5"）
    function fmtIndex(v) {
        return (Math.round(v * 100) / 100).toString()
    }

    // 上传进度 / 结果：桥接层的 message 信号
    Connections {
        target: typeof inprogress !== "undefined" && inprogress ? inprogress : null

        function onMessage(text) {
            if (!dlg.uploading && dlg.status.indexOf("补传") !== 0)
                return
            dlg.status = text
            if (text.indexOf("补传完成") === 0) {
                dlg.uploading = false
                // 传过的会从清单里消失；把结果留在这行，别被 reload 清掉
                dlg.reload()
                dlg.status = text
            }
        }
    }

    Column {
        anchors.fill: parent
        anchors.margins: Theme.spacingLg
        spacing: Theme.spacingMd

        // ---- 标题 ----
        Column {
            width: parent.width
            spacing: Theme.spacingXs

            Text {
                text: "上传本地观看记录"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontXl
                font.weight: Font.DemiBold
            }

            Text {
                width: parent.width
                text: "下面这些单集在本地看过，但 Bangumi 上还没标记。"
                      + "勾选后上传，成功的那一集在动态页会同时显示「本地」和「bgm」。"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
                lineHeight: 1.3
            }

            Text {
                width: parent.width
                visible: dlg.blockedCount > 0
                text: "另有 " + dlg.blockedCount
                      + " 集缺少 Bangumi 集号（扫描时没拿到集数元数据），无法上传、已跳过。"
                color: Theme.warningColor
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
            }
        }

        Rectangle {
            width: parent.width
            height: Theme.lineThin
            color: Theme.border
        }

        // ---- 工具条：全选 + 计数 ----
        Row {
            width: parent.width
            spacing: Theme.spacingMd

            AppButton {
                objectName: "uploadDialogSelectAll"
                text: (dlg.items.length > 0 && dlg.selectedCount === dlg.items.length)
                      ? "取消全选" : "全选"
                enabled: dlg.items.length > 0 && !dlg.uploading
                onClicked: dlg.checkAll(dlg.selectedCount !== dlg.items.length)
            }

            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: dlg.items.length > 0
                      ? "共 " + dlg.items.length + " 集，已勾选 "
                        + dlg.selectedCount + " 集"
                      : ""
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
            }
        }

        // ---- 清单（一行 = 一集）----
        ListView {
            id: list
            width: parent.width
            height: Math.max(120, parent.height - y - footer.height
                             - Theme.spacingMd * 2)
            clip: true
            model: dlg.items
            spacing: Theme.lineThin
            boundsBehavior: Flickable.StopAtBounds

            ScrollBar.vertical: AppScrollBar { policy: ScrollBar.AsNeeded }

            delegate: Rectangle {
                required property var modelData

                width: list.width
                height: 40
                color: "transparent"

                Row {
                    anchors.fill: parent
                    anchors.rightMargin: Theme.spacingSm
                    spacing: Theme.spacingSm

                    CheckBoxLine {
                        id: rowBox
                        anchors.verticalCenter: parent.verticalCenter
                        objectName: "uploadDialogRow_" + modelData.episodeId
                        text: ""                      // 只取方框，文字另排（要对齐）
                        enabled: !dlg.uploading

                        // **不能写 `checked: dlg.isChecked(...)` 这种绑定** ——
                        // 组件点击时内部会 `checked = !checked` 赋值，绑定会被
                        // 永久断开 ✗。改为"创建时 + 集合变化时"各同步一次：
                        // 创建时这一句覆盖了新滚出来的行 ✓
                        Component.onCompleted:
                            rowBox.checked = dlg.isChecked(modelData.episodeId)
                        onToggled: function (value) {
                            dlg.setChecked(modelData.episodeId, value)
                        }
                        Connections {
                            target: dlg
                            function onSelectionChanged() {
                                rowBox.checked = dlg.isChecked(modelData.episodeId)
                            }
                        }
                    }

                    // 动画名（固定宽度，便于纵向扫读）
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.round(parent.width * 0.36)
                        text: modelData.title
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontMd
                        elide: Text.ElideRight
                    }

                    // 「EP7」徽标
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.max(30, rowEpLabel.implicitWidth + 10)
                        height: 18
                        radius: Theme.radiusSm
                        color: Theme.accentSoft

                        Text {
                            id: rowEpLabel
                            anchors.centerIn: parent
                            text: "EP" + dlg.fmtIndex(modelData.epIndex)
                            color: Theme.accent
                            font.pixelSize: Theme.fontXs
                        }
                    }

                    // 集名
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - x
                        text: modelData.epTitle || "（无标题）"
                        color: Theme.textTertiary
                        font.pixelSize: Theme.fontSm
                        elide: Text.ElideRight
                    }
                }
            }

            // 空状态
            Text {
                anchors.centerIn: parent
                visible: dlg.items.length === 0
                text: dlg.uploading ? "上传中…"
                                    : "没有需要上传的记录 —— 本地看过的都已同步到 Bangumi ✓"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontMd
            }
        }

        // ---- 底部：状态（左）+ 按钮（右）----
        //
        // **踩坑（别用 layoutDirection: RightToLeft + childrenRect 算宽度）**：
        // 早期写成 RTL 的 Row、状态文字宽度取 `parent.width - childrenRect.width`
        // —— 实测文字被排到了**窗口左边之外**（截图里只在左下角露出几个字母 ✗），
        // 因为 RTL 下子项的 x 布局与这个宽度公式对不上。
        // 现在按"左文字 + 右按钮"自然顺序排，宽度直接由两个按钮的宽度反推。
        Row {
            id: footer
            width: parent.width
            spacing: Theme.spacingMd

            Text {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - uploadBtn.width - closeBtn.width
                       - Theme.spacingMd * 2
                text: dlg.status
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
                elide: Text.ElideRight
            }

            AppButton {
                id: uploadBtn
                objectName: "uploadDialogUpload"
                anchors.verticalCenter: parent.verticalCenter
                variant: "primary"
                text: dlg.uploading
                      ? "上传中…"
                      : (dlg.selectedCount > 0
                         ? "上传选中 " + dlg.selectedCount + " 集" : "上传选中")
                enabled: !dlg.uploading && dlg.selectedCount > 0
                onClicked: dlg.doUpload()
            }

            AppButton {
                id: closeBtn
                objectName: "uploadDialogClose"
                anchors.verticalCenter: parent.verticalCenter
                text: "关闭"
                onClicked: dlg.close()
            }
        }
    }
}
