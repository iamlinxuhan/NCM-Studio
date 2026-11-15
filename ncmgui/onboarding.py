"""首次启动：隐私政策与服务协议、初始设置、功能引导。

流程只走一次，状态记在 ``config_dir()/onboarding.json``：

1. 协议确认 —— 不勾选就不能继续，拒绝则退出程序；
2. 初始设置 —— 主题、音乐目录、MV 目录，可选择现在去登录；
3. 功能引导 —— 全屏遮罩逐步高亮侧栏、搜索、下载、播放条等位置。

自检/截图模式不调用本模块，避免在 CI 里弹出对话框。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PyQt6.QtCore import (
    QEvent,
    QEventLoop,
    QPoint,
    QRect,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QGraphicsDropShadowEffect
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .config import config_dir, default_music_dir, default_video_dir
from .widgets.card import accent_button, ghost_button

STATE_FILE = "onboarding.json"
STATE_VERSION = 1


PRIVACY_TEXT = """NCM Studio 隐私政策

一、我们不收集什么
  · 不上传、不出售任何个人数据，没有账号体系，也没有遥测。
  · 不保存你的网易云密码。登录由 ncmctl 完成，凭据写在
    ~/.ncmctl/cookie.json（POSIX 下仅本人可读），本程序只读、不写回。

二、本地保存了什么
  · config.json：主题、下载目录、代理等设置（POSIX 下权限 0600）
  · library.db：本地曲库索引、播放历史、MV 观看记录、本地 MV 收藏
  · downloads.json：下载任务与历史
  · cache/mv/：MV 播放中断时的完整视频缓存
  · onboarding.json：是否已完成首次引导

三、网络请求
  · 只访问网易云音乐接口与下载 CDN，以及检查更新用的个人站点与 GitHub。
  · 日志与错误信息会对 Cookie、令牌与完整 URL 做脱敏。

四、第三方组件
  · 本程序以 GNU AGPL-3.0 发布。
  · 随包分发的 ncmctl 以 MIT 许可发布，版权归 chaunsin 所有。
  · 随包分发的 mpv 以其各自的 GPL / LGPL 条款发布。
  · 完整许可文本随安装目录提供。
