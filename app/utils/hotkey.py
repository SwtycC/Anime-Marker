"""全局热键占用检测（Windows）。

**为什么需要**：设置页让用户"按出来"的回车键字符串（如 `ctrl+alt+p`）会被
注册成全局热键或作为普通按键发出去。但若该组合**已被别的软件注册**，
Windows 会在按键到达本程序窗口之前就用 `WM_HOTKEY` 拦截掉 —— 录制时
按下毫无反应，用户只会看到"按了没反应"，完全无法归因。

实测案例：`ctrl+alt+p` 在开发者机器上被占用（`RegisterHotKey` 返回
1409 `ERROR_HOTKEY_ALREADY_REGISTERED`），而 `ctrl+alt+i` 空闲。
换用后者立刻正常 —— 这类问题在代码里无从修复，只能**提前告知用户**。

做法：调用 `RegisterHotKey(None, id, mods, vk)` —— 不绑定窗口，纯探测。
成功说明当前没有进程占用该组合（随即注销，不留痕迹）；失败即被占用。

局限（必须说清，避免误导）：
  - 只能检测**以 RegisterHotKey 方式注册**的热键。低层键盘钩子
    （如某些输入法、游戏加速器、键盘映射软件）不会让探测失败，
    但仍可能吞键。
  - 无法查出**占用者是谁** —— Windows 不提供该 API。
因此返回值只是"提示"，不是"保证能用"。
"""

from __future__ import annotations

import ctypes
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ---- Win32 常量 ----
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# 组合键里允许出现的修饰键名 → Win32 标志位
_MOD_FLAGS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

# 主键名 → 虚拟键码（VK）。
#
# **与 launcher._send_keys 的使用面保持一致**：那边是把字符串原样交给
# keyboard / pyautogui，这里只用于"探测占用"，不需要覆盖全部按键 ——
# 只收常见的字母数字、功能键与少量符号，取不到的键就不做检测（返回
# None，由调用方按"未知，跳过检查"处理），而不是误报"被占用"。
_VK_NAMES: dict[str, int] = {}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK_NAMES[_c.lower()] = ord(_c)
for _d in "0123456789":
    _VK_NAMES[_d] = ord(_d)
for _i in range(1, 25):
    _VK_NAMES[f"f{_i}"] = 0x6F + _i          # VK_F1 = 0x70

_VK_NAMES.update({
    "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "minus": 0xBD, "plus": 0xBB, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF, "semicolon": 0xBA, "apostrophe": 0xDE,
    "leftbracket": 0xDB, "rightbracket": 0xDD, "backslash": 0xDC,
    "grave": 0xC0,
})


def _parse(shortcut: str) -> Optional[tuple[int, int]]:
    """把 `ctrl+alt+p` 解析成 (mods, vk)。无法解析返回 None。

    无法解析的情形（都**不做占用检测**，交给调用方忽略）：
      - 空串
      - 只有修饰键（如 `ctrl`）—— 单独修饰键不能作为全局热键
      - 不认识的键名
    """
    if not shortcut or not shortcut.strip():
        return None
    parts = [p.strip().lower() for p in shortcut.split("+") if p.strip()]
    if not parts:
        return None

    mods = 0
    keys: list[str] = []
    for p in parts:
        if p in _MOD_FLAGS:
            mods |= _MOD_FLAGS[p]
        else:
            keys.append(p)

    # 只允许**恰好一个**主键：`ctrl+alt+p` 合法；`ctrl+p+k` 不是热键写法
    if len(keys) != 1:
        return None
    # 至少要有一个修饰键，否则（如单按 `p`）注册全局热键没有意义，
    # 也会与实际使用（发单个键）语义不符
    if mods == 0:
        return None

    vk = _VK_NAMES.get(keys[0])
    if vk is None:
        return None
    return mods, vk


def is_available(shortcut: str) -> Optional[bool]:
    """检测快捷键组合是否**未被其他进程占用**。

    返回：
        True  —— 空闲（可注册，大概率能正常收到按键）
        False —— 已被占用（按下时可能被系统拦截，收不到）
        None  —— 无法判断（非 Windows / 组合无法解析 / 调用失败）

    **非 Windows 一律返回 None**：项目当前主要面向 Windows（另有 win32gui
    等依赖），但不该因此在别的平台上抛异常。
    """
    parsed = _parse(shortcut)
    if parsed is None:
        return None
    mods, vk = parsed

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (OSError, AttributeError):        # pragma: no cover - 非 Windows
        return None

    # 用 0 作为 id：RegisterHotKey 允许在同一线程内重复用同一 id 覆盖，
    # 这里紧接着就注销，不会与其他调用冲突。
    hotkey_id = 0xBEEF
    try:
        ok = user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk)
        if ok:
            user32.UnregisterHotKey(None, hotkey_id)
            return True
        err = ctypes.get_last_error()
        # 只有"已被注册"才算确定被占用；其他错误（权限、非法参数…）
        # 归入"无法判断"，避免把无关失败说成占用而误导用户
        if err == ERROR_HOTKEY_ALREADY_REGISTERED:
            return False
        log.debug("RegisterHotKey(%s) 失败 err=%s，无法判断占用", shortcut, err)
        return None
    except Exception as e:                   # pragma: no cover - 防御性
        log.debug("检测热键占用失败 %s: %s", shortcut, e)
        return None
