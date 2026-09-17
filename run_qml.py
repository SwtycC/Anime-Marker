"""兼容入口：与 main.py 完全等价。

保留原因：重构期间一直用 `python run_qml.py` 做对照启动，
外部脚本 / 快捷方式可能已记住这个命令，故保留为转发入口。
新代码请直接用 `python main.py`。
"""

from __future__ import annotations

import sys

from main import main

if __name__ == "__main__":
    sys.exit(main())
