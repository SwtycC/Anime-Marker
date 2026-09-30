# resources/icons/

界面图标的 SVG 资源目录。

所有图标都是**单色白色蒙版**，颜色由运行时按主题染色（见下方「约定」）。
基地址由 Python 侧注入为 `iconsBaseUrl`（见 `QmlApp`），QML 侧拼上
`<名字>.svg` 即为完整地址。

## 图标清单

`NavIcon.qml` 按 `kind` 映射到文件名；另有几个组件直接拼文件名使用。

| 文件 | 画布 | 绘制方式 | 使用位置 |
| --- | --- | --- | --- |
| `items-grid.svg` | 16×16 | 填充 | `NavIcon.kind="grid"` — 导航栏 · 海报墙 |
| `play.svg` | 16×16 | 填充 | `NavIcon.kind="play"` — 导航栏 · 在看 |
| `clock.svg` | 16×16 | 填充 | `NavIcon.kind="clock"` — 导航栏 · 动态 |
| `rss.svg` | 16×16 | 填充 | `NavIcon.kind="rss"` — 导航栏 · 订阅 |
| `settings.svg` | 16×16 | 填充 | `NavIcon.kind="gear"` — 导航栏 · 设置 |
| `search.svg` | 24×24 | 填充 | `NavIcon.kind="search"`、`SearchPill.qml` |
| `chevron-down.svg` | 16×16 | 描边 1.5 | `NavIcon.kind="chevron"`、`TagFilterPill.qml`（展开后旋转 180°）|
| `link.svg` | 16×16 | 填充 | `NavIcon.kind="link"` — 详情页标题右侧「在 Bangumi 查看」|
| `pen.svg` | 16×16 | 填充 | 详情页 · 标签编辑按钮（`DetailButton.iconSource`）|
| `arrow-left.svg` | 16×16 | 填充 | `NavIcon.kind="back"` / `kind="up"`、`BackButton.qml` |
| `arrow-up-bold.svg` | 16×16 | 描边 2.4 | `NavIcon.kind="upBold"` — 「返回顶部」悬浮按钮 |
| `plus.svg` | 16×16 | 描边 2.4 | `NavIcon.kind="plus"` — 「添加动漫」悬浮按钮 |
| `refresh.svg` | 16×16 | 描边 1.5 | `AddButton.qml`（扫描转圈）、详情页「重新扫描」按钮 |
| `close-small.svg` | 16×16 | 填充 | 备用（当前无引用）|

> 本目录只放 **SVG**。程序图标（`.ico`）与封面占位图（`.png`）不在
> 这里 —— 封面圆角遮罩在 `resources/poster_mask.png`。

## 约定

### 1. 颜色一律用白色

`fill="#FFFFFF"`（填充式）或 `stroke="#FFFFFF"`（描边式），颜色**不写进
文件**，运行时由 `MultiEffect` 的 `colorization` 按主题染色。

**fill / stroke 必须是白色，不能是黑色**（踩坑记录）：

`MultiEffect` 的 colorization 实际算法是

```
结果色 = colorizationColor × 源亮度
```

而不是「把源涂成 colorizationColor」。源是黑色时亮度为 0 —— 无论传什么
主题色，染出来都是**纯黑**。表现是整个导航栏图标常年黑色（既不跟随主题
色，也看不出「选中变白」），而 QML 侧**不报任何错**，极难定位。

染色只改颜色、不碰透明度，因此带洞的复合路径（齿轮中心孔、RSS 的圆弧
缺口）中心仍保持镂空。

### 2. 两种绘制方式都可用

| 方式 | 适用 | 例子 |
| --- | --- | --- |
| `fill="#FFFFFF"` 的**填充路径** | 有粗细变化的图形、带镂空的复合路径 | `items-grid`、`settings`、`link` |
| `fill="none"` + `stroke="#FFFFFF"` 的**描边路径** | 均匀线宽、圆头圆角 | `plus`、`arrow-up-bold`、`refresh`、`chevron-down` |

两者都能被 colorization 正确染色（白色源亮度为 1）。描边式更适合
「加粗」需求：改 `stroke-width` 即可，不必重画路径。

### 3. 画布尺寸

统一 **16×16**，`search.svg` 为 24×24。`Image` 用等比缩放（`PreserveAspectFit`），
画布不同不影响显示效果。

图标在 QML 里按需放大（如 「返回顶部」用到 20px），因此 SVG 用**矢量**
路径而不是位图；`NavIcon` 里 `sourceSize` 设为 64 是为了先放大解码、缩小后
边缘更干净。

### 4. 风格

线性图标：无填充块面、无阴影、单色（运行时染色）。新增图标请与现有
风格保持一致。

## 复用与特例

- **`kind="up"` 复用 `arrow-left.svg` + 旋转 90°**，而不是另存一份
  `arrow-up.svg`：arrow-left 是一条手工调过的贝塞尔路径，直接改路径容易
  画歪，而顺时针转 90° 在几何上严格等价于「向上」。
  旋转角度由 `NavIcon.iconRotation` 给出。
- **有加粗需求时另开文件**（如 `arrow-up-bold.svg`）：`arrow-left.svg` 被
  导航栏返回按钮共用，直接改粗会连带影响它。
- **属性名不能叫 `rotation`**：`Item` 自身已有内置 `rotation`，同名会让
  整个文件加载失败（报 "rotation is a read-only property"），并连带所有
  用到 `NavIcon` 的页面全部不可用。故用 `iconRotation`。
