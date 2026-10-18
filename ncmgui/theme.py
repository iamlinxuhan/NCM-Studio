"""暗夜霓虹主题：调色板、全局 QSS、字体与霓虹辉光。

QSS 里没有变量语法，这里用 ``string.Template`` 的 ``$NAME`` 占位符来注入调色板，
这样样式表本身仍然是可以直接阅读的纯 CSS。

注意 Qt 样式表的两个限制：
  * 不支持 ``box-shadow``，霓虹辉光只能靠 ``QGraphicsDropShadowEffect`` 模拟
  * 不支持 ``transition``，所以没有过渡动画
"""

from __future__ import annotations

from functools import lru_cache
from string import Template

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsDropShadowEffect, QWidget

# --------------------------------------------------------------------------- #
# 调色板
# --------------------------------------------------------------------------- #

BG = "#0a0e17"          # 窗口背景
BG_DEEP = "#070a11"     # 侧边栏 / 终端背景
PANEL = "#101624"       # 卡片
PANEL_ALT = "#161d2f"   # 卡片内的次级块、输入框
PANEL_HI = "#1b2437"    # 悬停态
BORDER = "#1e2a44"      # 常规描边
BORDER_HI = "#2b3c60"   # 强调描边

CYAN = "#00e5ff"        # 主强调
CYAN_SOFT = "#8ec9ff"   # 淡化强调（当前句的译文，不用主强调那么刺眼）
PURPLE = "#7c5cff"      # 副强调
GREEN = "#00ffa3"       # 成功
AMBER = "#ffb020"       # 警告
RED = "#ff4d6d"         # 危险

TEXT = "#e6edf7"        # 正文
TEXT_DIM = "#7b8aa5"    # 次要文字
TEXT_MUTE = "#4d5b75"   # 禁用 / 占位

#: 日志级别对应的颜色
LEVEL_COLORS = {
    "info": TEXT,
    "dim": TEXT_DIM,
    "success": GREEN,
    "warn": AMBER,
    "error": RED,
    "accent": CYAN,
}

_UI_CANDIDATES = (
    "Noto Sans CJK SC", "Source Han Sans SC", "Noto Sans CJK JP",
    "Microsoft YaHei", "PingFang SC", "WenQuanYi Micro Hei",
    "Inter", "Segoe UI", "DejaVu Sans", "Sans Serif",
)

_MONO_CANDIDATES = (
    # 前四个是各家自带的等宽字体：Hack/DejaVu 在 Linux 上，
    # Menlo 是 macOS 默认终端字体，Cascadia/Consolas 在 Windows 上
    "JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Fira Code",
    "Hack", "Source Code Pro", "Menlo", "DejaVu Sans Mono",
    "Consolas", "Monospace",
)


@lru_cache(maxsize=2)
def resolve_family(candidates: tuple[str, ...], fallback: str) -> str:
    """从候选列表里挑第一个系统真正装了的字体。

    需要 QApplication 已创建，因此延迟到首次调用时再查。
    """
    try:
        from PyQt6.QtGui import QFontDatabase

        installed = set(QFontDatabase.families())
    except Exception:  # pragma: no cover - 无 GUI 环境下直接回退
        return fallback

    for name in candidates:
        if name in installed:
            return name
    return fallback


def ui_family() -> str:
    return resolve_family(_UI_CANDIDATES, "sans-serif")


def mono_family() -> str:
    return resolve_family(_MONO_CANDIDATES, "monospace")


@lru_cache(maxsize=4)
def font_css(family: str) -> str:
    """把字体族名转成 QSS 可用的形式（含空格的名字要加引号）。"""
    return f'"{family}"' if " " in family else family


@lru_cache(maxsize=64)
def safe_glyph(char: str, fallback: str = "") -> str:
    """字体缺字时返回 fallback。

    侧边栏图标用的是几何符号，不同 Linux 发行版的字体覆盖差异很大，
    与其赌某个字形存在（缺字会显示成豆腐块），不如先问一下字体度量。
    """
    try:
        from PyQt6.QtGui import QFont, QFontMetrics

        metrics = QFontMetrics(QFont(ui_family()))
        return char if metrics.inFont(char[0]) else fallback
    except Exception:  # pragma: no cover - 无 GUI 环境
        return fallback


# --------------------------------------------------------------------------- #
# 样式表
# --------------------------------------------------------------------------- #

