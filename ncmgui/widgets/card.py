"""基础容器与按钮。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme


class Card(QFrame):
    """圆角卡片容器。内容加到 ``self.body`` 里。"""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setProperty("accent", "false")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 18)
        outer.setSpacing(12)

        if title:
            head = QVBoxLayout()
            head.setSpacing(3)
            label = QLabel(title)
            label.setObjectName("CardTitle")
            head.addWidget(label)
            if subtitle:
                sub = QLabel(subtitle)
                sub.setObjectName("CardSubtitle")
                sub.setWordWrap(True)
                head.addWidget(sub)
            outer.addLayout(head)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)

    def add(self, widget: QWidget | QHBoxLayout, stretch: int = 0) -> QWidget | QHBoxLayout:
        """往卡片主体里加控件或子布局。"""
        if isinstance(widget, QHBoxLayout):
            self.body.addLayout(widget, stretch)
        else:
            self.body.addWidget(widget, stretch)
        return widget

    def set_accent(self, on: bool = True) -> None:
        """高亮描边，用于强调当前处于活动状态的卡片。"""
        self.setProperty("accent", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class HLine(QFrame):
    """一像素分隔线。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HLine")
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class Chip(QLabel):
    """小状态标签，如「已登录」「运行中」。"""

    def __init__(self, text: str = "", state: str = "off", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Chip")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        """state 取值：``on`` / ``off`` / ``warn``。"""
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)


def accent_button(text: str, glow: bool = True) -> QPushButton:
    """渐变主按钮，可选霓虹辉光。"""
    button = QPushButton(text)
    button.setObjectName("Accent")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumHeight(38)
    if glow:
        theme.glow(button, theme.CYAN, blur=28, alpha=110)
    return button


def ghost_button(text: str, compact: bool = False) -> QPushButton:
    """次级描边按钮。"""
    button = QPushButton(text)
    button.setObjectName("Ghost")
    button.setProperty("compact", "true" if compact else "false")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if compact:
        button.setMinimumHeight(26)
    return button


def danger_button(text: str) -> QPushButton:
    """危险操作按钮 —— 会改动账号状态或不可逆的动作用它。"""
    button = QPushButton(text)
    button.setObjectName("Danger")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumHeight(34)
    return button


def page_header(title: str, subtitle: str = "") -> QWidget:
    """页面顶部的标题区。"""
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(4)

    label = QLabel(title)
    label.setObjectName("PageTitle")
    layout.addWidget(label)

    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("PageSubtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)

    return wrapper
