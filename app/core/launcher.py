"""播放器 + Lossless Scaling 启动器。

一次 play() 完成：起 LS（可选）→ 起 PotPlayer → **等播放器窗口就位并置前**
→ 发插帧快捷键。

**顺序为什么是这样（实测踩坑）**：小黄鸭的插帧快捷键只对**当前前台窗口**
生效，所以必须"播放器已经在前台"之后再发。早期实现是
`起 LS → 等 5s → 发快捷键 → 起播放器`，快捷键必然落在播放器之外 ——
表现就是"小黄鸭起来了，但没开缩放/补帧"。
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import win32gui

log = logging.getLogger(__name__)


class LauncherError(RuntimeError):
    pass


def _check_exists(path: str, label: str) -> None:
    if not path:
        raise LauncherError(f"{label} 路径未配置")
    if not Path(path).exists():
        raise LauncherError(f"{label} 不存在：{path}")


class PlayerLauncher:
    def __init__(
        self,
        player_path: str,
        ls_path: str = "",
        enable_ls: bool = True,
        ls_shortcut: str = "ctrl+alt+p",   # 须与小黄鸭一致；别用 ctrl+alt+l（QQ 锁定键）
        ls_start_delay: float = 5.0,
        player_start_delay: float = 1.0,
    ) -> None:
        self.player_path = player_path
        self.ls_path = ls_path
        self.enable_ls = enable_ls
        self.ls_shortcut = ls_shortcut
        self.ls_start_delay = ls_start_delay
        self.player_start_delay = player_start_delay

    def play(self, video_path: str) -> subprocess.Popen:
        """启动 LS（可选） + PotPlayer，返回 PotPlayer 进程句柄。"""
        _check_exists(self.player_path, "PotPlayer")
        if not Path(video_path).exists():
            raise LauncherError(f"视频文件不存在：{video_path}")

        # 小黄鸭先起（它启动慢，且起来后不抢前台），但**快捷键留到最后发**
        if self.enable_ls and self.ls_path:
            self._start_ls()

        time.sleep(max(0.0, self.player_start_delay))
        proc = subprocess.Popen([self.player_path, video_path])
        log.info("PotPlayer 已启动 pid=%s file=%s", proc.pid, video_path)

        if self.enable_ls and self.ls_path:
            self._activate_ls(proc)

        return proc

    def _start_ls(self) -> None:
        """只启动小黄鸭并等它就绪 —— **不发快捷键**（此时还没有前台播放窗口）。"""
        _check_exists(self.ls_path, "Lossless Scaling")
        subprocess.Popen([self.ls_path])
        log.info("Lossless Scaling 已启动，等待 %ss 就绪", self.ls_start_delay)
        time.sleep(max(0.0, self.ls_start_delay))

    def _activate_ls(self, proc) -> None:
        """等播放器窗口就位 → 置前 → 发插帧快捷键。"""
        hwnd = self._wait_player_window(proc)
        if hwnd:
            try:
                win32gui.SetForegroundWindow(hwnd)
                log.info("已将 PotPlayer 置前（hwnd=%s），准备发插帧快捷键", hwnd)
            except Exception as e:
                # 置前失败多半是 Windows 的前台窗口限制，快捷键可能落空
                log.warning("置前 PotPlayer 失败（快捷键可能落空）：%s", e)
        else:
            log.warning("未等到 PotPlayer 窗口，仍尝试发送插帧快捷键")
        self._send_shortcut()

    def _wait_player_window(self, proc, timeout: float = 8.0) -> Optional[int]:
        """轮询等待 PotPlayer 主窗口出现（最多 timeout 秒）。

        播放器从 Popen 到窗口可见通常要 1~3 秒，直接发快捷键会打空。
        播放器自己退出（poll() 非 None）时立即放弃等待。
        """
        from app.core.monitor import find_potplayer_hwnd   # 复用同一套窗口枚举
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                return None
            hwnd = find_potplayer_hwnd()
            if hwnd:
                return hwnd
            time.sleep(0.3)
        return None

    def _send_shortcut(self) -> None:
        """keyboard 优先，pyautogui 兜底。"""
        keys = [k.strip() for k in self.ls_shortcut.split("+") if k.strip()]
        if not keys:
            return
        try:
            import keyboard
        except ImportError:
            pass                      # 没装 → 走 pyautogui
        else:
            try:
                keyboard.press_and_release("+".join(keys))
                log.info("已发送插帧快捷键：%s", "+".join(keys))
                return
            except Exception as e:
                log.warning("keyboard 发送失败：%s，改用 pyautogui", e)
        try:
            import pyautogui
        except ImportError:
            # **踩坑**：这两个库没装时，早期只记一句"pyautogui 发送失败"，
            # 看起来像"快捷键发出去但没生效"，实际根本没发（实测踩过：
            # 表现是小黄鸭起来了却不补帧）。这里把原因写死。
            log.error("未安装 pyautogui / keyboard，无法发送插帧快捷键 —— "
                      "请执行：pip install pyautogui keyboard")
            return
        try:
            pyautogui.hotkey(*keys)
            log.info("已发送插帧快捷键（pyautogui）：%s", "+".join(keys))
        except Exception as e:
            log.error("pyautogui 发送失败：%s", e)
