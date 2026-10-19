"""详情页容器：入栈、返回。

歌单→歌手→专辑这样的连续跳转会压入多个 :class:`CollectionPage`，返回时逐层
弹出；从侧栏切走时整叠清空，避免下次打开还停在上次的内容。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..widgets.card import ghost_button
from .collection import CollectionPage

KIND_LABELS = {"playlist": "歌单", "album": "专辑", "artist": "歌手"}


class DetailHost(QWidget):
    """内容区里的详情栈，不是导航项。"""

    emptied = pyqtSignal()

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._pages: list[CollectionPage] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("DetailBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(18, 8, 18, 8)
        bar_layout.setSpacing(10)
        self.back_button = ghost_button("← 返回", compact=True)
        self.back_button.clicked.connect(self.back)
        self.crumb = QLabel("")
        self.crumb.setObjectName("Hint")
        bar_layout.addWidget(self.back_button)
        bar_layout.addWidget(self.crumb, 1)
        layout.addWidget(bar)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

    # ---------------------------------------------------------------- 导航

    def open(self, kind: str, item_id: int) -> None:
        if kind not in KIND_LABELS or not item_id:
            return
        page = CollectionPage(self.window)
        page.backRequested.connect(self.back)
        page.load(kind, int(item_id))
        self._pages.append(page)
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        self._sync_bar(kind)
        hook = getattr(page, "on_show", None)
        if callable(hook):
            hook()

    def back(self) -> None:
        if not self._pages:
            self.emptied.emit()
            return
        page = self._pages.pop()
        self.stack.removeWidget(page)
        page.shutdown()
        page.deleteLater()
        if not self._pages:
            self.emptied.emit()
            return
        current = self._pages[-1]
        self.stack.setCurrentWidget(current)
        self.crumb.setText(MARKER)
        hook = getattr(current, "on_show", None)
        if callable(hook):
            hook()

    def clear(self) -> None:
        while self._pages:
            page = self._pages.pop()
            self.stack.removeWidget(page)
            page.shutdown()
            page.deleteLater()

    def on_show(self) -> None:
        if self._pages:
            page = self._pages[-1]
            hook = getattr(page, "on_show", None)
            if callable(hook):
                hook()

    def _sync_bar(self, kind: str) -> None:
        self.crumb.setText(f"{KIND_LABELS.get(kind, '详情')}详情")


#: 从子页面返回时不知道上一层的类型；保留一个中性标记
MARKER = "详情"
