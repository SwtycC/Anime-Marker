import QtQuick
import QtQuick.Controls        // ToolTip（「更多」按钮的悬停提示）

// 底部状态栏：版本号 │ 扫描进度条 │ 扫描日志 │ 「更多」按钮。
//
// 布局（对应旧版 app/ui/status_bar.py）：
//     [ Anime Marker v1.0.0 ] │ [ ▓▓▓░░░ 3/10 ] 正在扫描：无职转生 第二季
//
// 注意事项：
// - 这是**真正占布局空间**的底栏，与悬浮导航不同 —— 它在内容之下，
//   高度固定 28px，由 Main.qml 用 ColumnLayout 放在页面栈下方。
//   因悬浮导航浮在内容区底部（navBottomMargin=20 + 胶囊高 54 ≈ 74px），
//   底栏在其下方不会被遮挡。
// - 进度条仅在有耗时任务时显示，完成后自动隐藏（腾出空间给日志文字）。
Item {
    id: root

    // ---- 对外状态 ----
    property string version: "1.0.0"
    property string message: "就绪"
    property int progressCurrent: 0
    property int progressTotal: 0
    property bool progressVisible: false
    /// 右侧「更多」按钮上要不要点小圆点（由 Main.qml 绑 appMenu.hasUpdate）
    property bool moreHasUpdate: false

    /// 「更多」按钮被点击（菜单位于状态栏之上，由 Main.qml 打开）
    signal moreClicked()

    readonly property real _percent: progressTotal > 0
                                    ? Math.min(1.0, progressCurrent / progressTotal)
                                    : 0.0

    implicitHeight: 28

    Rectangle {
        anchors.fill: parent
        color: Theme.surfaceBg

        // 顶部 1px 分隔线
        Rectangle {
            anchors.top: parent.top
            width: parent.width
            height: Theme.lineThin
            color: Theme.border
        }

        // 左侧这一行只放"版本号 │ 进度 │ 日志"。
        //
        // **右端必须由 moreRow 卡住**：日志文字用的是 `parent.width - x`
        // （吃掉整行剩余宽度），给它一个无边的 parent 就会一直铺到「更多」
        // 按钮底下，文字压在按钮上。所以这里锚 right 到 moreRow 的左边，
        // 而不是原来的 `anchors.fill`。
        Row {
            anchors.left: parent.left
            anchors.right: moreRow.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.leftMargin: Theme.pagePadding
            anchors.rightMargin: Theme.spacingMd
            spacing: Theme.spacingMd

            // ---- 版本号 ----
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Anime Marker v" + root.version
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
            }

            // ---- 分隔符 ----
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: Theme.lineThin
                height: 12
                color: Theme.border
            }

            // ---- 进度条 ----
            Row {
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacingSm
                visible: root.progressVisible

                // 进度条本体（自绘，避免 QtQuick Controls 的默认样式干扰）
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: 140
                    height: 6
                    radius: 3
                    color: Theme.surfaceAlt
                    border.width: Theme.lineThin
                    border.color: Theme.border

                    Rectangle {
                        width: Math.round(parent.width * root._percent)
                        height: parent.height
                        radius: parent.radius
                        color: Theme.accent

                        // 进度变化时平滑过渡，避免条状跳动
                        Behavior on width {
                            NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic }
                        }
                    }
                }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.progressTotal > 0
                          ? root.progressCurrent + "/" + root.progressTotal
                          : ""
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                }
            }

            // ---- 日志文字 ----
            Text {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - x
                text: root.message
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
                elide: Text.ElideRight
                maximumLineCount: 1
            }
        }

        // ---- 右侧：分隔符 + 「更多」按钮 ----
        //
        // 分隔符与"版本号 │ 日志"之间那个**同一形态**（1px × 12px，
        // Theme.border），两处对齐了才不会左边粗右边细。
        Row {
            id: moreRow
            anchors.right: parent.right
            // **比页面内容的 pagePadding(24) 更靠边**（实测反馈「再靠右一点」）：
            // 状态栏是窗口的边框条，不是页面内容，按钮贴着角落更像系统级的
            // 「更多」。菜单面板的右间距跟着一起改，两者右边缘对齐。
            anchors.rightMargin: Theme.spacingMd
            anchors.verticalCenter: parent.verticalCenter
            spacing: Theme.spacingMd

            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: Theme.lineThin
                height: 12
                color: Theme.border
            }

            // 按钮本体。状态栏只有 28px 高，图标 16px —— 靠 22×22 的
            // 热区给出一个能点中的范围（视觉上仍是那个小图标）。
            Item {
                id: moreBtn
                objectName: "statusBarMoreBtn"   // 诊断 / 自动化点按用
                anchors.verticalCenter: parent.verticalCenter
                width: 22
                height: 22

                Rectangle {
                    anchors.fill: parent
                    radius: Theme.radiusSm
                    color: moreMouse.containsMouse ? Theme.hoverFill
                                                   : "transparent"
                }

                NavIcon {
                    anchors.centerIn: parent
                    width: 16
                    height: 16
                    kind: "more"
                    color: moreMouse.containsMouse ? Theme.textPrimary
                                                   : Theme.textTertiary
                }

                // 有新版本时的小红点，压在图标的右上角（不遮住那三条线）
                Rectangle {
                    visible: root.moreHasUpdate
                    anchors.right: parent.right
                    anchors.top: parent.top
                    width: 6
                    height: 6
                    radius: 3
                    color: Theme.dangerColor
                }

                MouseArea {
                    id: moreMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.moreClicked()

                    ToolTip.visible: containsMouse
                    // **比别处快**（其余按钮用 500~600ms）：这个按钮只有
                    // 16px 图标 + 22px 热区，在状态栏角落里，鼠标扫过去时
                    // 600ms 的等待显得"点了没反应"。200ms 足够避免扫过时
                    // 闪烁，又基本是"停上去就出来"。
                    ToolTip.delay: 200
                    ToolTip.text: "更多"
                }
            }
        }
    }

    // ---- 对外接口 ----
    /// 显示状态文字；timeoutMs > 0 时到时自动清空
    Timer {
        id: clearTimer
        repeat: false
        onTriggered: root.message = ""
    }

    function setMessage(text, timeoutMs) {
        root.message = text || ""
        clearTimer.stop()
        if (timeoutMs > 0 && root.message !== "")
            clearTimer.interval = timeoutMs
        if (timeoutMs > 0 && root.message !== "")
            clearTimer.restart()
    }

    function startProgress(current, total) {
        root.progressCurrent = current
        root.progressTotal = total
        root.progressVisible = true
    }

    function setProgress(current, total) {
        root.progressCurrent = current
        root.progressTotal = total
        root.progressVisible = true
    }

    function stopProgress() {
        root.progressVisible = false
    }
}
