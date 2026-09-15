"""跨模块共享的布局常量。

集中定义可避免「主窗口反推窗口宽度」与「海报墙实际排布」两处参数漂移。
"""

from __future__ import annotations

# 海报墙
POSTER_SPACING = 16      # 卡片之间的水平/垂直间距
POSTER_MARGIN = 24       # 内容区四周留白
POSTER_COLUMNS = 5       # 初始窗口恰好容纳的列数

# 悬浮导航
NAV_BUTTON_SIZE = 40     # 圆形按钮直径
NAV_ICON_SIZE = 22       # 图标逻辑尺寸
NAV_BOTTOM_MARGIN = 20   # 胶囊距窗口底边的留白
NAV_PILL_HEIGHT = 54     # 胶囊高度（6+40+6 内边距）
NAV_CONTENT_GUTTER = NAV_PILL_HEIGHT + NAV_BOTTOM_MARGIN + 12  # = 86
