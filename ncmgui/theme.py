"""双主题：暗夜霓虹与冷调亮色，外加全局 QSS、字体与霓虹辉光。

主题在运行时可切换。所有 token 同时以模块级名字暴露（``theme.CYAN`` 等），
自绘控件在 ``paintEvent`` 里现取，因此切换后只需要 ``update()`` 重画一次。
QSS 里没有变量语法，这里用 ``string.Template`` 的 ``$NAME`` 占位符注入调色板，
这样样式表本身仍然是可以直接阅读的纯 CSS。

亮色不是「把暗色反过来」：底色用偏蓝的冷白、描边保持低饱和蓝灰、强调色仍取
青与紫，终端区则始终是深色 —— 这套组合在日光下清晰，又保留原来的科技感。

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

#: 暗色。原来的唯一一套配色，数值保持不变。
DARK = {
    "BG": "#0a0e17",          # 窗口背景
    "BG_DEEP": "#070a11",     # 侧边栏
    "PANEL": "#101624",       # 卡片
    "PANEL_ALT": "#161d2f",   # 卡片内的次级块、输入框
    "PANEL_HI": "#1b2437",    # 悬停态
    "BORDER": "#1e2a44",      # 常规描边
    "BORDER_HI": "#2b3c60",   # 强调描边

    "CYAN": "#00e5ff",        # 主强调
    "CYAN_SOFT": "#8ec9ff",   # 淡化强调（当前句的译文）
    "PURPLE": "#7c5cff",      # 副强调
    "GREEN": "#00ffa3",       # 成功
    "AMBER": "#ffb020",       # 警告
    "RED": "#ff4d6d",         # 危险

    "TEXT": "#e6edf7",        # 正文
    "TEXT_DIM": "#7b8aa5",    # 次要文字
    "TEXT_MUTE": "#4d5b75",   # 禁用 / 占位

    # 逐字歌词：未唱的字浅蓝，幕布扫过之后变深；LYRIC_CURTAIN_BAND 是软边宽度
    # 相对字号的倍数（实际取 max(10px, 字号 * 倍数)），保证边缘落在单个字内部时
    # 也看得出从左到右的连续渐变。
    "LYRIC_UNSUNG": "#a9d6ff",
    "LYRIC_SUNG": "#4a7fd0",
    "LYRIC_CURTAIN_BAND": 0.8,

    # 终端区在两种主题里都保持深色（见下面 LIGHT 里的说明），所以这套颜色
    # 两套主题共用一份。
    "CONSOLE_BG": "#070a11",
    "CONSOLE_TEXT": "#c3d2e6",
    "CONSOLE_DIM": "#7b8aa5",
    "CONSOLE_CYAN": "#4fd8ff",
    "CONSOLE_GREEN": "#3ddc97",
    "CONSOLE_AMBER": "#ffc857",
    "CONSOLE_RED": "#ff6b81",
    "CONSOLE_PURPLE": "#a78bfa",

    # 主题图标的太阳 / 月亮。图标画的是「当前主题」，所以暗色下实际只用到月亮。
    "SUN_COLOR": "#ffb020",
    "MOON_COLOR": "#e6edf7",

    # 半透明强调色。QSS 里不能做颜色运算，这些组合只能预先算好。
    "CYAN_WASH": "rgba(0, 229, 255, 38)",
    "CYAN_WASH_STRONG": "rgba(0, 229, 255, 45)",
    "GREEN_WASH": "rgba(0, 255, 163, 30)",
    "GREEN_EDGE": "rgba(0, 255, 163, 110)",
    "AMBER_WASH": "rgba(255, 176, 32, 28)",
    "AMBER_EDGE": "rgba(255, 176, 32, 110)",
    "RED_WASH": "rgba(255, 77, 109, 45)",
    "RED_WASH_STRONG": "rgba(255, 77, 109, 80)",
    "RED_EDGE": "rgba(255, 77, 109, 180)",
    "TEXT_MUTE_WASH": "rgba(123, 138, 165, 20)",

    "CYAN_HOVER": "#5cf0ff",
    "PURPLE_HOVER": "#9b83ff",
    "CYAN_DEEP": "#00b8cc",
    "PURPLE_DEEP": "#6448cc",
    "ACCENT_ON": "#06121c",   # 渐变主按钮上的文字
}

#: 亮色。冷白底 + 蓝灰描边 + 青/紫强调，尽量保留霓虹的色相，只把明度翻过来。
LIGHT = {
    "BG": "#edf2f9",
    "BG_DEEP": "#e4ecf7",
    "PANEL": "#ffffff",
    "PANEL_ALT": "#f4f8fd",
    "PANEL_HI": "#e8f0fb",
    "BORDER": "#d5e0ee",
    "BORDER_HI": "#a9bddb",

    # 青与紫都压到能在白底上读的深度；色相不变，仍是一眼能认出的那套霓虹
    "CYAN": "#0079a8",
    "CYAN_SOFT": "#2f6fa6",
    "PURPLE": "#5b45d6",
    "GREEN": "#00875f",
    "AMBER": "#a26000",
    "RED": "#c62b4c",

    "TEXT": "#13203a",
    "TEXT_DIM": "#51617c",
    "TEXT_MUTE": "#8391a7",

    # 浅底上的幕布：唱过的用偏亮的靛蓝而不是近黑的海军蓝 —— 原来的 #123f78
    # 在白底上像墨块，和整页的冷白调性不搭。两档之间仍保留约 2.7 倍的明度差
    # （与暗色主题一致），幕布边缘的字内渐变依然清楚。
    "LYRIC_UNSUNG": "#6b9bd4",
    "LYRIC_SUNG": "#1d4f96",
    "LYRIC_CURTAIN_BAND": 0.8,

    # 终端区刻意保持深色：它是「命令输出」，不是普通表单。日志行在追加时就把
    # 颜色写进了 QTextDocument，跟着主题改底会留下大片读不清的旧颜色；固定深底
    # + 固定配色既避免这个问题，也让亮色界面里保留一块硬核的科技感。
    "CONSOLE_BG": "#070a11",
    "CONSOLE_TEXT": "#c3d2e6",
    "CONSOLE_DIM": "#7b8aa5",
    "CONSOLE_CYAN": "#4fd8ff",
    "CONSOLE_GREEN": "#3ddc97",
    "CONSOLE_AMBER": "#ffc857",
    "CONSOLE_RED": "#ff6b81",
    "CONSOLE_PURPLE": "#a78bfa",

    "SUN_COLOR": "#dd9500",
    "MOON_COLOR": "#51617c",

    "CYAN_WASH": "rgba(0, 121, 168, 24)",
    "CYAN_WASH_STRONG": "rgba(0, 121, 168, 34)",
    "GREEN_WASH": "rgba(0, 135, 95, 22)",
    "GREEN_EDGE": "rgba(0, 135, 95, 120)",
    "AMBER_WASH": "rgba(162, 96, 0, 22)",
    "AMBER_EDGE": "rgba(162, 96, 0, 130)",
    "RED_WASH": "rgba(198, 43, 76, 22)",
    "RED_WASH_STRONG": "rgba(198, 43, 76, 40)",
    "RED_EDGE": "rgba(198, 43, 76, 150)",
    "TEXT_MUTE_WASH": "rgba(131, 145, 167, 18)",

    "CYAN_HOVER": "#2f9cc3",
    "PURPLE_HOVER": "#7a68e8",
    "CYAN_DEEP": "#006184",
    "PURPLE_DEEP": "#4936b8",
    "ACCENT_ON": "#ffffff",
}

PALETTES = {"dark": DARK, "light": LIGHT}

#: 当前主题名。切换只改这里和下面那批模块级 token。
_current = "dark"

#: 所有可随主题变化的 token 名（也是写进模块命名空间的那批名字）
TOKENS = tuple(DARK)

#: 日志级别对应的颜色
LEVEL_COLORS: dict[str, str] = {}


def _sync_tokens(name: str) -> None:
    """把某套调色板写进模块命名空间，并刷新派生表。"""
    values = PALETTES[name]
    globals().update(values)
    LEVEL_COLORS.update({
        "info": values["TEXT"],
        "dim": values["TEXT_DIM"],
        "success": values["GREEN"],
        "warn": values["AMBER"],
        "error": values["RED"],
        "accent": values["CYAN"],
    })


def normalize_theme(name: object) -> str:
    """只接受 ``dark`` / ``light``，其余一律按暗色处理。"""
    text = str(name or "").strip().lower()
    return text if text in PALETTES else "dark"


def current_theme() -> str:
    return _current


def is_dark() -> bool:
    return _current == "dark"


def set_theme(name: str) -> str:
    """切换当前主题并返回规范化后的名字（不负责重绘界面）。"""
    global _current
    _current = normalize_theme(name)
    _sync_tokens(_current)
    return _current


# 初始化：模块级 token 一开始就是暗色那套，保证 ``import`` 之后立即可用。
_sync_tokens(_current)

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
                stop:0 $CYAN_WASH, stop:1 transparent);
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

/* 二维码占位框。原来是一条内联样式，换主题时不会跟着变，收进 QSS 统一管理 */
QLabel#QrPlaceholder {
    color: $TEXT_DIM;
    font-size: 12px;
    border: 1px dashed $BORDER_HI;
    border-radius: 8px;
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
    color: $ACCENT_ON;
    font-weight: 700;
    border: none;
    border-radius: 6px;
    padding: 9px 22px;
}

QPushButton#Accent:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 $CYAN_HOVER, stop:1 $PURPLE_HOVER);
}

QPushButton#Accent:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 $CYAN_DEEP, stop:1 $PURPLE_DEEP);
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

/* 主题切换键：右侧留给自绘的太阳 / 月亮（见 widgets/themetoggle.py） */
QPushButton#Ghost[themeToggle="true"] {
    padding: 0 28px 0 10px;
    font-size: 12px;
    text-align: left;
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

QPushButton#Danger:hover { background: $RED_WASH; }
QPushButton#Danger:pressed { background: $RED_WASH_STRONG; }
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

QLineEdit[state="ok"]  { border-color: $GREEN_EDGE; }
QLineEdit[state="warn"] { border-color: $AMBER_EDGE; }
QLineEdit[state="bad"]  { border-color: $RED_EDGE; }

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
    selection-background-color: $CYAN_WASH_STRONG;
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
    background: $CYAN_WASH_STRONG;
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
    selection-background-color: $CYAN_WASH_STRONG;
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

/* 原文行由 LyricLineWidget 自绘（见 widgets/lyricline.py）；这里的
   QLabel#LyricLine 只用于占位文字和译文行。 */
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
    /* 原文 15px → 18px 是 1.2 倍；译文从 12px 等比放到 14.4px，
       保持「译文比原文小一号」的比例，不跟着原文一起变成同样大 */
    font-size: 14.4px;
    font-weight: 400;
}

QListView#QueueList { font-size: 12px; }

/* ------------------------------------------------------------ 终端区 */

QPlainTextEdit#Console {
    background: $CONSOLE_BG;
    color: $CONSOLE_TEXT;
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
    background: $CONSOLE_BG;
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
    background: $GREEN_WASH;
    color: $GREEN;
    border: 1px solid $GREEN_EDGE;
}

QLabel#Chip[state="off"] {
    background: $TEXT_MUTE_WASH;
    color: $TEXT_DIM;
    border: 1px solid $BORDER_HI;
}

QLabel#Chip[state="warn"] {
    background: $AMBER_WASH;
    color: $AMBER;
    border: 1px solid $AMBER_EDGE;
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
QMenu::item:selected { background: $CYAN_WASH_STRONG; color: $CYAN; }
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
    """按当前主题生成完整的全局样式表。"""
    values = {name: globals()[name] for name in TOKENS}
    values["MONO"] = font_css(mono_family())
    return _QSS.substitute(values)


def apply_theme(app, name: str | None = None) -> str:
    """把主题应用到 QApplication（样式表 + 字体 + 调色板）。

    ``name`` 为空时沿用当前主题。返回实际生效的主题名。切换主题后要重画
    自绘控件：``paintEvent`` 里现取 ``theme.*`` 的会跟着变，缓存成 pixmap 的
    （占位封面等）由各页面自己的 ``theme_changed`` 钩子重建。
    """
    from PyQt6.QtGui import QFont, QPalette

    if name is not None:
        set_theme(name)
    app.setStyleSheet(stylesheet())
    app.setFont(QFont(ui_family(), 10))

    # 占位符文字通过调色板设置：QSS 的 placeholder-text-color 各版本支持不一，
    # 而默认的占位色在深色背景上太亮，容易和真实内容混淆。
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(PANEL_ALT))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL_ALT))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(PANEL_ALT))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTE))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(PURPLE))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor(CYAN))
    app.setPalette(palette)

    # 自绘控件不经过 QSS，repolish 不会碰它们；统一 update 一次
    for widget in app.allWidgets():
        try:
            widget.update()
        except RuntimeError:  # 已进入删除队列的控件
            pass
    return current_theme()


def glow(widget: QWidget, color: str | None = None, blur: int = 26, alpha: int = 150):
    """给控件加一圈霓虹辉光。

    QSS 没有 box-shadow，只能靠 QGraphicsDropShadowEffect 模拟。
    返回 effect 对象，需要动态改色时可以直接调整。

    颜色默认在**调用时**取当前主题的强调色，不能写成默认参数 —— 那会在
    ``import`` 那一刻就绑死成暗色值，切到亮色后仍是旧颜色。
    """
    effect = QGraphicsDropShadowEffect(widget)
    tint = QColor(color or CYAN)
    tint.setAlpha(alpha)
    effect.setColor(tint)
    effect.setBlurRadius(blur)
    effect.setOffset(0, 0)
    widget.setGraphicsEffect(effect)
    return effect
