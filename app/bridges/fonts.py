"""字体桥接：内置字体、上传字体、下拉列表，以及界面字体的即时应用。

两类字体分开存放：

- **内置**：`resources/fonts/...`，随发行包分发，只读；
- **用户上传**：`paths.fonts_dir()`（开发态 `data/fonts`，
  打包态 `%APPDATA%/AnimeMarker/fonts`）。放用户数据目录而不是程序目录：
  Program Files 下通常不可写，且升级 / 重装不应丢掉用户添加的字体。

配置键 `ui.font_family` 存的是**字体族名**（QFontDatabase 返回的
family），空串 = 系统默认。存族名而不是文件路径：Qt 的 `font.family`
只认族名；同一族无论从哪个文件加载都能命中。

即时生效的做法见 `apply_font()`：Qt 的 `QGuiApplication.setFont()`
**只影响之后创建的 QML 项**，
所以切换时必须再遍历一遍窗口项树，逐个改 `font.family`。
"""

from __future__ import annotations

import filecmp
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, Slot
from PySide6.QtGui import QFont, QFontDatabase, QRawFont
from PySide6.QtQuick import QQuickItem, QQuickWindow
from shiboken6 import getCppPointer
from PySide6.QtWidgets import QApplication, QFileDialog

from app.core.config import Config
from app.utils.paths import fonts_dir, resource_path

log = logging.getLogger(__name__)

# 配置里表示「系统默认」的值（空串）
SYSTEM_FAMILY = ""
SYSTEM_LABEL = "系统默认"

# 内置字体：相对 resources/ 的路径 + 下拉菜单里的显示名。
#
# 显示名不取 QFontDatabase 返回的族名（那是英文 "LXGW WenKai GB Lite"），
# 用户要求显示中文名。
BUILTIN_FONT_REL = "fonts/LxgwWenkaiGB-Lite/LXGWWenKaiGBLite-Regular.ttf"
BUILTIN_FONT_LABEL = "霞鹜文楷 GB 轻便版"

# 允许上传的字体后缀（Qt 的 addApplicationFont 支持这三种）
FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}

# 用户自定义显示名（下拉菜单里显示的名字）的元数据。
# 与字体文件一起放在用户字体目录；文件名不进 FONT_SUFFIXES，扫描时会被忽略。
# 格式：{"version": 1, "fonts": {"<文件名>": {"name": "...", "family": "..."}}}
META_FILENAME = "fonts.json"


def _cache_key(path: Path) -> str:
    """用于去重缓存的文件键（Windows 大小写不敏感）。"""
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:  # pragma: no cover - 防御性
        return os.path.normcase(str(path))


