"""黑胶唱片样式的封面。

全部用 ``QPainter`` 画，不依赖任何图片资源：

* 盘面是径向渐变 + 几十圈同心细纹（groove），外沿一道高光；
* 中心圆标是专辑封面，播放时以 50ms 一帧缓慢旋转，暂停立刻停下；
* 唱臂从右上角的转轴伸向盘面：暂停时停在盘外的支架上，播放时落到外侧
  沟槽，臂身带一点 S 形弯曲，用属性动画过渡。

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

    def finish_arm_animation(self) -> None:
        """截图用：跳过过渡，直接把唱臂放到当前状态该在的位置。"""
        self._arm_anim.stop()
        self._set_arm(1.0 if self._playing else 0.0)

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
        inner = radius * 0.500
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
        # 真唱片的圆标约占直径的三分之一，但界面上封面本身就是主角，
        # 放大到 0.46 才不至于让黑胶盘面显得又空又大
        label_radius = radius * 0.46
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
        # 转轴在右上方。暂停 82°：整根臂停在盘外的支架上；播放 106°：
        # 唱针落到唱片外侧的沟槽（离盘心约 0.74 个盘半径），不会压在封面上
        pivot = QPointF(center.x() + radius * 0.98, center.y() - radius * 0.96)
        angle = math.radians(82.0 + 24.0 * self._arm)
        length = radius * 0.90
        direction = QPointF(math.cos(angle), math.sin(angle))
        normal = QPointF(-direction.y(), direction.x())
        head = QPointF(
            pivot.x() + direction.x() * length,
            pivot.y() + direction.y() * length,
        )

        def point_at(t: float, offset: float) -> QPointF:
            return QPointF(
                pivot.x() + direction.x() * length * t + normal.x() * offset,
                pivot.y() + direction.y() * length * t + normal.y() * offset,
            )

        # 臂身带一点 S 形弯曲。直杆 + 垂直唱头的组合太像硬盘读取针
        arm = QPainterPath(pivot)
        arm.cubicTo(
            point_at(0.34, -radius * 0.030),
            point_at(0.72, radius * 0.024),
            head,
        )

        # 盘外的唱臂支架。暂停时唱针正好停在这里
        rest_angle = math.radians(82.0)
        rest = QPointF(
            pivot.x() + math.cos(rest_angle) * length,
            pivot.y() + math.sin(rest_angle) * length,
        )
        painter.setBrush(QColor("#141b28"))
        painter.setPen(QPen(QColor("#4b576c"), max(1.0, radius * 0.006)))
        painter.drawEllipse(rest, radius * 0.048, radius * 0.048)
        painter.setBrush(QColor("#0a0e16"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(rest, radius * 0.024, radius * 0.024)

        # 影子：唱臂抬得越高，影子越淡，画面才有高度差
        shadow = QPainterPath(arm)
        shadow.translate(radius * 0.018, radius * 0.030)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                QColor(0, 0, 0, int(90 - 30 * self._arm)),
                max(2.0, radius * 0.020),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawPath(shadow)

        # 配重：转轴后方的一小截，末端一个圆柱
        back = QPointF(
            pivot.x() - direction.x() * radius * 0.12,
            pivot.y() - direction.y() * radius * 0.12,
        )
        back_end = QPointF(
            pivot.x() - direction.x() * radius * 0.23,
            pivot.y() - direction.y() * radius * 0.23,
        )
        painter.setPen(
            QPen(
                QColor("#8e9bb0"),
                max(2.0, radius * 0.011),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(pivot, back)
        painter.setPen(
            QPen(
                QColor("#39445a"),
                max(5.0, radius * 0.072),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(back, back_end)
        painter.setPen(
            QPen(
                QColor(214, 225, 242, 120),
                max(1.0, radius * 0.010),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(
            QPointF(back.x(), back.y() - radius * 0.016),
            QPointF(back_end.x(), back_end.y() - radius * 0.016),
        )

        # 臂身：深色外沿 + 亮色内芯，做出金属圆管的层次
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                QColor("#6d7a90"),
                max(3.0, radius * 0.020),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawPath(arm)
        painter.setPen(
            QPen(
                QColor("#e8eef9"),
                max(1.5, radius * 0.008),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawPath(arm)

        # 唱头：相对臂身偏一点角度，前端是小小的针尖
        shell_angle = angle - math.radians(16.0)
        shell = QPointF(math.cos(shell_angle), math.sin(shell_angle))
        shell_back = QPointF(
            head.x() - shell.x() * radius * 0.020,
            head.y() - shell.y() * radius * 0.020,
        )
        shell_front = QPointF(
            head.x() + shell.x() * radius * 0.085,
            head.y() + shell.y() * radius * 0.085,
        )
        painter.setPen(
            QPen(
                QColor("#1a2233"),
                max(5.0, radius * 0.038),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(shell_back, shell_front)
        painter.setPen(
            QPen(
                QColor("#aebbd0"),
                max(1.0, radius * 0.006),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(shell_back, shell_front)
        # 针尖朝向盘面
        tip = QPointF(
            shell_front.x() + normal.x() * radius * 0.018,
            shell_front.y() + normal.y() * radius * 0.018,
        )
        painter.setPen(
            QPen(
                QColor(theme.CYAN) if self._playing else QColor("#cfd9e8"),
                max(1.5, radius * 0.008),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(shell_front, tip)

        # 转轴底座
        painter.setBrush(QColor("#161d2c"))
        painter.setPen(QPen(QColor("#566379"), max(1.0, radius * 0.007)))
        painter.drawEllipse(pivot, radius * 0.070, radius * 0.070)
        painter.setBrush(QColor("#2b3549"))
        painter.setPen(QPen(QColor("#8f9db3"), max(1.0, radius * 0.006)))
        painter.drawEllipse(pivot, radius * 0.042, radius * 0.042)
        painter.setBrush(QColor(theme.CYAN) if self._playing else QColor(theme.TEXT_MUTE))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(pivot, radius * 0.013, radius * 0.013)