"""


def _license_text() -> str:
    """本程序的许可全文：源码运行时读仓库根，打包后读 assets。"""
    for path in (
        Path(__file__).resolve().parent.parent / "LICENSE",
        Path(__file__).resolve().parent / "assets" / "LICENSE",
    ):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return (
        "NCM Studio 以 GNU Affero General Public License v3.0 发布。\n\n"
        "完整条款：https://www.gnu.org/licenses/agpl-3.0.html"
    )


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

def state_path() -> Path:
    return config_dir() / STATE_FILE


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(**updates) -> None:
    data = load_state()
    data.update(updates)
    data["version"] = STATE_VERSION
    try:
        state_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def needs_onboarding() -> bool:
    return not bool(load_state().get("completed"))


# --------------------------------------------------------------------------- #
# 对话框
# --------------------------------------------------------------------------- #

class _TextDialog(QDialog):
    def __init__(self, parent, title: str, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        browser = QTextBrowser(self)
        browser.setPlainText(text)
        layout.addWidget(browser, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = ghost_button("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


class AgreementDialog(QDialog):
    """首次启动必须先同意的协议。拒绝则程序退出。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("隐私政策与服务协议")
        self.setModal(True)
        self.setMinimumSize(660, 500)

        layout = QVBoxLayout(self)
        title = QLabel("欢迎使用 NCM Studio")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        intro = QLabel(
            "开始之前，请阅读并同意下面的内容。"
            "本程序是第三方网易云音乐客户端，不收集任何个人数据。"
        )
        intro.setObjectName("Hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        browser = QTextBrowser(self)
        browser.setPlainText(PRIVACY_TEXT)
        layout.addWidget(browser, 1)

        links = QHBoxLayout()
        privacy = ghost_button("查看《隐私政策》", compact=True)
        privacy.clicked.connect(
            lambda: _TextDialog(self, "隐私政策", PRIVACY_TEXT).exec())
        terms = ghost_button("查看《服务协议》", compact=True)
        terms.clicked.connect(
            lambda: _TextDialog(self, "服务协议（AGPL-3.0）",
                                _license_text()).exec())
        links.addWidget(privacy)
        links.addWidget(terms)
        links.addStretch(1)
        layout.addLayout(links)

        self.agreed = QCheckBox(
            "我已阅读并同意《隐私政策》和《服务协议》")
        layout.addWidget(self.agreed)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        quit_button = ghost_button("退出")
        quit_button.clicked.connect(self.reject)
        buttons.addWidget(quit_button)
        self.accept_button = accent_button("同意并继续")
        self.accept_button.setEnabled(False)
        self.agreed.toggled.connect(self.accept_button.setEnabled)
        self.accept_button.clicked.connect(self.accept)
        buttons.addWidget(self.accept_button)
        layout.addLayout(buttons)


class SetupDialog(QDialog):
    """初始设置：主题、音乐目录、MV 目录，登录可以稍后再说。"""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self.cfg = window.cfg
        self.login_now = False
        self.setWindowTitle("初始设置")
        self.setModal(True)
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        title = QLabel("先做几项设置")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel("这些设置之后都能在「设置」页里改。")
        hint.setObjectName("Hint")
        layout.addWidget(hint)

        # 主题
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("主题"))
        self.theme_combo = QComboBox(self)
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.addItem("浅色", "light")
        current_theme = str(self.cfg.get("ui.theme", "dark") or "dark")
        self.theme_combo.setCurrentIndex(0 if current_theme != "light" else 1)
        theme_row.addWidget(self.theme_combo, 1)
        layout.addLayout(theme_row)

        # 音乐目录
        self.music_edit = self._path_row(
            layout, "音乐下载目录",
            str(self.cfg.get("download.output", "")
                or default_music_dir()))
        # MV 目录
        self.video_edit = self._path_row(
            layout, "MV 下载目录",
            str(self.cfg.get("download.mv_output", "")
                or default_video_dir()))

        login_row = QHBoxLayout()
        login_hint = QLabel("网易云账号可以在下一步登录，也可以稍后从「账号」页登录。")
        login_hint.setObjectName("Hint")
        login_hint.setWordWrap(True)
        login_row.addWidget(login_hint, 1)
        login_button = ghost_button("现在去登录", compact=True)
        login_button.clicked.connect(self._login_now)
        login_row.addWidget(login_button)
        layout.addLayout(login_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        skip = ghost_button("跳过")
        skip.clicked.connect(self.reject)
        buttons.addWidget(skip)
        done = accent_button("完成")
        done.clicked.connect(self.accept)
        buttons.addWidget(done)
        layout.addLayout(buttons)

    def _path_row(self, layout: QVBoxLayout, caption: str,
                  value: str) -> QLineEdit:
        row = QHBoxLayout()
        row.addWidget(QLabel(caption))
        edit = QLineEdit(value, self)
        edit.setClearButtonEnabled(True)
        row.addWidget(edit, 1)
        browse = ghost_button("选择…", compact=True)
        browse.clicked.connect(lambda: self._browse(edit))
        row.addWidget(browse)
        layout.addLayout(row)
        return edit

    def _browse(self, edit: QLineEdit) -> None:
        start = edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择目录", start)
        if chosen:
            edit.setText(chosen)

    def _login_now(self) -> None:
        self.login_now = True
        self.accept()

    def apply(self) -> None:
        theme_name = str(self.theme_combo.currentData() or "dark")
        self.cfg.set("ui.theme", theme_name)
        music = self.music_edit.text().strip()
        video = self.video_edit.text().strip()
        if music:
            self.cfg.set("download.output", music)
        if video:
            self.cfg.set("download.mv_output", video)
        self.cfg.save()
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, theme_name)
            refresh = getattr(self.window, "_refresh_theme_dependents", None)
            if callable(refresh):
                refresh()


# --------------------------------------------------------------------------- #
# 功能引导
# --------------------------------------------------------------------------- #

