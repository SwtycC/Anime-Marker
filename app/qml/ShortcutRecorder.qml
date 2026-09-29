import QtQuick

// 快捷键输入框（**按键录制**）：点一下进入录制态，直接在键盘上按组合键，
// 按下主键即录制完成并写回，格式与后端发送端完全一致。
//
// 为什么要做成录制而不是手打：
//   这两个值会被**原样**交给 `keyboard.press_and_release()` /
//   `pyautogui.hotkey()`（见 core/launcher._send_keys），后端不做任何
//   规范化。用户手打 `Ctrl+Alt+P`（大写）、`ctrl + alt + p`（多空格）、
//   `ctrl-alt-p`（错分隔符）、`enter`/`return` 混用 —— 任何一处不一致都
//   会"发出去了但目标软件收不到"，现象只是"按了没反应"，极难归因。
//   录制则天然产出规范格式，且用户按的就是他想绑定的那组键，不必在两个
//   软件之间来回比对文案。
//
// 顺带解决另一个坑：`ctrl+alt+l` 会被 QQ 的全局热键抢走。录制时**只读键、
// 不真正触发**任何热键，不会像手打时"顺手试一下"就触发 QQ 自锁；是否冲突
// 仍由设置页的说明文字提示。
//
// 录制态交互（**只有两个结束时机**）：
//   按住修饰键（Ctrl / Shift / Alt / Win）→ 实时预览（如 `ctrl+alt`）
//   再按主键 → 立即完成并写回（如 `ctrl+alt+p`）
//   Esc → 取消本次录制（保留原值）
//   点到别处失焦 / 再次点击输入框 → 退出录制，保留原值
//
// **修饰键松开不算结束**（踩坑，实测反馈）：早期实现里"只按修饰键、松手
// 即写回"，本意是支持单修饰键（如只绑 ctrl），但代价是用户按住 ctrl+alt
// 松开任一修饰键、还没来得及按主键，录制就被判为完成并退出了 —— 于是
// "按住 ctrl 和 alt 后按 p 没反应"（此时已不在录制态）。
//
// 更根本的是：**快捷键的语义就是同时按住**，"先按 ctrl 松手、再按 alt、
// 再按 p"拼出来的 ctrl+alt+p 并不是用户实际按下的组合（真实按下时三者
// 是同时生效的），支持它属于画蛇添足。因此现在**不允许分开慢按**：
// 修饰键松不松都不影响录制，必须按到主键才算数。
//
// 代价：无法只绑一个光秃的修饰键（如 `ctrl`）。这在实用上无损失 ——
// 单独一个 ctrl / shift 做全局键几乎不会被任何软件采用（会与正常打字
// 冲突），后端发送也是如此。
Rectangle {
    id: root

    /// 当前值（形如 `ctrl+alt+p`），双向可绑定
    property string value: ""
    /// 是否处于录制态
    property bool recording: false
    /// 录制过程中的预览文本（如 `ctrl+alt`）
    property string preview: ""
    /// 已按下的修饰键（小写、固定顺序 ctrl/alt/shift/win）
    property var mods: []

    /// 录制出的组合是否被其他软件占用（true=被占 / false=空闲 / null=未知）
    ///
    /// **为什么要检测**（实测）：`ctrl+alt+p` 被别的软件注册为全局热键后，
    /// Windows 会在按键到达窗口前就拦掉，用户按下去毫无反应 —— 而换成
    /// `ctrl+alt+i` 立刻正常。这类问题无法在 QML 里修复，只能**当场提示**。
    /// 由 Python 侧 `settingsBridge.hotkeyAvailable()` 提供三态判定。
    property var occupied: null

    /// **本次会话是否由用户手动录过**（决定要不要显示占用警告）。
    ///
    /// **必需的护栏**（踩坑）：`occupied` 的初值是 null、只有 `checkOccupied`
    /// 会写它，理论上加载时不会变红。但为了让"页面一打开就自己弹红字"这类
    /// 问题**从根上不可能发生**，这里再加一道独立判据 —— 警告只在用户
    /// **真的按下过一个完整组合**之后才允许出现。
    /// 任何绑定顺序、属性初始化、外部赋值都无法绕过它。
    property bool _recordedOnce: false

    /// 被占用时的提示文字（未录过 / 未占用 / 未知 / 正在录制 时都为空）
    readonly property string warnText:
        root._recordedOnce && root.occupied === false && !root.recording
        ? "该组合已被其他软件占用，可能收不到按键，建议换一个"
        : ""

    property string placeholder: "点击后直接按组合键"

    // 根项是**透明容器**：上面是输入框本体（frame），下面可能挂一行警告。
    //
    // **为什么拆出 frame**：被占用时的提示要显示在输入框**下方**，而
    // FormRow 的高度取的是本组件的 implicitHeight —— 若把边框画在根项上，
    // 加高根项就等于把输入框本身拉高（描边跟着变高，很难看）。
    // 因此根项只负责"排行"，视觉留在 frame 里。
    implicitHeight: frame.height + (warnText !== "" ? warnLabel.height + 4 : 0)
    implicitWidth: 200

    // 固定顺序：与后端拼接习惯一致，也便于肉眼比对
    readonly property var _modKeys: ["ctrl", "alt", "shift", "win"]

    Rectangle {
        id: frame
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 34
        radius: Theme.radiusSm
        color: root.recording ? Theme.accentSoft : Theme.surfaceBg
        // 边框只在**录制态**变主题色。
        //
        // **不要绑定 `input.activeFocus`**（踩坑，实测）：内部承载键盘事件的
        // Item 在录制时会拿到焦点，但"有焦点"并不等于"正在录制" —— 更糟的是
        // 组件一旦被创建/滚动进视口，若那个 Item 恰好拿到焦点，边框会**无缘
        // 无故变成主题色**，看起来像"报错了"（实测反馈：进设置页滚到这一行
        // 就有个红框，点别处才消失）。
        //
        // 视觉语义上也不需要它：本控件"点一下才进入录制"，状态全部由
        // `recording` 表达，再叠一个焦点态反而让人分不清是聚焦还是录制中。
        border.width: root.recording ? Theme.lineThick : Theme.lineThin
        border.color: root.recording ? Theme.accent : Theme.border

        Behavior on color { ColorAnimation { duration: Theme.durFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

        // 文本、光标、鼠标区、焦点项全部搬进 frame（锚点相对 frame 更直观）
        Text {
            id: displayLabel
            anchors.fill: parent
            anchors.leftMargin: Theme.spacingMd
            anchors.rightMargin: Theme.spacingMd + (root.recording ? 10 : 0)
            verticalAlignment: Text.AlignVCenter
            clip: true
            elide: Text.ElideRight
            font.pixelSize: Theme.fontMd
            text: {
                if (root.recording)
                    return root.preview !== "" ? root.preview : "请按下组合键…"
                return root.value !== "" ? root.value : root.placeholder
            }
            color: root.recording ? Theme.accent
                 : root.value !== "" ? Theme.textPrimary
                 : Theme.textTertiary
        }

        // 录制中的闪烁光标：给"正在等按键"一个活体反馈
        Rectangle {
            visible: root.recording
            anchors.verticalCenter: parent.verticalCenter
            x: Math.min(displayLabel.contentWidth + Theme.spacingMd,
                        parent.width - 12)
            width: 1
            height: 16
            color: Theme.accent
            SequentialAnimation on opacity {
                running: root.recording
                loops: Animation.Infinite
                NumberAnimation { to: 0; duration: 480 }
                NumberAnimation { to: 1; duration: 480 }
            }
        }

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            hoverEnabled: true
            onClicked: {
                if (root.recording)
                    root.stopRecording()
                else
                    root.startRecording()
            }
        }

        // 承载键盘焦点。**不用 TextInput**：那样录制时按 s 会被当成文字输入
        // 插进 value（"录快捷键时冒出字母"）。空 Item + Keys 处理器即可。
        //
        // **刻意不写 `focus: true`**（踩坑，实测）：那会让组件一被创建
        // （或滚动进视口）就**主动抢走焦点**，配合边框的 activeFocus 绑定
        // 表现为"进设置页滚到这一行就有个主题色红框，点别处才消失"。
        // 录制时所需的焦点由 `startRecording()` 里的
        // `input.forceActiveFocus()` 按需取得，不需要常驻。
        Item {
            id: input
            anchors.fill: parent
            Keys.onPressed: function (event) { root.handlePressed(event) }
            Keys.onReleased: function (event) { root.handleReleased(event) }
            onActiveFocusChanged: {
                // 点到别处失焦 → 退出录制（保留原值，不写半成品）
                if (!activeFocus && root.recording)
                    root.stopRecording()
            }
        }
    }

    // 被其他软件占用时的警告（见 occupied / warnText 的说明）。
    //
    // **不要写 `height: visible ? implicitHeight : 0`**（踩坑）：这是把
    // height 绑定到包含自身的表达式，QML 会判定为 binding loop 并刷警告。
    // 隐藏时高度归零由 `visible` + 父项 implicitHeight 里的条件共同保证，
    // 不需要再手动控制 height。
    Text {
        id: warnLabel
        anchors.top: frame.bottom
        anchors.topMargin: 4
        anchors.left: parent.left
        width: parent.width
        visible: root.warnText !== ""
        text: root.warnText
        color: Theme.dangerColor
        font.pixelSize: Theme.fontXs
        wrapMode: Text.WordWrap
    }

    function startRecording() {
        root.mods = []
        root.preview = ""
        root.occupied = null          // 上一轮的结论不带到本次录制
        root.recording = true
        input.forceActiveFocus()      // 拿到焦点才会收到 Keys 事件
    }
    function stopRecording() {
        root.recording = false
        root.mods = []
        root.preview = ""
    }

    /// 录到一个完整组合后调用：查是否被其他软件占用（三态，见 occupied）
    function checkOccupied(combo) {
        root.occupied = null
        if (combo === "")
            return
        if (typeof settingsBridge === "undefined" || !settingsBridge)
            return                   // 桥接不可用：不检查，也不误报
        root.occupied = settingsBridge.hotkeyAvailable(combo)
    }

    /// 组合键文本：修饰键（固定顺序）在前、主键在后，用 + 连接
    function compose(mods, key) {
        var out = []
        for (var i = 0; i < root._modKeys.length; i++) {
            var m = root._modKeys[i]
            if (mods.indexOf(m) >= 0)
                out.push(m)
        }
        if (key !== "")
            out.push(key)
        return out.join("+")
    }

    /// 事件里的修饰键 → 小写名字数组（按 event.modifiers 判定）。
    ///
    /// **踩坑（实测）**：按下**修饰键本身**时（如先按 Ctrl），Qt 给的
    /// `event.modifiers` 里往往**不含这个键自己** —— 只按 Ctrl 得到
    /// `modifiers == 0`。所以**不能**只靠这个函数判断"当前按住了什么"，
    /// 必须结合逐次按下时累积的 `root.mods`（见 onModifierDown/Up）。
    ///
    /// 注意这里**刻意不做 `event.key === Qt.Key_Control` 之类的兜底**：
    /// 早先版本加了这种兜底，副作用是当**主键**恰好是某个修饰键键码时
    /// （如某些布局下的特殊键），它会被误算进组合。修饰键自己按下时
    /// 由 `onModifierDown` 直接按 key 处理，不依赖本函数。
    function modsOf(event) {
        var out = []
        if (event.modifiers & Qt.ControlModifier)
            out.push("ctrl")
        if (event.modifiers & Qt.AltModifier)
            out.push("alt")
        if (event.modifiers & Qt.ShiftModifier)
            out.push("shift")
        if (event.modifiers & Qt.MetaModifier)
            out.push("win")
        return out
    }

    /// 修饰键键码 → 名字；不是修饰键返回空串
    function modifierNameOf(k) {
        if (k === Qt.Key_Control)
            return "ctrl"
        if (k === Qt.Key_Alt)
            return "alt"
        if (k === Qt.Key_Shift)
            return "shift"
        if (k === Qt.Key_Meta)
            return "win"
        return ""
    }

    /// 合并修饰键（去重、保持固定顺序由 compose 负责）
    function _withMod(mods, name) {
        var out = mods ? mods.slice() : []
        if (name !== "" && out.indexOf(name) < 0)
            out.push(name)
        return out
    }

    function _withoutMod(mods, name) {
        var out = []
        var src = mods || []
        for (var i = 0; i < src.length; i++)
            if (src[i] !== name)
                out.push(src[i])
        return out
    }

    /// Qt 键码 → 后端认的键名。
    ///
    /// **映射必须与后端一致**：字符串直接进 keyboard / pyautogui。
    /// 取名原则是这两个库都接受的通用名（`enter` 而非 `return`、
    /// `escape`、`pageup`…），并优先用**名字**而不是符号字符 ——
    /// 字符在不同键盘布局 / 输入法下不稳。
    function keyName(event) {
        var k = event.key
        if (k >= Qt.Key_A && k <= Qt.Key_Z)
            return String.fromCharCode(k).toLowerCase()
        if (k >= Qt.Key_0 && k <= Qt.Key_9)
            return String.fromCharCode(k)
        if (k >= Qt.Key_F1 && k <= Qt.Key_F24)
            return "f" + (k - Qt.Key_F1 + 1)
        switch (k) {
        case Qt.Key_Return:
        case Qt.Key_Enter:        return "enter"
        case Qt.Key_Tab:          return "tab"
        case Qt.Key_Backspace:    return "backspace"
        case Qt.Key_Delete:       return "delete"
        case Qt.Key_Insert:       return "insert"
        case Qt.Key_Home:         return "home"
        case Qt.Key_End:          return "end"
        case Qt.Key_PageUp:       return "pageup"
        case Qt.Key_PageDown:     return "pagedown"
        case Qt.Key_Space:        return "space"
        case Qt.Key_Up:           return "up"
        case Qt.Key_Down:         return "down"
        case Qt.Key_Left:         return "left"
        case Qt.Key_Right:        return "right"
        case Qt.Key_Minus:        return "minus"
        case Qt.Key_Equal:        return "plus"
        case Qt.Key_Comma:        return "comma"
        case Qt.Key_Period:       return "period"
        case Qt.Key_Slash:        return "slash"
        case Qt.Key_Semicolon:    return "semicolon"
        case Qt.Key_Apostrophe:   return "apostrophe"
        case Qt.Key_BracketLeft:  return "leftbracket"
        case Qt.Key_BracketRight: return "rightbracket"
        case Qt.Key_Backslash:    return "backslash"
        case Qt.Key_QuoteLeft:    return "grave"
        }
        return ""                  // 不认识的键：忽略，不写进组合
    }

    function isModifierKey(k) {
        return k === Qt.Key_Control || k === Qt.Key_Shift
            || k === Qt.Key_Alt || k === Qt.Key_Meta
    }

    // ---- 按键处理 ----
    //
    // 挂在持有焦点的 Item 上（见下方 `input`）。
    //
    // **不依赖 event.modifiers 的"当前状态"来判定组合**（踩坑）：真实键盘
    // 与合成事件的 modifiers 行为并不一致 —— 按下某个键时 Qt 给的
    // `modifiers` 未必包含此刻物理上已按住的修饰键（尤其 Alt 在 Windows
    // 上会被系统菜单循环吞掉、Ctrl 在某些布局下时序滞后），这正是"按住
    // ctrl+alt 后按 p 录不进去"的根因。
    //
    // 现在的做法：**修饰键按下/松开各自维护 `root.mods`**（key 是可靠的，
    // 一定对应真实按下的那个键），主键按下时把 `root.mods` 当作组合的
    // 修饰键部分。`event.modifiers` 只用作补充（并集），不作主依据。
    function handlePressed(event) {
        if (!root.recording)
            return
        // 长按产生的重复事件忽略：修饰键长按会反复触发，若参与逻辑会
        // 把已收集的组合清空/重复确认
        if (event.isAutoRepeat) {
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_Escape) {      // Esc = 取消（保留原值）
            root.stopRecording()
            event.accepted = true
            return
        }
        var modName = root.modifierNameOf(event.key)
        if (modName !== "") {
            // 修饰键：记下它，但不结束录制（见文件头"只有两个结束时机"）
            root.mods = root._withMod(root.mods, modName)
            root.preview = root.compose(root.mods, "")
            event.accepted = true
            return
        }
        var mainKey = root.keyName(event)
        if (mainKey === "") {
            // **未知键**：吞掉事件但不改动任何状态。
            // 早先版本这里若写回 value 会把组合清掉，表现成"按了没反应还
            // 把已按的 ctrl+alt 丢了"。现在只吞事件、继续等待主键。
            event.accepted = true
            return
        }
        // 主键按下 → 完成并写回。
        // 修饰键 = 累积的 root.mods ∪ event.modifiers（并集，容错两种来源）
        var mods = root.mods.slice()
        var fromEvent = root.modsOf(event)
        for (var i = 0; i < fromEvent.length; i++)
            mods = root._withMod(mods, fromEvent[i])
        var combo = root.compose(mods, mainKey)
        root.value = combo
        root.stopRecording()
        // 只有走到这里（用户真的按出了主键、录到一个完整组合）才允许
        // 显示占用警告 —— 见 _recordedOnce 的说明。
        root._recordedOnce = true
        root.checkOccupied(combo)
        event.accepted = true
    }

    function handleReleased(event) {
        if (!root.recording)
            return
        // **修饰键松开只从 mods 里移除它，不结束录制**（见文件头说明）。
        //
        // 早先版本在松手时启一个 150ms 定时器、"松手即写回"，结果是按住
        // ctrl+alt 时只要松开任一键录制就提前结束，用户再按 p 已经不在
        // 录制态 —— 即"按住 ctrl 和 alt 后按 p 无法输入"。
        //
        // 现在松手**不移除也不行**：若不移除，松开 alt 后按 p 会错录成
        // ctrl+alt+p（用户手上其实只剩 ctrl）。所以做"移除"，但仍然
        // 不结束录制 —— 结束只由"主键"或"Esc"触发。
        var name = root.modifierNameOf(event.key)
        if (name === "")
            return
        // autoRepeat 的松开事件也带着同样的 key，无需特判：移除是幂等的
        root.mods = root._withoutMod(root.mods, name)
        root.preview = root.compose(root.mods, "")
    }

}
