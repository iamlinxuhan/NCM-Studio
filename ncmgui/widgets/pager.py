"""分页控件：上一页 / 下一页 / 总数。

只负责显示和发信号，不取数据。页码从 1 开始，和网易云接口的 ``offset`` 换算
放在页面里，避免这里偷偷记住每页多少条。
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from .card import ghost_button


class Pager(QWidget):
    """一行分页。``set_page(page, has_more, total)``。"""

    pageChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._page = 1
        self._has_more = False
        self._total = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addStretch(1)

        self.prev_button = ghost_button("上一页", compact=True)
        self.prev_button.clicked.connect(lambda: self.go(self._page - 1))
        self.label = QLabel("")
        self.label.setObjectName("Hint")
        self.next_button = ghost_button("下一页", compact=True)
        self.next_button.clicked.connect(lambda: self.go(self._page + 1))

        layout.addWidget(self.prev_button)
        layout.addWidget(self.label)
        layout.addWidget(self.next_button)
        self._sync()

    @property
    def page(self) -> int:
        return self._page

    def set_page(self, page: int, has_more: bool = False, total: int = 0) -> None:
        self._page = max(1, int(page))
        self._has_more = bool(has_more)
        self._total = max(0, int(total))
        self._sync()

    def go(self, page: int) -> None:
        page = max(1, int(page))
        if page == self._page:
            return
        self._page = page
        self._sync()
        self.pageChanged.emit(page)

    def _sync(self) -> None:
        if self._total:
            self.label.setText(f"第 {self._page} 页 · 共 {self._total} 条")
        else:
            self.label.setText(f"第 {self._page} 页")
        self.prev_button.setEnabled(self._page > 1)
        self.next_button.setEnabled(self._has_more)
