import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material

// Google Material Design 风格的下拉框。
//
// 当前形态：**收起态**用与 AppTextField 完全相同的线性外框（1px
// Theme.border / 34px 高 / 聚焦主题色），**弹出层与选中项**保留
// Material 样式。这样既保留了 Material 的下拉弹层，又不会让这一行
// 比同页其他输入框高出一截、边框颜色也不一样。
//
// **样式导入是逐文件生效的**：`import QtQuick.Controls.Material` 只让
// **本文件里创建的控件**走 Material 风格，应用其余部分仍是 Basic。
// 因此不要把这个 import 加进 SettingsPage.qml —— 那会把整页的控件
// （连同 AppButton 之外的 Button/CheckBox……）一起换掉。
//
// 抽成独立组件的另一个好处：以后想换回自绘下拉框，只改这一个文件。
ComboBox {
    id: root

    // 选项：[{ name, family, builtin }]，由 SettingsPage 从 fontBridge 取。
    // family 为空串 = 系统默认。
    property var options: []

    // 当前字体族（"" = 系统默认）。由调用方绑定到设置项，
    // 用户选择后通过 fontPicked 回传。
    property string currentFamily: ""

    // 用户在下拉里选了一项（family 可能为空串 = 系统默认）
    signal fontPicked(string family)
    // 用户点了某个上传字体右侧的「设置」/「删除」
    signal editRequested(string fontId)
    signal deleteRequested(string fontId)

    // 弹层是否展开。**只为截屏 / 自动化探针提供**：PySide 侧
    // `property("popup")` 拿不到 QQuickPopup 指针（无转换器），
    // 无法从 Python 判断"点一下到底弹没弹"。
    readonly property bool popupVisible: popup ? popup.visible : false

    // 高度与同页的 AppTextField / AppButton 完全一致（都是 34）。
    // Material ComboBox 默认 56，会明显比其余控件高出一截。
    implicitHeight: 34
    height: 34
    font.pixelSize: Theme.fontMd

    // 颜色跟随应用主题；accent 是弹层选中态 / 焦点色。
    Material.theme: Theme.dark ? Material.Dark : Material.Light
    Material.accent: Theme.accent
    Material.foreground: Theme.textPrimary

    // **不要用 Material 自带的背景描边**：它用的是 Material.hintTextColor
    // （比应用里其他输入框的 Theme.border 更深、且深浅不随主题走），
    // 于是字体栏的框和相邻的 AppTextField 明显不是一个颜色，高度也偏大。
    // 这里换成与 AppTextField 完全相同的一层 Rectangle：
    // 1px Theme.border、4px 圆角、聚焦 / 弹层展开时变主题色。
    // 弹出层本身仍是 Material 风格（见 popup 默认实现）。
    background: Rectangle {
        implicitHeight: 34
        radius: Theme.radiusSm
        color: Theme.surfaceBg
        border.width: Theme.lineThin
        border.color: (root.activeFocus || root.popupVisible)
                      ? Theme.accent : Theme.border
        Behavior on border.color {
            ColorAnimation { duration: Theme.durFast }
        }
    }

    model: options
    textRole: "name"

    // ---- 自定义弹层条目 ----
    //
    // 默认的 Material delegate 是一个 MenuItem，无法在行内放「设置 / 删除」
    // 两个按钮，所以整个替换 delegate。行本身的点击由 selectIndex()
    // 手动处理（程序化改 currentIndex 不会发 activated）。
    delegate: Item {
        id: row
        required property var modelData
        required property int index

        width: ListView.view ? ListView.view.width : root.width
        height: 38

        readonly property bool isSelected: root.currentIndex === index
        readonly property bool isUploaded: modelData !== null
                                           && modelData !== undefined
                                           && modelData.uploaded === true
        readonly property bool isHovered: rowMouse.containsMouse
                                          || root.highlightedIndex === index

        Rectangle {
            anchors.fill: parent
            color: row.isHovered ? Theme.hoverFill
                 : row.isSelected ? Theme.accentSoft
                 : "transparent"
        }

        Text {
            id: nameLabel
            anchors.left: parent.left
            anchors.leftMargin: 16
            anchors.verticalCenter: parent.verticalCenter
            // 上传项右侧要留出按钮位置；系统默认 / 内置项占满整行
            width: parent.width - (row.isUploaded ? actions.width + 32 : 32)
            text: row.modelData ? String(row.modelData.name || "") : ""
            color: row.isSelected ? Theme.accent : Theme.textPrimary
            font.pixelSize: Theme.fontMd
            elide: Text.ElideRight
        }

        // **整行点击必须在 actions 之前声明**：QML 后声明的兄弟在上层，
        // actions 里的按钮才能先收到点击；反过来整行 MouseArea 会吃掉它们。
        MouseArea {
            id: rowMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.selectIndex(row.index)
        }

        Row {
            id: actions
            anchors.right: parent.right
            anchors.rightMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            spacing: 2
            visible: row.isUploaded

            // 「设置」：resources/icons/settings.svg（NavIcon 的 gear）
            Item {
                width: 28
                height: 26

                NavIcon {
                    anchors.centerIn: parent
                    width: 16
                    height: 16
                    kind: "gear"
                    color: editArea.containsMouse ? Theme.accent
                                                  : Theme.textSecondary
                }

                ToolTip.visible: editArea.containsMouse
                ToolTip.delay: 500
                ToolTip.text: "设置"

                MouseArea {
                    id: editArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        // 先取 id 再关弹层：关闭动画期间代理可能被销毁
                        var id = String(row.modelData.id || "")
                        root.closePopup()
                        root.editRequested(id)
                    }
                }
            }

            // 「删除」：resources/icons/trash.svg（NavIcon 的 trash）
            Item {
                width: 28
                height: 26

                NavIcon {
                    anchors.centerIn: parent
                    width: 16
                    height: 16
                    kind: "trash"
                    color: deleteArea.containsMouse
                           ? Qt.darker(Theme.dangerColor, 1.15)
                           : Theme.dangerColor
                }

                ToolTip.visible: deleteArea.containsMouse
                ToolTip.delay: 500
                ToolTip.text: "删除"

                MouseArea {
                    id: deleteArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        var id = String(row.modelData.id || "")
                        root.closePopup()
                        root.deleteRequested(id)
                    }
                }
            }
        }
    }

    /// 按 family 找下标；找不到时退回「系统默认」（0）。
    function indexOfFamily(family) {
        var want = (family === undefined || family === null)
                   ? "" : String(family)
        for (var i = 0; i < options.length; i++) {
            var item = options[i]
            var cur = (item && item.family !== undefined)
                      ? String(item.family) : ""
            if (cur === want)
                return i
        }
        return 0
    }

    /// 关闭弹层（探针 / 截图用；PySide 侧拿不到 popup 指针）
    function closePopup() {
        if (popup)
            popup.close()
    }

    /// 选中某一项并按"用户选择"处理（自定义 delegate 用）。
    ///
    /// **不能只改 currentIndex**：ComboBox 对程序化改 currentIndex 不会
    /// 发 `activated`，配置就不会跟着变（见文件末尾 onActivated 的说明）。
    function selectIndex(index) {
        if (!options || index < 0 || index >= options.length)
            return
        currentIndex = index
        activated(index)
        closePopup()
    }

    /// 把 currentIndex 同步到 currentFamily。
    ///
    /// `Qt.callLater` 是必须的：给 ComboBox 换 model 时它会先把自己的
    /// currentIndex 重置为 0，而 `onOptionsChanged` 可能与这次重置的
    /// 先后顺序不定。延到本帧末尾再同步，拿到的才是新 model。
    function syncIndex() {
        if (!options || options.length === 0)
            return
        var index = indexOfFamily(currentFamily)
        if (currentIndex !== index)
            currentIndex = index
    }

    onOptionsChanged: Qt.callLater(syncIndex)
    onCurrentFamilyChanged: Qt.callLater(syncIndex)
    Component.onCompleted: Qt.callLater(syncIndex)

    // 只在**用户实际选择**时回传（代码里改 currentIndex 不会触发 activated，
    // 因此程序化同步不会反过来改配置）。
    onActivated: function (index) {
        if (index < 0 || index >= options.length)
            return
        var item = options[index]
        var family = (item && item.family !== undefined)
                     ? String(item.family) : ""
        root.fontPicked(family)
    }
}