class FontBridge(QObject):
    """字体相关能力（QML 侧名字：`fontBridge`）。"""

    def __init__(self, config: Config,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._config = config

        # 系统默认字体（必须在应用任何自定义字体**之前**抓取）。
        # QmlApp.run() 里调用 prepare() 时才有 QApplication，所以这里先留空。
        self._system_font: Optional[QFont] = None

        # addApplicationFont 是进程级注册：按文件路径缓存结果，
        # 避免每次刷新下拉列表都重复注册（会不断产生重复族条目）。
        self._registered: set[str] = set()
        self._families: dict[str, list[str]] = {}

        self._builtin_family: str = ""
        self._builtin_checked: bool = False

        # 上次实际应用的字体族。用于跳过"字体没变"的重复遍历：
        # 每次点保存都会走 _rebuild_services → apply_saved()，若配置里
        # 字体没改，没必要把 1600+ 个 QML 项重新写一遍。
        self._applied_family: Optional[str] = None

    # ------------------------------------------------------------------
    # 启动 / 应用
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        """启动时调用：抓取系统默认字体、注册字体、应用配置里的字体。

        **必须在 `engine.load()` 之前调用**：QML 项在创建时读取
        `QGuiApplication.font()`，早一步设置好，首帧就是正确字体。
        """
        app = QApplication.instance()
        if app is None:  # pragma: no cover - 只会在异常调用顺序下发生
            log.warning("FontBridge.prepare() 时 QApplication 尚未创建")
            return

        if self._system_font is None:
            # 保存一份原始字体：切回「系统默认」时用它恢复，
            # 而不是用 QFont() 猜（不同平台 / 主题下的默认值不一样）。
            self._system_font = QFont(app.font())

        self.register_fonts()
        self.apply_font(self._saved_family(), walk=False)

    def register_fonts(self) -> None:
        """注册内置字体与用户上传目录里的全部字体（幂等）。"""
        if not self._builtin_checked:
            self._builtin_checked = True
            path = resource_path(BUILTIN_FONT_REL)
            if path.is_file():
                families = self._register_file(path)
                if families:
                    self._builtin_family = families[0]
                else:
                    log.warning("内置字体无法注册：%s", path)
            else:
                # 打包时漏了 resources/fonts 会出现这条 —— 界面仍可用，
                # 只是下拉菜单里没有内置字体。
                log.warning("内置字体文件缺失：%s", path)

        for path in self._user_font_files():
            self._register_file(path)

    def apply_saved(self) -> str:
        """读取配置并应用字体（设置页点「保存」后走这里）。"""
        return self.apply_font(self._saved_family(), walk=True)

    def apply_font(self, family: str, walk: bool = True) -> str:
        """应用字体族；未知族回退系统默认。返回实际生效的族名。

        `walk=True` 会把字族写进**已经存在**的每个 QML 项（Text /
        Control 都有 `font` 属性）。这是「点保存后立即生效」的关键：
        `QApplication.setFont()` 对已创建的项不生效。
        """
        app = QApplication.instance()
        if app is None:  # pragma: no cover - 防御性
            return ""

        if self._system_font is None:
            self._system_font = QFont(app.font())

        family = (family or "").strip()
        if family:
            known = {item["family"] for item in self.availableFonts()
                     if item["family"]}
            if family not in known:
                log.warning("配置的字体 %r 不存在（文件可能已被删除），"
                            "回退系统默认", family)
                family = ""

        target = family or self._system_font.family()

        # 字体没变就什么都不做。启动时 prepare() 已经用同一个字体建好了
        # 所有 QML 项；保存其它设置时再走一遍整棵项树纯属浪费
        # （1600+ 项在慢机器上要一百多毫秒）。
        changed = self._applied_family != target
        if not changed:
            return target

        # 之后新创建的 QML 项（如刚打开的弹窗、下拉代理项）直接继承这个字体
        font = QFont(self._system_font)
        font.setFamily(target)
        app.setFont(font)

        if walk:
            self._apply_to_windows(target)

        self._applied_family = target
        log.info("界面字体已应用：%s", target)
        return target

    def _apply_to_windows(self, family: str) -> None:
        """遍历所有已打开的 Qt Quick 窗口，逐个项改 font.family。"""
        seen: set[int] = set()
        for window in QApplication.allWindows():
            if not isinstance(window, QQuickWindow):
                continue
            try:
                self._walk_fonts(window, family, seen)
                window.update()
            except Exception as e:  # pragma: no cover - 防御性
                log.warning("刷新窗口字体失败：%s", e)

    def _walk_fonts(self, item: Any, family: str,
                    seen: set[int]) -> None:
        """递归设置 font.family，保留字号 / 字重等其余属性。

        只改子属性 `family`，不动整个 font 对象里的其它字段，
        所以主题切换后字号仍会正常联动。

        **必须同时走两棵树**：

        - `childItems()`（可视子项）：Repeater / Loader / 组件实例化出来的
          项，QObject 父级与可视父级不是同一套关系。海报墙 106 张卡片的
          文字**只在这棵树里**；只看 `children()` 时 1630 个带 font 的项
          只改到了 58 个，表现为「设置了字体，海报墙却迟迟不变」。
        - `children()`（QObject 子级）：尚未打开的 Dialog / Popup 的内部
          控件还没进可视树，只能从这里找；漏掉的话，切完字体再打开这些
          弹窗会看到旧字体。

        两棵树有大量重叠，所以用 C++ 指针去重（见下），否则深树会被
        重复遍历。
        """
        if item is None:
            return

        # 同一条 C++ 对象可能既在可视树里、又在 QObject 树里；用
        # C++ 指针去重，避免同一个子树被走两遍（深度大时会指数级重复）。
        try:
            key = int(getCppPointer(item)[0])
        except Exception:  # pragma: no cover - 防御性
            key = id(item)
        if key in seen:
            return
        seen.add(key)

        try:
            # 没有 font 属性的 QObject 会返回 None，不必先查 metaObject
            current = item.property("font")
            if isinstance(current, QFont) and current.family() != family:
                updated = QFont(current)
                updated.setFamily(family)
                item.setProperty("font", updated)
        except Exception:  # pragma: no cover - 单项失败不影响整窗
            pass

        children: list[Any] = []
        try:
            # ① 可视子项：海报卡片的 Repeater 代理只在这一棵树里
            if isinstance(item, QQuickItem):
                children.extend(item.childItems())
            # ② QObject 子级：未打开的 Dialog / Popup 的内部控件
            #    还没进可视树，只能从这里找
            children.extend(item.children())
        except Exception:  # pragma: no cover - 防御性
            pass
        for child in children:
            self._walk_fonts(child, family, seen)

    # ------------------------------------------------------------------
    # QML 接口
    # ------------------------------------------------------------------
    @Slot(result="QVariantList")
    def availableFonts(self) -> list[dict]:
        """下拉菜单数据：系统默认 + 内置 + 用户上传（按族名去重）。

        用户上传的条目额外带：
            uploaded = true    —— 下拉里才显示「设置 / 删除」
            id       = 文件名  —— 设置 / 删除的目标（不是绝对路径）
            name     = 用户自定义显示名，没设过就用字体族名
        """
        self.register_fonts()
        meta = self._load_metadata()

        out: list[dict] = [
            {"name": SYSTEM_LABEL, "family": SYSTEM_FAMILY,
             "builtin": False, "uploaded": False, "id": ""}
        ]
        seen: set[str] = set()

        if self._builtin_family:
            out.append({
                "name": BUILTIN_FONT_LABEL,
                "family": self._builtin_family,
                "builtin": True,
                "uploaded": False,
                "id": "",
            })
            seen.add(self._builtin_family)

        for path in self._user_font_files():
            families = self._register_file(path)
            if not families:
                continue
            family = families[0]
            if family in seen:
                continue
            seen.add(family)
            info = meta.get(path.name, {})
            custom = str(info.get("name") or "").strip()
            out.append({
                "name": custom or family,
                "family": family,
                "builtin": False,
                "uploaded": True,
                "id": path.name,
            })
        return out

    # ---------- 上传 / 重命名 / 删除（设置页小窗用）----------
    @Slot(result="QVariantMap")
    def pickFontFile(self) -> dict:
        """只弹文件选择框，**不添加**。

        返回：
            {"ok": true, "path": ..., "name": <默认显示名>, "family": ...}
            {"ok": false, "cancelled": true, ...}

        默认显示名优先取字体内部族名（不用先注册，QRawFont 直接读文件），
        读不出来才退回文件名。用户随后可以在小窗里改成任意名字。
        """
        path, _ = QFileDialog.getOpenFileName(
            None,
            "选择要添加的字体",
            "",
            "字体文件 (*.ttf *.otf *.ttc);;所有文件 (*)",
        )
        if not path:
            return {"ok": False, "cancelled": True,
                    "path": "", "name": "", "family": ""}

        family = self._read_family_name(path)
        return {
            "ok": True,
            "cancelled": False,
            "path": path,
            "family": family,
            "name": family or Path(path).stem,
        }

    @Slot(str, str, result="QVariantMap")
    def addFont(self, source: str, display_name: str = "") -> dict:
        """把小窗里选好的字体复制进来，并按用户填的名字登记。"""
        return self.addFontFile(source, display_name)

    def addFontFile(self, source: str, display_name: str = "") -> dict:
        """从指定路径添加字体（`addFont` 的实现，也便于自动化验证）。"""
        src = Path(source)
        if not src.is_file():
            return self._fail("文件不存在")
        if src.suffix.lower() not in FONT_SUFFIXES:
            return self._fail("只支持 .ttf / .otf / .ttc 字体文件")

        try:
            dest, created = self._copy_into_fonts_dir(src)
        except OSError as e:
            log.warning("复制字体失败：%s → %s：%s", src, fonts_dir(), e)
            return self._fail(f"复制字体失败：{e}")

        # 可能是同名文件、之前注册失败过 —— 清掉缓存后重新注册
        key = _cache_key(dest)
        self._registered.discard(key)
        self._families.pop(key, None)

        families = self._register_file(dest)
        if not families:
            # 本次新复制进来的无效文件别留在目录里，否则每次启动都白扫一遍。
            # **只删自己复制的那份**：用户可能直接把文件选在字体目录里，
            # 那种情况下 dest 就是他原来的文件，删掉等于毁了他的数据。
            if created:
                try:
                    dest.unlink()
                except OSError:  # pragma: no cover - 防御性
                    pass
            return self._fail("无法从该文件读取字体，请确认它是有效的字体文件")

        family = families[0]
        name = self._clean_name(display_name) or family
        self._remember_name(dest, name, family)
        log.info("已添加字体：%s（显示名 %s，%s）", family, name, dest)
        return {"ok": True, "cancelled": False,
                "family": family, "name": name, "id": dest.name,
                "message": "字体已添加"}

    @Slot(str, str, result="QVariantMap")
    def renameFont(self, font_id: str, display_name: str) -> dict:
        """修改用户上传字体的显示名（只改下拉里的叫法，不动字体文件）。"""
        path = self._safe_font_path(font_id)
        if path is None:
            return self._fail("字体文件不存在")

        families = self._register_file(path)
        meta = self._load_metadata()
        info = meta.get(path.name, {})
        family = (families[0] if families else "") or str(
            info.get("family") or "")
        if not family:
            return self._fail("无法读取该字体的族名")

        name = self._clean_name(display_name) or family
        meta[path.name] = {"name": name, "family": family}
        if not self._save_metadata(meta):
            return self._fail("无法保存字体名称（详见日志）")

        log.info("字体已重命名：%s → %s（%s）", family, name, path.name)
        return {"ok": True, "cancelled": False,
                "id": path.name, "family": family, "name": name,
                "message": "字体名称已保存"}

    @Slot(str, result="QVariantMap")
    def deleteFont(self, font_id: str) -> dict:
        """删除用户上传的字体文件与显示名记录。

        **只删字体目录里的那一份**（`_safe_font_path` 拒绝任何带路径
        分隔符的 id），不会碰到内置字体或系统字体。
        """
        path = self._safe_font_path(font_id)
        if path is None:
            return self._fail("字体文件不存在")

        families = self._register_file(path)
        meta = self._load_metadata()
        info = meta.get(path.name, {})
        family = (families[0] if families else "") or str(
            info.get("family") or "")
        name = str(info.get("name") or "").strip() or family or path.stem

        try:
            path.unlink()
        except OSError as e:
            log.warning("删除字体文件失败：%s：%s", path, e)
            return self._fail(f"无法删除字体文件：{e}")

        # 让同名文件以后能重新注册（进程内 addApplicationFont 无法卸载，
        # 但至少清掉我们自己的缓存，重新上传时会重新走注册流程）
        key = _cache_key(path)
        self._registered.discard(key)
        self._families.pop(key, None)

        meta.pop(path.name, None)
        if meta:
            self._save_metadata(meta)
        else:
            try:
                self._metadata_path().unlink()
            except OSError:
                pass

        log.info("字体已删除：%s（显示名 %s）", family or path.name, name)
        return {"ok": True, "cancelled": False,
                "id": path.name, "family": family, "name": name,
                "message": "字体已删除"}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _fail(message: str) -> dict:
        return {"ok": False, "cancelled": False,
                "family": "", "name": "", "id": "", "message": message}

    # ---- 自定义显示名（fonts.json）----
    def _metadata_path(self) -> Path:
        return fonts_dir() / META_FILENAME

    def _load_metadata(self) -> dict[str, dict[str, str]]:
        path = self._metadata_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("读取字体名称记录失败（将忽略）：%s", e)
            return {}
        fonts = raw.get("fonts") if isinstance(raw, dict) else None
        if not isinstance(fonts, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for key, value in fonts.items():
            if isinstance(value, dict):
                out[str(key)] = {
                    "name": str(value.get("name") or "").strip(),
                    "family": str(value.get("family") or "").strip(),
                }
            elif isinstance(value, str):
                # 兼容更早的简单格式（{"文件名": "显示名"}）
                out[str(key)] = {"name": value.strip(), "family": ""}
        return out

    def _save_metadata(self, meta: dict[str, dict[str, str]]) -> bool:
        path = self._metadata_path()
        try:
            path.write_text(
                json.dumps({"version": 1, "fonts": meta},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except OSError as e:
            log.warning("保存字体名称记录失败：%s", e)
            return False

    def _remember_name(self, path: Path, name: str, family: str) -> None:
        meta = self._load_metadata()
        meta[path.name] = {"name": name, "family": family}
        if not self._save_metadata(meta):
            # 名称记录写不进去不算添加失败：字体本身已经可用，
            # 只是重启后会退回族名显示。
            log.warning("字体已添加，但自定义名称未能保存：%s", path.name)

    @staticmethod
    def _clean_name(name: str) -> str:
        # 单行显示名：把换行 / 连续空白压成一个空格
        return " ".join(str(name or "").split())

    @staticmethod
    def _read_family_name(path: str) -> str:
        """读字体内部族名（不注册到进程里）。失败返回空串。"""
        try:
            raw = QRawFont(path, 16.0)
            if raw.isValid():
                return str(raw.familyName() or "")
        except Exception as e:  # pragma: no cover - 防御性
            log.debug("读取字体族名失败：%s：%s", path, e)
        return ""

    @staticmethod
    def _safe_font_path(font_id: str) -> Optional[Path]:
        """把 QML 传来的 id 限制成「字体目录下的一个文件名」。

        拒绝任何带路径分隔符 / `..` 的输入，防止通过设置 / 删除接口
        碰到目录外的文件。
        """
        raw = str(font_id or "").strip()
        if not raw or Path(raw).name != raw:
            return None
        candidate = Path(raw)
        if candidate.suffix.lower() not in FONT_SUFFIXES:
            return None
        path = fonts_dir() / raw
        try:
            if not path.is_file():
                return None
            if path.resolve().parent != fonts_dir().resolve():
                return None
        except OSError:  # pragma: no cover - 防御性
            return None
        return path

    def _saved_family(self) -> str:
        try:
            return (self._config.get("ui", "font_family", "") or "").strip()
        except Exception:  # pragma: no cover - 防御性
            return ""

    @staticmethod
    def _user_font_files() -> list[Path]:
        try:
            return sorted(
                (p for p in fonts_dir().iterdir()
                 if p.is_file() and p.suffix.lower() in FONT_SUFFIXES),
                key=lambda p: p.name.lower(),
            )
        except OSError as e:  # pragma: no cover - 防御性
            log.warning("扫描用户字体目录失败：%s", e)
            return []

    def _register_file(self, path: Path) -> list[str]:
        """注册一个字体文件并返回族名列表（同一路径只注册一次）。"""
        key = _cache_key(path)
        if key in self._registered:
            return self._families.get(key, [])

        families: list[str] = []
        try:
            font_id = QFontDatabase.addApplicationFont(str(path))
        except Exception as e:  # pragma: no cover - 防御性
            log.warning("注册字体异常：%s：%s", path, e)
            font_id = -1

        if font_id >= 0:
            for family in QFontDatabase.applicationFontFamilies(font_id):
                if family and family not in families:
                    families.append(family)

        if not families:
            log.warning("字体文件未返回可用族名：%s", path)

        self._registered.add(key)
        self._families[key] = families
        return families

    @staticmethod
    def _copy_into_fonts_dir(src: Path) -> tuple[Path, bool]:
        """复制到用户字体目录；重名且内容不同时追加序号，避免覆盖。

        返回 `(目标路径, 是否为新复制的文件)`。第二项用于失败清理：
        只有本次复制进来的才允许删，见 addFontFile。
        """
        directory = fonts_dir()
        dest = directory / src.name
        if dest.exists() and filecmp.cmp(src, dest, shallow=False):
            return dest, False

        stem, suffix = src.stem, src.suffix
        index = 1
        while dest.exists():
            dest = directory / f"{stem}-{index}{suffix}"
            index += 1
        shutil.copy2(src, dest)
        return dest, True
