"""账号卡片：头像、昵称与 UID。

侧边栏用紧凑版（一行头像 + 两行文字），账号页用完整版（大头像 + 签名）。
头像走 ``ImageStore``，与发现页封面共用缓存；还没登录或资料未到时画一个
渐变圆形占位，不留下空白方块。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from .. import theme
from ..images import rounded


class ProfileCard(QFrame):
    """可点击的账号信息卡。``compact`` 用于侧边栏，否则用于账号页。"""

    clicked = pyqtSignal()

    def __init__(self, window, parent=None, *, compact: bool = True) -> None:
        super().__init__(parent)
        self.window = window
        self.compact = bool(compact)
        self._profile: dict = {}
        self._logged_in = False
        self._avatar_key = ""
        self._avatar_size = 38 if self.compact else 76

        self.setObjectName("ProfileCard")
        self.setProperty("compact", "true" if self.compact else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        margin = 9 if self.compact else 18
        layout = QHBoxLayout(self)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(10 if self.compact else 16)

        self.avatar = QLabel()
        self.avatar.setFixedSize(self._avatar_size, self._avatar_size)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.avatar)

        text = QVBoxLayout()
        text.setSpacing(1 if self.compact else 4)
        self.name = QLabel("未登录")
        self.name.setObjectName("ProfileName")
        self.name.setProperty("full", "true" if not self.compact else "false")
        self.meta = QLabel("点击登录")
        self.meta.setObjectName("ProfileMeta")
        self.meta.setProperty("full", "true" if not self.compact else "false")
        self.meta.setWordWrap(False)
        text.addWidget(self.name)
        text.addWidget(self.meta)

        self.extra = QLabel("")
        self.extra.setObjectName("ProfileExtra")
        self.extra.setWordWrap(True)
        self.extra.setVisible(False)
        if not self.compact:
            text.addWidget(self.extra)
        text.addStretch(1)
        layout.addLayout(text, 1)

        self.window.image_store.imageReady.connect(self._on_image)
        self._render()

    # ------------------------------------------------------------ 对外接口

    def set_logged_in(self, logged_in: bool) -> None:
        logged_in = bool(logged_in)
        if logged_in == self._logged_in:
            return
        self._logged_in = logged_in
        if not logged_in:
            self._profile = {}
        self._render()

    def set_profile(self, profile: dict | None) -> None:
        self._profile = dict(profile or {})
        self._render()

    def refresh_theme(self) -> None:
        """主题切换后重画占位头像；已下载的头像不受影响。"""
        self._render()

    # ------------------------------------------------------------ 绘制

    def _render(self) -> None:
        profile = self._profile
        nickname = str(profile.get("nickname") or "").strip()
        try:
            uid = int(profile.get("userId") or 0)
        except (TypeError, ValueError):
            uid = 0

        if self._logged_in:
            name = nickname or "网易云用户"
            if uid:
                meta = f"UID {uid}"
                try:
                    vip = int(profile.get("vipType") or 0)
                except (TypeError, ValueError):
                    vip = 0
                if vip:
                    meta += " · VIP"
            else:
                meta = "正在读取账号信息…"
        else:
            name = "未登录"
            meta = "点击登录网易云账号"

        if self.compact:
            # 侧边栏只有 170px 宽；昵称长了就省略，避免把侧边栏顶宽
            width = 118
            name = self.name.fontMetrics().elidedText(
                name, Qt.TextElideMode.ElideRight, width
            )
            meta = self.meta.fontMetrics().elidedText(
                meta, Qt.TextElideMode.ElideRight, width
            )
        self.name.setText(name)
        self.meta.setText(meta)

        signature = str(profile.get("signature") or "").strip()
        if not self.compact:
            self.extra.setText(signature)
            self.extra.setVisible(bool(signature))

        url = str(profile.get("avatarUrl") or "").strip()
        if self._logged_in and url:
            key = f"avatar:{uid or url}"
            if key != self._avatar_key:
                self._avatar_key = key
                cached = self.window.image_store.get(key)
                if cached is not None:
                    self._set_avatar(cached)
                else:
                    self.avatar.setPixmap(self._placeholder(nickname))
                    self.window.image_store.request(key, url)
        else:
            self._avatar_key = ""
            self.avatar.setPixmap(self._placeholder(name))

    def _set_avatar(self, pixmap: QPixmap) -> None:
        size = self._avatar_size
        self.avatar.setPixmap(rounded(pixmap, size, size / 2))

    def _on_image(self, key: str, pixmap: QPixmap) -> None:
        if key == self._avatar_key:
            self._set_avatar(pixmap)

    def _placeholder(self, text: str) -> QPixmap:
        size = self._avatar_size
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            gradient = QLinearGradient(0, 0, size, size)
            gradient.setColorAt(0.0, QColor(theme.CYAN))
            gradient.setColorAt(1.0, QColor(theme.PURPLE))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawEllipse(0, 0, size, size)

            initial = (text or "♪").strip()[:1] or "♪"
            font = QFont()
            font.setPixelSize(max(12, int(size * 0.42)))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                pixmap.rect(), Qt.AlignmentFlag.AlignCenter, initial
            )
        finally:
            painter.end()
        return pixmap

    # ------------------------------------------------------------ 点击

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)
