import QtQuick
import QtQuick.Controls

// 删除用户上传字体的确认小窗。
//
// 与 ConfirmDialog（删动漫）分开：那个组件的补充说明写死了"媒体库记录、
// 视频文件不受影响"，放在字体场景完全是错的；字体删除**真的会删磁盘文件**。
Dialog {
    id: dlg

    property string fontId: ""
    property string fontName: ""

    signal confirmed(string fontId)

    modal: true
    anchors.centerIn: parent
    width: 460
    padding: Theme.spacingXl
    title: "删除字体"

    background: Rectangle {
        color: Theme.surfaceBg
        border.width: Theme.lineThin
        border.color: Theme.border
        radius: Theme.radiusMd
    }

    function openFor(id, name) {
        dlg.fontId = String(id || "")
        dlg.fontName = String(name || "")
        dlg.open()
    }

    contentItem: Column {
        width: parent.width
        spacing: Theme.spacingMd

        Text {
            width: parent.width
            text: "确定删除字体「" + dlg.fontName + "」吗？"
            color: Theme.textPrimary
            font.pixelSize: Theme.fontMd
            wrapMode: Text.WordWrap
        }

        Text {
            width: parent.width
            text: "字体文件会从用户字体目录中移除；"
                  + "如果它正在使用，会恢复为「系统默认」。"
            color: Theme.textTertiary
            font.pixelSize: Theme.fontSm
            lineHeight: 1.4
            wrapMode: Text.WordWrap
        }

        Row {
            anchors.right: parent.right
            spacing: Theme.spacingMd

            AppButton {
                objectName: "fontDeleteCancel"
                text: "取消"
                onClicked: dlg.close()
            }

            AppButton {
                objectName: "fontDeleteAccept"
                text: "删除"
                variant: "danger"
                onClicked: {
                    var id = dlg.fontId
                    dlg.close()
                    dlg.confirmed(id)
                }
            }
        }
    }
}
