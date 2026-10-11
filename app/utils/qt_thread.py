"""QThread 的两条保命约定（Qt 6 上踩过的坑，都往这里收）。

**① 每个线程必须有名字。**

Qt 6 里「销毁一个还在运行的 QThread」**不是警告，是 `qFatal`** ——
进程当场被 `__fastfail` 干掉（退出码 `0xC0000409`）：没有 Python 堆栈、
不触发 Windows 错误报告（所以事件查看器里查不到崩溃记录）、
`QmlApp.shutdown()` 也跑不完（日志断在半截，连「QML 应用关闭」都没有）。
用户看到的就是"软件自己关了"，唯一的线索只有控制台那一句：

    QThread: Destroyed while thread '' is still running

引号里是线程的 `objectName`。**项目里原先所有 worker 都没设过名字，
于是这句永远只有 `''`** —— 光看它完全不知道是哪一个线程炸的（实测排查了很久）。
`NamedThread` 把类名自动设成名字，出事时那句会变成 `thread 'PollWorker'`，
一眼定位。

**② 引用要留到 `finished`，不能留到 `done`。**

`done` 是 `run()` **内部**发的信号，那时线程还在跑。此时把
`self._x_worker = None` 摘掉最后一份引用，对象就会随引用计数或**循环 GC**
析构 → 撞上①那颗雷。为什么是"偶尔炸"：`done.emit()` 之后操作系统完全
可能把工作线程抢占一会儿，而主线程这边已经把引用清干净了。

`finished` 是 Qt 在**线程真正结束之后**才发的，挂在这上面才安全 ——
见 `retire_worker`。`core/rss_service.py` 的 `PollWorker` 是这套写法的范本
（那里写了三轮才修对，注释值得一并读）。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QThread


class NamedThread(QThread):
    """QThread 子类基类：自动把**类名**设成线程名（见模块说明①）。

    用法与 QThread 完全一样，只是把基类换掉：

        class PollWorker(NamedThread):
            def __init__(self, db, ...):
                super().__init__()          # 父类/参数照旧
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setObjectName(type(self).__name__)


def retire_worker(holder: Any, attr: str, worker: QThread) -> None:
    """线程**真正结束**后摘引用 + 交还 Qt 回收 —— **只能挂在 `finished` 上**。

        自建时：w.finished.connect(lambda: retire_worker(self, "_x_worker", w))

    为什么不能挂在 `done` 上、为什么必须是 `finished`：见模块说明②。

    `is not worker` 那层判断是为了**不误伤后来者**：`finished` 与
    `deleteLater` 都是排队投递的，时序可能与"又起了新线程"交错。
    """
    try:
        if getattr(holder, attr, None) is not worker:
            return
        setattr(holder, attr, None)
        worker.deleteLater()
    except RuntimeError:            # pragma: no cover - C++ 对象已被回收
        pass
