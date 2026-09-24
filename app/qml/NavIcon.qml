import QtQuick
import QtQuick.Effects

// 导航图标：外部 SVG + 按主题染色（薄封装）。
//
// 五个图标**全部**来自 `resources/icons/` 下同一套 16×16 填充式 SVG
// （文件名映射见下方 svgName），不再有任何自绘几何。
//
// 渲染链：Image(source) → MultiEffect（按 alpha 染色）
//   - 染色用 `colorization` 而非 `colorOverlay`：这批图标多是"带洞的复合
//     路径"（齿轮的中心孔、RSS 的圆弧缺口），colorization 只改颜色、不碰
//     透明度，镂空得以保留；叠加式填色会把它们糊成实心块。
//   - Image 本身 visible:false，只作为 MultiEffect 的 source，避免多渲染
//     一份（MultiEffect 会自行取它的纹理）。
//
// 颜色不写进 SVG 文件（源文件统一 fill="#000"），运行时由调用方传 color，
// 因此换主题色时图标自动跟随。
Item {
    id: root

    /// 图标种类：grid（海报墙）/ play（在看）/ clock（动态）/ rss（订阅）/ gear（设置）
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
    readonly property string svgName: kind === "grid"  ? "items-grid"
                                    : kind === "play"  ? "play"
                                    : kind === "clock" ? "clock"
                                    : kind === "rss"   ? "rss"
                                    : kind === "gear"  ? "settings"
                                    : ""
    readonly property string svgUrl: svgName !== "" && iconsBase !== ""
                                     ? iconsBase + svgName + ".svg" : ""

    Image {
        id: svgImage
        anchors.fill: parent
        visible: false
        source: root.svgUrl
        sourceSize.width: 64          // 放大解码，缩小后边缘更干净
        sourceSize.height: 64
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