_QSS = Template(
    """
* { outline: none; }

QWidget {
    background: transparent;
    color: $TEXT;
    font-size: 13px;
}

QMainWindow, QDialog { background: $BG; }

QToolTip {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER_HI;
    border-radius: 4px;
    padding: 5px 8px;
}

/* ---------------------------------------------------------------- 侧边栏 */

QWidget#Sidebar {
    background: $BG_DEEP;
    border-right: 1px solid $BORDER;
}

QLabel#Brand {
    color: $TEXT;
    font-size: 19px;
    font-weight: 700;
    letter-spacing: 3px;
}

QLabel#BrandSub {
    color: $TEXT_MUTE;
    font-size: 10px;
    letter-spacing: 2px;
}

QPushButton#Nav {
    text-align: left;
    padding: 10px 16px;
    border: none;
    border-left: 3px solid transparent;
    border-radius: 0px;
    background: transparent;
    color: $TEXT_DIM;
    font-size: 13px;
}

QPushButton#Nav:hover {
    background: $PANEL_ALT;
    color: $TEXT;
}

QPushButton#Nav:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(0, 229, 255, 38), stop:1 transparent);
    border-left: 3px solid $CYAN;
    color: $CYAN;
    font-weight: 600;
}

QPushButton#Nav:disabled { color: $TEXT_MUTE; }

QWidget#NavGroup {
    color: $TEXT_MUTE;
    font-size: 10px;
    letter-spacing: 2px;
    padding: 14px 16px 4px 16px;
}

/* ------------------------------------------------------------------ 卡片 */

QFrame#Card {
    background: $PANEL;
    border: 1px solid $BORDER;
    border-radius: 10px;
}

QFrame#Card[accent="true"] {
    border: 1px solid $BORDER_HI;
}

QLabel#CardTitle {
    color: $TEXT;
    font-size: 14px;
    font-weight: 600;
}

QLabel#CardSubtitle {
    color: $TEXT_DIM;
    font-size: 12px;
}

QLabel#PageTitle {
    color: $TEXT;
    font-size: 22px;
    font-weight: 700;
}

QLabel#PageSubtitle {
    color: $TEXT_DIM;
    font-size: 13px;
}

QLabel#Hint {
    color: $TEXT_MUTE;
    font-size: 11px;
}

QLabel#FieldLabel {
    color: $TEXT_DIM;
    font-size: 12px;
}

QFrame#HLine {
    background: $BORDER;
    max-height: 1px;
    border: none;
}

/* ------------------------------------------------------------------ 按钮 */

QPushButton#Accent {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 $CYAN, stop:1 $PURPLE);
    color: #06121c;
    font-weight: 700;
    border: none;
    border-radius: 6px;
    padding: 9px 22px;
}

QPushButton#Accent:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #5cf0ff, stop:1 #9b83ff);
}

QPushButton#Accent:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #00b8cc, stop:1 #6448cc);
}

QPushButton#Accent:disabled {
    background: $PANEL_HI;
    color: $TEXT_MUTE;
}

QPushButton#Ghost {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 8px 16px;
}

QPushButton#Ghost:hover {
    border-color: $CYAN;
    color: $CYAN;
    background: $PANEL_HI;
}

QPushButton#Ghost:pressed { background: $PANEL; }

QPushButton#Ghost:disabled {
    color: $TEXT_MUTE;
    border-color: $BORDER;
    background: $PANEL;
}

QPushButton#Ghost[compact="true"] {
    padding: 5px 10px;
    font-size: 12px;
}

/* 播放条上的控制键：平时只有图标，悬停才浮出一层淡底色。
   不再画按钮边框 —— 一排方框在 70px 的条里显得又粗又挤 */
QPushButton#Ghost[transport="flat"] {
    padding: 0px;
    border: none;
    background: transparent;
    border-radius: 6px;
}
QPushButton#Ghost[transport="flat"]:hover {
    background: $PANEL_ALT;
}
QPushButton#Ghost[transport="flat"]:disabled {
    background: transparent;
}

QPushButton#Danger {
    background: transparent;
    color: $RED;
    border: 1px solid $RED;
    border-radius: 6px;
    padding: 8px 18px;
}

QPushButton#Danger:hover { background: rgba(255, 77, 109, 45); }
QPushButton#Danger:pressed { background: rgba(255, 77, 109, 80); }
QPushButton#Danger:disabled { color: $TEXT_MUTE; border-color: $BORDER; }

/* ------------------------------------------------------------ 输入控件 */

QLineEdit, QPlainTextEdit, QTextEdit {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 7px 10px;
    selection-background-color: $PURPLE;
    selection-color: #ffffff;
}

QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover { border-color: $BORDER_HI; }

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus { border-color: $CYAN; }

QLineEdit:disabled, QPlainTextEdit:disabled {
    color: $TEXT_MUTE;
    background: $PANEL;
}

QLineEdit[state="ok"]  { border-color: rgba(0, 255, 163, 120); }
QLineEdit[state="warn"] { border-color: rgba(255, 176, 32, 160); }
QLineEdit[state="bad"]  { border-color: rgba(255, 77, 109, 180); }

QLineEdit[readOnly="true"] { color: $TEXT_DIM; }

QComboBox {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 7px 10px;
    min-width: 90px;
}

QComboBox:hover { border-color: $BORDER_HI; }
QComboBox:focus { border-color: $CYAN; }

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $TEXT_DIM;
    margin-right: 10px;
}

QComboBox::down-arrow:hover { border-top-color: $CYAN; }

QComboBox QAbstractItemView {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER_HI;
    border-radius: 6px;
    padding: 4px;
    selection-background-color: rgba(0, 229, 255, 45);
    selection-color: $CYAN;
    outline: none;
}

QSpinBox, QDoubleSpinBox {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 7px 6px;
}

QSpinBox:focus, QDoubleSpinBox:focus { border-color: $CYAN; }

QSpinBox::up-button, QSpinBox::down-button {
    background: $PANEL_HI;
    border: none;
    width: 18px;
}

QSpinBox::up-button { border-top-right-radius: 5px; }
QSpinBox::down-button { border-bottom-right-radius: 5px; }

QSpinBox::up-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid $TEXT_DIM;
}

QSpinBox::down-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid $TEXT_DIM;
}

QSpinBox::up-arrow:hover, QSpinBox::down-arrow:hover {
    border-bottom-color: $CYAN;
    border-top-color: $CYAN;
}

/* 复选框做成胶囊型开关 */
QCheckBox {
    color: $TEXT;
    spacing: 9px;
}

QCheckBox::indicator {
    width: 38px;
    height: 20px;
    border-radius: 10px;
    background: $PANEL_ALT;
    border: 1px solid $BORDER_HI;
}

QCheckBox::indicator:hover { border-color: $CYAN; }

QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 $CYAN, stop:1 $PURPLE);
    border: 1px solid $CYAN;
}

QCheckBox:disabled { color: $TEXT_MUTE; }

QRadioButton { color: $TEXT; spacing: 8px; }

QRadioButton::indicator {
    width: 15px;
    height: 15px;
    border-radius: 8px;
    background: $PANEL_ALT;
    border: 1px solid $BORDER_HI;
}

QRadioButton::indicator:checked {
    background: $CYAN;
    border: 4px solid $PANEL;
}

/* -------------------------------------------------------------- 滚动条 */

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}

QScrollBar::handle:vertical {
    background: $BORDER_HI;
    border-radius: 5px;
    min-height: 32px;
}

QScrollBar::handle:vertical:hover { background: $CYAN; }

QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}

QScrollBar::handle:horizontal {
    background: $BORDER_HI;
    border-radius: 5px;
    min-width: 32px;
}

QScrollBar::handle:horizontal:hover { background: $CYAN; }

QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QScrollArea { border: none; background: transparent; }

/* ------------------------------------------------------------ 标签页/列表 */

QTabWidget::pane { border: none; background: transparent; }

QTabBar { qproperty-drawBase: 0; }

QTabBar::tab {
    background: transparent;
    color: $TEXT_DIM;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 7px 14px;
    margin-right: 2px;
}

QTabBar::tab:hover { color: $TEXT; }

QTabBar::tab:selected {
    color: $CYAN;
    border-bottom: 2px solid $CYAN;
    font-weight: 600;
}

QTabBar::close-button {
    subcontrol-position: right;
    padding: 2px;
}

QListWidget, QListView, QTreeWidget {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 4px;
    outline: none;
}

QListWidget::item, QListView::item { padding: 5px 8px; border-radius: 4px; }

QListWidget::item:selected, QListView::item:selected {
    background: rgba(0, 229, 255, 45);
    color: $CYAN;
}

QListWidget::item:hover, QListView::item:hover { background: $PANEL_HI; }

/* 歌曲列表（我喜欢的音乐）—— 表格自己有滚动条，不再套 QScrollArea */

QTableView, QTableWidget {
    background: $PANEL_ALT;
    alternate-background-color: $PANEL;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 6px;
    gridline-color: transparent;
    selection-background-color: rgba(0, 229, 255, 45);
    selection-color: $CYAN;
    outline: none;
}

QTableView::item { padding: 4px 8px; }

QHeaderView::section {
    background: $PANEL;
    color: $TEXT_DIM;
    border: none;
    border-bottom: 1px solid $BORDER;
    padding: 7px 8px;
    font-size: 11px;
    font-weight: 600;
}

QTableCornerButton::section { background: $PANEL; border: none; }

/* 进度条 / 音量滑块 */

QSlider::groove:horizontal {
    height: 4px;
    background: $BORDER;
    border-radius: 2px;
}

QSlider::sub-page:horizontal {
    background: $CYAN;
    border-radius: 2px;
}

QSlider::add-page:horizontal {
    background: $BORDER;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: $CYAN;
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
}

QSlider::handle:horizontal:hover { background: $PURPLE; }

QSlider::handle:horizontal:disabled { background: $BORDER_HI; }

/* ------------------------------------------------------------ 播放条 */

QWidget#PlaybackBar {
    background: $PANEL;
    border-top: 1px solid $BORDER;
}

QLabel#PlaybackTime {
    color: $TEXT_DIM;
    font-size: 11px;
    font-family: $MONO;
}

/* 播放条左侧的「正在播放」，点一下打开播放页 */
QWidget#NowPlaying {
    background: transparent;
    border-radius: 7px;
}
QWidget#NowPlaying:hover {
    background: $PANEL_HI;
}
QLabel#NowPlayingTitle {
    color: $TEXT;
    font-size: 13px;
    font-weight: 600;
}
QLabel#NowPlayingArtist {
    color: $TEXT_DIM;
    font-size: 11px;
}

/* 点喇叭后弹出的竖直音量条 */
QWidget#VolumePopup {
    background: $PANEL_ALT;
    border: 1px solid $BORDER_HI;
    border-radius: 9px;
}
QLabel#VolumeValue {
    color: $TEXT_DIM;
    font-size: 11px;
    font-family: $MONO;
}
QSlider#VolumeSlider::groove:vertical {
    width: 4px;
    background: $BORDER;
    border-radius: 2px;
}
/* Qt 的竖直滑条里 sub-page 在滑块上方、add-page 在下方；
   这里要的是「已调到的音量」从底部填到滑块，所以填 add-page */
QSlider#VolumeSlider::sub-page:vertical {
    background: $BORDER;
    border-radius: 2px;
}
QSlider#VolumeSlider::add-page:vertical {
    background: $CYAN;
    border-radius: 2px;
}
QSlider#VolumeSlider::handle:vertical {
    background: $CYAN;
    width: 12px;
    height: 12px;
    margin: 0 -5px;
    border-radius: 6px;
}
QSlider#VolumeSlider::handle:vertical:hover {
    background: $PURPLE;
}

/* ------------------------------------------------------------ 播放页 */

QLabel#PlayerTitle {
    color: $TEXT;
    font-size: 26px;
    font-weight: 700;
}

QLabel#PlayerArtist {
    color: $CYAN;
    font-size: 14px;
}

QScrollArea#LyricsArea, QWidget#LyricsBody {
    background: transparent;
    border: none;
}

QLabel#LyricLine {
    color: $TEXT_DIM;
    font-size: 15px;
    padding: 2px 6px;
}

QLabel#LyricLine[current="true"] {
    color: $CYAN;
    font-size: 18px;
    font-weight: 700;
}

QLabel#LyricLine[translation="true"] {
    color: $TEXT_MUTE;
    font-size: 12px;
}

QLabel#LyricLine[current="true"][translation="true"] {
    color: $CYAN_SOFT;
    font-size: 12px;
    font-weight: 400;
}

QListView#QueueList { font-size: 12px; }

/* ------------------------------------------------------------ 终端区 */

QPlainTextEdit#Console {
    background: $BG_DEEP;
    color: #c3d2e6;
    border: none;
    border-radius: 0px;
    padding: 8px 10px;
    font-family: $MONO;
    font-size: 12px;
}

QWidget#ConsoleHead {
    background: $PANEL;
    border-top: 1px solid $BORDER;
    border-bottom: 1px solid $BORDER;
}

QWidget#ConsoleDock { background: $PANEL; border-top: 1px solid $BORDER; }

QLabel#ConsoleTitle {
    color: $TEXT_DIM;
    font-size: 11px;
    letter-spacing: 1px;
}

QLineEdit#StdinBar {
    background: $BG_DEEP;
    border: 1px solid $BORDER_HI;
    border-radius: 4px;
    padding: 5px 9px;
    font-family: $MONO;
    font-size: 12px;
}

QLabel#Chip {
    border-radius: 9px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
}

QLabel#Chip[state="on"] {
    background: rgba(0, 255, 163, 30);
    color: $GREEN;
    border: 1px solid rgba(0, 255, 163, 110);
}

QLabel#Chip[state="off"] {
    background: rgba(123, 138, 165, 20);
    color: $TEXT_DIM;
    border: 1px solid $BORDER_HI;
}

QLabel#Chip[state="warn"] {
    background: rgba(255, 176, 32, 28);
    color: $AMBER;
    border: 1px solid rgba(255, 176, 32, 110);
}

/* --------------------------------------------------------------- 其他 */

QMenu {
    background: $PANEL_ALT;
    color: $TEXT;
    border: 1px solid $BORDER_HI;
    border-radius: 6px;
    padding: 5px;
}

QMenu::item { padding: 6px 22px 6px 14px; border-radius: 4px; }
QMenu::item:selected { background: rgba(0, 229, 255, 45); color: $CYAN; }
QMenu::separator { height: 1px; background: $BORDER; margin: 5px 8px; }

QSplitter::handle { background: $BORDER; }
QSplitter::handle:vertical { height: 1px; }
QSplitter::handle:hover { background: $CYAN; }

QProgressBar {
    background: $PANEL_ALT;
    border: 1px solid $BORDER;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: transparent;
}

QProgressBar::chunk {
    border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 $CYAN, stop:1 $PURPLE);
}

QMessageBox { background: $PANEL; }
QMessageBox QLabel { color: $TEXT; }

QFileDialog QWidget { background: $BG; }

QStatusBar {
    background: $BG_DEEP;
    color: $TEXT_DIM;
    border-top: 1px solid $BORDER;
}

QStatusBar::item { border: none; }
"""
)


