"""播放器 + Lossless Scaling 启动器。

一次 play() 完成：起 LS（可选）→ 起 PotPlayer → **等播放器窗口就位并置前**
→ **全屏** → 发插帧快捷键。

**顺序为什么是这样（实测踩坑）**：小黄鸭的插帧快捷键只对**当前前台窗口**
生效，所以必须"播放器已经在前台"之后再发。早期实现是
`起 LS → 等 5s → 发快捷键 → 起播放器`，快捷键必然落在播放器之外 ——
表现就是"小黄鸭起来了，但没开缩放/补帧"。

**为什么要先全屏再发插帧键（实测踩坑）**：小黄鸭的 `WGC` 捕获 API 抓的是
"这个窗口"，输出尺寸跟随**捕获那一刻的窗口尺寸**。PotPlayer 启动时窗口
尺寸会跳变（先以小窗口出现，之后才应用记住的尺寸），若在跳变过程中被
捕获，LS 就锁在错误的尺寸上 —— 表现为**画面缩小、四周一圈黑边**，
手动切一次屏幕（触发窗口尺寸变化）才会恢复正常。

实测确认：由本程序启动时 PotPlayer **每次都是窗口化**（从未继承全屏），
因此这里可以**无条件**发一次 `Alt+Enter` 切全屏（不必先判断当前是否全屏，
那需要多一次 `GetWindowRect` 比对，且 Alt+Enter 是有状态的、判断反而更绕）。
发完不等固定时长，而是**轮询窗口矩形直到铺满屏幕**再发插帧键 ——
死等既可能不够（慢机器）也可能白等（快机器）。
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import win32api
import win32con
import win32gui
import win32process

log = logging.getLogger(__name__)


class LauncherError(RuntimeError):
    pass


def _check_exists(path: str, label: str) -> None:
    if not path:
        raise LauncherError(f"{label} 路径未配置")
    if not Path(path).exists():
        raise LauncherError(f"{label} 不存在：{path}")


def _window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """窗口矩形 (left, top, right, bottom)；失败返回 None。"""
    try:
        return win32gui.GetWindowRect(hwnd)
    except Exception:
        return None


def _window_desc(hwnd: int) -> str:
    """窗口的"类名 + 标题"描述，专供排查"键发给了谁"。

    排查焦点问题时，光看 hwnd 数字毫无意义 —— 必须知道那是哪个程序
    （实测踩过：以为是"焦点没给到 PotPlayer"，日志打出标题才发现
    前台其实是浏览器窗口）。
    """
    if not hwnd:
        return "<无>"
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd) or ""
        return f"{cls!r}({title[:40]!r})"
    except Exception:
        return f"<hwnd {hwnd}>"


def _hwnd_at_cursor() -> int:
    """鼠标指针下的窗口（`WindowFromPoint`）。

    比 `GetForegroundWindow()` 更能说明"用户看到的是谁在最上层"——
    全屏失败时打印它，能立刻区分"窗口没变"与"被别的窗口挡着"。
    """
    try:
        x, y = win32api.GetCursorPos()
        return win32gui.WindowFromPoint((x, y))
    except Exception:
        return 0


def _is_fullscreen(hwnd: int, tol: int = 2) -> bool:
    """窗口是否已铺满**它所在的那块显示器**。

    为什么不跟主屏比：多显示器下 PotPlayer 可能开在副屏上，跟主屏比会
    永远判为"没全屏"（然后反复发 Alt+Enter，反而在窗口/全屏之间来回切 ✗）。
    这里用 `MonitorFromWindow` 取窗口所在显示器的尺寸（含 work area 之外的
    任务栏区域 —— 全屏状态是覆盖任务栏的，所以不能拿 work area 比）。
    """
    rect = _window_rect(hwnd)
    if rect is None:
        return False
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    try:
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(monitor)
        mon_w = info["Monitor"][2] - info["Monitor"][0]
        mon_h = info["Monitor"][3] - info["Monitor"][1]
    except Exception:
        return False
    return w >= mon_w - tol and h >= mon_h - tol


class PlayerLauncher:
    def __init__(
        self,
        player_path: str,
        ls_path: str = "",
        enable_ls: bool = True,
        ls_shortcut: str = "ctrl+alt+p",   # 须与小黄鸭一致；别用 ctrl+alt+l（QQ 锁定键）
        fullscreen_shortcut: str = "alt+enter",   # PotPlayer 的全屏键，须与播放器一致
        ls_start_delay: float = 5.0,
        player_start_delay: float = 1.0,
        fullscreen_settle: float = 3.0,
    ) -> None:
        self.player_path = player_path
        self.ls_path = ls_path
        self.enable_ls = enable_ls
        self.ls_shortcut = ls_shortcut
        self.fullscreen_shortcut = fullscreen_shortcut
        self.ls_start_delay = ls_start_delay
        self.player_start_delay = player_start_delay
        # 全屏成功后、发插帧键之前的**额外稳定等待**（秒）。
        #
        # **为什么需要（实测踩坑）**：`_ensure_fullscreen()` 的判据是"窗口
        # 矩形铺满显示器"，但 PotPlayer 达到这个尺寸后**可能还在切换渲染
        # 模式**（窗口模式 → 全屏模式）。此时紧接着发插帧键，小黄鸭虽然在
        # 那一刻抓到了窗口，但随后的渲染模式切换会让捕获失效 ——
        # 实测症状："全屏后有帧数，一开始播放就没了"。
        # 手动操作时人按完 Alt+Enter 会自然停顿一下，所以手动没问题、
        # 程序自动化才会踩到 —— 这正是"手动可以、程序不行"的原因。
        self.fullscreen_settle = fullscreen_settle

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
        """等播放器窗口就位 → 置前 → **全屏** → 发插帧快捷键。

        "全屏"这一步不可省：小黄鸭的 WGC 捕获按"捕获那一刻的窗口尺寸"锁定
        输出尺寸，窗口化时捕获到的就是那块小窗口，表现为画面缩小 + 四周黑边
        （详见模块 docstring）。
        """
        hwnd = self._wait_player_window(proc)
        if hwnd:
            self._focus_window(hwnd)
            if self._ensure_fullscreen(hwnd) and self.fullscreen_settle > 0:
                # 见 __init__ 里 fullscreen_settle 的说明：窗口矩形一到屏幕
                # 尺寸就返回还不够，PotPlayer 此时可能仍在切换渲染模式，
                # 紧接着发插帧键会让 LS 抓不住 -> 表现为"全屏后没有帧数"。
                time.sleep(self.fullscreen_settle)
                log.info("全屏后等待 %.2fs（等渲染模式切换完成）",
                         self.fullscreen_settle)
        else:
            log.warning("未等到 PotPlayer 窗口，仍尝试发送插帧快捷键")
        self._send_shortcut()

    def _focus_window(self, hwnd: int) -> None:
        """把播放器置前**并确保它有键盘焦点**。

        **为什么不能只 `SetForegroundWindow`（核心踩坑）**：Windows 限制
        "非前台进程不得抢焦点"，`SetForegroundWindow` 在被限制时**不报错、
        静默失败** —— 窗口可能被提到前面，但键盘焦点仍在别处。此时后续发的
        `Alt+Enter` / 插帧键全部打到别的窗口上。

        实测症状：日志打印"已将 PotPlayer 置前"、`已发送按键：alt+enter`，
        但播放器**没有全屏**，且没有任何报错（因为确实没抛异常）。
        手动用鼠标点一下播放器再按 Alt+Enter 就能全屏 —— 这正是"焦点没给到"
        的典型表现。

        解法是标准的 `AttachThreadInput` 组合拳：把自己的输入线程附加到
        目标窗口所在线程，两者的输入队列合并后，`SetForegroundWindow`
        的限制就被绕过；设置完**必须立刻 detach**，否则会把自己的输入
        和对方绑死。
        """
        try:
            # 已在前台就不用折腾（重复 attach 无谓且有副作用）
            if win32gui.GetForegroundWindow() != hwnd:
                # 目标窗口若被最小化，先还原 —— 最小化的窗口拿不到焦点
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                self._force_foreground(hwnd)
            if win32gui.GetForegroundWindow() == hwnd:
                log.info("已将 PotPlayer 置前并取得焦点（hwnd=%s）", hwnd)
            else:
                # 拿不到也不阻断流程：后续按键可能落空，但至少插帧键仍会发
                log.warning("PotPlayer 未能取得前台焦点（hwnd=%s），"
                            "随后的全屏/插帧按键可能落空", hwnd)
        except Exception as e:
            log.warning("置前 PotPlayer 失败（快捷键可能落空）：%s", e)

    @staticmethod
    def _force_foreground(hwnd: int) -> None:
        """用 `AttachThreadInput` 绕过 Windows 的前台窗口限制。

        步骤（缺一不可）：
        1. 取**当前线程**与**目标窗口所属线程**的 id
        2. `AttachThreadInput(True)` 把两者输入队列合并 —— 合并后系统认为
           "是同一个线程在请求置前"，于是不再拦截
        3. `SetForegroundWindow` + `SetFocus`
        4. **务必** `AttachThreadInput(False)` 解除，否则两个线程的输入
           状态会一直纠缠（表现：输入法/快捷键行为诡异）

        任一步失败都尽量往下走，最终由调用方用 `GetForegroundWindow()`
        复核是否真的成功 —— 这里不抛异常，因为"置前失败"不该中断播放。
        """
        try:
            target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
            cur_thread = win32api.GetCurrentThreadId()
            attached = False
            if target_thread and target_thread != cur_thread:
                try:
                    win32process.AttachThreadInput(cur_thread, target_thread, True)
                    attached = True
                except Exception as e:
                    log.debug("附加输入线程失败（继续尝试直接置前）：%s", e)
            try:
                win32gui.SetForegroundWindow(hwnd)
                win32gui.SetFocus(hwnd)
            finally:
                # 无论置前是否成功都要解除附加
                if attached:
                    try:
                        win32process.AttachThreadInput(
                            cur_thread, target_thread, False)
                    except Exception as e:
                        log.debug("解除输入线程附加失败：%s", e)
        except Exception as e:
            log.debug("强制置前失败：%s", e)

    def _ensure_fullscreen(self, hwnd: int, wait: float = 3.0) -> bool:
        """按配置的全屏快捷键切换全屏，并**等窗口真的铺满屏幕**。

        **为什么必须真全屏（实测）**：小黄鸭的 WGC 捕获按"捕获那一刻的窗口
        尺寸"锁定输出尺寸 ——
          - 窗口化：画面缩小 + 四周黑边
          - 最大化：帧数极不稳定，甚至只有几帧
          - **真全屏（覆盖任务栏）**：帧数正常 ✓
        因此这一步不是"锦上添花"，而是 LS 出画的前提。

        **为什么无条件发、不先判断**：实测本程序启动的 PotPlayer 每次都是
        窗口化（从未继承全屏），判断反而多一次无用比对；而这个键是
        **有状态**的，一旦判断失误就会在窗口/全屏之间来回切。

        返回值：是否确认进入全屏。调用方据此决定要不要提示用户 ——
        失败时**仍继续**发插帧键（半好过完全没有），但要让用户知道。
        """
        keys = [k.strip() for k in (self.fullscreen_shortcut or "").split("+")
                if k.strip()]
        if not keys:
            log.warning("未配置全屏快捷键，跳过全屏（WGC 下帧数可能异常）")
            return _is_fullscreen(hwnd)
        # 发键前**重新夺一次焦点**：小黄鸭启动时会异步弹出浏览器（欢迎页/
        # 更新页），可能在 _focus_window() 之后才抢走前台 —— 那样 alt+enter
        # 就发到浏览器上了（实测症状：日志"取得焦点"却在 3s 后全屏超时）。
        # 这里再夺一次，把"置前"与"发键"之间的窗口期压到最小。
        if win32gui.GetForegroundWindow() != hwnd:
            log.info("发全屏键前发现前台已变（当前=%s），重新夺回焦点",
                     _window_desc(win32gui.GetForegroundWindow()))
            self._force_foreground(hwnd)
        log.info("发送全屏键时的前台窗口：%s（目标 hwnd=%s）",
                 _window_desc(win32gui.GetForegroundWindow()), hwnd)
        self._send_keys(*keys)
        deadline = time.time() + max(0.0, wait)
        while time.time() < deadline:
            if _is_fullscreen(hwnd):
                log.info("PotPlayer 已全屏（hwnd=%s），准备发插帧快捷键", hwnd)
                return True
            time.sleep(0.1)
        # 没等到：可能是全屏动画慢 / 焦点没拿到（按键落空）/ 快捷键与
        # PotPlayer 不一致。不阻断流程，但必须让上层能提示用户。
        rect = _window_rect(hwnd)
        log.warning("等 PotPlayer 全屏超时（%.1fs）—— 当前窗口矩形=%s，"
                    "鼠标所在窗口=%s。请确认「全屏快捷键」配置与 PotPlayer "
                    "一致（F5 → 基本 → 快捷键 里搜「全屏」）",
                    wait, rect, _window_desc(_hwnd_at_cursor()))
        return False

    def _send_shortcut(self) -> None:
        """发送配置里的插帧快捷键（须与小黄鸭中绑定的那组一致）。"""
        keys = [k.strip() for k in self.ls_shortcut.split("+") if k.strip()]
        if not keys:
            return
        self._send_keys(*keys)

    def _send_keys(self, *keys: str) -> None:
        """keyboard 优先，pyautogui 兜底。"""
        if not keys:
            return
        try:
            import keyboard
        except ImportError:
            pass                      # 没装 → 走 pyautogui
        else:
            try:
                keyboard.press_and_release("+".join(keys))
                log.info("已发送按键：%s", "+".join(keys))
                return
            except Exception as e:
                log.warning("keyboard 发送失败：%s，改用 pyautogui", e)
        try:
            import pyautogui
        except ImportError:
            # **踩坑**：这两个库没装时，早期只记一句"pyautogui 发送失败"，
            # 看起来像"快捷键发出去但没生效"，实际根本没发（实测踩过：
            # 表现是小黄鸭起来了却不补帧）。这里把原因写死。
            log.error("未安装 pyautogui / keyboard，无法发送按键 —— "
                      "请执行：pip install pyautogui keyboard")
            return
        try:
            pyautogui.hotkey(*keys)
            log.info("已发送按键（pyautogui）：%s", "+".join(keys))
        except Exception as e:
            log.error("pyautogui 发送失败：%s", e)

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
