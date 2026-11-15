"""听歌统计页。

数据来自 ``play_events``：90 天热力图、24 小时分布、歌曲/专辑/歌手排行、
音质构成。图表全部用 QPainter 自绘，不依赖图表库。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..library import StatsData, collect
from ..widgets.card import ghost_button
from ..widgets.charts import HeatmapWidget, HourBars, SliceBars
from .base import BasePage

WINDOW_DAYS = 90
RANK_LIMIT = 10


class StatsPage(BasePage):
    PAGE_TITLE = "听歌统计"
    PAGE_SUBTITLE = "根据本地播放记录汇总，统计最近 90 天。播放满 5 秒才会计入。"

    def build(self) -> None:
        self.db = self.window.library
        self._data = StatsData()

        self._build_summary()
        self._build_heatmap()
        self._build_charts()
        self._build_rankings()
        self._build_status()

    # ---------------------------------------------------------------- 构建

    def _build_summary(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._stats: dict[str, QLabel] = {}
        for key, caption in (
            ("plays", "总播放"),
            ("time", "总时长"),
            ("active", "活跃天数"),
            ("streak", "连续听歌"),
            ("longest", "最长连续"),
        ):
            card = QFrame()
            card.setObjectName("Card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(14, 12, 14, 11)
            layout.setSpacing(3)
            value = QLabel("—")
            value.setObjectName("StatValue")
            label = QLabel(caption)
            label.setObjectName("StatCaption")
            layout.addWidget(value)
            layout.addWidget(label)
            self._stats[key] = value
            row.addWidget(card, 1)
        self.content.addLayout(row)

    def _build_heatmap(self) -> None:
        card, layout = self._card(
            "最近 90 天",
            "颜色越亮表示当天播放越多；把鼠标停在格子上可以看明细。",
        )
        self.heatmap = HeatmapWidget()
        layout.addWidget(self.heatmap)
        self.content.addWidget(card)

    def _build_charts(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        hours_card, hours_layout = self._card("时段分布", "")
        self.hours_caption = QLabel("")
        self.hours_caption.setObjectName("CardSubtitle")
        hours_layout.addWidget(self.hours_caption)
        self.hours = HourBars()
        hours_layout.addWidget(self.hours)
        row.addWidget(hours_card, 3)

        quality_card, quality_layout = self._card("音质构成", "按播放次数统计。")
        self.qualities = SliceBars()
        quality_layout.addWidget(self.qualities)
        quality_layout.addStretch(1)
        row.addWidget(quality_card, 2)
        self.content.addLayout(row)

    def _build_rankings(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._rank_tables: dict[str, QTableWidget] = {}
        self._rank_empty: dict[str, QLabel] = {}

        for key, title, hint in (
            ("songs", "最常播放的歌曲", "按播放次数"),
            ("albums", "最常播放的专辑", "按播放次数"),
            ("artists", "最常播放的歌手", "按播放次数"),
        ):
            card, layout = self._card(title, hint)
            table = self._make_rank_table()
            empty = QLabel("还没有足够的数据")
            empty.setObjectName("Hint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty, 0, Qt.AlignmentFlag.AlignTop)
            layout.addWidget(table, 0, Qt.AlignmentFlag.AlignTop)
            self._rank_tables[key] = table
            self._rank_empty[key] = empty
            row.addWidget(card, 1)
        self.content.addLayout(row)

    def _build_status(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.refresh_btn = ghost_button("刷新", compact=True)
        self.refresh_btn.clicked.connect(self._reload)
        row.addWidget(self.status, 1)
        row.addWidget(self.refresh_btn)
        self.content.addLayout(row)

    def _card(self, title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        heading = QLabel(title)
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("CardSubtitle")
            sub.setWordWrap(True)
            layout.addWidget(sub)
        return card, layout

    def _make_rank_table(self) -> QTableWidget:
        table = QTableWidget(0, 3)
        table.horizontalHeader().setVisible(False)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(0, 26)
        table.setColumnWidth(2, 58)
        return table

    # ---------------------------------------------------------------- 数据

    def on_show(self) -> None:
        self._reload()

    def set_demo(self, data: StatsData) -> None:
        """截图用：直接渲染给定的统计数据。"""
        self._data = data
        self._render()

    def theme_changed(self) -> None:
        for widget in (self.heatmap, self.hours, self.qualities):
            widget.theme_changed()
        self._reload(repaint_only=True)

    def _reload(self, repaint_only: bool = False) -> None:
        self._data = collect(self.db, WINDOW_DAYS, rank_limit=RANK_LIMIT)
        if repaint_only:
            # 主题切换时只刷新图表颜色；表格文字由 QSS 自动变
            self.heatmap.set_data(self._data.days)
            self.hours.set_data(self._data.hours)
            self.qualities.set_data(self._data.qualities)
            self._update_texts()
            return
        self._render()

    def _render(self) -> None:
        data = self._data
        self.heatmap.set_data(data.days)
        self.hours.set_data(data.hours)
        self.qualities.set_data(data.qualities)

        for key in self._rank_tables:
            rows = getattr(data, key)
            self._fill_rank(self._rank_tables[key], self._rank_empty[key], rows)

        self._update_texts()

    def _update_texts(self) -> None:
        data = self._data
        self._stats["plays"].setText(str(data.total_plays))
        self._stats["time"].setText(data.total_listened_text)
        self._stats["active"].setText(f"{data.active_days} 天")
        self._stats["streak"].setText(f"{data.current_streak} 天")
        self._stats["longest"].setText(f"{data.longest_streak} 天")

        if data.total_plays:
            peak_end = (data.peak_hour + 1) % 24
            self.hours_caption.setText(
                f"最常在 {data.peak_hour:02d}:00–{peak_end:02d}:00 听歌，"
                f"共 {data.hours[data.peak_hour]} 次。"
            )
        else:
            self.hours_caption.setText("还没有播放记录。")

        if data.total_plays:
            self.status.setText(
                f"最近 {WINDOW_DAYS} 天共播放 {data.total_plays} 次，"
                f"累计 {data.total_listened_text}；"
                f"有 {data.active_days} 天在听歌。"
            )
        else:
            self.status.setText(
                f"最近 {WINDOW_DAYS} 天还没有播放记录，听几首歌再回来看看。"
            )

    def _fill_rank(self, table: QTableWidget, empty: QLabel, rows) -> None:
        table.setRowCount(len(rows))
        for index, entry in enumerate(rows):
            rank = QTableWidgetItem(str(index + 1))
            rank.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            name = QTableWidgetItem(entry.title)
            subtitle = entry.subtitle or ""
            name.setToolTip(f"{entry.title}\n{subtitle}".strip())
            plays = QTableWidgetItem(f"{entry.plays} 次")
            plays.setTextAlignment(
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            )
            table.setItem(index, 0, rank)
            table.setItem(index, 1, name)
            table.setItem(index, 2, plays)
            table.setRowHeight(index, 24)

        table.setFixedHeight(max(0, len(rows)) * 24 + 2)
        table.setVisible(bool(rows))
        empty.setVisible(not rows)
