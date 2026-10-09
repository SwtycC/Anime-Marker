import QtQuick

// 数值输入：输入框 +「−」「+」独立按钮。
//
// 与旧版 NumberField（app/ui/num_inputs.py）一致：
// 不用 QSpinBox 那种内嵌按钮（间距拉不开），改为独立按钮 + 显式间距。
// 也不响应滚轮（TextInput 天然不绑滚轮），避免误改数值。
//
// 布局：[−] [输入框] [单位] [+]
// 单位（`suffix`）在输入框**外面**，是为了让"秒"这类单位不可编辑、
// 也不参与输入框宽度计算 —— 否则「3 秒」和「0.90」（无单位）两行的
// 输入框宽度会不一致，右边缘无法对齐。
// 位置在输入框与加号**之间**：读起来是「3.0 秒」，跟随数值更直观。
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

    // 禁用态：整体降透明度 + 关掉内部交互。
    //
    // **必须显式处理**（踩坑，与 CheckBoxLine 同一个坑）：`Item` 的
    // `enabled: false` **不会**自动禁用子项 —— 下面的两个 MouseArea
    // 和 TextInput 都是独立 Item，不显式跟上的话，按钮看起来变灰了、
    // 点下去照样改数值（用户会以为设置坏了）。而 TextInput 更是会
    // 直接抢焦点，光标还能进到框里打字。
    opacity: enabled ? 1.0 : 0.45
    Behavior on opacity { NumberAnimation { duration: Theme.durFast } }

    readonly property int _btnSize: 30

    function format(v) {
        // **单位（suffix）不拼在这里**：输入框只放纯数字，单位由 inputRow
        // 末尾的 Text 显示（见下方 suffixLabel）。早期版本拼进 TextInput，
        // 于是"秒"会被当成可编辑内容（光标能进去、选中会一起复制），
        // 且各行的输入框宽度会被单位长度悄悄改掉。
        return root.decimals > 0 ? v.toFixed(root.decimals) : String(Math.round(v))
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
        // 输入框里只有数字（见 format 说明），无需再剥离 suffix；
        // 但仍容忍用户手打了 "3秒" 这类内容 —— parseFloat 会取到 3。
        var v = parseFloat(edit.text.trim())
        if (isNaN(v)) {
            edit.text = format(root.value)   // 非法输入还原
            return
        }
        setValue(v, true)
    }

    /// 诊断用：导出四段（减号 / 输入框 / 单位 / 加号）的几何，
    /// 便于脚本核对「单位是否真的夹在输入框与加号之间」。
    function debugLayout() {
        return {
            "minusX": Math.round(minusBtn.x),
            "minusW": Math.round(minusBtn.width),
            "fieldX": Math.round(field.x),
            "fieldW": Math.round(field.width),
            "suffixX": Math.round(suffixLabel.x),
            "suffixW": Math.round(suffixLabel.implicitWidth),
            "suffixVisible": suffixLabel.visible,
            "plusX": Math.round(plusBtn.x),
            "plusW": Math.round(plusBtn.width),
            "totalW": Math.round(root.width),
            // 顺序判据：输入框 < 单位 < 加号（单位不可见时跳过单位那一段）
            "ordered": minusBtn.x < field.x
                       && field.x < plusBtn.x
                       && (!suffixLabel.visible
                           || (field.x + field.width <= suffixLabel.x + 0.5
                               && suffixLabel.x <= plusBtn.x + 0.5))
        }
    }

    // ---- 四段式布局：减号 | 输入框 | 单位 | 加号 ----
    //
    // 现在用一个 Row 从左到右顺序排列，间距统一为 spacingMd，
    // 天然形成「− [输入框] 单位 +」的组合。注意 Row 不会拉伸子项，
    // 因此输入框必须显式给宽度（不能用 fillWidth）。
    //
    // **单位在输入框外**：这样"秒"不可编辑，且输入框宽度 =
    // 总宽 − 两个按钮 − 单位宽 − 三段间距，各行按同一公式算出来，
    // 右边缘（`+` 的右边）自然对齐。
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
                // 禁用时显式关掉（见根项 opacity 处的说明）
                enabled: root.enabled
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.nudge(-1)
            }
        }

        // 输入框：宽度 = 总宽 − 两个按钮 − 单位 − 三段间距
        Rectangle {
            id: field
            anchors.verticalCenter: parent.verticalCenter
            width: row.width - root._btnSize * 2 - row.spacing * 2
                   - (suffixLabel.visible ? suffixLabel.implicitWidth + row.spacing : 0)
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
                // 禁用时不可获得焦点 / 不可编辑（见根项说明）
                enabled: root.enabled
                activeFocusOnTab: root.enabled

                // 允许鼠标拖选（默认 false，框里的字选不中）。
                selectByMouse: true

                // **聚焦时整段选中** —— 这是"手打能不能用"的关键。
                //
                // 不选中的话，点进来的光标落在**点击处**（点"8080"中间就是第 2 位），
                // 于是敲的数字是**插进数字中间**而不是替换：8080 → 敲 9 → 80980 →
                // 连敲变成 8012380 → 失焦提交时被 clamp 截到上限 65535。
                // 用户看到的现象就是"打不进去，只能靠 ± 和退格"。
                // 整段选中后：敲的第一个字符直接替换整个值（3.0 这种小数也能整段重打），
                // 想局部改就**再点一下**——那时 activeFocus 没变、不触发这里，光标正常落位。
                onActiveFocusChanged: {
                    if (activeFocus)
                        selectAll()
                    else
                        root.commit()
                }

                onEditingFinished: root.commit()
            }

            // 指针层：**数字框上的指针要像文本框一样是 I 字形**。
            //
            // 必须显式加这一层：**裸 `TextInput` 的指针是箭头**，不是 I 字形
            // （`AppTextField` 同样是靠自己那层 MouseArea 才有的 I 字形）。
            // 本组件原来没有这一层，所以数字框上的指针一直是箭头。
            //
            // 三条都要照抄，缺一不可：
            //   - `acceptedButtons: Qt.NoButton` + `enabled: false`：不吃任何事件，
            //     点击/拖选照常落到下层 TextInput；
            //     只提供光标形状 —— **`cursorShape` 不受 `enabled` 约束**。
            //   - `anchors.fill: parent` 铺满整个输入框（含左右内边距），
            //     而不是只盖住文字那一小条。
            //   - 禁用态给箭头，与 `AppTextField` 只读态给箭头保持一致 ——
            //     这栏不能改，就不要暗示它能点。
            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                enabled: false
                acceptedButtons: Qt.NoButton
                cursorShape: root.enabled ? Qt.IBeamCursor : Qt.ArrowCursor
            }
        }

        // 单位：夹在输入框与加号之间（不可编辑、不参与数值解析）。
        // 空 suffix 时整段不占位（visible=false + field 的宽度公式里排除）。
        Text {
            id: suffixLabel
            anchors.verticalCenter: parent.verticalCenter
            visible: root.suffix.length > 0
            text: root.suffix
            color: Theme.textSecondary
            font.pixelSize: Theme.fontMd
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
                // 禁用时显式关掉（见根项 opacity 处的说明）
                enabled: root.enabled
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.nudge(1)
            }
        }
    }
}
