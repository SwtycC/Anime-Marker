import QtQuick

// 状态栏「更多」菜单：点状态栏最右侧那个三条水平线的按钮，从**下方弹出**。
//
// **铺满整个窗口**（由 Main.qml 用 `anchors.fill: parent` 摆好），面板自己
// 贴在右下角往上长。这样做是为了"点面板以外的地方关闭"：那需要一个覆盖
// 全窗的 MouseArea，而它必须和面板同层 —— 于是把两者放进同一个组件里。
//
// **不用 QtQuick.Controls 的 Menu**：本项目所有面板（TagFilterPill、筛选
// 面板、各个小窗）都是自绘的，走同一套视觉；混一个系统风格的菜单会在界面里
// "突出来"一块（与 ConfirmDialog 不用 MessageDialog 是同一个理由）。
//
// 行动作只发信号、不自己调后端：状态栏消息、小窗、浏览器都由 Main.qml
// 那层统一处理（与 PosterWallPage 只发 addAnimeRequested 同一套做法）。
Item {
    id: root

    /// 面板是否展开（由调用方切换；点外部会自动置回 false）
    property bool opened: false
    /// 面板下沿距离窗口底部的距离（= 状态栏高度 + 一点间隙，由 Main.qml 传）
    property real bottomOffset: 28 + Theme.spacingMd
    /// 「检查更新」那一行右侧要不要点小红点（绑 appMenu.hasUpdate）
    property bool hasUpdate: false
    /// 远端最新版本号（绑 appMenu.latestTag；空串不显示）
    property string latestTag: ""
    /// 是否正在检查（那一行显示"检查中…"并禁用点击）
    property bool checking: false

    /// 面板里某一项被点了。`action` 取值见下面的 model。
    signal triggered(string action)

    // 菜单项。
    //   action —— 给 Main.qml 分派用的标识
    //   icon   —— NavIcon 的 kind（见 resources/icons/README.md 的清单）
    readonly property var items: [
        {"action": "update", "icon": "update",   "text": "检查更新"},
        {"action": "logs",   "icon": "folder",   "text": "打开日志目录"},
        {"action": "help",   "icon": "help",     "text": "帮助"},
        {"action": "about",  "icon": "info",     "text": "关于"},
        {"action": "issues", "icon": "feedback", "text": "反馈问题"}
    ]

    function open() {
        root.opened = true
    }

    function close() {
        root.opened = false
    }

    // ---- 点击面板以外 → 关闭 ----
    //
    // 铺满整窗（本组件就是整窗大小）。**在面板之后声明**？不 —— 它在
    // 面板**之前**，面板后画即在上层，所以面板内的点击不会落到这里。
    MouseArea {
        anchors.fill: parent
        enabled: root.opened
        visible: root.opened
        onClicked: root.close()
    }

    // ---- 面板 ----
    Rectangle {
        id: panel
        objectName: "morePanel"      // 诊断 / 截图核对取几何用

        anchors.right: parent.right
        anchors.bottom: parent.bottom
        // 与状态栏那个按钮的右间距保持一致（两者右边缘对齐）
        anchors.rightMargin: Theme.spacingMd
        anchors.bottomMargin: root.bottomOffset
        width: 180
        height: col.implicitHeight + Theme.spacingSm * 2
        radius: Theme.radiusLg        // 面板统一用 radiusLg（见 Theme 的注释）
        color: Theme.elevatedBg
        border.width: Theme.lineThin
        border.color: Theme.border
        visible: root.opened

        // 展开动画：从下往上淡入 + 轻微位移，别直接"啪"地出现。
        // 只做 opacity/y，不做 height —— 改高度会触发整列重排，卡顿。
        opacity: root.opened ? 1 : 0
        Behavior on opacity {
            NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic }
        }

        Column {
            id: col
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.margins: Theme.spacingSm

            Repeater {
                model: root.items

                delegate: Rectangle {
                    id: row
                    required property var modelData
                    width: col.width
                    height: 32
                    radius: Theme.radiusSm
                    color: rowMouse.containsMouse && rowEnabled
                           ? Theme.hoverFill : "transparent"

                    // 正在检查时，那一行点不动（避免重复发请求）
                    readonly property bool rowEnabled:
                        !(row.modelData.action === "update" && root.checking)

                    // 行首图标：与导航栏同一套（NavIcon + 主题染色），
                    // 尺寸用 16 而不是导航栏那种 24 —— 菜单行只有 32px 高
                    NavIcon {
                        anchors.left: parent.left
                        anchors.leftMargin: Theme.spacingSm
                        anchors.verticalCenter: parent.verticalCenter
                        width: 16
                        height: 16
                        kind: row.modelData.icon
                        color: rowEnabled ? Theme.textSecondary
                                          : Theme.textTertiary
                    }

                    Text {
                        anchors.left: parent.left
                        // 图标左内边距 + 图标宽 + 图标与文字的间距
                        anchors.leftMargin: Theme.spacingSm + 16 + Theme.spacingSm
                        anchors.verticalCenter: parent.verticalCenter
                        text: (row.modelData.action === "update" && root.checking)
                              ? "检查更新…" : row.modelData.text
                        color: rowEnabled ? Theme.textPrimary : Theme.textTertiary
                        font.pixelSize: Theme.fontMd
                    }

                    // 右侧：版本号 + 小红点（只对"检查更新"那一行）
                    Row {
                        anchors.right: parent.right
                        anchors.rightMargin: Theme.spacingSm
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Theme.spacingSm
                        visible: row.modelData.action === "update"

                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            visible: root.latestTag !== "" && root.hasUpdate
                            text: root.latestTag
                            color: Theme.textTertiary
                            font.pixelSize: Theme.fontSm
                        }

                        Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            visible: root.hasUpdate
                            width: 6
                            height: 6
                            radius: 3
                            color: Theme.dangerColor
                        }
                    }

                    MouseArea {
                        id: rowMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: rowEnabled ? Qt.PointingHandCursor
                                                : Qt.ArrowCursor
                        enabled: rowEnabled
                        onClicked: {
                            root.close()          // 先收起，再让上层动作
                            root.triggered(row.modelData.action)
                        }
                    }
                }
            }
        }
    }

    // ---- 面板阴影 ----
    //
    // 与 NavBar 用**同一套做法**：叠几层低透明度圆角矩形。那里记了两次
    // 失败的尝试 —— `layer.enabled` + `MultiEffect(shadowEnabled)` 会在
    // 面板上方渲染出一块不透明矩形，而 `MultiEffect { source: ... }` 在
    // PySide6 下直接崩进程（退出码 0xC0000409）。这套零 GPU 特效依赖，
    // 明暗主题都稳。
    //
    // **面板是浮在任意内容之上的**（海报、白卡片都有），没有阴影时在浅色
    // 内容上会和背景糊在一起 —— 这是它和导航胶囊最大的不同，那一位底下
    // 永远是页面底色。
    Repeater {
        model: 3

        Rectangle {
            required property int index
            visible: root.opened
            anchors.fill: panel
            anchors.margins: -(index + 1)
            // 阴影整体偏下一两像素，比四面等宽自然
            anchors.topMargin: -(index + 1) + 2
            radius: Theme.radiusLg + index
            color: "#000000"
            opacity: (Theme.dark ? 0.18 : 0.05) / (index + 1)
            z: -1 - index
        }
    }
}
