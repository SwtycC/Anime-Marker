"""播放器档案：当前播放器的「路径 + 全屏快捷键」。

**为什么两项放在一起**：全屏快捷键是**播放器自己**定义的
（PotPlayer `alt+enter`、mpv `enter`、VLC `f`…），路径和它对不上就会出现
"按了没反应 → 插帧一直不开"。因此这两项**成对保存、成对读取**。

配置存法（config.ini，section `player`）：

    [player]
    path = C:\\Program Files\\DAUM\\PotPlayer\\PotPlayerMini64.exe
    fullscreen = alt+enter

只有一组值：当前用哪个播放器，就存它的路径和全屏键。
**目前只支持 PotPlayer** —— 进度读取（`core/monitor.py` 的 WM_USER 接口）
与窗口定位（`find_potplayer_hwnd`）都绑死了 PotPlayer，换别的播放器
只能播、不会自动记进度。等真正适配多播放器时再引入"档案"这一层，
现在不预留（避免存一堆用不上的键）。
"""

from __future__ import annotations

#: 配置节名
SECTION = "player"
#: 播放器 exe 路径
KEY_PATH = "path"
#: 该播放器自己的「全屏 / 还原」快捷键
KEY_FULLSCREEN = "fullscreen"


def resolve(config) -> tuple[str, str]:
    """取 (路径, 全屏快捷键)。

    直接读配置，不做任何回退/猜测：缺项就是空串，由调用方
    （`PlayerLauncher`）报「路径未配置」。空串不会被静默替换成
    某个默认路径 —— 那会让用户以为配置生效了。
    """
    path = (config.get(SECTION, KEY_PATH, "") or "").strip()
    fullscreen = (config.get(SECTION, KEY_FULLSCREEN, "") or "").strip()
    return path, fullscreen
