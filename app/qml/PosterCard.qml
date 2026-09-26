import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import QtQuick.Window      // Screen.devicePixelRatio（layer.textureSize 用）

// 海报卡片：固定尺寸、封面等比缩放居中、标题单行省略。
//
// 尺寸与旧版 PosterCard（app/ui/widgets.py）保持一致：宽 × 1.4 为封面区，
// 文本区固定 56px，确保迁移前后视觉一致。
Rectangle {
    id: root

    property int subjectId: 0
    property string title: ""
    property string meta: ""
    property string coverUrl: ""
    property string matchState: "auto"    // auto | manual | pending
    property int posterWidth: Theme.posterWidth

    signal clicked(int subjectId)

    readonly property int coverHeight: Math.round(posterWidth * Theme.posterRatio)
    readonly property bool _hovered: hoverArea.containsMouse

    width: posterWidth
    height: coverHeight + Theme.posterTextHeight

    // 卡片整体圆角：**四个角一致**。
    //
    // 改成四角一致的前提是**封面也裁了同弧度的圆角**（见 coverArea 的
    // MultiEffect + posterMaskUrl）。否则上方两角会露出卡片底色 ——
    // 深色海报上就是个刺眼的白缺口（这正是早期把上角做成直角的原因）。
    radius: Theme.radiusMd
    color: Theme.surfaceBg
    border.width: Theme.lineThin
    border.color: _hovered ? Theme.accent : Theme.border

    // 开 clip 是为了让文本区/悬停高亮等子项在**下方圆角**处被裁掉，
    // 否则子项会盖住圆角、露出尖角。
    clip: true

    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }

    // 悬停轻微上浮，增强"可点击"暗示。
    // 注意：不用 `y: hovered ? -3 : 0`，因为 y 由 Flow 布局掌管，
    // 直接改 y 会与布局打架；改用 transform 位移。
    transform: Translate {
        y: root._hovered ? -3 : 0
        Behavior on y { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic } }
    }

    Column {
        anchors.fill: parent
        spacing: 0

        // ---- 封面区 ----
        //
        // 封面用 `MultiEffect` + **预制圆角遮罩图**裁成圆角，使四个角与
        // 卡片的圆角一致。
        //
        // **为什么用预制遮罩图，而不是现画一个圆角矩形**（多轮实测，
        // 记录在此避免后人重走）：
        //   ① 遮罩取的是 **alpha 通道**（不是亮度，这点极易误解）。原想用
        //      `Rectangle { radius: 8 }` 现画一个，但作为遮罩源的它
        //      必须"真实渲染"才有纹理；`visible:false` 之外的几种藏法
        //      （`opacity:0` / 挪到屏幕外）实测拿不到纹理 → 输出空白
        //      （表现为"封面整张消失"）。让它正常显示再盖上去也不行。
        //   ② 父项 `clip` 只支持轴对齐矩形，裁不了圆角。
        //   ③ 现成的 `OpacityMask` 在 **Qt 6.9 已随 Qt5Compat 移入
        //      PySide6-Addons** —— 本机只装了 Essentials，没有该模块，
        //      `import` 直接失败（实测），所以只剩 MultiEffect 一条路。
        //
        // 结论：遮罩必须是**天然带 alpha 的图片资源** —— 这也正是
        // Qt Quick Controls 内部处理圆角/阴影的常规做法。
        // 遮罩文件：resources/poster_mask.png（32×32，纯白不透明，
        // **上两角** alpha=0 用来切圆角、**下两角**保持不透明），
        // 由 qml_app.py 注入为 `posterMaskUrl`，打包时在 anime_marker.spec
        // 的 datas 里单列。
        // （原先的生成脚本 `_gen_mask.py` 已不在仓库中；要重做就按上面
        //   那行描述用 PIL 画一张，别去 import 一个不存在的模块。）
        Item {
            id: coverArea
            width: parent.width
            height: root.coverHeight

            // 占位底（无封面时显示）
            Rectangle {
                id: coverPlaceholder
                anchors.fill: parent
                color: Theme.surfaceAlt
                visible: !coverImage.visible
            }

            // 封面本体：作为 coverEffect 的 source。
            //
            // **`layer.enabled` 对 source 是必需的**（实测）：遮罩类效果
            // 需要 source 提供独立纹理，不开 layer 时遮罩会整体失效
            // （四个角全变直角 —— 已实测确认）。
            //
            // **`sourceSize` 才是"海报看着糊"的真正解药**（本轮实测）。
            // 不给它时，Qt 会把 1227×1736 的原图**整张**传上 GPU，再靠
            // **一次双线性采样**缩到 200×280 —— 6 倍缩小只用 4 个纹素，
            // 细节成片丢失、边缘出现块状锯齿，这就是用户看到的"比原图模糊"。
            //
            // 给成"显示尺寸的 2 倍"后，Qt 在**加载期**先把图缩到 2 倍尺寸
            // （这一步用高质量滤波），GPU 只剩 2 倍缩小，重采样损失可忽略。
            //
            // 倍数实测（同一张封面缩到 200×280，与 LANCZOS 上限对比）：
            //     不给 sourceSize   Laplacian 13467  梯度 35.8  ← 锯齿
            //     ×1                Laplacian  2714  梯度 21.5  ← 偏糊
            //     ×1.5              Laplacian  2224  梯度 20.3  ← 更糊
            //     ×2                Laplacian  3431  梯度 22.9  ← 最接近上限
            //     ×3                Laplacian  9128  梯度 31.0  ← 又开始锯齿
            //     LANCZOS 上限       Laplacian  4308  梯度 25.7
            // ×2 是拐点：再小则 Qt 那一步缩放太粗，再大则 GPU 缩小重新锯齿。
            // （`mipmap: true` 也试过，Laplacian 1715 —— 比 ×1 还糊，弃用。）
            //
            // 乘 `Screen.devicePixelRatio` 是为了让"2 倍"始终相对**物理
            // 像素**：DPR=1 时 200×280 的卡片解出 400×560，DPR=2 时是
            // 800×1120。若写成"逻辑尺寸 ×2"，DPR=2 下就退化成上面的 ×1
            // 档（偏糊），所以 DPR 因子不能省。
            //
            // 与 `layer.textureSize` 的分工别搞混：`textureSize` 决定
            // **合成 FBO** 多大（1 倍物理像素），`sourceSize` 决定**解码**
            // 多大（2 倍物理像素）。两者差 2 倍是刻意的。
            Image {
                id: coverImage
                width: coverArea.width
                height: coverArea.height
                y: -height            // 由 coverEffect 负责显示，自身藏到屏外
                source: root.coverUrl
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: true
                enabled: false

                sourceSize.width: Math.round(width * Screen.devicePixelRatio * 2)
                sourceSize.height: Math.round(height * Screen.devicePixelRatio * 2)

                // 别顺手加 `mipmap: true`（实测，与直觉相反）：
                // 在**无 layer** 的探针里它确实有增益，但本卡片的 coverImage
                // 开了 `layer`，mip 选择会被放大到整张原图上 —— 真应用实测
                // Laplacian 3436 → 1822、梯度 22.89 → 19.01，明显更糊，
                // 而 RMSE 只从 15.59 微降到 14.89（"模糊能骗过 RMSE"）。
                // 所以这里只留 `sourceSize`，不要 mipmap。

                layer.enabled: true
                // FBO 按设备像素分配，避免 DPR>1 时的重采样模糊
                layer.textureSize: Qt.size(
                    Math.ceil(width * Screen.devicePixelRatio),
                    Math.ceil(height * Screen.devicePixelRatio))
                layer.smooth: true
            }

            // 遮罩源：预制的**上圆下直**遮罩图（见 resources/poster_mask.png）。
            //
            // 形状为什么要"上圆下直"：封面区下面紧接白色文字区，两者同宽
            // 且无缝相接。下方若也做圆角，交界处会露出两个小缺口
            // （缺口是卡片底色，深色海报上很显眼），像"海报被啃掉一块"。
            //
            // 用 `BorderImage` 做 9-slice 拉伸：上两个圆角区按原样保留、
            // 只拉伸中间那条纯色区域，因此适配任意卡片尺寸而**圆角不变形**
            // （普通 `Image` 拉伸会把圆弧拉成椭圆）。
            //
            // 具体裁切由下面的 `MultiEffect` 完成；`BorderImage` 只负责
            // 把遮罩图**按当前卡片尺寸**生成出来。
            //
            // `border` 取"圆角半径 + 1" = 9：确保 9 宫格的"角"完整包含
            // 整段圆弧，否则拉伸会切到弧线。
            //
            // **不要让它可见**：它与封面同矩形，但下两角是直角、中间纯白，
            // 画出来就是一张白板把封面盖死。它在 MultiEffect 遮罩路径下
            // 是安全的 —— 那里通过 `layer` 取纹理，不看 visible。
            BorderImage {
                id: coverMask
                anchors.fill: parent
                source: posterMaskUrl
                border { left: 9; top: 9; right: 9; bottom: 9 }
                smooth: true
                visible: false

                layer.enabled: true
                // 遮罩纹理也按设备像素生成：125%/150% 缩放下圆角边缘不发虚
                // （纯色硬边，成本可忽略）
                layer.textureSize: Qt.size(
                    Math.ceil(width * Screen.devicePixelRatio),
                    Math.ceil(height * Screen.devicePixelRatio))
            }

            // 圆角裁切：以 coverMask 的 **alpha** 为遮罩渲染封面。
            //
            // **为什么不能"干脆不用效果层"**：圆角只能靠遮罩裁，而
            // `Qt5Compat.GraphicalEffects.OpacityMask` 在本机**不存在**
            // （当前只装了 PySide6-Essentials，Qt5Compat 在 6.9 已并入
            // Addons —— 实测 qml 导入目录里没有它）。所以只剩 MultiEffect
            // 这一条路。
            //
            // **MultiEffect 不背"海报模糊"这个锅**（本轮实测澄清）：
            // 同一张封面，走 MultiEffect+遮罩与走朴素 Image，Laplacian
            // 分别是 13482 / 13467 —— 差 0.1%，等于没有差别；真应用截图
            // 也复现了同一个数（13427）。
            // 所以早先"MultiEffect 内部合成 FBO 分辨率偏低、先合成后放大
            // 导致细节糊掉"的推断，在本机（96 DPI / DPR=1）**不成立**，
            // 照着它加的 `layer.textureSize` 自然也没改善画质。
            // 真正的症结是 coverImage 缺 `sourceSize`，见上面的长注释。
            //
            // 下面这层"按 DPR 放大再缩回"的包装，在本机（DPR=1）是
            // **恒等变换**（width×1、scale÷1），不影响任何测量结果。
            // 保留它是为了 DPR>1 的显示器：MultiEffect 的中间纹理按 item
            // 的逻辑尺寸分配，DPR=2 时会被放大一次；把 item 放大到设备
            // 像素再 scale 回来，中间纹理就跟着变大。
            // —— 这一段**没有在高 DPI 屏上实测过**（手边只有 96 DPI 的
            // 屏），属于按机制推断的保险措施，别把它当成已验证的结论。
            Item {
                // 逻辑尺寸（也就是最终显示尺寸）
                readonly property int logicalWidth: coverArea.width
                readonly property int logicalHeight: coverArea.height
                readonly property real dpr: Math.max(1, Screen.devicePixelRatio)

                anchors.fill: parent
                // 效果层按设备像素放大，再整体缩回逻辑尺寸 ——
                // 于是内部所有按 width/height 分配的纹理（含遮罩合成）
                // 都是 DPR 倍分辨率，显示时是 1:1，不再糊
                width: logicalWidth * dpr
                height: logicalHeight * dpr
                scale: 1 / dpr
                transformOrigin: Item.TopLeft

                MultiEffect {
                    id: coverEffect
                    anchors.fill: parent
                    visible: coverImage.status === Image.Ready && root.coverUrl !== ""
                    source: coverImage
                    maskEnabled: true
                    maskSource: coverMask
                    // 阈值语义（Qt 文档）：alpha < min 的像素被裁掉。
                    // 遮罩透明区 alpha=0、实体区 alpha=1，0.5 是天然中间值。
                    maskThresholdMin: 0.5
                }
            }

            // 无封面占位：居中短横线（与旧版 _make_placeholder 视觉一致）
            Rectangle {
                anchors.centerIn: parent
                width: Math.round(parent.width * 0.4)
                height: Theme.lineThin
                color: Theme.borderStrong
                visible: !coverImage.visible
            }

            // 匹配状态标记
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.margins: Theme.spacingSm
                visible: root.matchState !== "auto"
                width: badgeLabel.implicitWidth + Theme.spacingMd
                height: 20
                radius: Theme.radiusSm
                color: root.matchState === "pending" ? Theme.warningColor : Theme.successColor

                Text {
                    id: badgeLabel
                    anchors.centerIn: parent
                    text: root.matchState === "pending" ? "待匹配" : "手动"
                    color: "#FFFFFF"
                    font.pixelSize: Theme.fontXs
                }
            }
        }

        // ---- 文本区 ----
        //
        // 外面包一层**带下圆角的背景矩形**（踩坑）。
        //
        // 为什么需要：卡片 `root` 设了 `radius`，但它同时开了 `clip: true`
        // —— Qt 的 `clip` 只按**轴对齐矩形**裁剪，于是子项（这层文本区）
        // 会一直画到卡片的直角边界，把卡片底部的两个圆角**盖成直角** ✗。
        // （`clip` 本身不影响"无子项"矩形的圆角，所以单看卡片是圆的，
        //   一旦有子项铺到底部就露馅。）
        //
        // 解法：让这层背景自己带上与卡片一致的**下圆角** ——
        // 它就是"卡片底部"在视觉上的实际呈现，圆角由它负责画。
        // 上两角保持直角：那里紧邻封面区，本来就该是无缝衔接。
        Rectangle {
            id: textArea
            width: parent.width
            height: root.height - root.coverHeight
            color: Theme.surfaceBg
            topLeftRadius: 0
            topRightRadius: 0
            bottomLeftRadius: Theme.radiusMd
            bottomRightRadius: Theme.radiusMd

            Column {
                anchors.fill: parent
                topPadding: 6
                leftPadding: 10
                rightPadding: 10
                spacing: 2

                Text {
                    width: parent.width - parent.leftPadding - parent.rightPadding
                    text: root.title
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontMd
                    elide: Text.ElideRight
                    maximumLineCount: 1
                }

                Text {
                    width: parent.width - parent.leftPadding - parent.rightPadding
                    text: root.meta
                    color: Theme.textTertiary
                    font.pixelSize: Theme.fontSm
                    elide: Text.ElideRight
                    maximumLineCount: 1
                }
            }
        }
    }

    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked(root.subjectId)

        ToolTip.visible: containsMouse && root.title.length > 0
        ToolTip.delay: 600
        ToolTip.text: root.title
    }
}
