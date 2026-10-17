"""表单卡片：把「标签 + 控件 + 提示」这种重复结构收敛成几个方法。

页面代码因此可以写成声明式的：

    card = FormCard("下载设置")
    card.add_line("歌曲 ID", hint="每行一个")
    card.add_combo("音质", ncmctl.DOWNLOAD_LEVELS, "lossless")
    card.add_path("输出目录", value=cfg.get("download.output"))
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .card import Card
from .pathpicker import PathList, PathPicker


def set_combo_data(combo: QComboBox, data) -> None:
    """按 ``userData`` 选中下拉项；找不到就保持原样。"""
    index = combo.findData(data)
    if index >= 0:
        combo.setCurrentIndex(index)


def combo_data(combo: QComboBox, default=None):
    """取当前下拉项的 ``userData``。"""
    value = combo.currentData()
    return default if value is None else value


class FormCard(Card):
    """带表单行布局的卡片。"""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        label_width: int = 92,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, subtitle, parent)
        self.label_width = label_width
        self._labels: list[QLabel] = []
        self._labels_sized = False

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """首次显示时把标签列宽调整到能容纳最长的那条标签。

        标签用固定宽度是为了让各行的控件左边缘对齐；但宽度写死会把
        「状态目录（--home）」这类长标签从左边截断，所以这里按实际需要再放一次。
        """
        super().showEvent(event)
        if self._labels_sized or not self._labels:
            return
        self._labels_sized = True

        needed = max(label.sizeHint().width() for label in self._labels)
        if needed > self.label_width:
            for label in self._labels:
                label.setFixedWidth(needed)

    # ------------------------------------------------------------------ 行

    def add_row(self, label: str, widget: QWidget, hint: str = "") -> QWidget:
        """通用的「标签 + 控件」行，提示文字贴在控件正下方。"""
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        if label:
            tag = QLabel(label)
            tag.setObjectName("FieldLabel")
            tag.setFixedWidth(self.label_width)
            tag.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            tag.setContentsMargins(0, 9, 0, 0)
            self._labels.append(tag)
            outer.addWidget(tag)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(widget)

        if hint:
            note = QLabel(hint)
            note.setObjectName("Hint")
            note.setWordWrap(True)
            column.addWidget(note)

        outer.addLayout(column, 1)
        self.body.addWidget(row)
        return widget

    # ------------------------------------------------------- 具体控件快捷方法

    def add_line(
        self,
        label: str,
        value: str = "",
        placeholder: str = "",
        hint: str = "",
        password: bool = False,
    ) -> QLineEdit:
        edit = QLineEdit(value)
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.add_row(label, edit, hint)
        return edit

    def add_combo(
        self,
        label: str,
        items: list[tuple[str, str]],
        value: str | None = None,
        hint: str = "",
    ) -> QComboBox:
        combo = QComboBox()
        for text, data in items:
            combo.addItem(text, data)
        if value is not None:
            set_combo_data(combo, value)
        self.add_row(label, combo, hint)
        return combo

    def add_spin(
        self,
        label: str,
        value: int,
        minimum: int,
        maximum: int,
        hint: str = "",
    ) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setFixedWidth(110)
        self.add_row(label, spin, hint)
        return spin

    def add_switch(self, label: str, checked: bool = False, hint: str = "") -> QCheckBox:
        box = QCheckBox(label)
        box.setChecked(checked)
        self.add_row("", box, hint)
        return box

    def add_text(
        self,
        label: str,
        value: str = "",
        placeholder: str = "",
        height: int = 110,
        hint: str = "",
    ) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setPlainText(value)
        edit.setPlaceholderText(placeholder)
        edit.setFixedHeight(height)
        self.add_row(label, edit, hint)
        return edit

    def add_path(
        self,
        label: str,
        mode: str = "dir",
        value: str = "",
        placeholder: str = "",
        default: str = "",
        hint: str = "",
    ) -> PathPicker:
        picker = PathPicker(mode=mode, value=value, placeholder=placeholder, default=default)
        self.add_row(label, picker, hint)
        return picker

    def add_pathlist(self, label: str, hint: str = "") -> PathList:
        widget = PathList()
        self.add_row(label, widget, hint)
        return widget

    def add_widget(self, widget: QWidget, label: str = "", hint: str = "") -> QWidget:
        """加入任意自定义控件。"""
        if label or hint:
            return self.add_row(label, widget, hint)
        self.body.addWidget(widget)
        return widget

    def add_row_of(self, *widgets: QWidget, spacing: int = 10) -> QHBoxLayout:
        """把若干控件横排成一行（例如一组并列按钮）。"""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(spacing)
        for widget in widgets:
            row.addWidget(widget)
        self.body.addLayout(row)
        return row
