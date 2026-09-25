import QtQuick
import QtQuick.Controls

// 海报墙：顶部搜索栏 + Flow 流式网格。
//
// 相比旧版手写 FlowLayout（QWidget 里要自己实现 heightForWidth），
// QML 的 Flow 原生支持自动换行 + 内容高度自适应，代码量大幅减少。
//
// 布局要点：
// - 搜索栏与底部导航一样是**悬浮层**（z: 1，不占布局），网格铺满整页、
//   滚动时从它们下面穿过；搜索栏本身固定不滚动，随时可点
// - Flickable 自身零边距，滚动条贴窗口右边缘
// - 搜索栏让出的高度由 Flow 的 _headerGap 承担（首行不被胶囊压住）
// - 24px 内边距放进 Flow 的 padding 里
// - 底部预留 navContentGutter，避免最后一行被悬浮导航遮挡
// - 点空白处收起搜索胶囊（见 Flickable 里那个 MouseArea）
//
// 搜索过滤在 QML 侧完成（`_load_subjects` 不动）：条目列表本来就整份
// 传给 QML 了，本地按标题筛一遍是纯展示逻辑，不必往桥接层加接口。
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

    /// 搜索关键词（与顶部搜索胶囊的输入内容**同一条数据**，见下方 alias）
    property alias searchText: searchPill.text
    /// 去掉首尾空白后的关键词（真正参与匹配 / 提示文案的那一份）
    readonly property string query: root.searchText.trim()
    /// 小写化后的关键词（QML 侧匹配用；免去每次 `matches` 再转一次）
    readonly property string _q: root.query.toLowerCase()
    readonly property bool filtering: root._q.length > 0
    /// 命中条数（提示"找到 N 部"；过滤关闭时即总数）
    readonly property int matchCount: root.countMatches(root._q)

    /// 搜索是否"进行中"：已有关键词，或搜索框正开着（有焦点）。
    /// 供外部"返回"入口判断 —— 鼠标后侧键在海报墙时靠它决定是退出搜索还是放行。
    readonly property bool searchActive: root.filtering || searchPill.focused

    /// 退出搜索：清空关键词 + 收起搜索胶囊（回到整墙）。
    /// 清空会触发 onQueryChanged，顺带把滚动位置带回顶部。
    function clearSearch() {
        root.searchText = ""
        searchPill.collapse()
    }

    // ---- 顶部搜索栏（悬浮层）----
    //
    // **不占布局**：和底部导航一样是"浮在内容之上"的层（z: 1），海报网格
    // 整个铺满页面、滚动时从搜索胶囊**下面**穿过去。
    // 早期写成"占一条顶部留白"（Flickable 从搜索栏下面开始）时，
    // 顶部那条带子永远是空的、且卡片会在带子下沿被硬生生裁断，
    // 观感像一条隔断（实测反馈"那一长条白色要透明"）。
    // 首行的位置由 Flow 自己的顶部偏移留出（见下面的 _headerGap），
    // 所以静止时首行不会被胶囊压住，滚动时就正常从胶囊下面滑过。
    Item {
        id: headerBox
        z: 1
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Theme.pagePadding
        height: searchPill.height          // 含投影留白

        Row {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: Theme.spacingMd

            SearchPill {
                id: searchPill
                // 同一行右侧还挂着"找到 N 部"，故胶囊自身宽度固定（展开在槽内进行）
            }

            // 命中数量：只在**确实筛出了结果**时出现。
            // 0 条的情况交给网格里的空状态解释（那里能完整写出关键词），
            // 这里再报一次"找到 0 部"就啰嗦了。
            Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.filtering && root.matchCount > 0
                text: "找到 " + root.matchCount + " 部"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontMd
            }
        }
    }

    Flickable {
        id: flick
        // 铺满整页：搜索栏是悬浮层（见 headerBox），不再从顶边切走一条
        anchors.fill: parent
        clip: true
        contentWidth: width
        // 内容高度 = 顶部留白（含悬浮搜索栏的让位）+ 网格高 + 底部留白
        // （给悬浮导航让位）。
        // 注意：不能只靠 Flow 的 bottomPadding —— Flickable 用的是
        // contentHeight，而我们显式设置了它，Flow 的 padding 不会自动计入。
        contentHeight: Math.max(
            Theme.pagePadding + content._headerGap + content.implicitHeight
                + Theme.navContentGutter, height)
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.vertical: AppScrollBar {
            id: vbar
            policy: ScrollBar.AsNeeded
        }

        // 点空白处 → 搜索胶囊收起（有搜索内容时不收，见 SearchPill.collapse）。
        //
        // 位置很讲究：声明在 Flow **之前**（位于卡片下层），所以
        // 点卡片仍然走卡片自己的点击（进详情页），只有点在真正的空白处
        // 才会落到这里。整片内容区都盖住（含首行上方的留白），
        // 于是"点页面任意空白"都能收起，不需要给每个容器单独加。
        MouseArea {
            x: 0
            y: 0
            width: flick.contentWidth
            height: flick.contentHeight
            onClicked: searchPill.collapse()
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

            // 首行上方额外让出悬浮搜索栏的高度（它不占布局，见 headerBox 的说明）：
            // 静止时首行正好落在胶囊下方不被压住，滚动时卡片从胶囊下面穿过。
            readonly property int _headerGap: headerBox.height + Theme.spacingMd

            x: Theme.pagePadding + Math.max(0, Math.round((_avail - _rowW) / 2))
            y: Theme.pagePadding + _headerGap
            width: _avail
            spacing: _spacing

            Repeater {
                // 注意：model 始终是**全量**列表，过滤靠 delegate 的 visible 实现
                model: root.subjects

                delegate: PosterCard {
                    required property var modelData

                    // 搜索过滤：不匹配的卡片**隐藏**，而不是从 model 里剔除。
                    //
                    // 为什么不用"过滤后的列表当 model"：Repeater 的 model 一变，
                    // 全部卡片会被销毁重建 —— 每张都要重新解码封面、重建
                    // QQuickItem，于是**每敲一个字卡一次**（同类问题见 Main.qml 里
                    // "返回海报墙不调 library.reload()"的踩坑）。Flow 会自动跳过
                    // visible:false 的子项，隐藏即可达到"只剩匹配项"的效果，
                    // 且已解码的封面缓存全部保留、滚动位置也不跳。
                    visible: !root.filtering || root.matches(modelData)

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

        // 搜索无结果：媒体库**有**数据、只是没匹配上 —— 与上面的"还没有条目"
        // 是两件事，文案也要分开（否则用户会以为库被清空了）
        Column {
            anchors.centerIn: parent
            spacing: Theme.spacingSm
            visible: root.filtering && root.matchCount === 0
                     && root.subjects.length > 0

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "没有匹配「" + root.query + "」的动漫"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontLg
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "换个关键词试试"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontMd
            }
        }
    }

    // 关键词变化时回到网格顶部：过滤后内容变短，原来的滚动位置会落在
    // 末尾（Flickable 会把越界的 contentY 夹到最大值），看到的是"最后几部"
    // 而不是最相关的前几部。
    onQueryChanged: flick.contentY = 0

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }

    /// 当前滚动位置 / 可滚动上限（截图、自动化核对用）
    readonly property real scrollY: flick.contentY
    readonly property real scrollMax: Math.max(0, flick.contentHeight - flick.height)

    /// 条目是否匹配当前关键词（`q` 已小写化）。
    ///
    /// 四个字段都查：系列卡片（分组模式）的 title 就是系列名，而单条的
    /// title 是「中文名或原名」——用户可能记得的是另一个译名，因此
    /// name / nameCn / seriesName 一并纳入。
    function matches(item, q) {
        if (!item)
            return false
        if (q === undefined)
            q = root._q
        return str(item.title).indexOf(q) >= 0
            || str(item.name).indexOf(q) >= 0
            || str(item.nameCn).indexOf(q) >= 0
            || str(item.seriesName).indexOf(q) >= 0
    }

    /// 命中条数。空关键词 = 全部（不做逐条比较，直接返回总数）
    function countMatches(q) {
        if (!q || q.length === 0)
            return root.subjects.length
        var n = 0
        for (var i = 0; i < root.subjects.length; i++) {
            if (matches(root.subjects[i], q))
                n++
        }
        return n
    }

    /// 取字段的小写字符串（字段可能缺失 / 为 null，直接 .toLowerCase() 会抛）
    function str(v) {
        return v ? String(v).toLowerCase() : ""
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
