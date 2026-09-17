pragma Singleton
import QtQuick

// 主题单例：集中管理颜色 / 圆角 / 间距 / 字号。
//
// 三条设计原则：
// 1. 所有页面只引用 Theme.xxx，不写死颜色数值。
// 2. 亮色/暗色由 `dark` 开关驱动，两套中性色都在此定义。
// 3. **主题色（accent）可在运行时切换**：只改 `accentSource` 一个值，
//    hover / pressed / 文字反色 / 选中底 / 淡背景等派生色全部由它实时算出，
//    因此「设置里换主题色 → 整个界面跟着变」不需要重启或重建任何页面。
//
// 命名约束（Qt 保留字，踩过坑）：
// - 属性名不能以 on 开头（`onXxx` 被当作信号处理器）→ 用 accentText 而非 onAccent
// - 不能与 QObject 内置成员重名（`warning` 与 warning() 信号冲突）→ 用 warningColor
QtObject {
    id: theme

    // ================= 1. 模式与主题色 =================
    // 亮色优先（白色简约线性为默认）
    property bool dark: false

    // 主题色源值：设置页只改这一个属性
    property color accentSource: "#2F6FEB"

    // ---- 由 accentSource 实时派生的强调色族 ----
    //
    // 踩坑记录：这里**不能**写成 `readonly property color x: mix(...)`。
    // Qt 6 对 color 属性做 QML 绑定时，函数返回的 QColor 在跨文件
    // 求值（本文件是 singleton）场景下会退化成 #000000，且不报错。
    // 因此改为「普通属性 + onAccentSourceChanged / onDarkChanged 显式重算」。
    property color accent: accentSource
    property color accentHover
    property color accentPressed
    property color accentText
    property color accentSoft

    // 主题色或模式变化时重算派生色
    onAccentSourceChanged: recalcAccent()
    onDarkChanged: recalcAccent()
    Component.onCompleted: recalcAccent()

    function recalcAccent() {
        var src = accentSource
        var isDarkMode = dark

        // 先算成局部变量，避免把三元表达式直接塞进 mix() 的参数 ——
        // 那会让 mix 内部取到的通道值失真（同 Qt.rgba 那个坑）。
        var hoverAmount = isDarkMode ? 0.12 : 0.10
        var softTarget = isDarkMode ? "#171A21" : "#FFFFFF"
        var softAmount = isDarkMode ? 0.82 : 0.90

        // hover 提亮 / pressed 压暗：比直接叠透明度更可控，浅色背景上不会"发灰"
        accentHover   = lighter(src, hoverAmount)
        accentPressed = darker(src, 0.14)
        // 压在其上的文字：按亮度自动选黑或白，保证对比度
        accentText    = isLight(src) ? "#1A1A1A" : "#FFFFFF"
        // 淡色底：与背景混合，用于标签、选中行
        accentSoft    = mixHexStr(src, softTarget, softAmount)
    }

    /// 与 mix() 等价，但接收颜色**字符串**（内部转 QColor 后再混），
    /// 用于「目标色来自三元表达式」的场景。
    function mixHexStr(c, otherHex, t) {
        var o = Qt.color(otherHex)
        return mix(c, o, t)
    }

    // ================= 2. 颜色：背景层次 =================
    // 亮色：窗口略灰、卡片纯白，靠 1px 描边分层（简约线性）
    property color windowBg:      dark ? "#0F1115" : "#F6F7F9"
    property color surfaceBg:     dark ? "#171A21" : "#FFFFFF"
    property color surfaceAlt:    dark ? "#1E222B" : "#F2F4F7"
    property color elevatedBg:    dark ? "#232833" : "#FFFFFF"

    // ================= 3. 颜色：文字层级 =================
    property color textPrimary:   dark ? "#F2F4F7" : "#1A1D23"
    property color textSecondary: dark ? "#9BA3B4" : "#5A6472"
    property color textTertiary:  dark ? "#6B7383" : "#8E97A4"

    // ================= 4. 颜色：边框与分隔 =================
    property color border:        dark ? "#2A303C" : "#E4E7EC"
    property color borderStrong:  dark ? "#3A4250" : "#CDD3DC"

    // ================= 5. 颜色：状态色 =================
    property color successColor:  dark ? "#3FB950" : "#1A7F37"
    property color warningColor:  dark ? "#D29922" : "#9A6700"
    property color dangerColor:   dark ? "#F85149" : "#CF222E"

    // ================= 6. 交互态填充 =================
    // 悬停/按下的中性填充（不依赖主题色，保持"线性"观感）
    property color hoverFill:     dark ? "#2A3140" : "#EEF1F6"
    property color pressedFill:   dark ? "#333C4D" : "#E3E8F0"

    // ================= 7. 圆角 =================
    // 简约线性：整体取较小圆角，避免"圆润感"
    property int radiusSm: 4      // 小控件：输入框、标签
    property int radiusMd: 8      // 卡片
    property int radiusLg: 12     // 面板、弹窗
    property int radiusFull: 9999 // 胶囊 / 圆形按钮

    // ================= 8. 边框宽度 =================
    property int lineThin: 1
    property int lineThick: 2     // 选中/聚焦态

    // ================= 9. 间距节奏（4px 基准）=================
    property int spacingXs: 4
    property int spacingSm: 8
    property int spacingMd: 12
    property int spacingLg: 16
    property int spacingXl: 24

    // ================= 10. 页面留白 =================
    property int pagePadding: 24

    // ================= 11. 字号 =================
    property int fontXs: 11
    property int fontSm: 12
    property int fontMd: 14
    property int fontLg: 18
    property int fontXl: 24
    property int fontDisplay: 30

    // ================= 12. 动效时长 =================
    property int durFast: 120
    property int durNormal: 200
    property int durSlow: 320

    // ================= 13. 悬浮导航 =================
    property int navButtonSize: 40
    property int navPillHeight: 54
    property int navBottomMargin: 20
    property int navContentGutter: 86   // 各页内容底部需预留的高度

    // ================= 14. 海报卡片 =================
    property int posterWidth: 200
    property real posterRatio: 1.4      // 封面高 = 宽 × 1.4
    property int posterTextHeight: 56
    property int posterSpacing: 16

    // ================= 15. 主题色预设 =================
    // 设置页的主题色选择器直接读这个数组；用户选中的值写入配置，
    // 下次启动由 Python 侧调 applyAccent() 还原。
    readonly property var accentPresets: [
        { "name": "哔哩哔哩粉", "value": "#FB7299" },
        { "name": "哔哩哔哩蓝", "value": "#00AEEC" },
        { "name": "蓝",   "value": "#2F6FEB" },
        { "name": "青",   "value": "#0E9F9F" },
        { "name": "绿",   "value": "#2DA44E" },
        { "name": "紫",   "value": "#8250DF" },
        { "name": "玫红", "value": "#D6336C" },
        { "name": "橙",   "value": "#D97706" },
        { "name": "红",   "value": "#CF222E" },
        { "name": "石墨", "value": "#4B5563" }
    ]

    // ================= 公共方法 =================

    /// 应用主题色（设置页 / Python 启动时调用）
    function applyAccent(colorValue) {
        accentSource = colorValue
    }

    /// 按「亮度」判断某色是否偏亮 —— 决定压在其上的文字用黑还是白。
    /// 采用 ITU-R BT.601 感知亮度权重。
    function isLight(c) {
        var r = c.r, g = c.g, b = c.b
        return (0.299 * r + 0.587 * g + 0.114 * b) > 0.62
    }

    /// 与另一色按比例混合：t=0 返回 c，t=1 返回 other。
    ///
    /// **踩坑记录**：不能用 `Qt.rgba(c.r + (o.r - c.r) * t, ...)`。
    /// 实测（Qt 6 / PySide6）当参数是**运行时算出的浮点表达式**时，
    /// `Qt.rgba()` 会静默返回黑色 `#000000`，且不报任何警告 —— 而传字面量
    /// （如 `Qt.rgba(0.5, 0.5, 0.5, 1.0)`）又正常，极难定位。
    /// 这里改为手工算通道值并拼成 `#RRGGBB` 字符串，结果与
    /// Python `QColor.redF()` 的线性混合完全一致（已交叉核算）。
    function mix(c, other, t) {
        function ch(a, b) {
            var v = Math.round((a + (b - a) * t) * 255)
            if (v < 0) v = 0
            if (v > 255) v = 255
            var s = v.toString(16)
            return s.length === 1 ? "0" + s : s
        }
        return "#" + ch(c.r, other.r) + ch(c.g, other.g) + ch(c.b, other.b)
    }

    /// 提亮（朝白色混合）
    function lighter(c, amount) {
        return mix(c, Qt.rgba(1, 1, 1, 1), amount)
    }

    /// 压暗（朝黑色混合）
    function darker(c, amount) {
        return mix(c, Qt.rgba(0, 0, 0, 1), amount)
    }
}
