"""黑胶唱片样式的封面。

全部用 ``QPainter`` 画，不依赖任何图片资源：

* 盘面是径向渐变 + 几十圈同心细纹（groove），外沿一道高光；
* 中心圆标是专辑封面，播放时以 50ms 一帧缓慢旋转，暂停立刻停下；
* 唱臂从右上角的转轴伸向盘面：暂停时抬到盘外，播放时落到外侧沟槽，
  臂身带一点 S 形弯曲，用属性动画过渡。

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

        # 盘面（阴影、渐变、46 圈沟槽、外沿高光）是静态的，每帧重画一遍
        # 纯属浪费；缓存成位图，只在尺寸/缩放比变化时重建
        self._disc_cache: QPixmap | None = None
        self._disc_cache_key: tuple[float, ...] | None = None
        # 封面按当前圆标大小预先缩放好，避免每帧都在 paintEvent 里做
        # SmoothTransformation（20 帧/秒就是每秒几十次重采样）
        self._cover_cache: QPixmap | None = None
        self._cover_cache_key: tuple[int, int] | None = None

        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._rotate)

        self._arm_anim = QPropertyAnimation(self, b"armProgress", self)
        self._arm_anim.setDuration(450)
        self._arm_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    # ------------------------------------------------------------ 对外接口

    def set_art(self, pixmap: QPixmap | None) -> None:
        self._art = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._cover_cache = None
        self._cover_cache_key = None
        self.update()

    def set_playing(self, playing: bool) -> None:
        playing = bool(playing)
        if playing == self._playing:
            return
        self._playing = playing
        self._sync_timer()
        self._animate_arm(1.0 if playing else 0.0)

    # ------------------------------------------------------------ 可见性

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:  # noqa: N802
        # 页面被切走时不空转；回来再继续
        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._disc_cache = None
        self._disc_cache_key = None
        self._cover_cache = None
        self._cover_cache_key = None
        super().resizeEvent(event)

    def _sync_timer(self) -> None:
        if self._playing and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()

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
        # 只有封面在转，盘面和唱臂都没动。只失效圆标那一块，别让整个
        # 控件（全屏时可能上千像素宽）每 50ms 重绘一次
        self.update(self._label_rect().toAlignedRect())

    # ------------------------------------------------------------ 绘制

    def _geometry(self) -> tuple[float, QPointF, float]:
        side = min(self.width(), self.height())
        radius = side * 0.395
        center = QPointF(self.width() * 0.47, self.height() * 0.53)
        return side, center, radius

    def _label_rect(self) -> QRectF:
        _, center, radius = self._geometry()
        label_radius = radius * 0.62
        # 圆标外还有一圈描边，留出抗锯齿的余量
        margin = max(4.0, radius * 0.03)
        extent = label_radius + margin
        return QRectF(
            center.x() - extent,
            center.y() - extent,
            extent * 2,
            extent * 2,
        )

    def _disc_rect(self, center: QPointF, radius: float) -> QRectF:
        # 阴影最大画到 1.02r，取 1.03r 留一点余量
        extent = radius * 1.03
        return QRectF(
            center.x() - extent,
            center.y() - extent,
            extent * 2,
            extent * 2,
        )

    def _disc_pixmap(self, center: QPointF, radius: float) -> QPixmap:
        dpr = self.devicePixelRatioF()
        key = (float(self.width()), float(self.height()), dpr)
        if self._disc_cache is not None and self._disc_cache_key == key:
            return self._disc_cache

        rect = self._disc_rect(center, radius)
        pixmap = QPixmap(
            max(1, round(rect.width() * dpr)),
            max(1, round(rect.height() * dpr)),
        )
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(-rect.left(), -rect.top())
            self._paint_disc(painter, center, radius)
        finally:
            painter.end()

        self._disc_cache = pixmap
        self._disc_cache_key = key
        return pixmap

    def _scaled_art(self, label_radius: float) -> QPixmap | None:
        if self._art is None:
            return None
        dpr = self.devicePixelRatioF()
        size = max(1, round(label_radius * 2 * dpr))
        key = (self._art.cacheKey(), size)
        if self._cover_cache is None or self._cover_cache_key != key:
            scaled = self._art.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            self._cover_cache = scaled
            self._cover_cache_key = key
        return self._cover_cache

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

            disc = self._disc_pixmap(center, radius)
            dpr = disc.devicePixelRatio()
            logical = QRectF(0.0, 0.0, disc.width() / dpr, disc.height() / dpr)
            logical.moveTopLeft(self._disc_rect(center, radius).topLeft())
            painter.drawPixmap(logical, disc, QRectF(disc.rect()))

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
        inner = radius * 0.655
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
        # 按网易云那种比例：封面约占盘半径的六成，只留一圈黑胶沟槽。
        # 真唱片没这么大，但界面上封面才是主角
        label_radius = radius * 0.62
        cx, cy = center.x(), center.y()

        clip = QPainterPath()
        clip.addEllipse(center, label_radius, label_radius)
        painter.save()
        painter.setClipPath(clip)

        if self._art is not None:
            scaled = self._scaled_art(label_radius)
            if scaled is not None:
                dpr = scaled.devicePixelRatio()
                width = scaled.width() / dpr
                height = scaled.height() / dpr
                painter.translate(cx, cy)
                painter.rotate(self._angle)
                painter.drawPixmap(
                    QRectF(-width / 2, -height / 2, width, height),
                    scaled,
                    QRectF(scaled.rect()),
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

        # 封面与盘面之间的一道暗缝，做出「嵌进去」的层次；不画中心轴孔 ——
        # 封面放大以后，中间再挖一个黑洞只会显得脏
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 170), max(2.0, radius * 0.014)))
        painter.drawEllipse(center, label_radius, label_radius)
        painter.setPen(QPen(QColor(255, 255, 255, 34), max(1.0, radius * 0.004)))
        painter.drawEllipse(center, label_radius * 1.014, label_radius * 1.014)

    def _paint_arm(self, painter: QPainter, center: QPointF, radius: float) -> None:
        # 转轴在右上方。暂停 82°：整根臂抬在盘外；播放 100°：唱针落到
        # 封面外那圈沟槽的中段（离盘心约 0.83 个盘半径），不会压在封面上
        # —— 封面放大到 0.62 之后，落点必须跟着外移
        pivot = QPointF(center.x() + radius * 0.98, center.y() - radius * 0.96)
        angle = math.radians(82.0 + 18.0 * self._arm)
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
