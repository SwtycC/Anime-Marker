import QtQuick
import QtQuick.Controls

// 「添加字体 / 重命名字体」小窗。
//
// 上传流程从"点上传直接弹系统文件框"改成两步：
//   ① 点「上传」先弹本窗；
//   ② 窗内「选择文件…」→ 自动带出字体内部族名作为默认显示名 →
//      用户可改成任意名字 → 添加。
//
// 「设置」按钮走 openEdit()：只改显示名，字体文件不动。
Dialog {
    id: dlg

    /// "add" = 添加新字体；"edit" = 修改已有上传字体的显示名
    property string mode: "add"
    /// 编辑模式的目标（用户字体目录里的文件名；由 FontComboBox 的 id 传入）
    property string fontId: ""
    /// 添加模式选中的源文件绝对路径
    property string sourcePath: ""
    /// 字体内部族名（只用于展示 / 回退，不作为显示名）
    property string family: ""
    /// 选中的文件名（只显示用）
    property string fileName: ""

    /// 操作成功：mode / family / name / id
    signal completed(string mode, string family, string name, string fontId)

    modal: true
    anchors.centerIn: parent
    width: 520
    padding: Theme.spacingXl
    title: dlg.mode === "edit" ? "字体设置" : "添加字体"

    background: Rectangle {
        color: Theme.surfaceBg
        border.width: Theme.lineThin
        border.color: Theme.border
        radius: Theme.radiusMd
    }

    /// 打开"添加"模式
    function openAdd() {
        dlg.mode = "add"
        dlg.fontId = ""
        dlg.sourcePath = ""
        dlg.family = ""
        dlg.fileName = ""
        nameField.text = ""
        errorText.text = ""
        dlg.open()
    }

    /// 打开"编辑显示名"模式（item 来自 FontComboBox 的 options 条目）
    function openEdit(item) {
        if (!item)
            return
        dlg.mode = "edit"
        dlg.fontId = String(item.id || "")
        dlg.sourcePath = ""
        dlg.family = String(item.family || "")
        dlg.fileName = String(item.id || "")
        nameField.text = String(item.name || "")
        errorText.text = ""
        dlg.open()
    }

    function baseName(path) {
        var parts = String(path || "").split(/[\\/]/)
        return parts.length > 0 ? parts[parts.length - 1] : String(path || "")
    }

    function chooseFile() {
        if (typeof fontBridge === "undefined" || !fontBridge) {
            errorText.text = "字体服务不可用"
            return
        }
        var r = fontBridge.pickFontFile()
        if (!r || r.cancelled === true)
            return
        if (r.ok !== true) {
            errorText.text = String(r.message || "无法读取该字体文件")
            return
        }
        dlg.sourcePath = String(r.path || "")
        dlg.family = String(r.family || "")
        dlg.fileName = baseName(dlg.sourcePath)
        // 默认名取字体内部族名；读不出来才用文件名
        nameField.text = String(r.name || dlg.family || dlg.fileName)
        errorText.text = ""
    }

    function submit() {
        if (typeof fontBridge === "undefined" || !fontBridge) {
            errorText.text = "字体服务不可用"
            return
        }

        var r
        if (dlg.mode === "edit") {
            r = fontBridge.renameFont(dlg.fontId, nameField.text.trim())
        } else {
            if (dlg.sourcePath === "") {
                errorText.text = "请先选择字体文件"
                return
            }
            r = fontBridge.addFont(dlg.sourcePath, nameField.text.trim())
        }

        if (!r || r.ok !== true) {
            errorText.text = String((r && r.message) || "操作失败")
            return
        }

        var m = dlg.mode
        var fam = String(r.family || dlg.family || "")
        var nm = String(r.name || nameField.text.trim() || fam)
        var id = String(r.id || dlg.fontId || "")
        dlg.close()
        dlg.completed(m, fam, nm, id)
    }

    contentItem: Column {
        width: parent.width
        spacing: Theme.spacingMd

        // ---- 文件 ----
        Column {
            width: parent.width
            spacing: Theme.spacingXs

            Text {
                text: "字体文件"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
            }

            Row {
                width: parent.width
                spacing: Theme.spacingSm

                AppTextField {
                    id: fileField
                    objectName: "fontDialogFileField"
                    width: parent.width - (dlg.mode === "edit"
                                           ? 0
                                           : chooseBtn.width + Theme.spacingSm)
                    text: dlg.fileName === "" ? "尚未选择" : dlg.fileName
                    readOnly: true
                }

                AppButton {
                    id: chooseBtn
                    objectName: "fontDialogChooseBtn"
                    visible: dlg.mode !== "edit"
                    text: "选择文件…"
                    onClicked: dlg.chooseFile()
                }
            }

            Text {
                width: parent.width
                visible: dlg.mode === "add"
                text: "支持 .ttf / .otf / .ttc"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontXs
            }
        }

        // ---- 显示名 ----
        Column {
            width: parent.width
            spacing: Theme.spacingXs

            Text {
                text: "显示名称"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
            }

            AppTextField {
                id: nameField
                objectName: "fontNameField"
                width: parent.width
                placeholder: "例如：我的文楷"
                selectAllOnFocus: true
            }

            Text {
                width: parent.width
                text: "只影响设置页下拉菜单里的叫法，不会修改字体文件本身。"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontXs
                wrapMode: Text.WordWrap
            }
        }

        Text {
            id: errorText
            width: parent.width
            visible: text !== ""
            color: Theme.dangerColor
            font.pixelSize: Theme.fontSm
            wrapMode: Text.WordWrap
        }

        Row {
            anchors.right: parent.right
            spacing: Theme.spacingMd

            AppButton {
                objectName: "fontDialogCancel"
                text: "取消"
                onClicked: dlg.close()
            }

            AppButton {
                objectName: "fontDialogAccept"
                text: dlg.mode === "edit" ? "保存" : "添加"
                variant: "primary"
                onClicked: dlg.submit()
            }
        }
    }
}
