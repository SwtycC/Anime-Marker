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

    /// 「添加动漫」按钮被点击（由 Main.qml 打开 AddAnimeDialog）。
    ///
    /// 页面本身不弹窗、也不调后端：它只表达"用户想添加动漫"这一意图，
    /// 窗体的创建/生命周期与后端调用统一放在 Main.qml（与 UploadDialog
    /// 同一套做法）。
    signal addAnimeRequested()

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

    // ---- 标签 / 状态筛选（面板见 TagFilterPill.qml）----
    /// 每类选中的 tag：`{"分类": "TV", ...}`（缺省 = 该类看"全部"）
    property var tagSelection: ({})
    /// 状态筛选：`""`（全部）/ `"matched"`（已匹配）/ `"unmatched"`（未匹配）
    property string stateFilter: ""
    /// 生效中的筛选维度数（标签栏数 + 状态栏）
    readonly property int activeFacetCount: root.countFacets()
    /// 是否有"标签 / 状态"维度的筛选（不含关键词）
    readonly property bool hasFacetFilter: root.activeFacetCount > 0
    /// 筛选面板是否展开（供外部"返回"入口判断）
    readonly property bool filterOpen: filterPill.opened

    /// 过滤是否生效：关键词 / 标签 / 制作公司 / 状态**任一维度有筛选条件**即为真
    /// （真正匹配时这些维度之间是"且"，见 matches()）
    readonly property bool filtering: root._q.length > 0 || root.hasFacetFilter
    /// 命中条数（提示"找到 N 部"；过滤关闭时即总数）
    readonly property int matchCount: root.countMatches(root._q)

    /// 搜索是否"进行中"：已有关键词、搜索框正开着（有焦点），或筛选面板展开着。
    /// 供外部"返回"入口判断 —— 鼠标后侧键在海报墙时靠它决定是退出搜索/筛选
    /// 还是放行。
    readonly property bool searchActive: root.filtering || searchPill.focused
                                         || filterPill.opened

    /// 收起筛选面板（外部"返回"入口用，见 Main.qml）
    function closeFilterPanel() {
        filterPill.opened = false
    }

    /// 面板里的某项被点中：kind 决定写进哪份状态（详见 TagFilterPill.rows）
    function applyFilter(kind, title, value) {
        if (kind === "state") {
            // 再点一次已选中的 = 取消（等价于点"全部"）
            root.stateFilter = (root.stateFilter === value) ? "" : value
            return
        }
        // **整体换一份新对象**：QML 的 var 属性就地改不会触发变更通知，
        // 而 matchCount / Repeater 的 visible 都挂在它上面
        var sel = ({})
        for (var k in root.tagSelection) {
            if (k !== title && root.tagSelection[k])
                sel[k] = root.tagSelection[k]
        }
        if (value && value !== (root.tagSelection[title] || ""))
            sel[title] = value
        root.tagSelection = sel
    }

    /// 生效中的筛选维度数（标签栏 + 状态栏）
    function countFacets() {
        var n = root.stateFilter ? 1 : 0
        for (var k in root.tagSelection) {
            if (root.tagSelection[k])
                n++
        }
        return n
    }

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

            // 标签 / 状态筛选：收起时是个圆形箭头按钮，点开成面板 ——
            // 它在行内的槽位会变宽，于是**把右边的搜索胶囊顶向右**
            // （需求里的"搜索按钮右移"，实现见 TagFilterPill.qml）。
            TagFilterPill {
                id: filterPill
                objectName: "filterPill"      // 诊断/探针用

                rows: root.filterRows
                selection: root.tagSelection
                stateValue: root.stateFilter
                // 面板不能顶到悬浮导航上：页面高 - 上下留白 - 导航让位
                // - 顶部条(箭头行) - 面板下内边距与一点呼吸位
                contentMaxHeight: Math.max(120, flick.height - Theme.pagePadding * 2
                                           - Theme.navContentGutter
                                           - Theme.navButtonSize - Theme.lineThin
                                           - Theme.spacingMd)
                onPicked: function (kind, title, value) {
                    root.applyFilter(kind, title, value)
                }
            }

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

        // 点空白处 → 搜索胶囊收起（有搜索内容时不收，见 SearchPill.collapse）
        // + 筛选面板收起。面板在 headerBox 那一层（z 更高），点面板本身不会
        // 落到这里，只有点在面板以外的区域才会。
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
            onClicked: {
                searchPill.collapse()
                filterPill.opened = false
            }
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

        // 筛选无结果：媒体库**有**数据、只是没匹配上 —— 与上面的"还没有条目"
        // 是两件事，文案也要分开（否则用户会以为库被清空了）。
        // 关键词与标签/状态两种成因的提示也不同：只选了标签却说"换个关键词
        // 试试"会让人找错方向。
        Column {
            anchors.centerIn: parent
            spacing: Theme.spacingSm
            visible: root.filtering && root.matchCount === 0
                     && root.subjects.length > 0

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.query.length > 0
                      ? "没有匹配「" + root.query + "」的动漫"
                      : "没有符合筛选条件的动漫"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontLg
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.query.length > 0 ? "换个关键词试试"
                                            : "换个标签或状态试试"
                color: Theme.textTertiary
                font.pixelSize: Theme.fontMd
            }
        }
    }

    // ---- 「返回顶部」悬浮按钮 ----
    //
    // **必须是 flick 的兄弟项**（不能放进 Flickable 的 content 里）：
    // 放进去就成了滚动内容的一部分，往下滚时它自己也会被滚走，
    // 也就谈不上"固定在右下角"了。这里与 headerBox 同一层，都"浮"在内容之上。
    //
    // 位置：底部**导航胶囊的右上方**——
    //   水平：贴右边缘（留 pagePadding，并避让滚动条宽度）
    //   垂直：导航胶囊顶边再往上一点
    //   （导航胶囊顶边 = 底边往上 navBottomMargin + navPillHeight，
    //     常量与 Main.qml 里 NavBar 的定位保持同一套）
    //
    // 显隐：只有真的向下滚过一段才出现。
    // 判据用 `contentY > 阈值` 而不是 `contentHeight > height`：
    // 后者在"内容只比视口高一点点"时也为真，会出现"刚进页面、没滚动
    // 就顶着个按钮"的情况；前者才真正表达"用户已经滚下去了"。
    // ---- 「添加动漫」悬浮按钮（**下方**）----
    //
    // 位置：与「返回顶部」上下排列，本按钮在**下**、返回顶部在上，
    // 两者右边缘对齐（都贴右下角）。
    //
    // **间距如何保证"悬停一个不影响另一个"**：
    //   ① 两个按钮之间留 4px：返回顶部锚在本按钮**上方**（见 backToTop）；
    //   ② 关键是 `FloatingActionButton` 的命中区（MouseArea）
    //      **只覆盖本体矩形、不含外圈那 4px 淡描边** —— 描边最容易互相
    //      侵入，把命中区圈到描边上就会出现"鼠标明明在 A 上，B 也判定为
    //      悬停"。这条在组件里已写死，此处配合间距即可。
    //   ③ 悬停时本体只向**左**扩张（右边缘锚在页面右侧），不会向上/向下
    //      压到另一个按钮。
    FloatingActionButton {
        id: addAnime
        objectName: "addAnimeButton"       // 诊断/探针用
        z: 2
        icon: "plus"
        label: "添加动漫"

        anchors.right: parent.right
        anchors.rightMargin: Theme.pagePadding
                            + (vbar.visible ? vbar.width : 0)
        // 底边与原先一样：导航胶囊顶边再往上一点
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Theme.navBottomMargin + Theme.navPillHeight
                              + Theme.spacingLg

        onClicked: root.addAnimeRequested()
    }

    BackToTopButton {
        id: backToTop
        objectName: "backToTopButton"      // 诊断/探针用
        z: 2                               // 高于 headerBox(1)，与导航同层
        // 出现阈值：**只要往下滚过一点点就出现**。
        //
        // 原先是 `> pagePadding * 2`（48px），实测反馈"出现得有点慢" ——
        // 用户滚过一两行卡片才见到按钮，感觉像"没及时响应"。
        // 现在降到 16px：轻微下滑即出现（仍不是 0，避免在顶部边缘时
        // 因惯性回弹反复闪烁）。
        visible: flick.contentY > 16
        opacity: visible ? 1 : 0
        // 淡入用 durFast（更"跟手"）；淡出仍走 durNormal，避免刚回顶就
        // 突兀消失。用两条 Behavior 无法区分方向，故统一取较快的 durFast。
        Behavior on opacity {
            NumberAnimation { duration: Theme.durFast }
        }

        anchors.right: parent.right
        anchors.rightMargin: Theme.pagePadding
                            + (vbar.visible ? vbar.width : 0)
        // **垂直方向锚在「添加动漫」上方**（上下排列，本按钮在上）。
        //
        // 间距：两圆之间留 **14px**（历次调整：4 → 6 → 14）。
        //
        // 这次跨过了"两侧描边不再交叠"的临界点：本体间距 > 8px 时，
        // 两圈各 4px 的淡描边之间也会空出一段，不再连成一片。
        // 于是在这个值上，`bottomMargin` 变成了**正数**，不再被 Math.max 兜底
        // —— 也就是说 Math.max 其实只是防止"填负数导致重叠"的保护，
        // 计算式本身始终是 `目标间距 − 两侧留白`。
        //
        // `anchors.bottom: addAnime.top` 锚的是**组件根项的顶边**，而根项比
        // 按钮本体每侧各多 `haloWidth`(4px，给那圈淡描边留的空间，
        // 见 FloatingActionButton 的说明)，故实际看到的间距 =
        //     bottomMargin + 4 + 4
        // 这里把两边减掉，保证肉眼看到的是 14px。
        anchors.bottom: addAnime.top
        anchors.bottomMargin: Math.max(0, 14 - addAnime.haloWidth
                                          - backToTop.haloWidth)

        // 只发出意图，滚动行为归本页处理（见下方 scrollTopAnim）。
        // 需要 scrollTopAnim / flick 都在作用域内，故用箭头函数式的直接调用。
        onClicked: scrollTopAnim.restart()
    }

    /// 平滑滚回顶部。
    ///
    /// **不用 `flick.contentY = 0`**：那是瞬间跳变，在长列表里会让人一下
    /// 失去位置感（想看的是"弹回去"的过程）。改为驱动 contentY 的动画。
    NumberAnimation {
        id: scrollTopAnim
        target: flick
        property: "contentY"
        to: 0
        duration: Theme.durSlow * 4
        easing.type: Easing.OutCubic
    }

    // 筛选项变化时回到网格顶部：过滤后内容变短，原来的滚动位置会落在
    // 末尾（Flickable 会把越界的 contentY 夹到最大值），看到的是"最后几部"
    // 而不是最前面的几部。
    onQueryChanged: flick.contentY = 0
    onTagSelectionChanged: flick.contentY = 0
    onStateFilterChanged: flick.contentY = 0

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }

    /// 当前滚动位置 / 可滚动上限（截图、自动化核对用）
    readonly property real scrollY: flick.contentY
    readonly property real scrollMax: Math.max(0, flick.contentHeight - flick.height)

    /// 条目是否匹配当前筛选（关键词 + 标签 + 状态，三者之间是"且"）。
    function matches(item, q) {
        if (!item)
            return false
        if (q === undefined)
            q = root._q
        return root.textMatches(item, q) && root.facetMatches(item)
    }

    /// 关键词是否命中（`q` 已小写化）。
    ///
    /// 四个字段都查：系列卡片（分组模式）的 title 就是系列名，而单条的
    /// title 是「中文名或原名」——用户可能记得的是另一个译名，因此
    /// name / nameCn / seriesName 一并纳入。
    ///
    /// **aliases 也纳入**：Bangumi 的每个条目都带一串别名（俗称、日文简称、
    /// 英文译名），用户想搜的往往是俗称而不是正式全名 —— 例如条目正式名是
    /// 「我们仍未知道那天所看见的花的名字。」，而用户只记得「未闻花名」。
    /// 不搜别名的话，这类条目等于搜不到。字段由 library._subject_to_dict
    /// 提供（空格拼接，见那里的说明）。
    function textMatches(item, q) {
        return str(item.title).indexOf(q) >= 0
            || str(item.name).indexOf(q) >= 0
            || str(item.nameCn).indexOf(q) >= 0
            || str(item.seriesName).indexOf(q) >= 0
            || str(item.aliases).indexOf(q) >= 0
    }

    /// 标签 / 状态 / 制作公司筛选是否命中（不含关键词那一维）。
    ///
    /// 多个筛选栏之间是"且"：选了「类型 = 奇幻」+「地区 = 日本」，
    /// 要求条目两个 tag 都带（与 Bangumi 站内筛选的口径一致）。
    function facetMatches(item) {
        if (!root.hasFacetFilter)
            return true
        // 状态：已匹配 = auto / manual（都关联到了 Bangumi 条目），
        // 未匹配 = pending（扫描时没匹配上，卡片上有「待匹配」角标）
        if (root.stateFilter === "matched" && item.matchState === "pending")
            return false
        if (root.stateFilter === "unmatched" && item.matchState !== "pending")
            return false
        // 收藏状态（「收藏状态」栏）：比的是收藏状态本身，不是 tag。
        // 取值 "1"~"5" = 具体状态，"0" = 未标记（见 makeCollectRow）。
        // **这一栏不进 filterCatalogue**（它是固定项、不来自 tag 统计），
        // 所以不能靠下面遍历目录的那段，得单独判一次。
        //
        // **状态从 `root.collectTypes` 取，不读 `item.collectType`**：
        // 那张墙的条目列表是长生命周期的缓存，收藏状态变了它不一定重取
        // （重取要重建全部卡片），读它会出现"详情页已经是「看过」、
        // 筛选里还算未标记"（用户实测，要重扫才更新）。映射取不到时
        // 才回落到条目自己的字段。
        var wantCollect = root.tagSelection[root.collectRowTitle] || ""
        if (wantCollect) {
            var cur = root.collectTypes[String(item.id)]
            if (cur === undefined)
                cur = item.collectType || 0
            if (String(cur) !== wantCollect)
                return false
        }
        // 按键走目录（而不是走 selection 的键）：每栏的取值来源不同 ——
        // 制作公司栏比的是 subjects 的 studio 字段，其余栏比的是 tag。
        //
        // **「其他」栏必须一起遍历**（踩坑）：`buildCatalogue()` 返回的是
        // `{rows: [...], other: {...}}`，"其他"**不在 rows 里**、被单独放在
        // other 字段。早期只遍历 rows，于是点「其他」栏里的任何标签都
        // **完全不过滤**（面板上选中态变了、列表却纹丝不动）✗。
        // 这里把 other 拼到末尾一起处理。
        var cat = root.filterCatalogue
        var rows = cat.rows.slice()
        if (cat.other && cat.other.tags && cat.other.tags.length > 0)
            rows.push(cat.other)
        for (var i = 0; i < rows.length; i++) {
            var want = root.tagSelection[rows[i].title]
            if (!want)
                continue
            var values = rows[i].source === "studio" ? root.itemStudios(item)
                                                     : root.itemTags(item)
            if (values.indexOf(want) < 0)
                return false
        }
        return true
    }

    /// 命中条数。没有任何筛选时直接返回总数（不做逐条比较）
    function countMatches(q) {
        if ((!q || q.length === 0) && !root.hasFacetFilter)
            return root.subjects.length
        var n = 0
        for (var i = 0; i < root.subjects.length; i++) {
            if (matches(root.subjects[i], q))
                n++
        }
        return n
    }

    // ---- 标签数据的取用 ----
    /// 条目 id → tag 名列表。由 Python 侧**一次查全量**后传入
    /// （LibraryBridge.tagsBySubject，键是字符串形式的 subject_id）——
    /// 若逐条查库，海报墙每次过滤都要跑 N 条 SQL。
    readonly property var tagsMap: typeof library !== "undefined" && library
                                   && library.tagsBySubject ? library.tagsBySubject : ({})

    /// tag 的同义写法 → 规范写法（合并成同一个筛选项）。
    ///
    /// 只作用于**海报墙的筛选**：库里怎么存的、详情页怎么显示的都不变。
    /// 归并的是 Bangumi 上指同一件事的不同俗称 —— 「轻小说改」「轻改」与
    /// 「小说改」是同一类来源，「漫改」=「漫画改」，「GAL改」=「游戏改」，
    /// 各列一个筛选项既占地方、又得逐个点才筛得全。
    /// 表外的写法原样保留（照旧落进"其他"栏）。
    readonly property var _tagSynonyms: ({
        "轻小说改": "小说改",
        "轻小说改编": "小说改",
        "轻改": "小说改",
        "小说改编": "小说改",
        "漫改": "漫画改",
        "漫画改编": "漫画改",
        "gal改": "游戏改",
        "galgame改": "游戏改",
        "游戏改编": "游戏改",
        "动画改编": "动画改",
        "影视改编": "影视改",
        "原创动画": "原创"
    })

    /// tag → 规范写法。大小写不敏感（`GAL改` / `gal改` 都算同一个）
    function canonicalTag(name) {
        var s = String(name)
        var hit = root._tagSynonyms[s] || root._tagSynonyms[s.toLowerCase()]
        return hit ? hit : s
    }

    /// 单个条目的 tag（tagsMap 的键是字符串形式的 subject_id）。
    /// **同义写法在这里就归一**并去重：目录计数与筛选匹配都拿归一后的名字，
    /// 于是「轻小说改」的条目点「小说改」也筛得到。
    function tagsOf(id) {
        var m = root.tagsMap
        var v = m ? m[String(id)] : undefined
        if (!v)
            return []
        var out = []
        for (var i = 0; i < v.length; i++) {
            var t = root.canonicalTag(v[i])
            if (out.indexOf(t) < 0)
                out.push(t)
        }
        return out
    }

    /// 一张卡片（单条或系列）的 tag 列表。
    ///
    /// 系列卡片取**各季的并集**：一张卡代表整部作品，任一季度带这个 tag
    /// 就该能被筛出来（同 _build_groups 里合并 aliases 的理由）。
    function itemTags(item) {
        if (!item)
            return []
        if (item.isGroup && item.childIds) {
            var out = []
            for (var i = 0; i < item.childIds.length; i++) {
                var sub = root.tagsOf(item.childIds[i])
                for (var j = 0; j < sub.length; j++) {
                    if (out.indexOf(sub[j]) < 0)
                        out.push(sub[j])
                }
            }
            return out
        }
        return root.tagsOf(item.id)
    }

    /// 一个条目/系列的**制作公司**列表。
    ///
    /// 数据来自 `subjects` 的 studio 字段（= Bangumi infobox 的「动画制作」，
    /// 扫描 / 手动匹配 / 进详情页时写入，见 bangumi_api.extract_studio）。
    /// 合作署名在 Python 侧已用 ` / ` 连接，这里拆开 —— 按其中任一家都能筛到。
    /// 分组模式下 Python 已把各季的公司合并进系列卡片（见 _build_groups）。
    function itemStudios(item) {
        if (!item || !item.studio)
            return []
        var parts = String(item.studio).split("/")
        var out = []
        for (var i = 0; i < parts.length; i++) {
            var s = parts[i].trim()
            if (s)
                out.push(s)
        }
        return out
    }

    // ---- 筛选面板的分类目录 ----
    //
    // 分类词表照参考图（分类 / 来源 / 类型 / 地区 / 受众），另加一栏
    // **制作公司**（取自 infobox 的「动画制作」，不是 tag）。三个关键取舍：
    //
    // 1. **只收录库里实际出现过的值**：面板里每个 chip 点下去都筛得出结果，
    //    空分类整行不显示 —— 词表里那些本库没有的（WEB、OVA、耽美…）
    //    列出来只会让人点到 0 条结果。
    // 2. **词表外的 tag 统一进"其他"栏**（声优、作品名、"异世界"这种站内
    //    俗称…），按出现次数降序，常用的排前面。
    //    **制作公司不在其中**：公司 tag 在取数时就被摘掉了（见
    //    LibraryBridge._load_tags_map），它们在"制作公司"栏单列。
    // 3. 制作公司栏排在最后（"其他"之前）：它是"作品是谁做的"，与
    //    分类/来源/类型那几栏的"作品是什么"不是一回事。
    //
    // 分类栏里**不放"其他"**（参考图里有）：本实现在末尾单列一栏"其他"
    // 收纳词表外的 tag，两者同名会让人分不清是哪一个。
    readonly property var _tagVocabulary: [
        { "title": "分类", "tags": ["TV", "WEB", "OVA", "剧场版", "动态漫画"] },
        { "title": "来源", "tags": ["原创", "漫画改", "游戏改", "小说改", "动画改", "影视改"] },
        { "title": "类型", "tags": ["科幻", "喜剧", "同人", "百合", "校园", "惊悚", "后宫",
                                  "机战", "悬疑", "恋爱", "奇幻", "推理", "运动", "耽美",
                                  "音乐", "战斗", "冒险", "萌系", "穿越", "玄幻", "乙女",
                                  "恐怖", "历史", "日常", "剧情", "武侠", "短片集",
                                  "美食", "职场"] },
        { "title": "地区", "tags": ["日本", "欧美", "中国", "美国", "韩国", "法国", "英国",
                                  "中国香港", "俄罗斯", "苏联", "捷克", "中国台湾",
                                  "马来西亚"] },
        { "title": "受众", "tags": ["BL", "GL", "子供向", "女性向", "少女向", "少年向",
                                  "青年向", "无cp"] }
    ]

    /// 面板行模型（TagFilterPill.rows）：状态栏 + 各分类栏 + 其他栏。
    /// 顺序即面板里的显示顺序，"其他"栏放最后（它最长，别把别的挤下去）。
    readonly property var filterRows: root.buildFilterRows()

    /// 分类目录：`{rows: [{title, tags: [...]}], other: {title, tags: [...]}}`
    readonly property var filterCatalogue: root.buildCatalogue()

    /// 全库「作品名集合」：用于把"其他"栏里的作品名/别名 tag 剔掉。
    ///
    /// **为什么要剔（实测）**：Bangumi 的 tag 里混着大量**作品名与角色/声优名**
    /// （截图里的「无职转生」「CLANNAD」「为美好的世界献上祝福」「泽野弘之」
    /// 「鬼灭之刃」…）。它们是别的作品的名字，对本作毫无分类意义 ——
    /// 点「无职转生」只会筛出"恰好被打过这个 tag 的条目"，通常就是那一部
    /// 自己，纯属噪声，还把真正有意义的题材 tag 挤到下面去。
    ///
    /// 判据：tag 名与**本库任一条目的**正式名（title/name/nameCn/seriesName）
    /// 或任一别名（大小写、空格无关）完全相同 → 视为作品名，剔除。
    /// 只做**完全匹配**，不做包含匹配 —— 「轻音」这类既是作品名简称、
    /// 又确实是题材俗称的词，用包含匹配会误杀。
    ///
    /// 缓存在 readonly property 里：`buildCatalogue()` 会随 subjects 变化
    /// 反复重算，而这个集合要遍历全库（O(N×M)），不缓存会明显拖慢面板。
    readonly property var _workNames: {
        var set = ({})
        var add = function (v) {
            var s = root.str(v).replace(/\s+/g, "")
            if (s)
                set[s] = true
        }
        for (var i = 0; i < root.subjects.length; i++) {
            var it = root.subjects[i]
            if (!it)
                continue
            add(it.title)
            add(it.name)
            add(it.nameCn)
            add(it.seriesName)
            // aliases 是空格拼接的一串，逐个拆开
            var al = root.str(it.aliases).split(" ")
            for (var j = 0; j < al.length; j++)
                add(al[j])
        }
        return set
    }

    /// 该 tag 是否是"作品名 / 别名"（→ 从"其他"栏剔除）
    function isWorkNameTag(name) {
        if (!name)
            return false
        var key = root.str(name).replace(/\s+/g, "")
        return key !== "" && root._workNames[key] === true
    }

    function buildFilterRows() {
        var rows = [{
            "title": "状态",
            "kind": "state",
            "chips": [{ "label": "全部", "value": "" },
                      { "label": "已匹配", "value": "matched" },
                      { "label": "未匹配", "value": "unmatched" }]
        }]
        var cat = root.filterCatalogue
        var collectRow = root.makeCollectRow()
        var placed = false
        for (var i = 0; i < cat.rows.length; i++) {
            rows.push(root.makeTagRow(cat.rows[i]))
            // 收藏状态栏：**紧跟「类型」之后**（用户指定的位置）
            if (cat.rows[i].title === root._collectRowAfter) {
                rows.push(collectRow)
                placed = true
            }
        }
        // 兜底：库里连「类型」栏都没有（该分类一个值都没出现过时整行不显示）
        // → 放在最后一个分类栏之后、其他 / 制作公司之前
        if (!placed)
            rows.push(collectRow)
        if (cat.other.tags.length > 0)
            rows.push(root.makeTagRow(cat.other))
        return rows
    }

    /// 收藏状态栏的标题。
    ///
    /// **为什么不叫「状态」**：面板第一行已经叫「状态」，含义是**匹配状态**
    /// （已匹配 / 未匹配）—— 这一栏筛的是 Bangumi 的收藏状态（想看/看过/…），
    /// 两者不是一回事，重名会让人分不清点的是哪一个。
    /// 这个标题同时是 `tagSelection` 的键（选中值存那儿）与 `facetMatches`
    /// 的依据，所以只在这里定义一次。
    readonly property string collectRowTitle: "收藏状态"
    /// 收藏状态栏插在哪个分类栏之后（见 buildFilterRows）
    readonly property string _collectRowAfter: "类型"

    /// 收藏状态栏：全部 + 五个状态 + 未标记。
    ///
    /// **值一律用字符串**（含未标记的 `"0"`）：`applyFilter` 用 `if (value)`
    /// 判断"选中了没"，数字 0 会被当成"没选"，那一项就永远点不中；
    /// 字符串 `"0"` 在 JS 里是真值，正好绕开。
    ///
    /// 「未标记」= `collectType == 0`（本地没有收藏状态：没查过、或确实没收藏）。
    /// 这两种情况界面上区分不了，对"筛出还没标过的番"这个用途也没必要区分。
    function makeCollectRow() {
        var chips = [{ "label": "全部", "value": "" }]
        var opts = root.collectOptions
        for (var i = 0; i < opts.length; i++) {
            chips.push({ "label": String(opts[i].text),
                         "value": String(opts[i].value) })
        }
        chips.push({ "label": "未标记", "value": "0" })
        return { "title": root.collectRowTitle, "kind": "collect", "chips": chips }
    }

    /// 收藏状态的可选项（想看/看过/…），由后端给（见 LibraryBridge.collectOptions）
    readonly property var collectOptions: typeof library !== "undefined"
                                          && library && library.collectOptions
                                          ? library.collectOptions : []

    /// `条目主键 → 收藏状态`，供「收藏状态」栏筛选（见 facetMatches）。
    /// 单独一份、挂在 collectTypeChanged 上：收藏状态一变就更新，
    /// 不用等整墙重取（那会重建全部卡片）。
    readonly property var collectTypes: typeof library !== "undefined"
                                        && library && library.collectTypes
                                        ? library.collectTypes : ({})

    /// 一个筛选栏 → 面板行（首项固定是"全部"= 清空本栏）。
    /// `cat.source` 决定面板/匹配按哪个字段取数（"tag" / "studio"）。
    function makeTagRow(cat) {
        var chips = [{ "label": "全部", "value": "" }]
        for (var i = 0; i < cat.tags.length; i++)
            chips.push({ "label": cat.tags[i], "value": cat.tags[i] })
        return { "title": cat.title, "kind": cat.source || "tag", "chips": chips }
    }

    function buildCatalogue() {
        // 先数一遍每个 tag 覆盖几张卡片（既是"本库有没有"的判据，也是
        // "其他"栏的排序依据）
        var counts = ({})
        for (var i = 0; i < root.subjects.length; i++) {
            var tags = root.itemTags(root.subjects[i])
            for (var j = 0; j < tags.length; j++) {
                var t = tags[j]
                if (root.isTimeTag(t))
                    continue
                counts[t] = (counts[t] || 0) + 1
            }
        }

        var rows = []
        var used = ({})
        for (var v = 0; v < root._tagVocabulary.length; v++) {
            var vocab = root._tagVocabulary[v]
            var hits = []
            for (var k = 0; k < vocab.tags.length; k++) {
                var name = vocab.tags[k]
                if (counts[name] !== undefined) {
                    hits.push(name)
                    used[name] = true
                }
            }
            if (hits.length > 0)
                rows.push({ "title": vocab.title, "tags": hits })
        }

        var rest = []
        for (var name2 in counts) {
            if (used[name2])
                continue
            // 作品名 / 别名不进"其他"栏（见 _workNames 的说明）。
            // 只在这里剔，**不影响 tag 数据本身** —— 详情页仍会显示它们，
            // 想按作品名找番请用上方的搜索框（那个走 textMatches）。
            if (root.isWorkNameTag(name2))
                continue
            rest.push(name2)
        }
        // 热度降序；同热度按字典序，保证顺序稳定（否则每次重建面板 chip 乱跳）
        rest.sort(function (a, b) {
            return counts[b] - counts[a] || (a < b ? -1 : (a > b ? 1 : 0))
        })

        // 制作公司栏：数据源与上面几栏不同（studio 字段，不是 tag），
        // 用 `source` 标出来。一部都没有时不出这一栏。
        var studioCounts = ({})
        for (var s = 0; s < root.subjects.length; s++) {
            var studios = root.itemStudios(root.subjects[s])
            for (var m = 0; m < studios.length; m++)
                studioCounts[studios[m]] = (studioCounts[studios[m]] || 0) + 1
        }
        var studioNames = []
        for (var studio in studioCounts)
            studioNames.push(studio)
        studioNames.sort(function (a, b) {
            return studioCounts[b] - studioCounts[a] || (a < b ? -1 : (a > b ? 1 : 0))
        })
        if (studioNames.length > 0) {
            rows.push({ "title": "制作公司", "tags": studioNames,
                        "source": "studio" })
        }

        return { "rows": rows, "other": { "title": "其他", "tags": rest } }
    }

    /// 时间类 tag（"2025"、"2025年4月"）：**不进筛选面板**。
    /// 它们几乎逐条唯一（每部番的年份都不同），放进去既占地方又筛不出什么。
    function isTimeTag(name) {
        return /^\d{4}(年\d{1,2}月?)?$/.test(name)
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

    // 数据变化时**有意什么都不做**（见下）。
    //
    // 这里原先写的是 `flick.contentY = 0`（"数据变化时刷新，滚动位置回到
    // 顶部"），而那是个 bug（实测反馈："在动漫详情页单条目重新扫描后，
    // 回退到海报墙会回到顶部"）：
    // `subjectsChanged` 不只是"列表内容真的变了"，它也是**单条目重扫的
    // 收尾** —— QmlApp._on_scan_finished 第一件事就是 `library.reload()`。
    // 用户点「重新扫描」时人在详情页，海报墙在后台被这条信号顶回顶部，
    // 返回时看到的就是"跳到了顶上"。
    // 而重扫只改集数，卡片**顺序和数量都没变**，没有任何理由移动视口。
    //
    // **不需要"先记住位置、重建后再贴回去"那一套**（A/B 对照实测过：
    // 把那套机制关掉，位置照样保住）：Repeater 重建时 Flickable 的
    // contentY 本来就会原样保留 —— 位置丢失**完全**来自上面那句显式归零。
    // 内容真的变短时（删掉条目），Flickable 自己会把越界的 contentY 夹进
    // 合法范围，那也是对的。
    //
    // 真正需要主动归零的只有**筛选变化**（见下面的 onQueryChanged /
    // onTagSelectionChanged / onStateFilterChanged）—— 那是用户明确要求
    // "看最前面几部"。
    Connections {
        target: typeof library !== "undefined" && library ? library : null
        function onSubjectsChanged() {
            // 有意留空：理由见上。留一个空实现是为了让"这里接过
            // subjectsChanged、但故意不做事"在代码里可见，
            // 免得以后有人以为漏了而把 `contentY = 0` 补回来。
        }
    }
}
