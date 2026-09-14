"""播放器 + Lossless Scaling 启动器。

一次 play() 调用完成：起 LS（可选） → 发插帧快捷键 → 起 PotPlayer。
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

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
        ls_shortcut: str = "ctrl+alt+l",
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

        if self.enable_ls and self.ls_path:
            self._start_ls()

        time.sleep(max(0.0, self.player_start_delay))
        proc = subprocess.Popen([self.player_path, video_path])
        log.info("PotPlayer 已启动 pid=%s file=%s", proc.pid, video_path)
        return proc

    def _start_ls(self) -> None:
        _check_exists(self.ls_path, "Lossless Scaling")
        subprocess.Popen([self.ls_path])
        log.info("Lossless Scaling 已启动，等待 %ss 后发插帧快捷键", self.ls_start_delay)
        time.sleep(max(0.0, self.ls_start_delay))
        self._send_shortcut()

    def _send_shortcut(self) -> None:
        """keyboard 优先，pyautogui 兜底。"""
        keys = [k.strip() for k in self.ls_shortcut.split("+") if k.strip()]
        if not keys:
            return
        try:
            import keyboard
            keyboard.press_and_release("+".join(keys))
            log.info("已发送插帧快捷键：%s", "+".join(keys))
            return
        except Exception as e:
            log.warning("keyboard 发送失败：%s，改用 pyautogui", e)
        try:
            import pyautogui
            pyautogui.hotkey(*keys)
            log.info("已发送插帧快捷键（pyautogui）：%s", "+".join(keys))
        except Exception as e:
            log.error("pyautogui 发送失败：%s", e)
