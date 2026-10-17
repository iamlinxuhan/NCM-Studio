"""黑胶唱片样式的封面。

全部用 ``QPainter`` 画，不依赖任何图片资源：

* 盘面是径向渐变 + 几十圈同心细纹（groove），外沿一道高光；
* 中心圆标是专辑封面，播放时以 50ms 一帧缓慢旋转，暂停立刻停下；
* 唱臂从右上角的转轴伸向盘面，播放时落下、暂停时抬起，用属性动画过渡。

只旋转中心封面而不是整张位图，视觉上与整盘在转一样 —— 盘面纹理本来就是圆的。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    pyqtProperty,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .. import theme

#: 每帧转多少度。20 帧/秒 × 1.2° ≈ 24°/秒，约 15 秒一圈 —— 比真唱片慢，
#: 但足够看出在转，又不会让画面显得躁。
DEGREES_PER_FRAME = 1.2
FRAME_MS = 50


class VinylWidget(QWidget):
    """一张会转的黑胶唱片。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._art: QPixmap | None = None
        self._angle = 0.0
        self._playing = False
        self._arm = 0.0

        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("播放时唱片转动、唱臂落下；暂停时停住并抬起")

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._rotate)

        self._arm_anim = QPropertyAnimation(self, b"armProgress", self)
        self._arm_anim.setDuration(450)
        self._arm_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    # ------------------------------------------------------------ 对外接口

    def set_art(self, pixmap: QPixmap | None) -> None:
        self._art = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.update()

    def set_playing(self, playing: bool) -> None:
        playing = bool(playing)
        if playing == self._playing:
            return
        self._playing = playing
        if playing:
            self._timer.start()
        else:
            self._timer.stop()
        self._animate_arm(1.0 if playing else 0.0)

    def sizeHint(self):  # noqa: N802
        from PyQt6.QtCore import QSize

        return QSize(380, 380)

    # ------------------------------------------------------------ 属性动画

    def _get_arm(self) -> float:
        return self._arm

    def _set_arm(self, value: float) -> None:
        self._arm = max(0.0, min(1.0, float(value)))
        self.update()

    armProgress = pyqtProperty(float, fget=_get_arm, fset=_set_arm)

    def _animate_arm(self, target: float) -> None:
        self._arm_anim.stop()
        self._arm_anim.setStartValue(self._arm)
        self._arm_anim.setEndValue(target)
        self._arm_anim.start()

    def _rotate(self) -> None:
        self._angle = (self._angle + DEGREES_PER_FRAME) % 360.0
        self.update()

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:  # noqa: N802
        side = min(self.width(), self.height())
        if side <= 0:
            return
        radius = side * 0.395
        center = QPointF(self.width() * 0.47, self.height() * 0.53)

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            self._paint_disc(painter, center, radius)
            self._paint_label(painter, center, radius)
            self._paint_arm(painter, center, radius)
        finally:
            painter.end()

    def _paint_disc(self, painter: QPainter, center: QPointF, radius: float) -> None:
        cx, cy = center.x(), center.y()

        # 投影：先画一个略大的半透明圆，让唱盘从背景里浮起来
        shadow = QRadialGradient(cx, cy + radius * 0.06, radius * 1.02)
        shadow.setColorAt(0.0, QColor(0, 0, 0, 150))
        shadow.setColorAt(0.86, QColor(0, 0, 0, 90))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(shadow))
        painter.drawEllipse(center, radius * 1.02, radius * 1.02)

        # 盘面本体
        body = QRadialGradient(cx - radius * 0.25, cy - radius * 0.3, radius * 1.5)
        body.setColorAt(0.0, QColor("#1a1d24"))
        body.setColorAt(0.55, QColor("#0c0e13"))
        body.setColorAt(1.0, QColor("#040508"))
        painter.setBrush(QBrush(body))
        painter.setPen(QPen(QColor(255, 255, 255, 32), max(1.0, radius * 0.006)))
        painter.drawEllipse(center, radius, radius)

        # 同心细纹。alpha 做一点起伏，看起来才像压出来的沟槽而不是描边
        inner = radius * 0.345
        grooves = 46
        for index in range(grooves):
            ratio = index / max(1, grooves - 1)
            r = inner + (radius * 0.975 - inner) * ratio
            alpha = 12 + int(12 * (0.5 + 0.5 * math.sin(index * 1.7)))
            painter.setPen(QPen(QColor(255, 255, 255, alpha), max(1.0, radius * 0.0025)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, r, r)

        # 外沿高光：右上亮、左下暗，给一点金属感
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 55), max(1.0, radius * 0.008)))
        painter.drawArc(
            QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
            20 * 16,
            70 * 16,
        )
        painter.setPen(QPen(QColor(0, 229, 255, 28), max(1.0, radius * 0.006)))
        painter.drawArc(
            QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
            200 * 16,
            60 * 16,
        )

    def _paint_label(self, painter: QPainter, center: QPointF, radius: float) -> None:
        label_radius = radius * 0.325
        cx, cy = center.x(), center.y()

        clip = QPainterPath()
        clip.addEllipse(center, label_radius, label_radius)
        painter.save()
        painter.setClipPath(clip)

        if self._art is not None:
            size = int(label_radius * 2)
            scaled = self._art.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.translate(cx, cy)
            painter.rotate(self._angle)
            painter.drawPixmap(
                int(-scaled.width() / 2), int(-scaled.height() / 2), scaled
            )
        else:
            fallback = QRadialGradient(cx, cy, label_radius)
            fallback.setColorAt(0.0, QColor(theme.PANEL_HI))
            fallback.setColorAt(1.0, QColor(theme.PANEL))
            painter.setBrush(QBrush(fallback))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(center, label_radius, label_radius)
            painter.setPen(QPen(QColor(theme.TEXT_MUTE)))
            font = QFont()
            font.setPixelSize(max(12, int(label_radius * 0.9)))
            painter.setFont(font)
            painter.drawText(
                QRectF(cx - label_radius, cy - label_radius, label_radius * 2, label_radius * 2),
                Qt.AlignmentFlag.AlignCenter,
                "♪",
            )
        painter.restore()

        # 圆标描边 + 中心轴孔
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 46), max(1.0, radius * 0.005)))
        painter.drawEllipse(center, label_radius, label_radius)

        hole = label_radius * 0.085
        painter.setBrush(QColor("#05070b"))
        painter.setPen(QPen(QColor(255, 255, 255, 60), max(1.0, radius * 0.003)))
        painter.drawEllipse(center, hole, hole)

        painter.setBrush(QColor(theme.CYAN) if self._playing else QColor(theme.BORDER_HI))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, hole * 0.42, hole * 0.42)

    def _paint_arm(self, painter: QPainter, center: QPointF, radius: float) -> None:
        pivot = QPointF(center.x() + radius * 1.02, center.y() - radius * 1.02)
        length = radius * 0.88
        # 播放时约 132°，唱针落在盘面；暂停时抬到 105°，指回盘外
        angle = math.radians(105.0 + 27.0 * self._arm)
        end = QPointF(
            pivot.x() + length * math.cos(angle),
            pivot.y() + length * math.sin(angle),
        )

        # 配重：转轴另一侧的一小段
        back = QPointF(
            pivot.x() - radius * 0.20 * math.cos(angle),
            pivot.y() - radius * 0.20 * math.sin(angle),
        )
        painter.setPen(
            QPen(
                QColor("#aab6c8"),
                max(5.0, radius * 0.045),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(pivot, back)
        painter.setBrush(QColor("#2a3348"))
        painter.setPen(QPen(QColor("#c9d5e6"), max(1.0, radius * 0.006)))
        painter.drawEllipse(back, radius * 0.052, radius * 0.052)

        # 臂身
        painter.setPen(
            QPen(
                QColor("#e8eef8"),
                max(3.0, radius * 0.022),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(pivot, end)

        # 唱头：垂直于臂的一小段
        side = QPointF(math.cos(angle + math.pi / 2), math.sin(angle + math.pi / 2))
        head_a = QPointF(end.x() + side.x() * radius * 0.05, end.y() + side.y() * radius * 0.05)
        head_b = QPointF(end.x() - side.x() * radius * 0.05, end.y() - side.y() * radius * 0.05)
        painter.setPen(
            QPen(
                QColor("#d7e2f2"),
                max(4.0, radius * 0.03),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(head_a, head_b)

        # 转轴
        painter.setBrush(QColor("#1b2437"))
        painter.setPen(QPen(QColor("#8f9db3"), max(1.0, radius * 0.008)))
        painter.drawEllipse(pivot, radius * 0.085, radius * 0.085)
        painter.setBrush(QColor(theme.CYAN) if self._playing else QColor(theme.TEXT_MUTE))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(pivot, radius * 0.025, radius * 0.025)
