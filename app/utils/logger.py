"""日志初始化。

使用标准 logging + RotatingFileHandler，按大小滚动，避免无限膨胀。
重复调用 setup_logging() 幂等。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 2 * 1024 * 1024  # 2 MB
BACKUP_COUNT = 3

_initialized = False


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

    _initialized = True
    root.info("日志初始化完成：%s", log_file)
