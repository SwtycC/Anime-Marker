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

    // ---- 输入框 ----
    Rectangle {
        id: field
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: root.width - root._btnSize * 2 - Theme.spacingMd * 2
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

    // ---- 减号 ----
    Rectangle {
        id: minusBtn
        anchors.right: plusBtn.left
        anchors.rightMargin: Theme.spacingMd
        anchors.verticalCenter: parent.verticalCenter
        width: root._btnSize
        height: root._btnSize
        radius: Theme.radiusSm
        color: minusMouse.pressed ? Theme.pressedFill
             : minusMouse.containsMouse ? Theme.hoverFill
             : "transparent"
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

    // ---- 加号 ----
    Rectangle {
        id: plusBtn
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        width: root._btnSize
        height: root._btnSize
        radius: Theme.radiusSm
        color: plusMouse.pressed ? Theme.pressedFill
             : plusMouse.containsMouse ? Theme.hoverFill
             : "transparent"
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
