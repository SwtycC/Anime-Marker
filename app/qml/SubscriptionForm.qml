import QtQuick

// 「新增 / 编辑订阅」表单（从 SubscriptionPage 抽出来的可复用组件）。
//
// **为什么要抽出来**（实测需求）：表单原先写死在页面顶部，点某条订阅的
// 「编辑」时，表单跑到**页面最上方** —— 用户刚点的是列表里第 N 条，
// 视线却要跳到顶部，而且看不出"我正在改哪一条"。
// 现在这个组件能被**放在任意位置**：卡片下方内联展开（编辑）或用一次
// 在页面顶部（新增）。
//
// 表单本身不碰数据：填好后 emit `submitted(name, url, rule)`，
// 由调用方决定是新增还是更新（见 SubscriptionPage.saveForm）。
Item {
    id: root

    /// 标题文案（"添加订阅" / "编辑订阅"）
    property string title: "添加订阅"
    property string nameText: ""
    property string urlText: ""
    property string ruleValue: "new_only"

    /// 用户点「保存」；参数为清洗后的值
    signal submitted(string name, string url, string rule)
    /// 用户点「取消」
    signal cancelled()

    // **必须显式绑 `height`，不能只给 `implicitHeight`**（踩坑，实测
    // "点添加/编辑都不弹出表单"）：
    //
    // 普通 `Item` 的 `height` 默认是 **0**，`implicitHeight` 只是"建议高度"
    // —— 只有 Layout / 某些 Control 会去读它。本组件是被放进一个**普通
    // Column** 里的（不是 ColumnLayout），Column 按子项的 `height` 排布，
    // 而原实现只写了 `implicitHeight: box.implicitHeight`（我没给 height），
    // 于是整个表单的 height 一直是 0：
    //   - 内部 `box` 虽然自己有高度，被父项 0 高度裁掉 → **完全看不见**；
    //   - Column 按 0 高排布 → 下方的卡片位置不变，看起来"什么都没发生"。
    //
    // 抽组件之前能用，是因为那时的根项直接是 `Rectangle` —— Rectangle
    // 默认就把 `height` 绑到 `implicitHeight` 上，所以不显式写也对。
    // 换成 `Item` 当根项时这个默认行为就没了。
    implicitHeight: box.height
    height: implicitHeight

    Rectangle {
        id: box
        width: parent.width
        height: formColumn.implicitHeight + Theme.spacingLg * 2
        color: Theme.surfaceAlt
        border.width: Theme.lineThin
        border.color: Theme.border
        radius: Theme.radiusMd

        Column {
            id: formColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Theme.spacingLg
            spacing: Theme.spacingSm

            Text {
                text: root.title
                color: Theme.textPrimary
                font.pixelSize: Theme.fontMd
                font.weight: Font.DemiBold
            }

            //
            // 放在名称栏，且**只此一个** —— 两个按钮做同一件
            //   事只会让人犹豫点哪个。
            //
            // 结论：按钮跟着**产物**走，不跟着**输入**走。
            FormRow {
                width: parent.width
                label: "名称"
                Row {
                    width: parent.width
                    spacing: Theme.spacingSm

                    AppTextField {
                        id: nameField
                        objectName: "subFormNameField"
                        width: parent.width - fetchNameBtn.width - Theme.spacingSm
                        text: root.nameText
                        placeholder: "留空则用域名（如 dmhy）"
                    }

                    // 「获取名称」：抓一次「订阅地址」那个 RSS，
                    // 从条目标题推断番名并填进左边的名称框，
                    // 用户再手动改成想要的名字。
                    // 按钮读的是 urlField，所以地址为空时后端会提示
                    // "请先填写订阅地址"（见 suggestSubjectName）。
                    AppButton {
                        id: fetchNameBtn
                        objectName: "subFormFetchNameBtn"
                        text: "获取名称"
                        onClicked: {
                            if (typeof rss === "undefined" || !rss)
                                return
                            rss.suggestSubjectName(urlField.text)
                        }
                    }
                }
            }

            FormRow {
                width: parent.width
                label: "订阅地址"
                AppTextField {
                    id: urlField
                    objectName: "subFormUrlField"
                    width: parent.width
                    text: root.urlText
                    placeholder: "https://example.com/rss.xml"
                }
            }

            FormRow {
                width: parent.width
                label: "判新规则"
                // 两个规则的差别用 `?` 弹窗解释（括号文案已按要求去掉，
                // 但差别必须让人看得到 —— 实测反馈"本地没有的和补齐不是
                // 一个意思吗"，说明光看名称分辨不出来）
                // 说明用 **HTML 富文本**（不是纯文本！）——
                // 踩坑：FormRow 的 hint 弹窗原先是一个普通 `Text`，
                // 默认 `textFormat: Text.AutoText` **不解析 Markdown**，
                // 于是早期写的 `**一层都不查**` 被原样显示成带星号的
                // 文字（实测截图："文本加粗效果没实现"）。
                // 纯文本里没法表达加粗，改用 RichText + `<b>`。
                // 换行统一用 `<br>`（RichText 下也能用 \n，但混排时
                // `<br>` 更不容易被忽略）。
                //
                // 内容更新（实测需求）：查重**只有两层**了 ——
                // 原第 ③ 层"本程序下载过"已去掉，见 rss_matcher._dedup。
                hint: "<p>「只下新集」和「全部下载」有什么区别？</p>"
                      + "<p><b>只下新集</b>：检查两层，任一层认为「已经有了」"
                      + "就跳过 ——<br>"
                      + "&nbsp;&nbsp;① 本地媒体库里已有这一集（硬盘上有文件）<br>"
                      + "&nbsp;&nbsp;② qBittorrent 里已有同名任务<br>"
                      + "&nbsp;&nbsp;适合日常追番。</p>"
                      + "<p><b>全部下载</b>：上面两层<b>一层都不查</b> —— "
                      + "把订阅源里的<br>"
                      + "&nbsp;&nbsp;内容（经过「下载器」的标题过滤后）"
                      + "全部下发一遍。<br>"
                      + "&nbsp;&nbsp;适合「想重新拉一份完整资源」，"
                      + "会重复下载已有的集。</p>"
                // 用分段选择器而非下拉框：只有两个固定选项，
                // 且能让用户一眼看到另一个选项是什么（详见 SegmentedControl）
                SegmentedControl {
                    id: ruleBox
                    objectName: "subFormRuleBox"
                    width: parent.width
                    // 文案用**纯名称**（实测要求"去掉后面的括号及其中的字"）。
                    // 两者的差别写在 `?` 说明里，不塞进按钮 ——
                    // 括号里的解释会把按钮撑得很宽，也不好看。
                    options: [
                        { "label": "只下新集", "value": "new_only" },
                        { "label": "全部下载", "value": "all" }
                    ]
                    currentValue: root.ruleValue
                    // **必须接 selected 信号**（踩坑，实测反馈"无法点击
                    // 全部下载"）：`currentValue` 是**单向**的普通属性 ——
                    // 控件内部点击只会 emit `selected(value)`，**不会自己改
                    // currentValue**。早期只写了初始值而没接信号，于是点
                    // 「全部下载」时信号没人处理、高亮永远停在「只下新集」，
                    // 看起来就是"点不动"；连保存也受影响（saveForm 读的
                    // 就是 currentValue）。
                    onSelected: function (v) {
                        ruleBox.currentValue = v
                    }
                }
            }

            Row {
                anchors.right: parent.right
                spacing: Theme.spacingSm

                AppButton {
                    objectName: "subFormCancelBtn"
                    text: "取消"
                    onClicked: root.cancelled()
                }

                AppButton {
                    objectName: "subFormSaveBtn"
                    text: "保存"
                    variant: "primary"
                    onClicked: root.submitted(nameField.text, urlField.text,
                                              ruleBox.currentValue)
                }
            }
        }
    }

    /// 外部填值（新增时清空、编辑时预填）
    function setValues(name, url, rule) {
        nameField.text = name || ""
        urlField.text = url || ""
        ruleBox.currentValue = rule || "new_only"
    }

    /// 读当前输入（「获取名称」回填时要保持其余字段不变，故需逐个读取）
    function currentUrl() { return urlField.text }
    function currentName() { return nameField.text }
    function getRule() { return ruleBox.currentValue }

    /// 只改名称（「获取名称」回填用）—— 保持地址/规则不动
    function setNameOnly(name) {
        nameField.text = name || ""
    }
}
