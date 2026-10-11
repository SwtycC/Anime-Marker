"""日志初始化。

使用标准 logging + RotatingFileHandler，按大小滚动，避免无限膨胀。
重复调用 setup_logging() 幂等。

另外把 **Qt 自己的消息接进本日志**（见 `install_qt_message_handler`）：
Qt 的消息默认只写 stderr，而本程序"自己关掉"的那一类死法（Qt6 的
`qFatal`，见 app/utils/qt_thread.py）在日志文件里原本**一个字都不留**。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 2 * 1024 * 1024  # 2 MB
BACKUP_COUNT = 3

_initialized = False

#: Qt 消息级别 → 本应用的日志级别。
#:
#: Debug 落 DEBUG 是**故意的**：QML 的 `console.log` 走的就是 QtDebugMsg，
#: 落到 INFO 会把日志刷满；要抓 QML 输出时把日志级别调到 DEBUG 即可
#: （`setup_logging(..., level=logging.DEBUG)`）。
_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def install_qt_message_handler() -> None:
    """把 Qt / QML 的消息转发进本应用的日志。

    **为什么必须有**：Qt 的消息（QML 的告警、`qWarning`、以及致命的
    `QThread: Destroyed while thread '...' is still running`）默认只写
    stderr。而后者是 `qFatal` —— 写完就把整个进程 `__fastfail` 掉，
    `shutdown()` 都跑不完，于是**日志文件里断在半截、控制台只有那一行**，
    事后完全无从查起。接进来之后，至少日志里会留下"哪个时间点、
    哪条线程"。

    几条约束：

    - **只能写一行、不能做重活**：handler 在 `QtFatalMsg` 返回后立刻被
      Qt 杀掉，多做的事（发信号、弹窗）不会有人看。
    - **必须自己吞异常**：handler 里抛出的异常没法被捕获，会滚成雪球。
    - `QtFatalMsg` 时**先 flush 所有 handler** 再返回：进程马上就没了，
      留在缓冲区里的那行就是唯一的那行。
    """

    def _handler(mode: QtMsgType, context, message: str) -> None:
        try:
            level = _QT_LEVELS.get(mode, logging.WARNING)
            where = ""
            if context is not None and context.file:
                where = "（%s:%s）" % (context.file, context.line)
            logging.getLogger("qt").log(level, "%s%s", message, where)
            if mode == QtMsgType.QtFatalMsg:
                # 进程随后即被 Qt 终结，缓冲区里的东西必须现在就落盘
                for h in logging.getLogger().handlers:
                    try:
                        h.flush()
                    except Exception:
                        pass
        except Exception:          # pragma: no cover - handler 不得再抛
            pass

    qInstallMessageHandler(_handler)


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """初始化根 logger。"""
    global _initialized
    if _initialized:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "anime_marker.log"

    root = logging.getLogger()
    root.setLevel(level)
    # 清理已有 handler，避免重复
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    fh = RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # 放在最后：handler 必须先就位，否则 Qt 早期的消息没地方去
    install_qt_message_handler()

    _initialized = True
    root.info("日志初始化完成：%s", log_file)