def stylesheet() -> str:
    """生成完整的全局样式表。"""
    return _QSS.substitute(
        BG=BG,
        BG_DEEP=BG_DEEP,
        PANEL=PANEL,
        PANEL_ALT=PANEL_ALT,
        PANEL_HI=PANEL_HI,
        BORDER=BORDER,
        BORDER_HI=BORDER_HI,
        CYAN=CYAN,
        CYAN_SOFT=CYAN_SOFT,
        PURPLE=PURPLE,
        GREEN=GREEN,
        AMBER=AMBER,
        RED=RED,
        TEXT=TEXT,
        TEXT_DIM=TEXT_DIM,
        TEXT_MUTE=TEXT_MUTE,
        MONO=font_css(mono_family()),
    )


def apply_theme(app) -> None:
    """把主题应用到 QApplication（样式表 + 字体 + 调色板）。"""
    from PyQt6.QtGui import QFont, QPalette

    app.setStyleSheet(stylesheet())
    app.setFont(QFont(ui_family(), 10))

    # 占位符文字通过调色板设置：QSS 的 placeholder-text-color 各版本支持不一，
    # 而默认的占位色在深色背景上太亮，容易和真实内容混淆。
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTE))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(PURPLE))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)


def glow(widget: QWidget, color: str = CYAN, blur: int = 26, alpha: int = 150):
    """给控件加一圈霓虹辉光。

    QSS 没有 box-shadow，只能靠 QGraphicsDropShadowEffect 模拟。
    返回 effect 对象，需要动态改色时可以直接调整。
    """
    effect = QGraphicsDropShadowEffect(widget)
    tint = QColor(color)
    tint.setAlpha(alpha)
    effect.setColor(tint)
    effect.setBlurRadius(blur)
    effect.setOffset(0, 0)
    widget.setGraphicsEffect(effect)
    return effect
