import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 「更换海报」小窗（详情页顶部「更换海报」按钮 → Main.qml 打开）。
//
// 能做两件事：
//   1. 自定义海报：选本地图片 → library.setCustomCover() 复制到
//      covers/<sid>_custom.<ext> 并写库（原版缓存文件不动）
//   2. 恢复原版海报：library.restoreCover() 把 cover_path 指回
//      Bangumi 封面的本地缓存（缓存被清理过时由 Python 侧重新下载）
//
// 数据流：open() → library.coverInfo() 取当前海报状态 → 操作 → 成功后
// reload() 本窗预览 + 发 applied(subjectId, message)；Main.qml 据此刷新
// 详情页与状态栏，海报墙由 LibraryBridge 的 subjectsChanged 自动刷新。
// 预览读的 coverUrl 自带 ?v= 版本号（见 LibraryBridge._cover_file_url），
// 换完立刻能看到新图，不会被 QML 的图片解码缓存糊住。
Window {
    id: dlg

    /// 海报变更成功（subjectId 本地主键，message 给状态栏的提示文案）
    signal applied(int subjectId, string message)

    // ---- 状态 ----
    property int subjectId: 0
    property string subjectTitle: ""
    property string coverUrl: ""        // 当前海报（file:// URL，带版本号）
    property bool isCustom: false       // 当前是否为自定义海报
    property bool hasOriginal: false    // 有无原版可恢复（未匹配 Bangumi 时没有）
    property string status: ""
    property bool statusOk: true        // false 时状态文字标红

    width: 500
    height: 620
    minimumWidth: 440
    minimumHeight: 520
    modality: Qt.ApplicationModal
    title: "更换海报"
    color: Theme.windowBg
    flags: Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint

    /// 打开小窗（外部调用）
    function open(subjectId, title) {
        dlg.subjectId = subjectId
        dlg.subjectTitle = title || ""
        dlg.status = ""
        dlg.reload()
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
    }

    /// 重新读取当前海报状态（打开时、每次更换/恢复成功后）
    function reload() {
        if (typeof library === "undefined" || !library || dlg.subjectId <= 0)
            return
        var info = library.coverInfo(dlg.subjectId)
        dlg.coverUrl = info.coverUrl || ""
        dlg.isCustom = info.isCustom === true
        dlg.hasOriginal = info.hasOriginal === true
    }

    /// 选图并应用（「选择图片…」按钮 / 点击预览图）
    function pickAndApply() {
        if (typeof library === "undefined" || !library)
            return
        var path = library.pickImage()
        if (!path)
            return                      // 用户取消了文件对话框
        dlg.status = ""
        var r = library.setCustomCover(dlg.subjectId, path)
        dlg.status = r.message || ""
        dlg.statusOk = r.ok === true
        if (r.ok === true) {
            dlg.reload()
            dlg.applied(dlg.subjectId, "海报已更换")
        }
    }

    /// 恢复原版海报（「恢复原版海报」按钮）
    function restoreOriginal() {
        if (typeof library === "undefined" || !library)
            return
        dlg.status = ""
        var r = library.restoreCover(dlg.subjectId)
        dlg.status = r.message || ""
        dlg.statusOk = r.ok === true
        if (r.ok === true) {
            dlg.reload()
            dlg.applied(dlg.subjectId, "已恢复原版海报")
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.spacingLg
        spacing: Theme.spacingMd

        // ---- 标题 ----
        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingXs

            Text {
                text: "更换海报"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontXl
                font.weight: Font.DemiBold
            }

            Text {
                Layout.fillWidth: true
                visible: dlg.subjectTitle !== ""
                text: dlg.subjectTitle
                color: Theme.textTertiary
                font.pixelSize: Theme.fontSm
                elide: Text.ElideRight
            }
        }

        // ---- 预览（与详情页封面同尺寸：240 × 240×1.4）----
        // 点预览图 = 点「选择图片…」，最大的那块区域就是主要操作
        Item {
            Layout.fillWidth: true
            height: previewRect.height

            Rectangle {
                id: previewRect
                anchors.horizontalCenter: parent.horizontalCenter
                width: 240
                height: Math.round(240 * Theme.posterRatio)
                color: Theme.surfaceAlt
                border.width: previewMouse.containsMouse
                              ? Theme.lineThick : Theme.lineThin
                border.color: previewMouse.containsMouse
                              ? Theme.accent : Theme.border
                radius: Theme.radiusMd
                clip: true

                Image {
                    anchors.fill: parent
                    source: dlg.coverUrl
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    visible: status === Image.Ready && source != ""
                }

                Text {
                    anchors.centerIn: parent
                    visible: dlg.coverUrl === ""
                    text: "无封面"
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                }

                // 「自定义」徽标：一眼看出当前用的不是 Bangumi 原图
                Rectangle {
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.margins: Theme.spacingSm
                    width: customLabel.implicitWidth + Theme.spacingSm * 2
                    height: 20
                    radius: Theme.radiusSm
                    color: Theme.accentSoft
                    visible: dlg.isCustom

                    Text {
                        id: customLabel
                        anchors.centerIn: parent
                        text: "自定义"
                        color: Theme.accent
                        font.pixelSize: Theme.fontXs
                    }
                }

                MouseArea {
                    id: previewMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: dlg.pickAndApply()
                }
            }
        }

        Text {
            Layout.fillWidth: true
            text: "选择一张本地图片作为这部动漫的海报；想换回 Bangumi 原图时点「恢复原版海报」。"
            color: Theme.textTertiary
            font.pixelSize: Theme.fontSm
            wrapMode: Text.WordWrap
            lineHeight: 1.3
        }

        Rectangle {
            Layout.fillWidth: true
            height: Theme.lineThin
            color: Theme.border
        }

        // ---- 操作按钮 ----
        Row {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            AppButton {
                text: "选择图片…"
                onClicked: dlg.pickAndApply()
            }

            AppButton {
                // 当前就是原版（非自定义）时没有"恢复"可言
                enabled: dlg.isCustom && dlg.hasOriginal
                text: "恢复原版海报"
                onClicked: dlg.restoreOriginal()
            }
        }

        Item { Layout.fillHeight: true }    // 弹性空隙，把底栏压到窗口底部

        // ---- 底部：状态（左）+ 关闭（右）----
        // 排列方式沿用 UploadDialog 的踩坑结论：左文字 + 右按钮自然顺序，
        // 文字宽度由按钮宽度反推（不要 RTL + childrenRect）
        Row {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Text {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - closeBtn.width - Theme.spacingMd
                text: dlg.status
                color: dlg.statusOk ? Theme.textSecondary : Theme.dangerColor
                font.pixelSize: Theme.fontSm
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }

            AppButton {
                id: closeBtn
                anchors.verticalCenter: parent.verticalCenter
                text: "关闭"
                onClicked: dlg.close()
            }
        }
    }
}
