import QtQuick

// 数值输入：输入框 +「−」「+」独立按钮。
//
// 与旧版 NumberField（app/ui/num_inputs.py）一致：
// 不用 QSpinBox 那种内嵌按钮（间距拉不开），改为独立按钮 + 显式间距。
// 也不响应滚轮（TextInput 天然不绑滚轮），避免误改数值。
Item {
    id: root

    property real value: 0
    property real minimum: 0
    property real maximum: 9999
    property real step: 1
    property int decimals: 0
    property string suffix: ""

    signal valueModified(real value)

    implicitWidth: 220
    implicitHeight: 34

    readonly property int _btnSize: 30

    function format(v) {
        var text = root.decimals > 0 ? v.toFixed(root.decimals) : String(Math.round(v))
        return text + root.suffix
    }

    function clamp(v) {
        var x = Math.min(root.maximum, Math.max(root.minimum, v))
        if (root.decimals > 0) {
            var f = Math.pow(10, root.decimals)
            return Math.round(x * f) / f
        }
        return Math.round(x)
    }

    function setValue(v, notify) {
        var c = clamp(v)
        if (c === root.value)
            return
        root.value = c
        edit.text = format(c)
        if (notify)
            root.valueModified(c)
    }

    function nudge(dir) {
        // 浮点步进会累积误差（0.95 + 0.05 = 1.0000000000000002），
        // 故先按 decimals 定标再运算。
        var f = Math.pow(10, root.decimals)
        var scaled = Math.round(root.value * f) + dir * Math.round(root.step * f)
        setValue(scaled / f, true)
    }

    function commit() {
        var raw = edit.text
        if (root.suffix.length > 0 && raw.indexOf(root.suffix) >= 0)
            raw = raw.substring(0, raw.indexOf(root.suffix))
        var v = parseFloat(raw.trim())
        if (isNaN(v)) {
            edit.text = format(root.value)   // 非法输入还原
            return
        }
        setValue(v, true)
    }

    /// 诊断用：导出三段（减号 / 输入框 / 加号）的几何，
    /// 便于脚本核对「输入框是否真的夹在两个按钮之间」。
    function debugLayout() {
        return {
            "minusX": Math.round(minusBtn.x),
            "minusW": Math.round(minusBtn.width),
            "fieldX": Math.round(field.x),
            "fieldW": Math.round(field.width),
            "plusX": Math.round(plusBtn.x),
            "plusW": Math.round(plusBtn.width),
            "totalW": Math.round(root.width),
            "ordered": minusBtn.x < field.x && field.x < plusBtn.x
        }
    }

    // ---- 三段式布局：减号 | 输入框 | 加号 ----
    //
    // 踩坑记录：早期实现是 `field` 锚在 `parent.left`、两个按钮锚在右侧，
    // 结果是「输入框在减号左边」，而不是夹在两个按钮中间 ——
    // 视觉上像两个独立的加减按钮 + 一个无关的输入框。
    //
    // 现在用一个 Row 从左到右顺序排列三者，间距统一为 spacingMd，
    // 天然形成「− [输入框] +」的组合。注意 Row 不会拉伸子项，
    // 因此输入框必须显式给宽度（不能用 fillWidth）。
    Row {
        id: row
        anchors.fill: parent
        spacing: Theme.spacingMd

        // 减号
        Rectangle {
            id: minusBtn
            anchors.verticalCenter: parent.verticalCenter
            width: root._btnSize
            height: root._btnSize
            radius: Theme.radiusSm
            color: minusMouse.pressed ? Theme.pressedFill
                 : minusMouse.containsMouse ? Theme.hoverFill
                 : Theme.fade(Theme.hoverFill)
            border.width: Theme.lineThin
            border.color: Theme.border

            Behavior on color { ColorAnimation { duration: Theme.durFast } }

            Text {
                anchors.centerIn: parent
                // U+2212 真减号，比连字符视觉居中更好
                text: "\u2212"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontLg
            }

            MouseArea {
                id: minusMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.nudge(-1)
            }
        }

        // 输入框：宽度 = 总宽 − 两个按钮 − 两段间距
        Rectangle {
            id: field
            anchors.verticalCenter: parent.verticalCenter
            width: row.width - root._btnSize * 2 - row.spacing * 2
            height: root.height
            radius: Theme.radiusSm
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: edit.activeFocus ? Theme.accent : Theme.border

            Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

            TextInput {
                id: edit
                anchors.fill: parent
                anchors.leftMargin: Theme.spacingMd
                anchors.rightMargin: Theme.spacingMd
                verticalAlignment: TextInput.AlignVCenter
                color: Theme.textPrimary
                font.pixelSize: Theme.fontMd
                selectionColor: Theme.accent
                selectedTextColor: Theme.accentText
                text: root.format(root.value)

                onEditingFinished: root.commit()
                onActiveFocusChanged: {
                    if (!activeFocus)
                        root.commit()
                }
            }
        }

        // 加号
        Rectangle {
            id: plusBtn
            anchors.verticalCenter: parent.verticalCenter
            width: root._btnSize
            height: root._btnSize
            radius: Theme.radiusSm
            color: plusMouse.pressed ? Theme.pressedFill
                 : plusMouse.containsMouse ? Theme.hoverFill
                 : Theme.fade(Theme.hoverFill)
            border.width: Theme.lineThin
            border.color: Theme.border

            Behavior on color { ColorAnimation { duration: Theme.durFast } }

            Text {
                anchors.centerIn: parent
                text: "+"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontLg
            }

            MouseArea {
                id: plusMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.nudge(1)
            }
        }
    }
}
