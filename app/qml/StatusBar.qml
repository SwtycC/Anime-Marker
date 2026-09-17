import QtQuick

// 底部状态栏：版本号 │ 扫描进度条 │ 扫描日志。
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

        Row {
            anchors.fill: parent
            anchors.leftMargin: Theme.pagePadding
            anchors.rightMargin: Theme.pagePadding
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
