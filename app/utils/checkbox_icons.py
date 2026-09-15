"""生成 QSS 用对勾 SVG 图标（复选框 ::indicator:checked 的 image）。

QSS 无法直接绘制对勾，只能通过 image: 引用文件；这里在运行时把 SVG
写到用户数据目录（打包后为 %APPDATA%），避免依赖 resources 打进 exe。

对应 Uiverse 设计：勾形为旋转 45° 的 L 形描边，此处用 path 等价实现。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# QSS 的 url() 对非 ASCII 路径支持不稳（如项目路径含中文），
# 统一生成到系统临时目录下的纯 ASCII 路径
_ICON_DIR = Path(tempfile.gettempdir()) / "anime_marker_icons"

# 14x14 视口，勾形与 Uiverse 版比例一致（短边 0.25em / 长边 0.5em 相对 1.3em）
_CHECK_PATH = "M3.2 7.4 L5.8 10 L10.8 4.2"

_SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">
  <path d="{path}" fill="none" stroke="{color}" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""

# 各风格选中态对勾颜色：填充色为浅底时用白勾，深底时用深勾
CHECK_COLORS = {
    "light": "#FFFFFF",   # 配 #2563EB / #111111 填充
    "dark": "#111111",    # 配 #FAFAFA 填充
}

# 加减号的描边颜色（浅色系深灰 / 深色系浅灰）
SPIN_SYMBOL_COLORS = {
    "light": "#5F5F5F",
    "dark": "#AAAAAA",
}

# 加减号：一条水平线（+ 再补一条竖线），14x14 视口居中
_PLUS_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">
  <path d="M2.5 6 H9.5 M6 2.5 V9.5" fill="none" stroke="{color}"
        stroke-width="1.4" stroke-linecap="round"/>
</svg>
"""
_MINUS_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">
  <path d="M2.5 6 H9.5" fill="none" stroke="{color}"
        stroke-width="1.4" stroke-linecap="round"/>
</svg>
"""


def ensure_check_icons() -> dict[str, str]:
    """生成对勾 SVG，返回 {key: 可写进 QSS 的路径}。

    - 输出到系统临时目录（纯 ASCII），规避 QSS url() 中文路径失效
    - 路径统一用正斜杠，避免 Windows 反斜杠被 QSS 当转义符
    """
    out_dir = _ICON_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("创建图标目录失败 %s: %s", out_dir, e)
        return {}

    result: dict[str, str] = {}
    for key, color in CHECK_COLORS.items():
        fp: Path = out_dir / f"check_{key}.svg"
        svg = _SVG_TEMPLATE.format(path=_CHECK_PATH, color=color)
        try:
            # 内容固定，已存在且一致则跳过重写
            if not fp.exists() or fp.read_text(encoding="utf-8") != svg:
                fp.write_text(svg, encoding="utf-8")
            result[key] = fp.as_posix()
        except OSError as e:
            log.warning("写入对勾图标失败 %s: %s", fp, e)
    return result


def ensure_spin_icons() -> dict[str, str]:
    """生成加减号 SVG，返回 {"plus_light": path, "minus_light": path, ...}。

    用于 QSpinBox::up-arrow / down-arrow——QSS 的 border 三角技巧在
    小尺寸下容易被渲染成一条粗线，改用 image 引用 SVG 更可靠。
    """
    out_dir = _ICON_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("创建图标目录失败 %s: %s", out_dir, e)
        return {}

    result: dict[str, str] = {}
    for key, color in SPIN_SYMBOL_COLORS.items():
        for name, tpl in (("plus", _PLUS_SVG), ("minus", _MINUS_SVG)):
            fp: Path = out_dir / f"spin_{name}_{key}.svg"
            svg = tpl.format(color=color)
            try:
                if not fp.exists() or fp.read_text(encoding="utf-8") != svg:
                    fp.write_text(svg, encoding="utf-8")
                result[f"{name}_{key}"] = fp.as_posix()
            except OSError as e:
                log.warning("写入加减号图标失败 %s: %s", fp, e)
    return result
