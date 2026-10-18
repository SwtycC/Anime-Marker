import QtQuick
import QtQuick.Controls

// 海报墙「排序」胶囊。
//
// **为什么单独做一个控件、而不是塞进筛选面板**：
//   ① 语义不同：筛选面板每行都是"缩小结果集"，排序只改顺序 ——
//      混进去会让面板里「全部」这个 chip 的含义变得含糊；
//   ② 使用频率不同：筛选设定一次基本不动，排序经常来回切。
//      让高频操作藏进"点开 → 滚动找 → 点选 → 收起"代价太大；
//   ③ 状态要看得见：排序是**当前态**，不像筛选那样能从卡片数量感知。
//      胶囊上直接写「名称 / 评分 / 日期」，一眼知道现在按什么排。
//
// 视觉与交互沿用 `TagFilterPill` 那一套（收起 = 圆形按钮、展开 = 白底面板、
// 宽度动画把右侧搜索胶囊顶向右），只是内容简单得多：一列单选项。
//
// **本组件不持有排序状态**：当前值（`current`）与选项（`options`）都由页面
// 传入，点选只发 `picked` 信号。这样"谁改排序"只有一个源头（后端），
// 组件被销毁重建也不会丢状态。
Item {
    id: root

    /// 当前排序方式的值（如 `"name"`），由页面绑定
    property string current: "name"
    /// 选项模型：`[{"value": "name", "label": "名称"}, ...]`
    property var options: []

    /// 展开 / 收起（由页面持有，外部"点空白处收起"也要能改它）
    property bool opened: false

    signal picked(string value)

    /// ---- 排名刷新----
    /// 是否正在刷新（后端 `rankRefreshing`）
    property bool refreshing: false
    /// 进度文案（"刷新排名… 23/106"），空闲空串
    property string progressText: ""
    /// 上次刷新时间的人话（"3 天前" / "从未刷新"）
    property string lastRefreshedText: ""
    /// 点了「刷新排名」
    signal refreshRequested()

    /// 展开后的面板宽度。三项文案都很短，320 足够（含内边距）
    property int panelWidth: 320

    readonly property int collapsedSize: Theme.navButtonSize
    readonly property int _pad: Theme.spacingMd
    //: 投影留白（与 TagFilterPill / SearchPill 同一约定，值也一样）
    readonly property int _shadowPad: 6

    /// 当前选中项的文案（收起态显示在按钮旁）
    function currentLabel() {
        for (var i = 0; i < options.length; i++)
            if (options[i].value === current)
                return options[i].label
        return ""
    }

    /// 收起态 = 圆形；展开后 = 一个"按钮 + 文字"的胶囊。
    ///
    /// **为什么收起时也要比一个纯圆宽**：排序是"有状态的"，只用一个图标
    /// 看不出现在按什么排。所以收起态就带上当前值（「名称」）。
    /// 宽度按文字实际宽度算，三项文案长度不同也不会跳动。
    readonly property real _collapsedW:
        collapsedSize + Theme.spacingSm + collapsedLabel.implicitWidth
        + Theme.spacingMd

    /// 展开后的面板内容高度（一行标题 + 每个选项一行 + 底部内边距）。
    ///
    /// **提到 root 上算、让 body 与 panel 都引用它**：
    /// 曾经写成 `panel.height: body.height`，`body.height` 又依赖
    /// `options.length` —— Qt 在解析这种"父项与子项互相引用"时把高度
    /// 当成 0，结果面板里**一行都不渲染**（点了评分/日期像没反应，
    /// 其实信号已经发到后端了）。
    /// 现在依赖是单向的：`_panelHeight`（纯输入）→ body / panel 各自读取。
    /// 底部「刷新排名」那一行的高度（分隔线 + 一行文字）
    readonly property int _refreshRowHeight: 40

    readonly property real _panelHeight:
        collapsedSize + Theme.lineThin + options.length * 32
        + _refreshRowHeight + _pad

    width: opened ? panelWidth : _collapsedW
    // **root 高度恒为收起态**（同 TagFilterPill）：height 参与父级布局
    // （headerBox 的 Row 顶对齐 + headerBox 高度），跟着面板一起长会把
    // 整片海报网格顶下去。面板本身以**溢出子项**的方式向下画。
    height: collapsedSize + _shadowPad

    Behavior on width {
        NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic }
    }

    // ---- 投影层（与 TagFilterPill 同一手法：两层低透明度圆角矩形）----
    Repeater {
        model: 2

        Rectangle {
            required property int index
            anchors.centerIn: body
            anchors.verticalCenterOffset: 3 + index
            width: body.width + index * 2
            height: body.height + index * 2
            radius: body.radius
            color: "#000000"
            opacity: (Theme.dark ? 0.22 : 0.08) / (index + 1)
            z: -1 - index
        }
    }

    // ---- 本体 ----
    Rectangle {
        id: body
        x: 0
        y: root._shadowPad / 2
        width: root.width
        // 高度取 root 上算好的 `_panelHeight`（单向依赖，见那里的说明）
        height: root.opened ? root._panelHeight : root.collapsedSize
        // 收起 = 胶囊（两端半圆）；展开 = 圆角长方形
        radius: root.opened ? Theme.radiusLg : height / 2
        color: root.opened
               ? Theme.surfaceBg
               : (hover.hovered ? Theme.accentHover : Theme.accent)
        border.width: root.opened ? Theme.lineThin : 0
        border.color: Theme.border
        clip: true

        Behavior on height {
            NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic }
        }
        Behavior on radius {
            NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic }
        }
        Behavior on color { ColorAnimation { duration: Theme.durSlow } }

        HoverHandler { id: hover }

        // ---- 收起态的内容：图标 + 当前文案 ----
        //
        // 收起时整块就是按钮本身，所以文字要跟着淡出/淡入，而不是被
        // 宽度动画硬挤（挤会先变成省略号，很脏）。
        Item {
            id: collapsedContent
            width: root._collapsedW
            height: root.collapsedSize
            opacity: root.opened ? 0 : 1   // 展开瞬间让位给面板内容
            visible: opacity > 0

            Behavior on opacity {
                NumberAnimation { duration: Theme.durFast }
            }

            // 排序图标：三条横线、长度递减（常见的"排序"符号）
            Column {
                anchors.verticalCenter: parent.verticalCenter
                x: Math.round((root.collapsedSize - width) / 2)
                spacing: 2

                Repeater {
                    model: [10, 7, 4]

                    Rectangle {
                        required property var modelData
                        width: modelData
                        height: 1.6
                        radius: height / 2
                        color: Theme.accentText
                    }
                }
            }

            Text {
                id: collapsedLabel
                anchors.left: parent.left
                anchors.leftMargin: root.collapsedSize
                anchors.verticalCenter: parent.verticalCenter
                text: root.currentLabel()
                color: Theme.accentText
                font.pixelSize: Theme.fontSm
            }
        }

        // 收起态点击 → 展开；已展开时不响应（展开后由顶部条收起）
        MouseArea {
            anchors.fill: parent
            enabled: !root.opened
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.opened = true
        }

        // ---- 展开态 ----
        //
        // 面板内容整体**定宽**（panelWidth）：宽度动画期间若跟着变宽，
        // 文字会一路重排、抖到动画结束（同 TagFilterPill 的踩坑）。
        Item {
            id: panel
            width: root.panelWidth
            // **不要写 `height: body.height`**（那是循环依赖，
            // 高度会被算成 0，面板里一行都不渲染）。见 root._panelHeight。
            height: root._panelHeight
            visible: root.opened
            opacity: root.opened ? 1 : 0

            Behavior on opacity {
                NumberAnimation { duration: Theme.durFast }
            }

            // 顶部条：标题 + 收起按钮（点整条收起）
            Item {
                id: topStrip
                width: parent.width
                height: root.collapsedSize

                Text {
                    anchors.left: parent.left
                    anchors.leftMargin: root._pad
                    anchors.verticalCenter: parent.verticalCenter
                    text: "排序方式"
                    color: Theme.textSecondary
                    font.pixelSize: Theme.fontMd
                }

                NavIcon {
                    anchors.right: parent.right
                    anchors.rightMargin: root._pad
                    anchors.verticalCenter: parent.verticalCenter
                    width: 14
                    height: 14
                    kind: "chevron"
                    rotation: -90          // 朝上 = "点这里收起"
                    color: Theme.textTertiary
                }

                MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.opened = false
                }

                Rectangle {
                    anchors.bottom: parent.bottom
                    width: parent.width
                    height: Theme.lineThin
                    color: Theme.border
                }
            }

            // 选项列表：一列单选
            Column {
                id: optionsColumn
                anchors.top: topStrip.bottom
                anchors.topMargin: Theme.spacingXs
                width: parent.width

                Repeater {
                    model: root.options

                    delegate: Rectangle {
                        required property var modelData

                        readonly property bool active:
                            String(modelData.value) === root.current

                        width: panel.width
                        height: 32
                        color: active ? Theme.accentSoft
                             : (optArea.containsMouse ? Theme.hoverFill
                                                      : "transparent")

                        Behavior on color {
                            ColorAnimation { duration: Theme.durFast }
                        }

                        Text {
                            anchors.left: parent.left
                            anchors.leftMargin: root._pad
                            anchors.verticalCenter: parent.verticalCenter
                            text: modelData.label
                            color: active ? Theme.accent : Theme.textPrimary
                            font.pixelSize: Theme.fontSm
                            font.weight: active ? Font.DemiBold : Font.Normal
                        }

                        // 选中态打勾（单选视觉，比只有底色更明确）。
                        // **用文字 ✓ 而不是图标**：resources/icons 里没有
                        // 勾选图标，而为它单独加一个 SVG 不值当 ——
                        // 详情页集数行也是用文字 ✓ 表示"已看"，风格一致。
                        Text {
                            anchors.right: parent.right
                            anchors.rightMargin: root._pad
                            anchors.verticalCenter: parent.verticalCenter
                            visible: parent.active
                            text: "✓"
                            color: Theme.accent
                            font.pixelSize: Theme.fontSm
                            font.weight: Font.DemiBold
                        }

                        MouseArea {
                            id: optArea
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                root.opened = false
                                root.picked(String(modelData.value))
                            }
                        }
                    }
                }
            }

            // ---- 底部：刷新排名 ----
            //
            // **为什么放在这里而不是设置页**：它只对"按排名排序"有意义，
            // 放在排序面板里是"在用的地方刷新在用的数据"，不用切页。
            // 设置页那项管的是**自动刷新的周期**，两件事分开。
            //
            // 左侧显示上次刷新时间，让用户自己判断要不要点；
            // 刷新中禁用点击并显示进度（"刷新排名… 23/106"）。
            Column {
                anchors.top: optionsColumn.bottom
                width: parent.width

                // 分隔线
                Rectangle {
                    width: parent.width
                    height: Theme.lineThin
                    color: Theme.border
                }

                Rectangle {
                    id: refreshRow
                    width: parent.width
                    height: root._refreshRowHeight
                    color: refreshArea.containsMouse && !root.refreshing
                           ? Theme.hoverFill : "transparent"

                    Behavior on color {
                        ColorAnimation { duration: Theme.durFast }
                    }

                    Row {
                        anchors.left: parent.left
                        anchors.leftMargin: root._pad
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Theme.spacingSm

                        // 刷新图标：刷新中旋转。
                        NavIcon {
                            anchors.verticalCenter: parent.verticalCenter
                            width: 13
                            height: 13
                            kind: "refresh"
                            color: root.refreshing ? Theme.textTertiary
                                                   : Theme.textSecondary

                            // 旋转用 NumberAnimation 做无限循环；
                            // 停下时角度归零，避免停在半途
                            rotation: 0
                            NumberAnimation on rotation {
                                running: root.refreshing
                                from: 0
                                to: 360
                                duration: 900
                                loops: Animation.Infinite
                            }
                        }

                        Column {
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 1

                            Text {
                                text: root.refreshing ? "刷新中…" : "刷新排名"
                                color: root.refreshing ? Theme.textTertiary
                                                       : Theme.textPrimary
                                font.pixelSize: Theme.fontSm
                            }

                            Text {
                                // 刷新中显示进度，空闲显示上次刷新时间
                                text: root.refreshing
                                      ? root.progressText
                                      : ("上次：" + (root.lastRefreshedText
                                                     || "从未刷新"))
                                color: Theme.textTertiary
                                font.pixelSize: Theme.fontXs
                            }
                        }
                    }

                    MouseArea {
                        id: refreshArea
                        anchors.fill: parent
                        hoverEnabled: true
                        enabled: !root.refreshing
                        cursorShape: root.refreshing ? Qt.ArrowCursor
                                                     : Qt.PointingHandCursor
                        onClicked: root.refreshRequested()
                    }
                }
            }
        }
    }
}