class _TourBubble(QFrame):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setObjectName("TourBubble")
        # QFrame 默认不绘制 QSS 背景，必须显式打开，否则气泡会“透明”
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 顶层透明遮罩窗口里的子控件拿不到全局 Card 背景，这里显式给一套
        self.setStyleSheet(
            f"QFrame#TourBubble {{ background: {theme.PANEL};"
            f" border: 1px solid {theme.BORDER}; border-radius: 10px; }}"
            f"QFrame#TourBubble QLabel#CardTitle {{ color: {theme.TEXT}; }}"
            f"QFrame#TourBubble QLabel#Hint {{ color: {theme.TEXT_DIM}; }}")
        self.setMinimumWidth(320)
        self.setMaximumWidth(420)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 120))
        self.setGraphicsEffect(shadow)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)
        self.title = QLabel("")
        self.title.setObjectName("CardTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.body = QLabel("")
        self.body.setObjectName("Hint")
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        row = QHBoxLayout()
        self.skip_btn = ghost_button("跳过", compact=True)
        row.addWidget(self.skip_btn)
        row.addStretch(1)
        self.next_btn = accent_button("下一步")
        self.next_btn.setMinimumHeight(32)
        row.addWidget(self.next_btn)
        layout.addLayout(row)


class TourOverlay(QWidget):
    """全屏透明遮罩 + 高亮洞 + 跟随气泡。"""

    finished = pyqtSignal()

    def __init__(self, window) -> None:
        # 盖在主窗口的中央区域上，而不是另开一个全屏顶层窗口：
        # Wayland 不允许客户端自己摆顶层窗口，子控件才是 100% 贴合的做法
        parent = window.centralWidget() or window
        super().__init__(parent)
        self.window = window
        self._parent = parent
        self._index = -1
        self._hole: QRect | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.bubble = _TourBubble(self)
        self.bubble.next_btn.clicked.connect(self.next_step)
        self.bubble.skip_btn.clicked.connect(self.finish)

        pages = window.pages
        self._steps = (
            (None, lambda: window.nav_widget, "导航栏",
             "发现、播客、MV、漫游都在这里切换。"),
            (lambda: window.show_page("discover"),
             lambda: getattr(pages.get("discover"), "quick_search", None),
             "搜索", "想听什么直接搜，支持歌曲、歌单、歌手。"),
            (lambda: window.show_page("download"),
             lambda: pages.get("download"), "下载",
             "在这里管理下载任务，支持查重和 VIP 提示。"),
            (None, lambda: window.playback_bar, "播放控制",
             "全局播放控制在这里，随时切歌、调音量。"),
            (None, lambda: getattr(window.playback_bar, "now_playing", None),
             "播放页与歌词", "点击左侧封面或歌词区域，进入沉浸式播放页。"),
            (lambda: window.show_page("local"),
             lambda: pages.get("local"), "本地曲库",
             "如果您有本地音乐，可以在这里扫描管理。"),
            (lambda: window.show_page("settings"),
             lambda: pages.get("settings"), "设置",
             "在这里调整主题、下载路径、更新程序等。"),
        )

    # ------------------------------------------------------------ 流程

    def start(self) -> None:
        self.setGeometry(self._parent.rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._parent.installEventFilter(self)
        self.next_step()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        if obj is self._parent and event.type() == QEvent.Type.Resize:
            self.setGeometry(self._parent.rect())
            QTimer.singleShot(0, self._position)
        return super().eventFilter(obj, event)

    def next_step(self) -> None:
        self._index += 1
        if self._index >= len(self._steps):
            self.finish()
            return
        prepare, _target, title, text = self._steps[self._index]
        if prepare is not None:
            prepare()
        self.bubble.title.setText(title)
        self.bubble.body.setText(text)
        self.bubble.next_btn.setText(
            "完成" if self._index == len(self._steps) - 1 else "下一步")
        QTimer.singleShot(200, self._position)

    def _position(self) -> None:
        if self._index < 0 or self._index >= len(self._steps):
            return
        widget = self._steps[self._index][1]()
        if widget is None or not widget.isVisible():
            # 控件不在屏幕上（窗口太小、页面没切过来）就跳过这一步
            self.next_step()
            return
        top_left = widget.mapTo(self._parent, QPoint(0, 0))
        self._hole = QRect(top_left, widget.size())
        self._place_bubble(self._hole)
        self.update()

    def _place_bubble(self, hole: QRect) -> None:
        """优先放在目标右侧，放不下再下方、上方。"""
        size = self.bubble.sizeHint()
        width = min(max(320, size.width()), 420)
        height = size.height()
        margin = 16
        x = hole.right() + margin
        y = max(margin, min(hole.top(), self.height() - height - margin))
        if x + width > self.width() - margin:
            x = min(max(margin, hole.center().x() - width // 2),
                    max(margin, self.width() - width - margin))
            y = hole.bottom() + margin
            if y + height > self.height() - margin:
                y = max(margin, hole.top() - height - margin)
        self.bubble.setGeometry(x, y, width, height)
        self.bubble.raise_()

    def finish(self) -> None:
        if not self.isVisible():
            return
        self._parent.removeEventFilter(self)
        self.hide()
        self.finished.emit()

    def run(self) -> None:
        loop = QEventLoop()
        self.finished.connect(loop.quit)
        self.start()
        loop.exec()

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 轻遮罩：真实界面仍然看得见，只是压暗一档
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._hole is None:
            return
        hole = self._hole.adjusted(-6, -6, 6, 6)
        # 高亮区再叠一层浅色，视觉上比周围亮，配合描边指目标
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.drawRoundedRect(hole, 10, 10)
        painter.setPen(QPen(QColor(theme.CYAN), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(hole, 10, 10)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() == Qt.Key.Key_Escape:
            self.finish()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        QTimer.singleShot(0, self._position)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def run(window, app) -> bool:
    """执行首次流程；用户拒绝协议时返回 False（调用方应退出）。"""
    state = load_state()
    if state.get("completed"):
        return True

    if not state.get("agreement"):
        dialog = AgreementDialog(window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        save_state(agreement=True, agreed_at=int(time.time()))

    login_now = False
    if not state.get("setup_done"):
        setup = SetupDialog(window)
        setup.exec()
        setup.apply()
        login_now = setup.login_now
        save_state(setup_done=True)

    if not state.get("tour_done") and window.isVisible():
        overlay = TourOverlay(window)
        overlay.run()
        save_state(tour_done=True)

    save_state(completed=True, completed_at=int(time.time()))
    window.show_page("discover")
    if login_now:
        window.show_page("account")
    return True
