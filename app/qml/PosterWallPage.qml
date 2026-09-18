import QtQuick
import QtQuick.Controls

// 海报墙：Flow 流式网格。
//
// 相比旧版手写 FlowLayout（QWidget 里要自己实现 heightForWidth），
// QML 的 Flow 原生支持自动换行 + 内容高度自适应，代码量大幅减少。
//
// 布局要点：
// - 外层零边距，滚动条贴窗口右边缘
// - 24px 内边距放进 Flow 的 padding 里
// - 底部预留 navContentGutter，避免最后一行被悬浮导航遮挡
Item {
    id: root

    signal subjectClicked(int subjectId)

    // 进入详情页（由 Main.qml 处理）
    //
    // 防御性写法：library 是运行时注入的上下文属性，单独加载本文件
    // （如开发时的组件预览、静态检查）时并不存在，直接写
    // `library.subjects` 会抛 ReferenceError。这里用 typeof 兜底。
    property var subjects: typeof library !== "undefined" && library
                           ? library.subjects : []

    Flickable {
        id: flick
        anchors.fill: parent
        clip: true
        contentWidth: width
        // 内容高度 = 顶部留白 + 网格高 + 底部留白（给悬浮导航让位）。
        // 注意：不能只靠 Flow 的 bottomPadding —— Flickable 用的是
        // contentHeight，而我们显式设置了它，Flow 的 padding 不会自动计入。
        contentHeight: Math.max(
            Theme.pagePadding + content.implicitHeight
                + Theme.navContentGutter, height)
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.vertical: AppScrollBar {
            id: vbar
            policy: ScrollBar.AsNeeded
        }

        Flow {
            id: content
            // ---- 水平居中 ----
            //
            // Flow 自身**没有对齐属性**（不像 Row 有 layoutDirection），
            // 它总是从 x 开始左对齐排布。窗口宽度不是「列宽整数倍」时，
            // 右侧会留下一条空白，视觉上整片网格偏左（实测明显）。
            //
            // 解决办法：算出这一行实际有几列，再把这行卡片的总宽度
            // 相对可用宽度居中，用 x 偏移补偿。Flow 内所有行共用同一个
            // x，因此只需算一次（按满行算，末行卡片数少时也一起居中）。
            //
            // 注意：不能直接把 Flow 的 width 改成内容宽度然后居中 ——
            // Flow 依赖给定宽度来决定在哪换行，改窄会导致换行点变化。
            readonly property int _cardW: Theme.posterWidth
            readonly property int _spacing: Theme.posterSpacing
            readonly property int _avail: flick.width - Theme.pagePadding * 2
                                          - (vbar.visible ? vbar.width : 0)
            // 这一行最多能放几列（至少 1 列，避免除零/负宽度）
            readonly property int _cols: Math.max(1, Math.floor(
                (_avail + _spacing) / (_cardW + _spacing)))
            // 满行总宽 = N 张卡 + (N-1) 段间距
            readonly property int _rowW: _cols * _cardW + (_cols - 1) * _spacing

            x: Theme.pagePadding + Math.max(0, Math.round((_avail - _rowW) / 2))
            y: Theme.pagePadding
            width: _avail
            spacing: _spacing

            Repeater {
                model: root.subjects

                delegate: PosterCard {
                    required property var modelData

                    subjectId: modelData.id
                    title: modelData.title
                    coverUrl: modelData.coverUrl
                    matchState: modelData.matchState
                    posterWidth: Theme.posterWidth

                    meta: root.buildMeta(modelData)

                    onClicked: function (sid) { root.subjectClicked(sid) }
                }
            }
        }

        // 空状态：整体居中，避免文字贴左上角
        Column {
            anchors.centerIn: parent
            spacing: Theme.spacingSm
            visible: root.subjects.length === 0

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "还没有条目"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontLg
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "请到「设置」中配置媒体库并扫描"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontMd
            }
        }
    }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }

    // 卡片副标题文案（与旧版 _make_card 一致）
    function buildMeta(item) {
        var parts = []
        if (item.isGroup) {
            parts.push(item.childCount + " 部")
            if (item.totalEps > 0)
                parts.push("已看 " + item.watchedEps + "/" + item.totalEps)
            return parts.join(" · ")
        }
        if (item.totalEps > 0)
            parts.push("共 " + item.totalEps + " 集")
        return parts.join(" · ")
    }

    // 数据变化时刷新（library.subjects 变化会自动触发 Repeater 重建，
    // 这里只需保证滚动位置回到顶部）
    Connections {
        target: typeof library !== "undefined" && library ? library : null
        function onSubjectsChanged() {
            flick.contentY = 0
        }
    }
}
