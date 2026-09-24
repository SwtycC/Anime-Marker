# resources/icons/

放置图标资源：

- `app.ico` — 程序图标（PyInstaller `--icon` 与窗口图标）
- `placeholder_cover.png` — 封面下载失败时的占位图（建议 300×424 灰阶线性占位）

## 导航栏图标（SVG）

悬浮导航栏的图标，`NavIcon.qml` 按名字加载（基地址由 Python 侧注入，
见 `QmlApp` 的 `iconsBaseUrl`）：

| 文件 | 对应导航项 |
| --- | --- |
| `items-grid.svg` | 海报墙 |
| `play.svg` | 在看 |
| `clock.svg` | 动态 |
| `rss.svg` | 订阅 |
| `settings.svg` | 设置 |
| `pen.svg` | （备用，尚未使用）|

约定：

- 画布 **16×16**、`fill="#000"` 的**填充式**路径 —— 颜色不写进文件，
  运行时由 `MultiEffect` 的 `colorization` 按主题染色
  （只改颜色、不碰透明度，因此带洞的复合路径中心仍镂空）。
- 五个导航图标全部来自这一套，风格统一；`NavIcon.qml` 只是
  「加载 + 染色」的薄封装，不再持有自绘几何。

> 视觉要求（§5.8.0）：线性图标，无填充块面、无阴影、单色（运行时染色）。
