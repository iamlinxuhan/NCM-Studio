"""主题切换键：自绘的太阳 / 月亮。

图标**不用 emoji 字符**，自己画。``☀``（U+2600）在 Unicode 里默认是文本呈现，
``🌙``（U+1F319）在本机也常被 fontconfig 交给了单色字体；整条 UI 字体链上
未必排得到彩色 emoji 字体，于是两个字形都是黑的。更麻烦的是「长什么样」取决
于机器上装了什么字体，换台机器就变样。

自绘之后颜色取 ``theme.*``、形状自己定，跨平台一致，也才有了「太阳是暖的」
这件事。图标画的是**当前**主题：亮色配太阳、深色配月亮，一眼就知道自己在
哪一档，不必先反推点击后会去哪儿。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QBrush, QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import QPushButton, QWidget

from .. import theme

_SUN_DOTS = 8       # 太阳外圈圆点数
_SUN_DISC = 0.62    # 中心圆盘半径 / r
_SUN_DOT = 0.15     # 单个外圈圆点半径 / r
_SUN_ORBIT = 0.82   # 外圈圆点轨道半径 / r

#: 图标设计边长。按钮高 26，16px 的图标上下还能各留 5px。
ICON_PX = 16


class ThemeIcon(QWidget):
    """太阳 / 月亮。颜色在 ``paintEvent`` 里现取主题 token。"""

    def __init__(self, sun: bool, size: int = ICON_PX, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sun = bool(sun)
        self._size = float(size)
        self.setFixedSize(size, size)
        # 不吃鼠标事件：它盖在按钮右侧，吃掉那一小块按钮就点不动了
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_sun(self, sun: bool) -> None:
        """切换太阳 / 月亮；值没变就不重画。"""
        sun = bool(sun)
        if sun != self._sun:
            self._sun = sun
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        radius = self._size / 2.0
        if self._sun:
            self._paint_sun(painter, center, radius)
        else:
            self._paint_moon(painter, center, radius)
        painter.end()

    @staticmethod
    def _paint_sun(painter: QPainter, center: QPointF, radius: float) -> None:
        """一个实心圆盘 + 一圈小圆点 —— 就是 ☀ 的画法。

        刻意不用放射状长射线：那种「细长尖刺 + 小圆心」是好几家 AI 产品的标志
        形态，缩到 16px 几乎重合；圆点怎么摆都不会撞上任何一家的 logotype。
        """
        color = QColor(theme.SUN_COLOR)
        disc = radius * _SUN_DISC
        dot = radius * _SUN_DOT
        orbit = radius * _SUN_ORBIT
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        for index in range(_SUN_DOTS):
            # 减去 π/2：让第一个点落在正上方而不是正右方，看起来才「正」
            angle = math.tau * index / _SUN_DOTS - math.pi / 2.0
            painter.drawEllipse(
                QPointF(
                    center.x() + math.cos(angle) * orbit,
                    center.y() + math.sin(angle) * orbit,
                ),
                dot,
                dot,
            )
        painter.drawEllipse(center, disc, disc)

    @staticmethod
    def _paint_moon(painter: QPainter, center: QPointF, radius: float) -> None:
        """月牙 = 大圆**减去**一个偏移的等大圆。

        用 ``QPainterPath.subtracted``，而不是「在圆上再画一个底色圆」：按钮有
        hover / pressed / disabled 几种底色，拿固定颜色去盖，总有一种会露馅。
        真正的布尔减不关心底下是什么。
        """
        outer = QPainterPath()
        outer.addEllipse(center, radius, radius)
        # 偏移量决定月牙最厚处：等大圆错开 d，留下的月牙最厚就是 d。
        # 0.62r 实测既不瘦成一条线，也不胖成一个缺口圆。
        hole = QPainterPath()
        hole.addEllipse(
            QPointF(center.x() + radius * 0.62, center.y() - radius * 0.30),
            radius,
            radius,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(theme.MOON_COLOR)))
        painter.drawPath(outer.subtracted(hole))


class ThemeToggle(QPushButton):
    """侧边栏里的「主题」键：文字在左，自绘图标在右。

    图标是按钮的子控件，固定在右侧；QSS 用 ``themeToggle`` 属性给右侧留出
    28px（见 theme.py），文字不会和图标叠在一起。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("主题（点击以切换）", parent)
        self.setObjectName("Ghost")
        self.setProperty("compact", "true")
        self.setProperty("themeToggle", "true")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)
        self.setAccessibleName("切换深色 / 浅色主题")

        self._icon = ThemeIcon(not theme.is_dark(), ICON_PX, self)
        self.refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._place_icon()

    def _place_icon(self) -> None:
        margin = 10
        x = self.width() - margin - self._icon.width()
        y = (self.height() - self._icon.height()) // 2
        self._icon.move(max(0, x), max(0, y))

    def refresh(self) -> None:
        """主题变化后更新图标与提示。"""
        dark = theme.is_dark()
        self._icon.set_sun(not dark)
        self.setToolTip(f"当前：{'深色' if dark else '亮色'}主题 · 点击切换")
        self._place_icon()
        self.update()
