"""把耗时任务挪到后台线程。

解析一个几百首的歌单要跑好几次 ``ncmctl curl``，几秒钟起步。这段逻辑若留在
主线程里，窗口会整个僵住 —— 连「停止」都点不动。

**为什么用 ``threading.Thread`` 而不是 ``QThread``：** 后台跑的其实是纯 Python
（起子进程 + 解析 JSON），完全不需要 Qt 的事件循环。而 ``QThread`` 有个麻烦的
生命周期约束：线程还在跑时对象被销毁会直接让程序崩掉，用户关窗口时正好会撞上。
守护线程没有这个问题 —— 进程退出时它随进程一起消失，产出的结果本来就是丢弃的。

结果通过 Qt 信号送回界面：从非 Qt 线程 emit 是安全的，PyQt 会自动改用队列连接，
槽函数仍然在主线程执行，因此界面可以照常更新控件。

用法::

    task = BackgroundTask(lambda: resolver.plan(targets, output), parent=self)
    resolver.progress = task.progress.emit
    task.done.connect(self._on_plan)      # self 是 QObject，保证槽在主线程跑
    task.failed.connect(self._on_error)
    task.start()
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal


class BackgroundTask(QObject):
    """在线程里跑一次函数，把结果用信号送回界面。只能启动一次。"""

    #: 进度说明（人话，可直接显示给用户）
    progress = pyqtSignal(str)
    #: 百分比 0～100；``-1`` 表示总长未知（下载时服务端没给 Content-Length）
    percent = pyqtSignal(int)
    #: 成功，携带返回值
    done = pyqtSignal(object)
    #: 失败，携带面向用户的错误说明
    failed = pyqtSignal(str)

    #: 仍在跑的任务。关窗时用来等「正在写盘」的任务收尾。
    _active: set["BackgroundTask"] = set()
    _active_lock = threading.Lock()

    def __init__(
        self,
        fn: Callable[[], Any],
        parent: QObject | None = None,
        *,
        critical: bool = False,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self._thread: threading.Thread | None = None
        self._finished = False
        #: 关键任务（写歌词/封面/标签）在关窗时要多等一会儿，避免写到一半退出。
        #: 写入本身是原子的，等不到也只是少处理几首，不会损坏文件。
        self.critical = bool(critical)
        #: 置位后任务在下一次检查点收手。任务函数需要主动查询它。
        self.cancel_event = threading.Event()

    # ------------------------------------------------------------------ 控制

    def start(self) -> bool:
        """启动后台线程。重复调用返回 False。"""
        if self._thread is not None:
            return False
        self._thread = threading.Thread(
            target=self._run, name="ncmgui-task", daemon=True
        )
        with BackgroundTask._active_lock:
            BackgroundTask._active.add(self)
        self._thread.start()
        return True

    def cancel(self) -> None:
        """请求任务在下一次检查点停止（不会强杀线程）。"""
        self.cancel_event.set()

    @classmethod
    def pending_critical(cls) -> list["BackgroundTask"]:
        """还没结束的关键任务快照。"""
        with cls._active_lock:
            return [task for task in cls._active
                    if task.critical and not task._finished]

    @classmethod
    def wait_critical(cls, timeout_ms: int = 5000) -> bool:
        """等关键任务结束，最多 ``timeout_ms`` 毫秒。

        返回 True 表示已全部结束；False 表示超时。调用方在主线程里等的时候
        应当穿插 ``processEvents``，否则完成信号得不到处理。
        """
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        while True:
            if not cls.pending_critical():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.02, remaining))

    @property
    def is_running(self) -> bool:
        """是否还在跑。"""
        return self._thread is not None and not self._finished

    @property
    def finished(self) -> bool:
        """是否已经出结果（不论成败）。"""
        return self._finished

    # ------------------------------------------------------------------ 线程内

    def _run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成界面上的提示
            self._emit(self.failed, str(exc).strip() or exc.__class__.__name__)
        else:
            self._emit(self.done, result)
        finally:
            self._finished = True
            with BackgroundTask._active_lock:
                BackgroundTask._active.discard(self)

    @staticmethod
    def _emit(signal, payload: object) -> None:
        """发信号；界面可能已经关掉了，那就不必再送。"""
        try:
            signal.emit(payload)
        except RuntimeError:
            pass
