import QtQuick
import QtQuick.Effects
import QtQuick.Window      // Screen.devicePixelRatio（按物理像素解码用）

// 图标：外部 SVG + 按主题染色（薄封装）。
//
// 图标**全部**来自 `resources/icons/` 下同一套填充式 SVG
// （文件名映射见下方 svgName），不再有任何自绘几何。
// 导航图标是 16×16，另有给搜索胶囊用的 search（24×24，见 SearchPill.qml）——
// viewBox 尺寸不同不影响显示：Image 用 PreserveAspectFit。
//
// 渲染链：Image(source) → MultiEffect（染色）
//   - 染色用 `colorization` 而非 `colorOverlay`：这批图标多是"带洞的复合
//     路径"（齿轮的中心孔、RSS 的圆弧缺口），colorization 不碰透明度，
//     镂空得以保留；叠加式填色会把它们糊成实心块。
//   - Image 本身 visible:false，只作为 MultiEffect 的 source，避免多渲染
//     一份（MultiEffect 会自行取它的纹理）。
//
// **SVG 的 fill 必须是白色**（踩坑记录）：`colorization` 的实际算法是
//   `结果色 = colorizationColor × 源亮度`
// 而不是"把源涂成 colorizationColor"。源是黑色时亮度为 0，无论传什么
// 主题色，染出来都是**纯黑** —— 表现是整个导航栏图标长期黑色（既不跟随
// 主题色，选中项也变不白），而 QML 侧不报任何错，极难定位。
// 颜色依然不写进 SVG（一律 fill="#FFFFFF" 当"白色蒙版"），
// 主题色由调用方传 `color`，因此换主题色时图标自动跟随。
Item {
    id: root

    /// 图标种类：grid（海报墙）/ play（在看）/ clock（动态）/ rss（订阅）/ gear（设置）/
    /// search（搜索）/ chevron（展开箭头，非选中时朝下，展开后由调用方旋转 180°）
    property string kind: "grid"
    /// 图标颜色（由 NavBar 按选中 / 悬停 / 常态传入）
    property color color: Theme.textSecondary
    // 外部 SVG 的基地址（由 Python 侧注入，见 QmlApp 的
    // setContextProperty "iconsBaseUrl"）。
    //
    // 为什么由 Python 注入而不是写相对路径：QML 的导入根是 app/，而图标在
    // 项目根的 resources/ 下；相对路径在开发态 / 打包态结构还不一样。
    property string iconsBase: typeof iconsBaseUrl !== "undefined"
                               ? iconsBaseUrl : ""

    implicitWidth: 24
    implicitHeight: 24

    /// kind → SVG 文件名（不含扩展名）；未知 kind 返回空串（不渲染）
    readonly property string svgName: kind === "grid"   ? "items-grid"
                                    : kind === "play"   ? "play"
                                    : kind === "clock"  ? "clock"
                                    : kind === "rss"    ? "rss"
                                    : kind === "gear"   ? "settings"
                                    : kind === "search" ? "search"
                                    : kind === "chevron" ? "chevron-down"
                                    : kind === "link"   ? "link"
                                    : kind === "back"   ? "arrow-left"
                                    // up：复用 arrow-left 的 SVG + 旋转 90°
                                    // （见下面的 iconRotation）
                                    : kind === "up"     ? "arrow-left"
                                    // upBold：独立的**加粗**上箭头。
                                    // 为什么不复用 up：arrow-left 的笔画宽约 1。
                                    // 1（在 16 的 viewBox 里），放大到 20px 显示
                                    // 时偏细（实测反馈"箭头想要粗一点"）。
                                    // 直接改 arrow-left 会影响导航栏的返回按钮，
                                    // 所以单开一个描边加粗的 SVG。
                                    : kind === "upBold" ? "arrow-up-bold"
                                    // plus：添加（描边加粗的加号）
                                    : kind === "plus"   ? "plus"
                                    // trash：删除（垃圾桶）
                                    : kind === "trash"  ? "trash"
                                    : ""
    readonly property string svgUrl: svgName !== "" && iconsBase !== ""
                                     ? iconsBase + svgName + ".svg" : ""

    /// 需要旋转的角度（度）。
    ///
    /// **为什么用旋转而不是新建一个 up.svg**（取舍）：`arrow-left` 是一条
    /// 手工调过的贝塞尔路径，直接改 `fill`/路径很容易画歪；而 arrow-left
    /// 顺时针转 90° 正好是"向上"（`↑`），几何上严格等价。
    /// 复用已验证的资源、不引入手写 SVG，出错面最小。
    ///
    /// **命名注意**：**不能叫 `rotation`** —— `Item` 自身已有内置属性
    /// `rotation`（且为只读式绑定），再声明同名 property 会让整个文件
    /// 加载失败（实测报 "rotation is a read-only property"，连带所有用到
    /// NavIcon 的页面全部不可用）。所以用 `iconRotation`。
    readonly property real iconRotation: kind === "up" ? 90 : 0

    Item {
        id: rot
        anchors.fill: parent
        rotation: root.iconRotation
        // 旋转默认绕中心（transformOrigin 默认即 Center），箭头指向正确

        Image {
            id: svgImage
            anchors.fill: parent
            visible: false
            source: root.svgUrl

            // 按"实际显示尺寸 × DPR"解码（原先固定 64）。
            //
            // **先说清楚实测结论**：锯齿的成因**不是**这一步。实测过
            // 三种解码尺寸缩到 22px 显示后的抗锯齿过渡像素数：
            //     sourceSize=64 → 202 个
            //     sourceSize=22 → 200 个
            //     sourceSize=44 → 201 个
            // 三者基本一致（差 ≤2，可视为噪声）—— 说明 64 → 22 的那次
            // 缩小由 SmoothTransformation 处理得很干净，**不是**锯齿来源。
            //
            // 那为什么还是改成按显示尺寸解码：纯粹是省事省钱 ——
            // 原来每次都把 16 单位的路径光栅成 64×64 位图（4096 像素），
            // 再缩到 22×22 只用其中一小部分；现在直接生成需要的尺寸，
            // 省掉一次大图分配与一次全图重采样。
            //
            // 真正影响观感的是下面 MultiEffect 的 `smooth` / 图层尺寸，
            // 见那里的说明。改这里时**不要**以为它修了锯齿。
            //
            // 空尺寸兜底：组件尚未布局完成时 width/height 可能为 0
            // （NavIcon 默认 implicitWidth 24，但调用方会覆盖），
            // 为 0 会让 Image 不加载 —— 用 implicit 尺寸兜一下。
            readonly property real _renderSize: root.width > 0
                                                ? root.width : root.implicitWidth
            sourceSize.width: Math.max(1, Math.round(
                _renderSize * Screen.devicePixelRatio))
            sourceSize.height: Math.max(1, Math.round(
                _renderSize * Screen.devicePixelRatio))
            fillMode: Image.PreserveAspectFit
            asynchronous: false
        }

        MultiEffect {
            anchors.fill: parent
            visible: root.svgUrl !== "" && svgImage.status === Image.Ready
            source: svgImage
            colorization: 1.0
            colorizationColor: root.color
        }
    }
}
