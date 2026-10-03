import QtQuick

// 「想看 / 看过 / 在看 / 搁置 / 抛弃」分段选择器（详情页海报下方）。
//
// 容器：浅灰圆角条 + 一圈极淡描边 + 4px 内边距；五格**平分容器内宽**
// （铺满，左右不留白，见下面的 width）。选中：**主题色实底 + 反色文字
// （加粗）**；悬停（未选中）：稳定的中性灰实底。
//
// **选中态的颜色反复过一次，这里记清楚最终口径**（2026-10-03）：
//   ① 最初按"选中颜色为主题色"做成主题色实底；
//   ② 之后为对齐 uiverse.io 的 radio-inputs 参考图，改成"白色胶囊浮在
//      灰条上、主题色只体现在文字上"；
//   ③ 现在按用户明确要求**改回①：背景就是主题色**（与 AppButton 的主按钮
//      同一套观感：`Theme.accent` 底 + `Theme.accentText` 反色字）。
// 参考图那套中性风到此为止，不要再按"白胶囊"改回去。
//
// **颜色全部用纯三元绑定、且不含任何 Behavior**（两条理由，都踩过）：
//   ① 速度：用户要求"跟展示图出现的时间一样快" —— 海报是立刻出现的，
//      选中态却要等一个 120ms 的 ColorAnimation 才到位，体感就是"慢半拍"。
//      去掉动画后，颜色与数据同一帧到位。
//   ② 闪灰：`Behavior on color` + 从 `"transparent"`（= rgba(0,0,0,0)，
//      **黑色**透明）插值到浅灰，会**先扫过一段深灰再变浅** ——
//      "悬停瞬间为灰色然后变浅"就是这个。项目里早写过解法
//      （`Theme.fade()`，见 Theme.qml 的长注释），本组件一度漏用。
//      这里更进一步：连动画都不留，这类插值问题从根上不存在。
//
// "快"一共有三个着力点，本文件只负责最后一个，另两个别漏掉：
//   ① 进详情页时本地没有状态快照 → 后端先查收藏同步缓存，**同步**给出
//      选中值（LibraryBridge.requestCollectType）；
//   ② 点击 → 乐观更新，点下去那一帧就亮（DetailPage.collectPending）；
//   ③ 颜色切换本身不做动画（本文件）。
//
// **为什么不用 QtQuick.Controls 的 RadioButton / ButtonGroup**：那两个
// 自带 indicator 与默认样式，要改到本设计的样子需要重写 background +
// indicator + contentItem 三处，代码量比直接用 Rectangle + MouseArea
// 还多；而这里每个"按钮"其实就几行属性（底、字、粗体）。
Item {
    id: root

    /// 可选状态：`[{ "value": 1, "text": "想看" }, ...]`
    property var options: []
    /// 当前选中的状态值（0 = 未选中 —— 该条目还没有收藏状态）
    property int currentValue: 0
    /// 是否可交互（写入进行中时置灰）
    property bool interactive: true

    /// 用户点了某一项（携带其 value）
    signal activated(int value)

    // 尺寸由**按钮宽度之和**直接算出，不依赖 layout.width。
    //
    // 现在宽度走一个**与布局无关的纯数据遍历**：把每个选项的文字宽度
    // 用 FontMetrics 量出来再加总。
    implicitWidth: Math.round(_itemsWidth) + padding * 2
    implicitHeight: itemHeight + padding * 2

    /// 单项的**内容宽度** = 最长那个选项的文字宽 + 左右留白。
    ///
    /// 只用于 `implicitWidth`（父级没给定宽时的建议宽度）。
    /// 实际显示时五格是**平分容器内宽**的（见 layout.width 与 delegate
    /// 的 width），铺满整条、左右不留白（用户实测要求："按钮左右两边还有
    /// 留白区域，占满留白区域"）—— 所以父级定宽时这两个值可能比它窄。
    ///
    /// 取"最长"而不是逐项自适应：五格等宽才好按（原版 CSS 的
    /// `flex: 1 1 auto` 也是等分的语义），宽度不一致会让整条看起来歪。
    ///
    /// **踩坑（量宽量出 NaN，还往控制台刷错误）**：Qt 6 的
    /// `FontMetrics.advanceWidth` 是**函数**（`advanceWidth(text)`），
    /// **没有 `text` 属性**。原先写的是
    ///     `_metrics.text = options[i].text; w = _metrics.advanceWidth`
    /// 于是每求值一次就抛一句
    ///     `Error: Cannot assign to non-existent property "text"`
    /// 到控制台（用户实测反馈里就有这一串），而 `advanceWidth` 没拿到文字
    /// 参数 → 返回 NaN → 整条控件的 implicitWidth 也成了 NaN。
    /// 现在文字**作为参数传进去**（实测 `"想看"` 在 fontSm/DemiBold 下 = 26px）。
    ///
    /// 注意别改回 `horizontalAdvance`：这个 Qt 构建里
    /// `QQuickFontMetrics` 没有那个方法（实测 `typeof === "undefined"`）。
    readonly property real _maxTextWidth: {
        var w = 0
        for (var i = 0; i < (options ? options.length : 0); i++) {
            w = Math.max(w, _metrics.advanceWidth(options[i].text))
        }
        return w
    }
    readonly property real _itemWidth: _maxTextWidth + Theme.spacingLg * 2
    readonly property real _itemsWidth: _itemWidth * (options ? options.length : 0)

    FontMetrics {
        id: _metrics
        font.pixelSize: Theme.fontSm
        // 量的是**选中态**的宽度（加粗比常规略宽），
        // 否则点击选中后文字会被按钮截掉一点点
        font.weight: Font.DemiBold
    }

    /// 容器内边距（原版 `padding: 0.25rem` ≈ 4px）
    readonly property int padding: 4
    /// 单个按钮高度（原版 `padding: .5rem 0` + 一行字 ≈ 30px）
    readonly property int itemHeight: 30
    /// 圆角（原版 0.5rem = 8px）
    readonly property int itemRadius: Theme.radiusMd

    // ---- 容器：浅灰圆角条 ----
    Rectangle {
        id: box
        anchors.fill: parent
        radius: itemRadius
        // 原版是 #EEE。写成主题派生色而不是字面量：暗色主题下 #EEE 会是
        // 一块刺眼的白斑。亮色用 surfaceAlt（浅灰，≈#EEE 的观感）。
        color: Theme.dark ? Theme.windowBg : Theme.surfaceAlt
        border.width: Theme.lineThin
        border.color: Theme.border
        // 不裁剪（没有 clip）：胶囊靠 `anchors.margins` 与容器内边距留白，
        // 不会碰到这条描边

        Row {
            id: layout
            // **铺满容器内宽**（左右各留 `padding`），五格再按
            // `layout.width / 5` 平分。
            //
            // **不要在这里取 `Math.max(..., _itemsWidth)`**（踩过一次）：
            // 那句话的意思是"容器太窄就别挤，按内容宽度来" —— 但实测
            // `_itemsWidth` = 275px（五格 × (最长文字 + 左右留白)）**比
            // 详情页给的内宽 232px 还宽**，于是 Row 比容器还宽、两端的
            // 胶囊直接露到灰条外面（每格 55px vs 应有的 46px）。
            // 内容宽度只该用来定 `implicitWidth`（父级不给定宽时的建议宽度，
            // 见 _itemWidth），父级一旦定宽就平分它给的宽度。
            //
            // 用 x/y 手算居中，**不用 anchors.centerIn**：
            // anchors 会让 Row 的宽度不再由内容撑开（踩坑见 implicitWidth
            // 处的说明），进而使 delegate 的宽度解析失败。
            width: parent.width - root.padding * 2
            x: Math.round((parent.width - width) / 2)
            y: Math.round((parent.height - height) / 2)
            spacing: 0

            Repeater {
                model: root.options

                delegate: Item {
                    required property var modelData
                    required property int index

                    readonly property bool selected:
                        root.currentValue === modelData.value
                    readonly property bool hovered: mouse.containsMouse

                    // 五格平分 Row 的宽度（= 平分容器内宽），**别写
                    // `btn.implicitWidth`** —— 那等于让按钮量自己，形成
                    // 自引用，实测求值为 0。
                    width: layout.width / root.options.length
                    height: root.itemHeight

                    Rectangle {
                        id: pill
                        anchors.fill: parent
                        anchors.margins: 1
                        radius: root.itemRadius
                        // 选中 = **主题色实底**（悬停时再提亮一档，与主按钮
                        // 同源）；未选中的常态与悬停都取**同一个灰**
                        // （`Theme.hoverFillStrong`）的不同 alpha —— 这正是
                        // `Theme.fade()` 的用法：RGB 恒定、只变 alpha，
                        // 所以悬停是"灰得**越来越实**"，不会扫过别的颜色。
                        //
                        // 常驻态用 `Theme.fade(...)` 而不是 `"transparent"`：
                        // 后者是黑色透明，任何逐分量插值都会先发黑（见文件头）。
                        color: parent.selected
                               ? (parent.hovered ? Theme.accentHover : Theme.accent)
                               : (parent.hovered
                                  ? Theme.hoverFillStrong
                                  : Theme.fade(Theme.hoverFillStrong))

                        Text {
                            id: label
                            anchors.centerIn: parent
                            text: modelData.text
                            // 选中：**反色文字**（主题色底上的对比色，
                            // 与 AppButton 的 primary 变体同一口径）
                            color: parent.parent.selected
                                   ? Theme.accentText
                                   : (parent.parent.hovered
                                      ? Theme.textPrimary
                                      : Theme.textSecondary)
                            font.pixelSize: Theme.fontSm
                            font.weight: parent.parent.selected
                                         ? Font.DemiBold : Font.Normal
                        }
                    }

                    MouseArea {
                        id: mouse
                        anchors.fill: parent
                        hoverEnabled: true
                        // 选中项本身仍可点（点它=无操作，由上层去重），
                        // 但写入进行中时全部禁用，避免并发点击
                        enabled: root.interactive
                        cursorShape: enabled ? Qt.PointingHandCursor
                                             : Qt.ArrowCursor
                        onClicked: root.activated(modelData.value)
                    }
                }
            }
        }
    }
}
