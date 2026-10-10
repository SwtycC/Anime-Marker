import QtQuick
import QtQuick.Controls

// 线性输入框：1px 描边，聚焦时描边变主题色（不改变底色，保持"线性"观感）。
Rectangle {
    id: root

    property alias text: input.text
    property string placeholder: ""
    property bool echoPassword: false

    /// 只读：不可编辑、不可获得焦点。
    ///
    /// 视觉上同时做三件事（缺一用户就看不出"这栏不能改"）：
    ///   ① 底色用 `surfaceAlt`（比输入框浅一档）
    ///   ② 文字用 `textSecondary`（降级）
    ///   ③ 去掉聚焦描边效果（既然拿不到焦点）
    /// 光标形状也改回箭头，避免给出"可点"的错误暗示。
    property bool readOnly: false

    /// 聚焦时把整段内容选中（默认关：其余调用处的行为不变）。
    ///
    /// 给**纯数字的短输入框**用（如「Web UI 端口」）：整段选中后敲的第一个字符直接替换整个值；想局部改
    /// 再点一下即可（那时焦点没变、不会再全选）。
    /// 与 `NumberStepper` 里的处理保持同一套手感。
    ///
    /// **不再兼职"能不能拖选"**：早期它同时兼着 `selectByMouse`，
    /// 于是默认 `false` 的那些框（全应用绝大多数）连按住左键拖都选不中字。
    /// 两件事已经拆开 —— 拖选见下面 `selectByMouse`，恒定打开。
    property bool selectAllOnFocus: false

    signal accepted()
    signal edited()

    /// 主动放弃焦点（清掉聚焦描边、结束文字选中）。
    ///
    /// **必须在组件内部做**：
    /// 聚焦描边读的是 **`input.activeFocus`**（内部那个 TextInput 的焦点），
    /// 而外部只能拿到本组件（外层 Rectangle）—— 给它设 `focus = false`
    /// **什么都不会发生**（Rectangle 从来不是焦点项），描边照旧亮着。
    /// 所以暴露这个方法，由外部在需要时调用。
    function blur() {
        input.focus = false
        // 选中态也要清：焦点没了但高亮的选区还留着，看着仍像"在编辑"
        if (input.selectedText !== "")
            input.deselect()
    }

    implicitWidth: 200
    implicitHeight: 34
    radius: Theme.radiusSm
    color: root.readOnly ? Theme.surfaceAlt : Theme.surfaceBg
    border.width: Theme.lineThin
    border.color: root.readOnly
                  ? Theme.border
                  : (input.activeFocus ? Theme.accent : Theme.border)

    /// 指针层：**I 字形就是靠这一层给的**（别把 cursorShape 写死成箭头）。
    ///
    /// **两条实测结论，之前这里的注释是错的，一并订正**：
    ///   ① **裸 `TextInput` 的指针是箭头，不是 I 字形**。
    ///      所以"去掉这里就恢复 TextInput 自带的 I 字形"不成立 —— 去掉就只剩箭头。
    ///   ② `cursorShape` **不受 `enabled` 约束**：MouseArea 哪怕 `enabled: false`，
    ///      指针划过它的区域时形状照样生效。所以这里可以放心地"可编辑时不参与
    ///      事件（`acceptedButtons: Qt.NoButton`）+ 只给光标形状"。
    /// 形状跟着 `readOnly` 走：可编辑 → I 字形；只读 → 箭头（这栏不能改，
    /// 就别暗示能点），同时吃掉点击。
    MouseArea {
        anchors.fill: parent                    // 铺满整块，含左右内边距
        hoverEnabled: true
        // 可编辑时不参与事件（点击给 TextInput）；只读时吃掉点击
        enabled: root.readOnly
        acceptedButtons: root.readOnly ? Qt.AllButtons : Qt.NoButton
        cursorShape: root.readOnly ? Qt.ArrowCursor : Qt.IBeamCursor
    }
    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

    TextInput {
        id: input
        anchors.fill: parent
        anchors.leftMargin: Theme.spacingMd
        anchors.rightMargin: Theme.spacingMd
        verticalAlignment: TextInput.AlignVCenter
        color: root.readOnly ? Theme.textSecondary : Theme.textPrimary
        font.pixelSize: Theme.fontMd
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText
        echoMode: root.echoPassword ? TextInput.Password : TextInput.Normal
        clip: true
        // ---- 鼠标拖选：**恒定打开** ----
        //
        // 裸 `TextInput` 的 `selectByMouse` 默认是 `false`（只有
        // QtQuick.Controls 的 `TextField` 才默认 `true`）—— 于是框里的字
        // 只能用键盘选：按住左键从第一个字拖到最后一个字，松手时**一个都没
        // 选上**。
        //
        // 与只读态不冲突：只读时上面那层 MouseArea 吃掉了点击
        // （`acceptedButtons: Qt.AllButtons`），根本到不了这里。
        selectByMouse: true
        onActiveFocusChanged: {
            if (root.selectAllOnFocus && activeFocus)
                selectAll()
        }
        // readOnly 用 activeFocusOnPress 拦住"点一下就聚焦"；
        // 注意**不能用 enabled: false** —— 那样文字会整片变淡到几乎看不清，
        // 而这栏的内容（API 地址）恰恰是要让用户看清楚的。
        activeFocusOnPress: !root.readOnly
        readOnly: root.readOnly
        // **不要在这里写 `cursorShape`**：那是 QtQuick.Controls 的 `TextField`
        // 才有的属性，裸 `TextInput` 。指针形状全靠上面
        // 那层铺满的 MouseArea 提供。

        onAccepted: root.accepted()
        onTextChanged: root.edited()

        Text {
            anchors.fill: parent
            verticalAlignment: Text.AlignVCenter
            text: root.placeholder
            color: Theme.textTertiary
            font.pixelSize: Theme.fontMd
            visible: input.text.length === 0 && !input.activeFocus
        }
    }
}
