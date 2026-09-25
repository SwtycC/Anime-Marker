import QtQuick
import QtQuick.Controls

// 展开式搜索胶囊：收起时是圆形图标按钮，悬停 / 聚焦 / 有输入内容时向右展开成输入框。
//
// 视觉移植自 uiverse.io 的展开搜索框（Tailwind 版），按本项目主题体系改写：
//   原版                                → 本实现
//   w-[60px] h-[60px] hover:w-[270px]   → 40 → 260（对齐 Theme.navButtonSize 的控件尺度）
//   bg-[#4070f4]                        → Theme.accent（跟随主题色实时变化，悬停用 accentHover）
//   rounded-full                        → radius: height / 2（圆角随高度实时算，展开过程不变形）
//   flex items-center（图标槽 + 输入框）  → 图标居中于左侧"正方槽"，输入框占右侧剩余宽度
//   fill-white / text-white             → Theme.accentText（按主题色亮度自动取黑或白）
//   transition: duration-300            → Behavior on width（Theme.durSlow）
//   hover 才展开                         → 悬停 / 聚焦 / 文本非空 三者之一即保持展开
//
// **为什么"文本非空时不缩回"**：收起后输入框不可见，但过滤**仍然生效** ——
// 海报墙会停在"只剩几条"的状态，而按钮上看不出任何"正在过滤"的迹象，
// 用户会以为数据丢了。保持展开，过滤状态（以及清除入口）始终可见。
//
// 本组件只管交互与外观、不碰数据：输入内容由外部读 `text` 自行过滤。
//
// 投影同样用「叠两层低透明度圆角矩形」模拟 —— 不用 MultiEffect 的原因见
// NavBar.qml（layer + shadowEnabled 会渲染出实心块，MultiEffect{source:…}
// 在 PySide6 下会崩进程）。
Item {
    id: root

    /// 输入内容（外部读它做过滤）
    property alias text: input.text
    property string placeholder: "搜索动漫名字"

    /// 收起态直径，同时是展开态高度与左侧图标槽宽度 ——
    /// 三者取同一个值，"收起时图标恰好居中于整圆"就成了自然结果，无需额外补偿。
    property int pillHeight: Theme.navButtonSize
    /// 完全展开时的宽度（原版 60→270，这里按项目控件尺度缩小）
    property int expandedWidth: 260

    /// 回车（输入框的惯常行为，交给外部决定"立刻应用"还是忽略）
    signal accepted()
    /// 用户主动清空（点 × 或按 Esc）。内容变化本身请直接看 `text` ——
    /// 这里只标记"用户明确要清空"这一动作，便于外部做额外处理（如重新聚焦网格）。
    signal cleared()

    readonly property bool hovered: hover.hovered
    /// 输入框是否持有焦点（= "搜索框正处于打开状态"）。
    /// 与 `expanded` 的区别：只看焦点，不看鼠标悬停（外部判断"是否该退出搜索"
    /// 时用这个，避免鼠标恰好飘过就被当成在搜索）。
    readonly property bool focused: input.activeFocus
    /// 展开条件（见文件头说明）
    readonly property bool expanded: root.hovered || root.focused
                                     || input.text.length > 0

    /// 收起：交出输入框焦点（外部"点了胶囊以外的地方"时调用）。
    /// 有搜索内容时不会真的收起 —— `expanded` 还有 `text 非空` 这一条。
    ///
    /// 为什么需要显式调用：点击空白处（卡片、Flickable、页面留白）**不会**
    /// 自动转移焦点 —— 那些元素都不抢焦点，输入框会一直保持着 activeFocus，
    /// 胶囊也就一直展开着。
    function collapse() {
        input.focus = false
    }

    // 压在主题色底上的前景色：按主题色亮度自动取黑或白
    readonly property color _fg: Theme.accentText

    readonly property int _shadowPad: 6

    // 宽度**不**随展开变化：本组件在布局里占一个固定槽位，
    // 否则同一行右侧的"找到 N 部"会跟着胶囊一起左右横跳。
    implicitWidth: root.expandedWidth
    implicitHeight: root.pillHeight + _shadowPad
    // 尺寸必须显式落到 width/height：implicit 尺寸只对 Layout / 定位器生效，
    // 裸 Item 不写这一行就是 0×0（见 NavBar.qml 的同类踩坑）。
    width: implicitWidth
    height: implicitHeight

    // ---- 投影层 ----
    Repeater {
        model: 2

        Rectangle {
            required property int index
            // 跟随胶囊本体（本体宽度在动画，投影宽度属性绑上去即同步，无需另做动画）
            anchors.centerIn: pill
            anchors.verticalCenterOffset: 3 + index
            width: pill.width + index * 2
            height: pill.height + index * 2
            radius: height / 2
            color: "#000000"
            opacity: (Theme.dark ? 0.22 : 0.08) / (index + 1)
            z: -1 - index
        }
    }

    // ---- 胶囊本体 ----
    Rectangle {
        id: pill
        // 左对齐（与 CSS 版一致：从左侧的圆形向右"长"出来），
        // 垂直**居中** —— 这样外部把它和同行的文字一起按行居中时，
        // 对齐的是胶囊实体而不是"实体 + 底部投影留白"，
        // 否则同行的文字会比胶囊中心低 _shadowPad/2。
        x: 0
        anchors.verticalCenter: parent.verticalCenter
        width: root.expanded ? root.expandedWidth : root.pillHeight
        height: root.pillHeight
        radius: height / 2                       // 两端半圆 → 收起态即正圆
        color: root.hovered ? Theme.accentHover : Theme.accent
        // 收起过程中图标 / 文字会超出胶囊边界，必须裁掉
        // （Qt 只能裁轴对齐矩形，但这里两端是半圆、内容又在中间，看不出来）
        clip: true

        Behavior on width {
            NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic }
        }
        Behavior on color { ColorAnimation { duration: Theme.durSlow } }

        // 悬停检测用 HoverHandler 而不是 MouseArea：它只"观察"悬停、不吃事件，
        // 因此不会挡住下面 TextInput 的鼠标选区操作。
        HoverHandler { id: hover }

        // 点空白处（图标区 / 胶囊底色）聚焦输入框。
        // 放在 TextInput **之前**声明 → 位于其下层，输入框内的点击仍归 TextInput。
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: input.forceActiveFocus()
        }

        // ---- 搜索图标：固定居中于左侧"正方槽" ----
        // 槽宽 = 胶囊高，于是收起态（宽 = 高）下图标恰好居中于整圆，
        // 展开后则停在左端 —— 与 CSS 版的图标 div 一致。
        NavIcon {
            id: icon
            kind: "search"
            anchors.verticalCenter: parent.verticalCenter
            x: Math.round((root.pillHeight - width) / 2)
            width: Math.round(root.pillHeight * 0.5)
            height: width
            color: root._fg
        }

        // ---- 输入框 ----
        TextInput {
            id: input
            // 起点在图标槽右侧；收起态（宽 = 高）时宽度为 0 → 自然"藏起来"
            x: root.pillHeight
            // 右侧**恒定**留出清除按钮的位置：若按"有内容才留"，敲下第一个字符时
            // 输入框会突然缩窄、文字跟着跳一下。
            width: Math.max(0, pill.width - x - root.pillHeight)
            height: pill.height
            verticalAlignment: TextInput.AlignVCenter
            color: root._fg
            font.pixelSize: Theme.fontMd
            clip: true
            selectByMouse: true
            selectionColor: root._fg
            selectedTextColor: Theme.accent

            onAccepted: root.accepted()

            // 光标：默认光标是黑/白硬编码色，压在本项目可变主题色底上不一定看得见，
            // 故自绘一个（颜色跟随前景色）。custom delegate 不会自动闪烁，这里补上。
            cursorDelegate: Rectangle {
                width: 1
                color: root._fg

                SequentialAnimation on opacity {
                    loops: Animation.Infinite
                    PropertyAnimation { to: 0; duration: 500 }
                    PropertyAnimation { to: 1; duration: 500 }
                }
            }

            Keys.onEscapePressed: {
                input.text = ""
                input.focus = false          // 释放焦点 → 鼠标不在胶囊上时收起
                root.cleared()
            }
        }

        // 占位文案（TextInput 没有 placeholderText，要自己叠一层）
        // 弱化效果用整体 opacity 而非"和底色混一个颜色"—— 见 Theme.mix 的踩坑记录：
        // 跨文件用函数算出的颜色做绑定有静默失效的风险。
        Text {
            x: input.x
            anchors.verticalCenter: parent.verticalCenter
            width: input.width
            text: root.placeholder
            color: root._fg
            opacity: pill.width > root.pillHeight + 24 ? 0.7 : 0
            font.pixelSize: Theme.fontMd
            elide: Text.ElideRight
            // **判空必须带上 preeditText**（踩坑记录）：输入法组字期间
            // `text` 仍是空串（内容还没上屏），但屏幕上已经画出候选字了 ——
            // 只看 text 的话占位文案会留在原地，和候选字**叠在一起**
            // （实测："阿斯顿"三个字直接压在"搜索动漫名字"上）。
            // preeditText 就是这段组字中的内容，非空即让位。
            visible: input.text.length === 0 && input.preeditText.length === 0

            Behavior on opacity { NumberAnimation { duration: Theme.durNormal } }
        }

        // ---- 清除按钮（有内容时才出现）----
        Item {
            id: clearBtn
            anchors.right: parent.right
            width: root.pillHeight
            height: root.pillHeight
            visible: input.text.length > 0

            Text {
                anchors.centerIn: parent
                text: "×"
                color: root._fg
                opacity: clearArea.containsMouse ? 1.0 : 0.7
                font.pixelSize: Theme.fontLg

                Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
            }

            MouseArea {
                id: clearArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    input.text = ""
                    input.forceActiveFocus()   // 清空后留在输入态，方便直接敲下一个词
                    root.cleared()
                }
            }
        }
    }
}
