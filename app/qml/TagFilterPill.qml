import QtQuick
import QtQuick.Controls

// 标签筛选胶囊：收起时是圆形图标按钮（向下箭头），点开后展开成一块圆角长方形面板。
//
// 与 SearchPill 的关系：原型同源（收起 = 圆形图标按钮，展开靠宽度动画），
// 但三处**有意做得不同**：
//   ① 展开后的形状是**圆角长方形**（radiusMd/Lg），不是两端半圆的胶囊；
//   ② 高度会随内容一起长（SearchPill 只往右长）；
//   ③ 本组件在行内占的槽位**会变宽**，因此展开时把右侧的搜索胶囊顶向右 ——
//      这就是需求里"搜索按钮右移"的由来（SearchPill 的槽位是固定的）。
//
// **高度必须锁死在收起态的槽位尺寸**（见下面的 height）：headerBox 的高度
// 是按子项算出来的，Flow 的 _headerGap 又读 headerBox.height —— 本项若跟着
// 面板一起长高，整片海报网格会在展开瞬间被顶下去。所以 root.height 恒为
// collapsedSize + 投影留白，面板实体（body）以**溢出子项**的方式往下画：
// QML 不裁溢出内容，视觉上正确，布局上纹丝不动。
//
// 内容一律**定宽**（panelWidth），不随展开动画变化 —— 否则 Flow 会在动画的
// 每一帧重新换行，chips 一路抖到展开结束（同 SearchPill 里"输入框宽度写死"
// 的思路：多余部分由 body 的 clip 裁掉，无需逐帧重排）。
//
// 本组件只管画与点、不碰数据：行模型（rows）与选中态（selection / stateValue）
// 都由页面持有并传入，点选只发信号出去。
Item {
    id: root

    /// 展开 / 收起（由页面持有：外部"点空白处收起"也要能改它）
    property bool opened: false

    /// 面板行模型（页面组装）：
    /// `[{ "title": "分类", "kind": "tag", "chips": [{"label": "TV", "value": "TV"}, ...] }, ...]`
    /// `kind` 决定选中态从哪读：`tag` → `selection[行标题]`，`state` → `stateValue`。
    /// chips 里 value 为空串的那一项即"全部"（表"本行不筛"）。
    property var rows: []
    /// 每类选中的 tag：`{"分类": "TV", ...}`（缺省 = 该类看"全部"）
    property var selection: ({})
    /// 状态筛选：`""`（全部）/ `"matched"`（已匹配）/ `"unmatched"`（未匹配）
    property string stateValue: ""

    /// 展开后的宽度（故意比 SearchPill 的 260 宽：一行要放得下 3~4 个 chip）
    property int panelWidth: 400
    /// 滚动区最大高度（由页面按可用空间传入：不能顶到悬浮导航上）
    property int contentMaxHeight: 420

    /// 选中某项（`value` 为空串 = 选中"全部"）。分类标题与 kind 一并回传，
    /// 页面据此决定写进 selection 还是 stateValue。
    signal picked(string kind, string title, string value)

    /// 收起态尺寸，同时是展开后箭头所在的左上角槽位尺寸
    readonly property int collapsedSize: Theme.navButtonSize
    readonly property int _pad: Theme.spacingMd
    readonly property int _innerW: panelWidth - _pad * 2
    /// 投影留白（与 SearchPill 同一约定，值也取一样）
    readonly property int _shadowPad: 6

    // 滚动区高度：内容不足时按内容算（面板随内容变矮），超出则封顶后可滚动
    readonly property real _listHeight: Math.min(contentMaxHeight,
                                                 Math.max(0, rowsCol.implicitHeight))
    readonly property real _panelHeight: collapsedSize + Theme.lineThin
                                         + _listHeight + _pad

    // 宽度动画让出/收回行内空间（搜索胶囊随之右移 / 复位）。
    //
    // **高度含投影留白**（= SearchPill 的 implicitHeight 算法，两者必须一样）：
    // headerBox 里那个 Row 是**顶对齐**摆放子项的，而 SearchPill 的槽位高
    // 46px（40 实体 + 6 留白、实体居中于槽位）——本项若只给自己 40px，
    // 实体就贴着槽位顶边，比搜索胶囊**高 3px**（实测反馈："下拉按钮和搜索
    // 按钮不在同一水平线"）。给同样的 46px、实体下移 3px 后，两个实体的
    // 上下边完全齐平。
    width: opened ? panelWidth : collapsedSize
    height: collapsedSize + _shadowPad
    Behavior on width {
        NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic }
    }

    // ---- 投影层：两层低透明度圆角矩形（不用 MultiEffect，原因见 NavBar.qml）----
    // 圆角绑到 body.radius：收起时是圆的、展开时是长方形的，阴影要跟着变。
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

    // ---- 面板本体 ----
    Rectangle {
        id: body
        x: 0
        // 实体在槽位里**居中**（收起态上下各留 3px 给投影），展开时只向下长。
        // 注意别写成 `y: (root.height - height) / 2` —— height 在做动画，
        // 那样面板会向上越长越高（上半截跑到搜索栏上方去）。
        y: root._shadowPad / 2
        width: root.width
        height: root.opened ? root._panelHeight : root.collapsedSize
        // 收起 = 正圆（radius = 高一半）；展开 = 普通圆角长方形
        radius: root.opened ? Theme.radiusLg : height / 2
        // 收起时是**主题色实心**（与搜索胶囊一致），展开后让位给"白底卡片"
        color: root.opened
               ? Theme.surfaceBg
               : (hover.hovered ? Theme.accentHover : Theme.accent)
        border.width: root.opened ? Theme.lineThin : 0
        border.color: Theme.border
        // 动画期间内容会超出面板范围（宽度/高度都还没长到位），必须裁掉
        clip: true

        Behavior on height { NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic } }
        Behavior on radius { NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic } }
        Behavior on color { ColorAnimation { duration: Theme.durSlow } }

        // 悬停检测用 HoverHandler：只"观察"，不吃事件（同 SearchPill）
        HoverHandler { id: hover }

        // ---- 顶部条：箭头 + 标题 ----
        // 宽度写死 panelWidth（不是 parent.width）：展开动画期间不能重排，
        // 多余部分交给 body 的 clip。
        Item {
            id: topStrip
            width: root.panelWidth
            height: root.collapsedSize

            // 箭头槽的底色（仅展开时显示 —— 收起时整块是主题色实心圆，
            // 圆里再压一个浅灰方块会很脏）
            Rectangle {
                x: 0
                y: 0
                width: root.collapsedSize
                height: root.collapsedSize
                radius: Theme.radiusSm
                color: root.opened && arrowArea.containsMouse ? Theme.hoverFill
                                                              : "transparent"

                Behavior on color { ColorAnimation { duration: Theme.durFast } }
            }

            // 箭头：收起时居中于圆（x/y 见下），展开后停在左上角 40×40 槽位里 ——
            // 两个位置用**同一个公式**（槽位尺寸恒为 collapsedSize），无需分支。
            NavIcon {
                id: arrow
                kind: "chevron"
                width: Math.round(root.collapsedSize * 0.5)
                height: width
                x: Math.round((root.collapsedSize - width) / 2)
                y: Math.round((root.collapsedSize - height) / 2)
                color: root.opened ? Theme.textPrimary : Theme.accentText
                // 展开后箭头翻转朝上（= "点这里收起"），动画让状态变化看得出来
                rotation: root.opened ? 180 : 0

                Behavior on rotation {
                    NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic }
                }
                Behavior on color { ColorAnimation { duration: Theme.durSlow } }
            }

            // 点击区域 = 整个箭头槽（收起时正好是整圆，展开后是面板左上角）
            MouseArea {
                id: arrowArea
                x: 0
                y: 0
                width: root.collapsedSize
                height: root.collapsedSize
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.opened = !root.opened
            }

            Text {
                anchors.left: parent.left
                anchors.leftMargin: root.collapsedSize + Theme.spacingXs
                anchors.verticalCenter: parent.verticalCenter
                text: "筛选"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontMd
                opacity: root.opened ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
            }

            // 顶部条与滚动区之间的分隔线（同样只在展开态出现）
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: Theme.lineThin
                color: Theme.border
                opacity: root.opened ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
            }
        }

        // ---- 滚动区：各分类的 chip 行 ----
        Flickable {
            id: scroller
            x: root._pad
            y: root.collapsedSize + Theme.lineThin
            width: root._innerW
            height: root.opened ? root._listHeight : 0
            contentWidth: width
            contentHeight: rowsCol.implicitHeight + root._pad
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            visible: root.opened

            ScrollBar.vertical: AppScrollBar {
                id: vbar
                policy: ScrollBar.AsNeeded
            }

            Column {
                id: rowsCol
                // 常年留出滚动条的宽度：若按"条出现才让位"，内容会在滚动条
                // 忽隐忽现时反复重排（chip 跳一下），不如恒定让位。
                width: scroller.width - vbar.implicitWidth
                spacing: Theme.spacingMd

                Repeater {
                    model: root.rows

                    delegate: Column {
                        id: rowCol
                        required property var modelData

                        readonly property bool isState: modelData.kind === "state"
                        /// 本行当前选中的值（空串 = 全部）
                        readonly property string current: isState
                            ? root.stateValue
                            : (root.selection[modelData.title] || "")
                        /// chip 宽度上限：制作公司那一栏写的是
                        /// 「京都アニメーション（京阿尼）」，把上限放宽些，
                        /// 否则**恰恰是最想让人看见的中文名**被省略号吃掉；
                        /// 其他栏的长 tag 仍收紧（一行要尽量多放几个）
                        readonly property int maxChipWidth:
                            modelData.kind === "studio" ? 240 : 168

                        width: rowsCol.width
                        spacing: Theme.spacingSm

                        Text {
                            text: rowCol.modelData.title
                            color: Theme.textSecondary
                            font.pixelSize: Theme.fontSm
                        }

                        Flow {
                            width: parent.width
                            spacing: Theme.spacingSm

                            Repeater {
                                model: rowCol.modelData.chips

                                delegate: Rectangle {
                                    id: chip
                                    required property var modelData

                                    readonly property bool active:
                                        String(modelData.value) === rowCol.current
                                    readonly property bool hovered: chipArea.containsMouse

                                    // 长 tag（"关于我转生变成史莱姆这档事"这类）
                                    // 不能把面板撑爆，限宽后由文字省略
                                    width: Math.min(chipLabel.implicitWidth
                                                    + Theme.spacingMd * 2,
                                                    rowCol.maxChipWidth)
                                    height: 26
                                    radius: Theme.radiusSm
                                    // 选中 = 主题色实心（同参考图里的"全部"）
                                    color: active ? Theme.accent
                                         : hovered ? Theme.hoverFill
                                         : Theme.surfaceAlt
                                    border.width: Theme.lineThin
                                    border.color: active ? Theme.accent : Theme.border

                                    Behavior on color { ColorAnimation { duration: Theme.durFast } }

                                    Text {
                                        id: chipLabel
                                        anchors.centerIn: parent
                                        width: Math.min(implicitWidth, parent.width
                                                        - Theme.spacingMd * 2)
                                        text: chip.modelData.label
                                        color: chip.active ? Theme.accentText
                                                           : Theme.textSecondary
                                        font.pixelSize: Theme.fontSm
                                        elide: Text.ElideRight

                                        Behavior on color { ColorAnimation { duration: Theme.durFast } }
                                    }

                                    MouseArea {
                                        id: chipArea
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: root.picked(rowCol.modelData.kind,
                                                               rowCol.modelData.title,
                                                               String(chip.modelData.value))

                                        // 被限宽省略的（长作品名、长公司名）悬停看全文
                                        ToolTip.visible: containsMouse
                                                        && chipLabel.truncated
                                        ToolTip.delay: 500
                                        ToolTip.text: chipLabel.text
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
