"""搜索页：歌曲 / 专辑 / 歌手 / 歌单。

搜索建议 300ms 防抖；热搜接口失效时只显示搜索历史，不阻塞搜索本身。
所有请求都在后台线程，切页/连续输入时用 generation 丢掉过期结果。
"""

from __future__ import annotations

from PyQt6.QtCore import QStringListModel, Qt, QTimer
from PyQt6.QtWidgets import (
    QCompleter,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError
from ..widgets.card import ghost_button
from ..widgets.covercard import CoverGrid
from ..widgets.pager import Pager
from ..widgets.songtable import SongFilterProxy, SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage

KINDS = (
    ("歌曲", "song"),
    ("专辑", "album"),
    ("歌手", "artist"),
    ("歌单", "playlist"),
)
PAGE_SIZE = 50
HISTORY_LIMIT = 20


class SearchPage(BasePage):
    PAGE_TITLE = "搜索"
    PAGE_SUBTITLE = "网易云曲库。未登录也能搜索公开内容。"
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._suggest_task: BackgroundTask | None = None
        self._hot_task: BackgroundTask | None = None
        self._generation = 0
        self._hot_loaded = False
        self._last_kind = "song"
        self._last_keyword = ""
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        self.actions = self.window.actions
        self._build_toolbar()
        self._build_chips()
        self._build_results()
        self._build_pager()

        self._suggest_timer = QTimer(self)
        self._suggest_timer.setSingleShot(True)
        self._suggest_timer.setInterval(300)
        self._suggest_timer.timeout.connect(self._load_suggestions)

        self.search.textChanged.connect(self._on_text_changed)
        self.search.returnPressed.connect(lambda: self._start_search(1))
        self.kind_combo.currentIndexChanged.connect(lambda _i: self._on_kind_changed())
        self.table.songActivated.connect(self._play_song)
        self.table.contextMenuRequested.connect(self._show_menu)
        self.grid.itemClicked.connect(self._open_card)

        self.actions.likedChanged.connect(self._refresh_likes)
        self._render_chips()

    def _build_toolbar(self) -> None:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索歌曲、专辑、歌手或歌单")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search, 1)

        self.kind_combo = QComboBox()
        for label, value in KINDS:
            self.kind_combo.addItem(label, value)
        layout.addWidget(self.kind_combo)

        self.search_button = ghost_button("搜索")
        self.search_button.clicked.connect(lambda: self._start_search(1))
        layout.addWidget(self.search_button)

        self.completer_model = QStringListModel(self)
        self.completer = QCompleter(self.completer_model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setMaxVisibleItems(8)
        self.search.setCompleter(self.completer)
        self.content.addWidget(bar)

    def _build_chips(self) -> None:
        self.chips_box = QWidget()
        self.chips_layout = QVBoxLayout(self.chips_box)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setSpacing(6)
        self.content.addWidget(self.chips_box)

    def _build_results(self) -> None:
        self.status = QLabel("输入关键词开始搜索。")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

        self.result_stack = QStackedWidget()

        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.model = SongTableModel(self)
        self.proxy = SongFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = SongTableView()
        self.table.setModel(self.proxy)
        table_layout.addWidget(self.table)
        self.result_stack.addWidget(table_page)

        self.grid = CoverGrid(self.window.image_store)
        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.grid_scroll.setWidget(self.grid)
        self.result_stack.addWidget(self.grid_scroll)

        self.content.addWidget(self.result_stack, 1)

    def _build_pager(self) -> None:
        self.pager = Pager()
        self.pager.pageChanged.connect(self._start_search)
        self.content.addWidget(self.pager)

    # ---------------------------------------------------------------- 展示

    def focus_search(self) -> None:
        """应用内快捷键 Ctrl+F：切到搜索页并选中当前关键词。"""
        self.search.setFocus()
        self.search.selectAll()

    def on_show(self) -> None:
        self.actions.ensure_liked()
        if not self._hot_loaded:
            self._load_hot()
        if not self.search.text().strip():
            self.search.setFocus()
        if self.window.defer_page_loads:
            return
        self._render_chips()

    def _on_text_changed(self, text: str) -> None:
        text = (text or "").strip()
        if len(text) >= 2:
            self._suggest_timer.start()
        else:
            self._suggest_timer.stop()
        if not text:
            # 清空输入框回到热搜/历史；搜索过之后 chips_box 是隐藏的
            self.chips_box.setVisible(True)
            self._render_chips()

    def _on_kind_changed(self) -> None:
        if self._last_keyword:
            self._start_search(1)

    def _load_suggestions(self) -> None:
        keyword = self.search.text().strip()
        if len(keyword) < 2:
            return
        client = self.window.client

        def work():
            return client.suggest(keyword)

        task = BackgroundTask(work, parent=self)
        self._suggest_task = task

        def done(words) -> None:
            if self._suggest_task is task:
                self.completer_model.setStringList(list(words or [])[:8])

        task.done.connect(done)
        task.failed.connect(lambda _message: None)
        task.start()

    def _load_hot(self) -> None:
        self._hot_loaded = True
        client = self.window.client

        def work():
            return client.hot_searches()

        task = BackgroundTask(work, parent=self)
        self._hot_task = task

        def done(words) -> None:
            self._hot_words = list(words or [])
            self._render_chips()

        task.done.connect(done)
        task.failed.connect(lambda _message: None)
        task.start()

    # ---------------------------------------------------------------- 检索

    def search_for(self, text: str) -> None:
        """给别的页面用：填入关键词并立即搜索。"""
        self.search.setText((text or "").strip())
        self._start_search(1)

    def _start_search(self, page: int = 1) -> None:
        keyword = self.search.text().strip()
        if not keyword:
            self.status.setText("输入关键词开始搜索。")
            return
        kind = str(self.kind_combo.currentData() or "song")
        self._last_keyword = keyword
        self._last_kind = kind
        self._remember(keyword)
        self.chips_box.setVisible(False)

        if self._task is not None and self._task.is_running:
            self._task.cancel()
        self._generation += 1
        generation = self._generation
        self.search_button.setEnabled(False)
        self.status.setText(f"正在搜索「{keyword}」…")

        client = self.window.client
        limit = PAGE_SIZE if kind == "song" else 30

        def work():
            return client.search(keyword, kind, page, limit)

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(lambda data: self._on_results(data, generation))
        task.failed.connect(lambda message: self._on_failed(message, generation))
        task.start()

    def _on_results(self, data, generation: int) -> None:
        if generation != self._generation:
            return
        self.search_button.setEnabled(True)
        kind = data.kind
        if kind == "song":
            self.model.set_songs(data.items)
            self.result_stack.setCurrentIndex(0)
            self.table.scrollToTop()
        else:
            cards = [
                {
                    "key": f"{kind}:{item.id}",
                    "title": item.name,
                    "subtitle": self._card_subtitle(kind, item),
                    "payload": item.id,
                    "url": self._card_url(item),
                }
                for item in data.items
            ]
            self.grid.set_items(cards)
            self.result_stack.setCurrentIndex(1)
            self.grid_scroll.verticalScrollBar().setValue(0)
        self.pager.set_page(data.page, data.has_more, data.total)
        if data.items:
            self.status.setText(f"找到 {data.total} 条结果")
        else:
            self.status.setText(f"没有找到「{self._last_keyword}」相关的{self._kind_label(kind)}。")

    def _on_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.search_button.setEnabled(True)
        self.status.setText(f"搜索失败：{message}")

    @staticmethod
    def _kind_label(kind: str) -> str:
        for label, value in KINDS:
            if value == kind:
                return label
        return "内容"

    @staticmethod
    def _card_subtitle(kind: str, item) -> str:
        if kind == "album":
            return item.artist_text or "专辑"
        if kind == "artist":
            return item.subtitle
        return item.subtitle

    @staticmethod
    def _card_url(item) -> str:
        return getattr(item, "pic_url", "") or getattr(item, "cover_url", "")

    # ---------------------------------------------------------------- 交互

    def _play_song(self, song) -> None:
        self.actions.play(song, self.model.songs())

    def _show_menu(self, song, position) -> None:
        self.actions.show_menu(self.table, song, self.model.songs(), position)

    def _open_card(self, payload) -> None:
        kind = self._last_kind
        if kind in ("playlist", "album", "artist") and payload:
            self.window.open_detail(kind, int(payload))

    def _refresh_likes(self) -> None:
        # 搜索表格没有红心列，这里只需要让下一次右键菜单读到最新状态
        self.model.set_playing(self.window.playback.current.id
                               if self.window.playback.current else None)

    # ---------------------------------------------------------------- 历史

    def _remember(self, keyword: str) -> None:
        history = [str(item) for item in (self.cfg.get("search.history", []) or [])
                   if str(item).strip()]
        history = [keyword] + [item for item in history if item != keyword]
        self.cfg.set("search.history", history[:HISTORY_LIMIT])

    def _history(self) -> list[str]:
        raw = self.cfg.get("search.history", []) or []
        return [str(item) for item in raw if str(item).strip()][:8]

    def _render_chips(self) -> None:
        if not self.chips_box.isVisible():
            # 搜索过之后 chips 区被收起；输入框清空时再显示
            return
        while self.chips_layout.count():
            item = self.chips_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            layout = item.layout()
            if layout is not None:
                _clear_layout(layout)

        hot = getattr(self, "_hot_words", [])
        if hot:
            self.chips_layout.addLayout(self._chip_row("热搜", hot[:12]))
        history = self._history()
        if history:
            row = self._chip_row("最近", history)
            clear = ghost_button("清空", compact=True)
            clear.clicked.connect(self._clear_history)
            row.addWidget(clear)
            row.addStretch(1)
            self.chips_layout.addLayout(row)
        if not hot and not history:
            self.chips_layout.addWidget(self._hint_label())

    def _chip_row(self, title: str, words: list[str]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("Hint")
        row.addWidget(label)
        for word in words[:12]:
            button = ghost_button(str(word), compact=True)
            button.clicked.connect(lambda _checked, w=word: self.search_for(w))
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _hint_label(self) -> QLabel:
        label = QLabel("可以搜索歌曲、专辑、歌手或歌单。")
        label.setObjectName("Hint")
        label.setWordWrap(True)
        return label

    def _clear_history(self) -> None:
        self.cfg.set("search.history", [])
        self.cfg.save()
        self._render_chips()


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            _clear_layout(child)
