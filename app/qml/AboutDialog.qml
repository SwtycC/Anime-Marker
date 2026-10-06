import QtQuick

// 「关于」小窗：版本号 / 作者 / 许可证 / 仓库地址。
//
// 与 ConfirmDialog 同一个壳（`Window` + 自绘背景 + 主题色按钮），
// 而不是 `Window` 之外的另一种浮层 —— 项目里所有小窗都是这个形态，
// 混一种系统风格的小窗会在界面里"突出来"一块。
//
// **不放"检查更新"按钮**：那件事在状态栏「更多」菜单里，这里只做展示。
// 两个入口做同一件事，用户会怀疑它们结果不一样。
Window {
    id: dlg

    /// 版本号（由 Main.qml 传 appVersion，与状态栏那份同源）
    property string version: ""

    width: 420
    height: 240
    minimumWidth: 360
    minimumHeight: 240
    modality: Qt.ApplicationModal
    title: "关于 Anime Marker"
    color: Theme.windowBg
    flags: Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint

    /// 打开小窗（外部调用）
    function open() {
        dlg.show()
        dlg.raise()
        dlg.requestActivate()
    }

    Column {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Theme.spacingXl
        spacing: Theme.spacingMd

        Text {
            width: parent.width
            text: "Anime Marker"
            color: Theme.textPrimary
            font.pixelSize: Theme.fontLg
            font.bold: true
        }

        Text {
            width: parent.width
            text: "v" + dlg.version
            color: Theme.textTertiary
            font.pixelSize: Theme.fontSm
        }

        Text {
            width: parent.width
            text: "本地动漫媒体库管理：扫描入库、Bangumi 收藏同步、"
                  + "RSS 订阅下载、播放进度自动标记。"
            color: Theme.textSecondary
            font.pixelSize: Theme.fontSm
            lineHeight: 1.4
            wrapMode: Text.WordWrap
        }

        // 许可证：**必须列出来**。这个程序用了 PySide6（LGPL-3.0），
        // 分发二进制时要给出对应许可与获取途径；详情见随包的
        // THIRD_PARTY_LICENSES.md。
        Text {
            width: parent.width
            text: "第三方组件许可见 THIRD_PARTY_LICENSES.md"
            color: Theme.textTertiary
            font.pixelSize: Theme.fontSm
            wrapMode: Text.WordWrap
        }
    }

    Row {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Theme.spacingXl
        spacing: Theme.spacingMd

        AppButton {
            objectName: "aboutRepoBtn"
            text: "打开仓库"
            // 仓库地址由后端给（appMenu.openRepo 里写死一处）——
            // QML 不该知道站点地址，域名/仓库改名时只改一处。
            onClicked: if (typeof appMenu !== "undefined" && appMenu) appMenu.openRepo()
        }

        AppButton {
            objectName: "aboutCloseBtn"
            text: "关闭"
            variant: "primary"
            onClicked: dlg.close()
        }
    }
}
