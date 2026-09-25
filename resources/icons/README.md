# resources/icons/

放置图标资源：

- `app.ico` — 程序图标（PyInstaller `--icon` 与窗口图标）
- `placeholder_cover.png` — 封面下载失败时的占位图（建议 300×424 灰阶线性占位）

## 导航栏图标（SVG）

悬浮导航栏的图标，`NavIcon.qml` 按名字加载（基地址由 Python 侧注入，
见 `QmlApp` 的 `iconsBaseUrl`）：

| 文件 | 对应位置 |
| --- | --- |
| `items-grid.svg` | 导航栏 · 海报墙 |
| `play.svg` | 导航栏 · 在看 |
| `clock.svg` | 导航栏 · 动态 |
| `rss.svg` | 导航栏 · 订阅 |
| `settings.svg` | 导航栏 · 设置 |
| `search.svg` | 海报墙搜索胶囊（`SearchPill.qml`，24×24 画布）|
| `pen.svg` | 详情页 · 标签编辑按钮 |
| `close-small.svg` | 备用 |

约定：

- 画布 **16×16**（`search.svg` 为 24×24，`Image` 用等比缩放，不影响）、
  `fill="#FFFFFF"` 的**填充式**路径 —— 颜色不写进文件，运行时由
  `MultiEffect` 的 `colorization` 按主题染色。
- **fill 必须是白色，不能是黑色**（踩坑记录）：`MultiEffect` 的
  colorization 实际算法是 `结果色 = colorizationColor × 源亮度`，
  黑色源亮度为 0 —— 无论传什么主题色，染出来都是**黑**的。
  表现是整个导航栏图标常年黑色（既不跟随主题色，也看不出"选中变白"）。
  染色只改颜色、不碰透明度，因此带洞的复合路径中心仍镂空。
- 图标全部来自这一套，风格统一；`NavIcon.qml` 只是
  「加载 + 染色」的薄封装，不再持有自绘几何。

> 视觉要求（§5.8.0）：线性图标，无填充块面、无阴影、单色（运行时染色）。
