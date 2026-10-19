"""封面卡片与网格：发现页、搜索结果、收藏页共用。

卡片只拿 ``key``（缓存键）、``url``（封面地址）和任意 ``payload``（通常是
ID）。点击时把 payload 原样发出去，页面自己决定打开歌单/专辑/歌手。

图片由 :class:`ncmgui.images.ImageStore` 在后台下载；卡片销毁后迟到的图片
不会写进已经删掉的控件。
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..images import placeholder, rounded

#: 卡片总宽。封面 150，左右留 6 像素给悬停描边。
CARD_WIDTH = 162
COVER_SIZE = 150


class CoverCard(QFrame):
    """一张封面卡片：图片 + 标题 + 副标题。"""

    clicked = pyqtSignal(object)
    contextRequested = pyqtSignal(object, object)

    def __init__(
        self,
        store,
        key: str,
        title: str,
        subtitle: str = "",
        payload: object = None,
        *,
        parent: QWidget | None = None,
        round_cover: bool = False,
        cover_size: int = COVER_SIZE,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("CoverCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.setFixedWidth(max(100, int(cover_size) + 12))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._store = store
        self._key = str(key)
        self._payload = payload
        self._round = bool(round_cover)
        self._full_title = title or ""
        self._full_subtitle = subtitle or ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 8)
        layout.setSpacing(6)

        self.cover = QLabel()
        self.cover.setFixedSize(cover_size, cover_size)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setPixmap(placeholder(cover_size))
        layout.addWidget(self.cover)

        self.title_label = QLabel()
        self.title_label.setObjectName("CoverTitle")
        layout.addWidget(self.title_label)

        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("CoverSubtitle")
        layout.addWidget(self.subtitle_label)

        self._apply_text()
        self.setToolTip("\n".join(part for part in (self._full_title,
                                                    self._full_subtitle) if part))

        if store is not None:
            store.imageReady.connect(self._on_image)
            cached = store.request(self._key, "")
            if cached is not None:
                self._set_pixmap(cached)

    # ------------------------------------------------------------ 数据

    def set_content(self, key: str, title: str, subtitle: str = "",
                    payload: object = None, url: str = "") -> None:
        """复用卡片时换内容。旧图片立即清掉，不能让它停留在新标题上。"""
        self._key = str(key)
        self._payload = payload
        self._full_title = title or ""
        self._full_subtitle = subtitle or ""
        self._apply_text()
        self.cover.setPixmap(placeholder(self.cover.width()))
        if self._store is not None:
            cached = self._store.request(self._key, url)
            if cached is not None:
                self._set_pixmap(cached)

    def _apply_text(self) -> None:
        width = max(60, self.width() - 12)
        metrics = QFontMetrics(self.title_label.font())
        self.title_label.setText(
            metrics.elidedText(self._full_title, Qt.TextElideMode.ElideRight, width)
        )
        metrics = QFontMetrics(self.subtitle_label.font())
        self.subtitle_label.setText(
            metrics.elidedText(self._full_subtitle, Qt.TextElideMode.ElideRight, width)
        )
        self.title_label.setToolTip(self._full_title)
        self.subtitle_label.setToolTip(self._full_subtitle)

    def _on_image(self, key: str, pixmap: QPixmap) -> None:
        if str(key) != self._key:
            return
        self._set_pixmap(pixmap)

    def _set_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self.cover.setPixmap(
            rounded(pixmap, self.cover.width(),
                    radius=self.cover.width() / 2 if self._round else None)
        )

    # ------------------------------------------------------------ 交互

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit(self._payload)
        super().mouseReleaseEvent(event)

    def _context_menu(self, position) -> None:
        if self._payload is not None:
            self.contextRequested.emit(
                self._payload, self.mapToGlobal(position)
            )


class CoverGrid(QWidget):
    """响应式封面网格。

    Qt 没有现成的流式布局；这里用 ``QGridLayout``，按可用宽度算列数。卡片可以
    预先创建并复用（搜索翻页、收藏刷新时只换内容），避免每次重建一堆 QLabel。
    """

    itemClicked = pyqtSignal(object)
    itemContextRequested = pyqtSignal(object, object)

    def __init__(
        self,
        store,
        parent: QWidget | None = None,
        *,
        round_cover: bool = False,
        cover_size: int = COVER_SIZE,
        spacing: int = 10,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._round = bool(round_cover)
        self._cover_size = int(cover_size)
        self._spacing = int(spacing)
        self._columns = 0
        self._cards: list[CoverCard] = []

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(self._spacing)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

    # ------------------------------------------------------------ 内容

    def set_items(self, items: Sequence[dict]) -> None:
        """``items`` 每项：``key/title/subtitle/payload/url``。"""
        items = list(items)
        while len(self._cards) < len(items):
            card = CoverCard(
                self._store, "", "", "", None, parent=self,
                round_cover=self._round, cover_size=self._cover_size,
            )
            card.clicked.connect(self.itemClicked)
            card.contextRequested.connect(self.itemContextRequested)
            self._cards.append(card)
        for card, item in zip(self._cards, items):
            card.set_content(
                item.get("key", ""), item.get("title", ""),
                item.get("subtitle", ""), item.get("payload"),
                item.get("url", ""),
            )
        for card in self._cards[len(items):]:
            card.setVisible(False)
        for card in self._cards[:len(items)]:
            card.setVisible(True)
        self._relayout(force=True)

    def clear(self) -> None:
        self.set_items([])

    def count(self) -> int:
        return sum(1 for card in self._cards if not card.isHidden())

    # ------------------------------------------------------------ 布局

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self, force: bool = False) -> None:
        visible = [card for card in self._cards if not card.isHidden()]
        if not visible:
            self._columns = 0
            return
        available = max(self.width(), CARD_WIDTH)
        columns = max(1, (available + self._spacing) // (CARD_WIDTH + self._spacing))
        if not force and columns == self._columns:
            return
        self._columns = columns
        for card in self._cards:
            self._grid.removeWidget(card)
        for position, card in enumerate(visible):
            self._grid.addWidget(card, position // columns, position % columns)
