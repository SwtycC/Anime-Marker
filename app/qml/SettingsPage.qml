import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 设置页：Bangumi / 路径 / 启动器 / 监控 / 扫描与匹配 / qBittorrent / RSS / 界面。
//
// 数据流：
//   进入页面 → settingsBridge.getAll() → 填充各控件
//   点「保存」→ 组装 values 字典 → settingsBridge.saveAll(values)
//
// 「界面」区块是唯一即时生效的部分（主题），其余需保存后由
// QmlApp 重建依赖服务。
Item {
    id: root

    // 用户主动切换主题（由 Main.qml 监听后写入 config.ini）
    signal accentPicked(color value)
    signal themeModePicked(bool isDark)

    // 保存结果提示（Main.qml 转成状态栏消息）
    signal statusMessage(string text)

    // 表单值（一次读回，用属性承载便于绑定）
    property var cfg: ({})

    function loadConfig() {
        root.cfg = typeof settingsBridge !== "undefined" && settingsBridge
                   ? settingsBridge.getAll() : {}
    }

    Component.onCompleted: loadConfig()

    // 每次切到本页时重新读取，避免外部改动后显示旧值
    function refresh() { loadConfig() }

    /// 滚动到指定位置（截图/诊断用）
    function scrollTo(y) {
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height))
    }

    /// 诊断用：按 id 设置控件值（自动化测试脚本调用）
    function debugSet(fieldId, value) {
        // 通过 children 递归查找 objectName 匹配的控件
        function find(item, depth) {
            if (depth > 12 || !item || !item.children)
                return null
            for (var i = 0; i < item.children.length; i++) {
                var c = item.children[i]
                if (c.objectName === fieldId)
                    return c
                var r = find(c, depth + 1)
                if (r)
                    return r
            }
            return null
        }
        var w = find(root, 0)
        if (!w) {
            console.log("debugSet: 未找到", fieldId)
            return false
        }
        // 按控件类型选择要写的属性
        var tn = w.toString()
        if (tn.indexOf("NumberStepper") >= 0) {
            w.value = parseFloat(value)
        } else if (tn.indexOf("SegmentedControl") >= 0) {
            w.currentValue = String(value)
        } else if (tn.indexOf("CheckBoxLine") >= 0) {
            w.checked = (value === true || value === "true")
        } else {
            w.text = String(value)
        }
        return true
    }

    // 当前主题色 / 模式（用于界面区块的即时切换）
    function setValue(key, value) {
        var c = root.cfg
        c[key] = value
        root.cfg = c
    }

    function getValue(key, fallback) {
        var v = root.cfg ? root.cfg[key] : undefined
        return (v === undefined || v === null) ? (fallback || "") : v
    }

    function getBool(key, fallback) {
        var v = String(root.getValue(key, fallback ? "true" : "false")).toLowerCase()
        return v === "true" || v === "1" || v === "yes"
    }

    function getInt(key, fallback) {
        var v = parseInt(root.getValue(key, String(fallback)))
        return isNaN(v) ? fallback : v
    }

    function getFloat(key, fallback) {
        var v = parseFloat(root.getValue(key, String(fallback)))
        return isNaN(v) ? fallback : v
    }

    // ---- 保存 ----
    function collect() {
        var v = {}
        // Bangumi
        v["bangumi.token"] = tokenField.text.trim()
        v["bangumi.username"] = usernameField.text.trim()
        v["bangumi.api_base"] = apiBaseField.text.trim() || "https://api.bgm.tv"
        v["bangumi.proxy"] = proxyField.text.trim()
        v["bangumi.ep_timeline_count"] = epTimelineField.value
        v["bangumi.auto_upload"] = autoUploadBox.checked
        // 路径
        v["general.library_path"] = libraryField.text.trim()
        v["general.player_path"] = playerField.text.trim()
        v["general.ls_path"] = lsField.text.trim()
        // 启动器
        v["launcher.enable_ls"] = enableLsBox.checked
        v["launcher.ls_shortcut"] = lsShortcutField.text.trim()
        // 监控
        v["monitor.poll_interval"] = pollIntervalField.value
        v["monitor.trigger_threshold"] = thresholdField.value
        // 扫描
        v["scanner.season_patterns"] = seasonModeSeg.currentValue
        v["scanner.season_display"] = seasonDisplaySeg.currentValue
        v["scanner.accept_score"] = acceptScoreField.value
        v["scanner.accept_gap"] = acceptGapField.value
        // qBittorrent
        v["qbittorrent.host"] = qbHostField.text.trim() || "127.0.0.1"
        v["qbittorrent.port"] = qbPortField.value
        v["qbittorrent.username"] = qbUserField.text.trim()
        v["qbittorrent.password"] = qbPassField.text
        // RSS
        v["rss.poll_interval"] = rssPollField.value
        v["rss.rule"] = rssRuleSeg.currentValue
        v["rss.auto_download"] = rssAutoBox.checked
        return v
    }

    function save() {
        if (typeof settingsBridge === "undefined" || !settingsBridge) {
            root.statusMessage("设置桥接不可用")
            return false
        }
        var ok = settingsBridge.saveAll(root.collect())
        root.statusMessage(ok ? "配置已保存" : "配置保存失败（详见日志）")
        if (ok)
            root.loadConfig()
        return ok
    }

    function saveAndScan() {
        if (!root.save())
            return
        settingsBridge.requestScan()
    }

    /// 用「当前输入框里的 Token」解析 username 并回填到「用户 ID」栏。
    ///
    /// 必要性：`/v0/users/{username}` 要的是 username 而不是昵称，
    /// 用户在设置页很难分辨该填哪个；一键解析能直接给出正确值。
    ///
    /// 注意传的是**输入框里的 Token** 而不是已保存的 Token ——
    /// 否则用户改了 Token 但还没保存时会解析出旧账号。
    function detectUsername() {
        if (typeof inprogress === "undefined" || !inprogress) {
            root.statusMessage("桥接不可用，无法检测")
            return
        }
        var r = inprogress.resolveUsername(tokenField.text.trim())
        if (r.ok)
            usernameField.text = r.username
        root.statusMessage(r.message)
    }

    Flickable {
        id: flick
        anchors.fill: parent
        clip: true
        contentWidth: width
        contentHeight: content.implicitHeight + Theme.pagePadding * 2
                             + Theme.navContentGutter
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: AppScrollBar {
            id: settingsBar
            policy: ScrollBar.AsNeeded
        }

        // 点击设置页空白处：让当前聚焦的输入框（AppTextField / NumberStepper）
        // 交出焦点、边框恢复原状 —— QML 里点击空白不会自动移走焦点。
        //
        // 实现要点（三条缺一不可）：
        //   1. 必须是 Flickable 的**直接子项**：空白处的 press 被 Flickable
        //      独占接收，挂在页面根上的 MouseArea / TapHandler 都收不到；
        //   2. height 取 contentHeight（内容坐标系，铺满整个可滚动区域），
        //      z:-1 压到表单之下 —— 点输入框 / 按钮时事件被它们拿走，
        //      这个 MouseArea 收不到，焦点不会被误清；
        //   3. onPressed 里**先清焦点再 accepted=false**：把事件交还给
        //      Flickable，空白处拖拽滚动不受影响。
        MouseArea {
            width: flick.width
            height: flick.contentHeight
            z: -1
            onPressed: function (mouse) {
                root.forceActiveFocus()
                mouse.accepted = false
            }
        }

        // 用 Column + 显式宽度，而不是 ColumnLayout。
        //
        // 原因：ColumnLayout 作为 Flickable 的直接子项时，其尺寸不由父级
        // Layout 驱动，子项的 `Layout.fillWidth` 得不到有效约束 ——
        // 实测所有 FormRow 宽度为 0（高度正常），整个表单不可见。
        // 改用 Column + 每行显式 width 后布局稳定。
        Column {
            id: content
            x: Theme.pagePadding
            y: Theme.pagePadding
            width: flick.width - Theme.pagePadding * 2
                   - (settingsBar.visible ? settingsBar.width : 0)
            spacing: Theme.spacingXl

            // ==================== 标题 ====================
            Column {
                width: parent.width
                spacing: Theme.spacingXs

                Text {
                    text: "设置"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontXl
                    font.weight: Font.DemiBold
                }
                Text {
                    text: "Token · 路径 · 阈值 · 界面"
                    color: Theme.textSecondary
                    font.pixelSize: Theme.fontMd
                }
            }

            // ==================== 界面（即时生效）====================
            Column {
                width: parent.width
                spacing: Theme.spacingLg

                SectionHeader {
                    width: parent.width
                    title: "界面"
                    hint: "主题色与模式立即生效，无需保存或重启"
                }

                FormRow {
                    width: parent.width
                    label: "外观模式"
                    SegmentedControl {
                        id: modeSeg
                        options: [
                            { "label": "白色简约", "value": "light" },
                            { "label": "深色",     "value": "dark" }
                        ]
                        currentValue: Theme.dark ? "dark" : "light"
                        onSelected: function (value) {
                            var isDark = value === "dark"
                            if (Theme.dark === isDark)
                                return
                            Theme.dark = isDark
                            root.themeModePicked(isDark)
                        }
                    }
                }

                FormRow {
                    width: parent.width
                    label: "主题色"
                    AccentPicker {
                        onPicked: function (value) {
                            Theme.applyAccent(value)
                            root.accentPicked(value)
                        }
                    }
                }

                FormRow {
                    width: parent.width
                    label: "海报宽度"
                    Row {
                        spacing: Theme.spacingMd
                        NumberStepper {
                            id: posterWidthField
                            objectName: "posterWidthField"
                            value: root.getFloat("ui.poster_width", 200)
                            minimum: 120
                            maximum: 400
                            step: 20
                            suffix: " px"
                        }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: "（下次扫描或重启后生效）"
                            color: Theme.textTertiary
                            font.pixelSize: Theme.fontXs
                        }
                    }
                }
            }

            // ==================== Bangumi ====================
            Column {
                width: parent.width
                spacing: Theme.spacingLg

                SectionHeader {
                    width: parent.width
                    title: "Bangumi"
                    hint: "Token 用于标记看过与拉取在看列表，需勾选「读取收藏」权限"
                }

                FormRow {
                    width: parent.width
                    label: "Access Token"
                    // ---- 帮助按钮「浮在输入框左外侧」----
                    //
                    // 布局意图：输入框**严格占满内容列**（左边与其它栏对齐，
                    // 右边也齐平），按钮用负 x 伸进左侧的**标签区**。
                    //
                    // 可行性：FormRow 的标签列固定 132px，而标签文字
                    // 「Access Token」只占约 90px，右侧留有约 40px 空档 ——
                    // 足够放一个 20px 的小圆。这样既不占用输入框的宽度，
                    // 也不影响任何一栏的对齐。
                    //
                    // 踩坑记录（前两版都因此返工）：
                    //   ① 按钮放左侧**并参与布局**（输入框 left: 按钮.right）
                    //      → Token 输入框比其它栏右移「按钮宽 + 间距」，
                    //        四栏左边缘参差不齐（实测偏差 28px）。
                    //   ② 按钮放输入框右侧 → 左侧对齐了，但按钮与「检测」
                    //      按钮挤在右侧同一列，视觉上"操作区"偏重，且
                    //      Token 输入框比其它栏窄了 28px。
                    // 本版把按钮**移出布局流**（负偏移 + 绝对定位），
                    // 因此四个输入框的 x 完全一致（实测偏差 0px）。
                    Item {
                        width: parent.width
                        height: 34

                        AppTextField {
                            id: tokenField
                            objectName: "tokenField"
                            text: root.getValue("bangumi.token", "")
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            echoPassword: true
                            placeholder: "从 next.bgm.tv/demo/access-token 生成"
                        }

                        // 负偏移伸到标签区。按钮宽 20 + 间距 8 = 28px，
                        // 而标签右侧空档约 40px，因此不会压到 "Access Token" 文字。
                        HelpButton {
                            id: tokenHelpBtn
                            objectName: "tokenHelpBtn"
                            x: -width - Theme.spacingSm
                            anchors.verticalCenter: parent.verticalCenter
                            tooltip: "如何获取 Access Token"
                            onClicked: tokenDialog.open()
                        }
                    }
                }

                FormRow {
                    width: parent.width
                    label: "用户 ID"
                    hint: "username，不是昵称"
                    Row {
                        width: parent.width
                        spacing: Theme.spacingSm

                        AppTextField {
                            id: usernameField
                            objectName: "usernameField"
                            text: root.getValue("bangumi.username", "")
                            width: parent.width - detectBtn.width - Theme.spacingSm
                            placeholder: "留空则自动从 Token 解析（推荐）"
                        }

                        AppButton {
                            id: detectBtn
                            anchors.verticalCenter: parent.verticalCenter
                            text: "检测"
                            onClicked: root.detectUsername()
                        }
                    }
                }

                FormRow {
                    width: parent.width
                    label: "API 地址"
                    AppTextField {
                        id: apiBaseField
                        objectName: "apiBaseField"
                        text: root.getValue("bangumi.api_base", "")
                        width: parent.width
                        placeholder: "https://api.bgm.tv"
                    }
                }

                FormRow {
                    width: parent.width
                    label: "代理"
                    // 留空时应用会用**系统代理**（requests 会读 Windows 的
                    // Internet 设置），所以"没填"不等于"没走代理"。
                    // 直连失败最常见的原因是：代理客户端的**分流规则**把
                    // bgm.tv 判给了直连（它是国内域名，常被国内规则集收录），
                    // 此时换节点无用 —— 要让 bgm.tv 走隧道的规则才管用。
                    hint: "留空则用系统代理；若拉取失败，请确认代理客户端把 bgm.tv 走了代理而非直连"
                    AppTextField {
                        id: proxyField
                        objectName: "proxyField"
                        text: root.getValue("bangumi.proxy", "")
                        width: parent.width
                        placeholder: "http://127.0.0.1:7890（可选）"
                    }
                }

                FormRow {
                    width: parent.width
                    label: "动态显示条数"
                    // 这个数**只管显示**：动态页（本地 + Bangumi）首屏显示
                    // 最近 N 条，向下滚动或点「加载更多」每次再追加一页
                    // （一页至少 50 条）。
                    //
                    // **与同步范围彻底无关**：逐集记录的同步是"全量一次 +
                    // 之后增量"（按收藏状态与水位判断该重拉哪几部），
                    // 所以这里调大调小都不会多打或少打请求，也不会删数据。
                    // 早期这个数兼作抓取范围（"凑够 N 条即停"），导致排在
                    // 后面的上百部番永远拉不到 —— 已废弃。
                    hint: "「动态」页首屏显示最近 N 条，向下滚动可加载更多（同步范围不受影响）"
                    Row {
                        spacing: Theme.spacingMd
                        NumberStepper {
                            id: epTimelineField
                            objectName: "epTimelineField"
                            value: root.getFloat("bangumi.ep_timeline_count", 30)
                            minimum: 0
                            maximum: 300
                            step: 5          // 需求：按一下 ±5
                            suffix: " 条"
                            width: 180
                        }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: "0 = 关闭「动态」页的逐集记录"
                            color: Theme.textTertiary
                            font.pixelSize: Theme.fontXs
                        }
                    }
                }

                FormRow {
                    width: parent.width
                    label: "自动上传"
                    // 只管"**看完那一刻是否立即同步**"（实测选定的节点）。
                    // 关掉之后：本地记录照写（来源 tag 只有「本地」），什么时候
                    // 上传由「动态 → 上传」小窗决定 —— 差集与幂等由那边保证，
                    // 所以关掉**不会丢记录**，只是延迟同步。
                    hint: "看完一集后立即标记到 Bangumi；关闭则只记本地，改用「动态 → 上传」手动补传"
                    CheckBoxLine {
                        id: autoUploadBox
                        objectName: "autoUploadBox"
                        checked: root.getBool("bangumi.auto_upload", true)
                        text: "启用（关闭后可在「动态 → 上传」里手动补传）"
                    }
                }
            }

            // ==================== 路径 ====================
            Column {
                width: parent.width
                spacing: Theme.spacingLg

                SectionHeader {
                    width: parent.width
                    title: "路径"
                    hint: "多个媒体库目录用分号 ; 分隔"
                }

                FormRow {
                    width: parent.width
                    label: "媒体库根目录"
                    Row {
                        width: parent.width
                        spacing: Theme.spacingMd

                        AppTextField {
                            id: libraryField
                            objectName: "libraryField"
                        text: root.getValue("general.library_path", "")
                            width: parent.width - browseLibBtn.width - Theme.spacingMd
                        }
                        AppButton {
                            id: browseLibBtn
                            text: "浏览…"
                            onClicked: {
                                var p = settingsBridge.pickDirectory(libraryField.text)
                                if (p)
                                    libraryField.text = p
                            }
                        }
                    }
                }

                FormRow {
                    width: parent.width
                    label: "PotPlayer 路径"
                    Row {
                        width: parent.width
                        spacing: Theme.spacingMd

                        AppTextField {
                            id: playerField
                            objectName: "playerField"
                        text: root.getValue("general.player_path", "")
                            width: parent.width - browsePlayerBtn.width - Theme.spacingMd
                        }
                        AppButton {
                            id: browsePlayerBtn
                            text: "浏览…"
                            onClicked: {
                                var p = settingsBridge.pickFile("选择 PotPlayer", playerField.text)
                                if (p)
                                    playerField.text = p
                            }
                        }
                    }
                }

                FormRow {
                    width: parent.width
                    label: "小黄鸭路径"
                    Row {
                        width: parent.width
                        spacing: Theme.spacingMd

                        AppTextField {
                            id: lsField
                            objectName: "lsField"
                        text: root.getValue("general.ls_path", "")
                            width: parent.width - browseLsBtn.width - Theme.spacingMd
                        }
                        AppButton {
                            id: browseLsBtn
                            text: "浏览…"
                            onClicked: {
                                var p = settingsBridge.pickFile("选择 Lossless Scaling", lsField.text)
                                if (p)
                                    lsField.text = p
                            }
                        }
                    }
                }
            }

            // ==================== 启动器与监控 ====================
            Column {
                width: parent.width
                spacing: Theme.spacingLg

                SectionHeader {
                    width: parent.width
                    title: "启动器与监控"
                    hint: "播放前自动启动小黄鸭插帧；进度达阈值时自动标记看过"
                }

                FormRow {
                    width: parent.width
                    label: "插帧"
                    CheckBoxLine {
                        id: enableLsBox
                        objectName: "enableLsBox"
                        checked: root.getBool("launcher.enable_ls", true)
                        text: "播放前启用小黄鸭插帧"
                    }
                }

                FormRow {
                    width: parent.width
                    label: "插帧快捷键"
                    AppTextField {
                        id: lsShortcutField
                        objectName: "lsShortcutField"
                        text: root.getValue("launcher.ls_shortcut", "")
                        width: parent.width
                        placeholder: "ctrl+alt+l"
                    }
                }

                FormRow {
                    width: parent.width
                    label: "轮询间隔"
                    NumberStepper {
                        id: pollIntervalField
                        objectName: "pollIntervalField"
                        value: root.getFloat("monitor.poll_interval", 3)
                        minimum: 1
                        maximum: 60
                        step: 1
                        suffix: " 秒"
                    }
                }

                FormRow {
                    width: parent.width
                    label: "触发阈值"
                    NumberStepper {
                        id: thresholdField
                        objectName: "thresholdField"
                        value: root.getFloat("monitor.trigger_threshold", 0.95)
                        minimum: 0.5
                        maximum: 1.0
                        step: 0.05
                        decimals: 2
                        width: 180
                    }
                }
            }

            // ==================== 扫描与匹配 ====================
            Column {
                width: parent.width
                spacing: Theme.spacingLg

                SectionHeader {
                    width: parent.width
                    title: "扫描与匹配"
                    hint: "季数识别与匹配阈值改动后需重新扫描"
                }

                FormRow {
                    width: parent.width
                    label: "季数识别"
                    SegmentedControl {
                        id: seasonModeSeg
                        objectName: "seasonModeSeg"
                        currentValue: root.getValue("scanner.season_patterns", "cn")
                        options: [
                            { "label": "第X季 / S1 / Season 1", "value": "cn" },
                            { "label": "额外识别罗马数字 II / III", "value": "all" }
                        ]
                    }
                }

                FormRow {
                    width: parent.width
                    label: "多季展示"
                    SegmentedControl {
                        id: seasonDisplaySeg
                        objectName: "seasonDisplaySeg"
                        currentValue: root.getValue("scanner.season_display", "flat")
                        options: [
                            { "label": "平铺", "value": "flat" },
                            { "label": "聚合", "value": "grouped" }
                        ]
                    }
                }

                FormRow {
                    width: parent.width
                    label: "匹配阈值"
                    hint: "分数低于此值转手动确认"
                    Row {
                        spacing: Theme.spacingMd
                        NumberStepper {
                            id: acceptScoreField
                            objectName: "acceptScoreField"
                            value: root.getFloat("scanner.accept_score", 60)
                            minimum: 0
                            maximum: 200
                            step: 5
                            width: 140
                        }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: "与第二名的差距"
                            color: Theme.textTertiary
                            font.pixelSize: Theme.fontSm
                        }
                        NumberStepper {
                            id: acceptGapField
                            objectName: "acceptGapField"
                            value: root.getFloat("scanner.accept_gap", 30)
                            minimum: 0
                            maximum: 200
                            step: 5
                            width: 140
                        }
                    }
                }
            }

            // ==================== qBittorrent ====================
            Column {
                width: parent.width
                spacing: Theme.spacingLg

                SectionHeader {
                    width: parent.width
                    title: "qBittorrent（订阅下载）"
                    hint: "用于把 RSS 命中的新集推送到 qBittorrent 下载"
                }

                FormRow {
                    width: parent.width
                    label: "Web UI 地址"
                    AppTextField {
                        id: qbHostField
                        objectName: "qbHostField"
                        text: root.getValue("qbittorrent.host", "")
                        width: parent.width
                        placeholder: "127.0.0.1"
                    }
                }

                FormRow {
                    width: parent.width
                    label: "Web UI 端口"
                    NumberStepper {
                        id: qbPortField
                        objectName: "qbPortField"
                        value: root.getFloat("qbittorrent.port", 8080)
                        minimum: 1
                        maximum: 65535
                        step: 1
                        width: 160
                    }
                }

                FormRow {
                    width: parent.width
                    label: "用户名"
                    AppTextField {
                        id: qbUserField
                        objectName: "qbUserField"
                        text: root.getValue("qbittorrent.username", "")
                        width: parent.width
                        placeholder: "admin"
                    }
                }

                FormRow {
                    width: parent.width
                    label: "密码"
                    AppTextField {
                        id: qbPassField
                        objectName: "qbPassField"
                        text: root.getValue("qbittorrent.password", "")
                        width: parent.width
                        echoPassword: true
                    }
                }
            }

            // ==================== RSS ====================
            Column {
                width: parent.width
                spacing: Theme.spacingLg

                SectionHeader {
                    width: parent.width
                    title: "RSS 订阅"
                    hint: "定时抓取订阅源，按规则判定是否下发下载"
                }

                FormRow {
                    width: parent.width
                    label: "轮询间隔"
                    NumberStepper {
                        id: rssPollField
                        objectName: "rssPollField"
                        value: root.getFloat("rss.poll_interval", 30)
                        minimum: 5
                        maximum: 720
                        step: 5
                        suffix: " 分钟"
                        width: 180
                    }
                }

                FormRow {
                    width: parent.width
                    label: "默认下载规则"
                    SegmentedControl {
                        id: rssRuleSeg
                        objectName: "rssRuleSeg"
                        currentValue: root.getValue("rss.rule", "new_only")
                        options: [
                            { "label": "只下新集", "value": "new_only" },
                            { "label": "补缺集",   "value": "fill_gap" },
                            { "label": "完结整包", "value": "complete_pack" },
                            { "label": "仅通知",   "value": "manual" }
                        ]
                    }
                }

                FormRow {
                    width: parent.width
                    label: "自动下载"
                    CheckBoxLine {
                        id: rssAutoBox
                        objectName: "rssAutoBox"
                        checked: root.getBool("rss.auto_download", false)
                        text: "启用自动下载（关闭时命中新集仅入库为待确认）"
                    }
                }
            }
        }
    }

    // ==================== Token 获取说明弹窗 ====================
    Dialog {
        id: tokenDialog
        modal: true
        anchors.centerIn: parent
        width: 520
        padding: Theme.spacingXl
        title: "如何获取 Access Token"

        background: Rectangle {
            color: Theme.surfaceBg
            border.width: Theme.lineThin
            border.color: Theme.border
            radius: Theme.radiusMd
        }

        contentItem: Column {
            width: parent.width
            spacing: Theme.spacingMd

            Text {
                width: parent.width
                text: "在浏览器中打开以下地址，登录 Bangumi 后即可生成个人 Access Token："
                color: Theme.textPrimary
                font.pixelSize: Theme.fontMd
                wrapMode: Text.WordWrap
            }

            // 可点击的地址（选中复制 + 一键打开）
            Rectangle {
                width: parent.width
                height: 38
                radius: Theme.radiusSm
                color: Theme.surfaceAlt
                border.width: Theme.lineThin
                border.color: Theme.border

                Text {
                    id: tokenUrl
                    anchors.left: parent.left
                    anchors.leftMargin: Theme.spacingMd
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - openTokenBtn.width - Theme.spacingLg
                    text: "https://next.bgm.tv/demo/access-token"
                    color: Theme.accent
                    font.pixelSize: Theme.fontSm
                    elide: Text.ElideRight
                    // 允许鼠标选中复制（TextEdit 才有 selectByMouse）
                    // 这里用 Text + 一键打开按钮，更省事
                }

                AppButton {
                    id: openTokenBtn
                    anchors.right: parent.right
                    anchors.rightMargin: 4
                    anchors.verticalCenter: parent.verticalCenter
                    height: 30
                    text: "打开"
                    onClicked: Qt.openUrlExternally(
                                   "https://next.bgm.tv/demo/access-token")
                }
            }

            Text {
                width: parent.width
                text: "注意事项：\n"
                      + "1. 生成时必须勾选「读取收藏」权限，否则无法拉取在看列表。\n"
                      + "2. 生成的 Token 只显示一次，请及时复制保存。\n"
                      + "3. 下方「用户 ID」栏留空即可 —— 程序会用 Token 自动解析。"
                color: Theme.textSecondary
                font.pixelSize: Theme.fontSm
                lineHeight: 1.5
                wrapMode: Text.WordWrap
            }

            Row {
                anchors.right: parent.right

                AppButton {
                    text: "知道了"
                    variant: "primary"
                    onClicked: tokenDialog.close()
                }
            }
        }
    }

    // ==================== 底部操作栏（固定，不随滚动）====================
    //
    // 关键：**不要用整条全宽的实色 Rectangle**。
    // 早期实现是 `Rectangle { anchors.left/right: parent; height: 60;
    // color: Theme.windowBg }`，它会在底部铺出一条不透明横带 ——
    // 悬浮导航浮在它之上时，视觉上就是「导航栏外面上半部分有一块白块」。
    // 现在改为只让按钮自身成为浮层，不铺背景。
    Row {
        id: actionBar
        anchors.right: parent.right
        anchors.rightMargin: Theme.pagePadding
        // 底边与悬浮导航下沿对齐，按钮组整体在导航上方
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Theme.navBottomMargin + Theme.navPillHeight + 8
        spacing: Theme.spacingMd

        AppButton {
            text: "保存并扫描"
            variant: "primary"
            onClicked: root.saveAndScan()
        }
        AppButton {
            text: "保存"
            onClicked: root.save()
        }
    }
}
