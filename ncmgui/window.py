"""主窗口：侧边栏导航 + 页面栈 + 底部作业控制台。"""

from __future__ import annotations

import time
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, VERSION, ncmctl, theme
from .actions import SongActions
from .api import ApiClient, ApiError, AuthRequired
from .config import Config, cleanup_private_tmp
from .history_recorder import HistoryRecorder
from .images import CoverStore, ImageStore, fetch as fetch_image
from .desktop.lyrics_window import DesktopLyricsWindow
from .desktop.tray import TrayIcon
from .library import LibraryDB
from .lyrics_service import LyricsService
from .mpris import MprisService
from .pages.account import AccountPage
from .pages.base import TabbedPage
from .pages.detail import DetailHost
from .pages.discover import DiscoverPage
from .pages.favorites import FavoritesPage
from .pages.groups import AdvancedPage, HistoryStatsPage, TransferPage
from .pages.liked import LikedPage
from .pages.local import LocalPage
from .pages.player import PlayerPage
from .pages.search import SearchPage
from .pages.settings import SettingsPage
from .pages.streaming import StreamingPage
from .playback import PlaybackController
from .scrobble import ScrobbleReporter
from .streaming import StreamingRegistry
from .widgets.console import ConsolePanel, classify
from .widgets.playbackbar import PlaybackBar
from .widgets.profilecard import ProfileCard
from .widgets.themetoggle import ThemeToggle
from .workers import BackgroundTask

#: 侧边栏项目：(键, 图标, 名称, 页面类)
#:
#: 侧边栏只放这 10 项；关系紧密的页面在页面内部用标签页收拢，
#: 「搜索」则并进发现页（见 ``EXTRA_PAGES`` 与 ``PAGE_NAV``）。
NAV_ITEMS = (
    ("discover", "✦", "发现", DiscoverPage),
    ("liked", "♡", "我喜欢的音乐", LikedPage),
    ("favorites", "★", "收藏", FavoritesPage),
    ("local", "♪", "本地曲库", LocalPage),
    ("history_stats", "⟲", "历史与统计", HistoryStatsPage),
    ("transfer", "▼", "下载与云盘", TransferPage),
    ("streaming", "◉", "流媒体", StreamingPage),
    ("advanced", "▧", "高级工具", AdvancedPage),
    ("account", "◈", "账号", AccountPage),
    ("settings", "⚙", "设置", SettingsPage),
)

#: 不在侧边栏出现、但仍需放进 stack 的页面。搜索由发现页顶部的
#: 搜索框进入，内容区直接换成搜索结果
EXTRA_PAGES = (
    ("search", "⌕", "搜索", SearchPage),
)

#: 页面键 -> 侧边栏按键。标签子页与搜索页都借父项的选中状态，
#: 侧边栏因此不会出现「没选中任何一项」的空档
PAGE_NAV = {
    "search": "discover",
    "history": "history_stats",
    "stats": "history_stats",
    "download": "transfer",
    "ncm": "transfer",
    "cloud": "transfer",
    "tasks": "advanced",
    "proxy": "advanced",
    "tools": "advanced",
}


class MainWindow(QMainWindow):
    """应用主窗口。"""

    #: 登录状态发生变化
    loginChanged = pyqtSignal(bool)
    #: 在线校验拿到的账号资料（昵称、头像、UID）；登出时发空字典
    profileChanged = pyqtSignal(dict)

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.binary = ncmctl.find_binary(cfg.get("ncmctl_path", ""))
        self._logged_in = ncmctl.is_logged_in(cfg.get("home", ""))
        #: cookie.json 的 (mtime, size)；变化说明刚登录/登出，触发在线校验
        self._cookie_stamp = self._cookie_signature()
        self._login_task: BackgroundTask | None = None
        #: 从播放页返回时回到的控件（可能是详情页，不是导航页）
        self._player_return_widget: QWidget | None = None
        #: 截图/自检时置真，切页面不触发联网加载
        self.defer_page_loads = False
        #: 托盘「退出」会置真，绕过「关闭到托盘」的拦截
        self._force_quit = False

        # 本地曲库要在建页之前打开：本地页在 build() 里直接查询它
        self.library = LibraryDB(
            str(self.cfg.get("library.db") or "").strip() or None
        )
        # 播放器与封面缓存要在建页之前就位：各页面在 build() 里直接订阅它们
        self.covers = CoverStore(self)
        self.image_store = ImageStore(
            self,
            fetcher=lambda url: fetch_image(url, self._api_proxy()),
        )
        self.client = ApiClient(cfg.get("home", ""), proxy=self._api_proxy())
        self.playback = PlaybackController(
            cfg,
            binary=lambda: self.binary,
            opts=self.global_opts,
            parent=self,
        )
        #: 自建流媒体服务器（Subsonic/Jellyfin）的配置与取链
        self.streaming = StreamingRegistry(cfg, parent=self)
        #: 写盘失败时在状态栏提示（保存只由用户操作触发，那时状态栏已建好）
        self.streaming.saveFailed.connect(self.toast)
        self.playback.set_stream_provider(self.streaming.resolve)
        #: 歌词在播放页与桌面歌词窗之间共享，避免同一首歌请求两次
        self.lyrics = LyricsService(self, parent=self)
        self.actions = SongActions(self)
        #: 播放满 5 秒后写一条本地播放历史；统计页直接读同一张表
        self.history = HistoryRecorder(self.library, self.playback, parent=self)
        #: 可选：把每轮播放记录同步到网易云账号（默认关闭）
        self.scrobble = ScrobbleReporter(
            self.cfg,
            client_provider=lambda: self.client,
            parent=self,
        )
        self.history.playStarted.connect(self.scrobble.on_started)
        # 听满 5 秒才补发 startPlay：快速切歌不制造开始日志洪水
        self.history.playQualified.connect(self.scrobble.on_qualified)
        self.history.playSettled.connect(self.scrobble.on_settled)
        #: 蓝牙耳机/系统媒体键走 MPRIS；真正 start() 由 main.py 决定，
        #: 自检与截图不往会话总线上注册服务
        self.mpris = MprisService(self.playback, self, parent=self)
        #: 从播放页「返回」时回到的导航页
        self._last_page_key = "discover"

        # 上次异常退出可能留下含二维码/Cookie 的临时文件；只清超过一小时的，
        # 不会碰到同时运行的另一个实例正在用的文件
        try:
            cleanup_private_tmp()
        except OSError:
            pass

        self.setWindowTitle(f"{APP_NAME} · 第三方音乐播放器")
        self.setMinimumSize(920, 620)
        self._restore_window_size()

        self._build_ui()

        # 桌面歌词与托盘在页面建好之后创建：托盘菜单要能调播放控制与歌词窗
        self.desktop_lyrics = DesktopLyricsWindow(self)
        self.desktop_lyrics.closed.connect(self._on_desktop_lyrics_closed)
        self.tray = TrayIcon(self)
        self.tray_available = self.tray.start()
        QTimer.singleShot(0, self._restore_desktop_lyrics)

        self._refresh_login_ui()
        self._poll_login()
        # 启动阶段不联网；等窗口立起来之后再校验一次登录态
        QTimer.singleShot(1800, self._probe_account)
        # 栈默认停在发现页，但 on_show 只有 show_page 才会触发。不补这一下，
        # 首次启动的推荐区一直是空的，红心 ID 集合也不会加载（播放条红心
        # 因此一直显示未喜欢）。延后到事件循环第一拍，截图模式可以先设
        # defer_page_loads 把网络请求挡掉。
        QTimer.singleShot(0, self._show_initial_page)

    # ==================================================================== 构建

    def _restore_window_size(self) -> None:
        """按上次关闭时的大小打开；超出当前屏幕就缩回可用区域。"""
        try:
            width = int(self.cfg.get("window.width", 1180))
            height = int(self.cfg.get("window.height", 780))
        except (TypeError, ValueError):
            width, height = 1180, 780
        width = max(920, width)
        height = max(620, height)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, max(920, available.width()))
            height = min(height, max(620, available.height()))
        self.resize(width, height)
        # 真正最大化要等入口把界面建好再调用，这里只记下意图
        self.start_maximized = bool(self.cfg.get("window.maximized", False))

    def _save_window_size(self) -> None:
        self.cfg.set("window.maximized", self.isMaximized())
        # 最大化/全屏时尺寸就是屏幕尺寸，记下来反而会盖掉用户平时的窗口大小
        if self.isMaximized() or self.isFullScreen():
            return
        size = self.size()
        self.cfg.set("window.width", size.width())
        self.cfg.set("window.height", size.height())

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_body(), 1)

        self.setCentralWidget(central)
        self._build_statusbar()

    # ---------------------------------------------------------------- 侧边栏

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(216)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 品牌区
        brand = QWidget()
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(18, 20, 18, 16)
        brand_layout.setSpacing(2)

        name = QLabel("NCM STUDIO")
        name.setObjectName("Brand")
        sub = QLabel("MUSIC DESKTOP CLIENT")
        sub.setObjectName("BrandSub")
        brand_layout.addWidget(name)
        brand_layout.addWidget(sub)

        # 账号卡直接展示头像、昵称与 UID；点一下进账号页。
        # 比原来只有「已登录」三个字的状态标签更有用，也顺手把侧边栏顶部
        # 从纯文字变成有层次的区域
        self.profile_card = ProfileCard(self, compact=True)
        self.profile_card.clicked.connect(lambda: self.show_page("account"))
        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 12, 0, 0)
        profile_row.addWidget(self.profile_card, 1)
        brand_layout.addLayout(profile_row)

        self.theme_toggle = ThemeToggle()
        self.theme_toggle.clicked.connect(self.toggle_theme)

        # 「主题（点击以切换）」比登录状态宽得多，挤在同一行会溢出侧边栏，
        # 所以单独占一行
        theme_row = QHBoxLayout()
        theme_row.setContentsMargins(0, 8, 0, 0)
        theme_row.addWidget(self.theme_toggle, 1)
        brand_layout.addLayout(theme_row)

        layout.addWidget(brand)

        # 导航
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_index: dict[str, int] = {}
        #: 标签子页 -> 宿主页，show_page 用它先切到正确的标签
        self._tab_hosts: dict[str, TabbedPage] = {}

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}

        # 导航项多了以后，小窗口装不下所有按钮；放进滚动区，品牌区和底部
        # 信息保持固定
        nav_widget = QWidget()
        nav_widget.setObjectName("NavList")
        nav_layout = QVBoxLayout(nav_widget)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)

        for _index, (key, glyph, label, page_cls) in enumerate(NAV_ITEMS):
            button = QPushButton(f"  {theme.safe_glyph(glyph, '·')}   {label}")
            button.setObjectName("Nav")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, k=key: self.show_page(k))

            self._nav_group.addButton(button)
            self._nav_buttons[key] = button
            nav_layout.addWidget(button)

            page = page_cls(self)
            self.pages[key] = page
            self.stack.addWidget(page)
            self._nav_index[key] = self.stack.count() - 1
            self._register_tabs(key, page)

        # 搜索页不在侧边栏，但仍在 stack 里；发现页的搜索框切到这里
        for key, _glyph, _label, page_cls in EXTRA_PAGES:
            page = page_cls(self)
            self.pages[key] = page
            self.stack.addWidget(page)
            self._nav_index[key] = self.stack.count() - 1

        nav_layout.addStretch(1)
        nav_scroll = QScrollArea()
        nav_scroll.setObjectName("NavScroll")
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav_scroll.setWidget(nav_widget)
        layout.addWidget(nav_scroll, 1)

        # 播放页覆盖内容区但不是导航项：放在所有导航页之后，_nav_index 不变
        self.player_page = PlayerPage(self)
        self.stack.addWidget(self.player_page)

        # 详情页也是一个非导航栈；歌单/专辑/歌手都在这里打开
        self.detail_host = DetailHost(self)
        self.detail_host.emptied.connect(
            lambda: self.show_page(self._last_page_key or "account")
        )
        self.stack.addWidget(self.detail_host)

        if "discover" in self._nav_buttons:
            self._nav_buttons["discover"].setChecked(True)
        self.playback.set_local_provider(self.pages["liked"].local_path)
        self.playback.errorRaised.connect(self._on_playback_error)
        self.playback.playerExited.connect(self._on_player_exited)
        self.playback.sleepTimerFinished.connect(self.toast)
        self.player_page.backRequested.connect(self.return_from_player)

        # 底部信息
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(18, 12, 18, 16)
        footer_layout.setSpacing(3)

        path_label = QLabel(self.binary or "未找到 ncmctl")
        path_label.setObjectName("Hint")
        path_label.setWordWrap(True)
        path_label.setToolTip(self.binary or "请到「设置」页指定 ncmctl 路径")

        version_label = QLabel(f"GUI v{VERSION}")
        version_label.setObjectName("Hint")

        footer_layout.addWidget(path_label)
        footer_layout.addWidget(version_label)
        layout.addWidget(footer)

        return sidebar

    # ---------------------------------------------------------------- 主题

    def toggle_theme(self) -> None:
        """切换明暗主题，写回配置，并通知所有自绘 / 缓存颜色的控件。"""
        app = QApplication.instance()
        if app is None:
            return
        target = "light" if theme.is_dark() else "dark"
        theme.apply_theme(app, target)
        # 记住用户选的主题；下次启动在 main.py 里先于建窗口读出来
        self.cfg.set("ui.theme", target)
        self.cfg.save()
        self._refresh_theme_dependents()

    def _refresh_theme_dependents(self) -> None:
        """主题换过之后，把不吃 QSS 的那部分逐个刷新。"""
        self.theme_toggle.refresh()
        self.profile_card.refresh_theme()
        self._retint_glows()
        self._refresh_console_tab_colors()
        for widget in (self.playback_bar, self.player_page, *self.pages.values()):
            hook = getattr(widget, "theme_changed", None)
            if callable(hook):
                hook()

    def _retint_glows(self) -> None:
        """主按钮的霓虹辉光是 QGraphicsDropShadowEffect，QSS 管不到。"""
        for widget in QApplication.allWidgets():
            if widget.objectName() != "Accent":
                continue
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsDropShadowEffect):
                continue
            tint = QColor(theme.CYAN)
            tint.setAlpha(110)
            effect.setColor(tint)

    def _refresh_console_tab_colors(self) -> None:
        """标签文字颜色是逐次 setTabTextColor 设的，换主题后要重设一遍。"""
        bar = self.console_tabs.tabBar()
        current = self.console_tabs.currentIndex()
        for index in range(self.console_tabs.count()):
            text = self.console_tabs.tabText(index)
            if text.startswith("● "):
                color = theme.CYAN
            elif "失败" in text:
                color = theme.RED
            elif index == current:
                color = theme.TEXT
            else:
                color = theme.TEXT_DIM
            bar.setTabTextColor(index, QColor(color))

    # ------------------------------------------------------- 内容区 + 控制台

    def _build_body(self) -> QWidget:
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(2)

        self.splitter.addWidget(self.stack)

        # 控制台容器：一条细标题栏 + 作业标签页
        console = QWidget()
        console.setObjectName("ConsoleDock")
        console_layout = QVBoxLayout(console)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("ConsoleHead")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 6, 12, 6)
        bar_layout.setSpacing(10)

        self.console_title = QLabel("作业控制台")
        self.console_title.setObjectName("ConsoleTitle")
        self.console_hint = QLabel("尚无作业")
        self.console_hint.setObjectName("Hint")

        self.collapse_button = QPushButton("折叠")
        self.collapse_button.setObjectName("Ghost")
        self.collapse_button.setProperty("compact", "true")
        self.collapse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_button.clicked.connect(self._toggle_console)

        bar_layout.addWidget(self.console_title)
        bar_layout.addWidget(self.console_hint)
        bar_layout.addStretch(1)
        bar_layout.addWidget(self.collapse_button)

        self.console_tabs = QTabWidget()
        self.console_tabs.setDocumentMode(True)
        self.console_tabs.setTabsClosable(False)
        self.console_tabs.currentChanged.connect(self._on_tab_changed)

        console_layout.addWidget(bar)
        console_layout.addWidget(self.console_tabs, 1)

        self.console_dock = console
        self.splitter.addWidget(console)
        self.splitter.setSizes([540, 240])
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self._console_collapsed = False

        # 初始没有作业，先把控制台收起来，把空间让给页面
        self._set_console_visible(False)

        # 底部播放条通栏常驻，切到哪一页都在
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self.splitter, 1)
        self.playback_bar = PlaybackBar(self)
        self.playback_bar.queueRequested.connect(self.toggle_queue_panel)
        # 点播放条左侧的封面/歌名回到黑胶唱片页；从播放页「返回」之后
        # 侧栏里没有它的入口，这里是唯一的直达路径
        self.playback_bar.playerRequested.connect(self.open_player_page)
        body_layout.addWidget(self.playback_bar)
        return body

    def _build_statusbar(self) -> None:
        self.status_message = QLabel("")
        self.status_message.setObjectName("Hint")
        self.statusBar().addWidget(self.status_message, 1)

        self.status_right = QLabel("")
        self.status_right.setObjectName("Hint")
        self.statusBar().addPermanentWidget(self.status_right)
        self._update_status_right()

    # ==================================================================== 导航

    def _register_tabs(self, host_key: str, page: QWidget) -> None:
        """把带标签页的宿主页里的子页注册进 ``self.pages``。

        子页共用宿主的 stack 索引：``show_page("download")`` 先切到
        ``transfer`` 宿主，再由 :meth:`show_page` 选中「下载」标签。
        """
        if not isinstance(page, TabbedPage):
            return
        for child_key, child in page.tabs.items():
            self.pages[child_key] = child
            self._nav_index[child_key] = self._nav_index[host_key]
            self._tab_hosts[child_key] = page

    def _show_initial_page(self) -> None:
        """窗口立起来后触发默认页的懒加载。

        页面内容都在 ``on_show`` 里；启动时栈只是「停在」发现页，并没有走
        :meth:`show_page`。少了这一下，发现页推荐区会空着，``ensure_liked``
        也不会被调用。
        """
        if self.defer_page_loads:
            return
        page = self.pages.get("discover")
        if page is not None and self.stack.currentWidget() is page:
            self.show_page("discover")

    def show_page(self, key: str) -> None:
        index = self._nav_index.get(key)
        if index is None:
            return
        # 从详情页切到导航页：整叠详情作废
        if self.stack.currentWidget() is self.detail_host:
            self.detail_host.clear()
        self._last_page_key = key
        self.stack.setCurrentIndex(index)
        # 子页与搜索页借父项的按钮，侧边栏始终有一项处于选中状态
        nav_key = PAGE_NAV.get(key, key)
        button = self._nav_buttons.get(nav_key)
        if button is not None:
            button.setChecked(True)
        host = self._tab_hosts.get(key)
        if host is not None:
            host.select_tab(key)
            page: QWidget | None = host
        else:
            page = self.pages.get(key)
        # 懒加载页面（如红心歌单）在这里才去取数据，启动时不必联网
        hook = getattr(page, "on_show", None)
        if callable(hook) and not self.defer_page_loads:
            try:
                hook()
            except RuntimeError:
                pass

    def open_detail(self, kind: str, item_id: int) -> None:
        """打开歌单/专辑/歌手详情。所有列表页都走这个入口。"""
        current = self.stack.currentWidget()
        for key, page in self.pages.items():
            if page is current:
                self._last_page_key = key
                break
        # 详情不是导航项，侧栏任何按钮都不该保持选中
        self._nav_group.setExclusive(False)
        for button in self._nav_buttons.values():
            button.setChecked(False)
        self._nav_group.setExclusive(True)
        self.stack.setCurrentWidget(self.detail_host)
        self.detail_host.open(kind, int(item_id))

    def toggle_queue_panel(self) -> None:
        """播放条的播放列表按钮：展开队列，再点一次收起。

        不在播放页时先切过去并展开；已经在播放页且队列开着，就只收面板，
        不再走一遍 ``open_player_page``（那会把页面重置成显示状态）。
        """
        if self.stack.currentWidget() is self.player_page and self.player_page.queue_visible:
            self.player_page.set_queue_visible(False)
            return
        self.open_player_page(queue=True)

    def open_player_page(self, queue: bool | None = None) -> None:
        """切到播放页；``queue`` 为真时顺带展开播放队列。"""
        current = self.stack.currentWidget()
        if current is not self.player_page:
            self._player_return_widget = current
        for key, page in self.pages.items():
            if page is current:
                self._last_page_key = key
                break

        # 播放页不是导航项，侧栏任何按钮都不该保持选中
        self._nav_group.setExclusive(False)
        for button in self._nav_buttons.values():
            button.setChecked(False)
        self._nav_group.setExclusive(True)

        self.stack.setCurrentWidget(self.player_page)
        if queue is not None:
            self.player_page.set_queue_visible(queue)
        hook = getattr(self.player_page, "on_show", None)
        if callable(hook) and not self.defer_page_loads:
            try:
                hook()
            except RuntimeError:
                pass

    def return_from_player(self) -> None:
        widget = self._player_return_widget
        if (widget is not None and widget is not self.player_page
                and self.stack.indexOf(widget) >= 0):
            self.stack.setCurrentWidget(widget)
            if widget is self.detail_host:
                self._nav_group.setExclusive(False)
                for button in self._nav_buttons.values():
                    button.setChecked(False)
                self._nav_group.setExclusive(True)
                self.detail_host.on_show()
                return
            for key, page in self.pages.items():
                if page is widget:
                    button = self._nav_buttons.get(PAGE_NAV.get(key, key))
                    if button is not None:
                        button.setChecked(True)
                    self._last_page_key = key
                    hook = getattr(page, "on_show", None)
                    if callable(hook) and not self.defer_page_loads:
                        try:
                            hook()
                        except RuntimeError:
                            pass
                    return
        self.show_page(self._last_page_key or "account")

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Escape and self.stack.currentWidget() is self.player_page:
            self.return_from_player()
            event.accept()
            return

        # 应用内快捷键。用 keyPressEvent 而不是全局 QShortcut：输入框拿到
        # 空格/字母时应该正常输入，事件根本不冒泡到这里。
        if modifiers == Qt.KeyboardModifier.NoModifier and key == Qt.Key.Key_Space:
            self.playback.toggle()
            event.accept()
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Left:
                self.playback.previous()
            elif key == Qt.Key.Key_Right:
                self.playback.next()
            elif key == Qt.Key.Key_Up:
                self.playback.set_volume(self.playback.volume + 5)
            elif key == Qt.Key.Key_Down:
                self.playback.set_volume(self.playback.volume - 5)
            elif key == Qt.Key.Key_F:
                self.show_page("search")
                page = self.pages.get("search")
                focus = getattr(page, "focus_search", None)
                if callable(focus):
                    focus()
            elif key == Qt.Key.Key_Comma:
                self.show_page("settings")
            elif key == Qt.Key.Key_L:
                self.toggle_desktop_lyrics()
            else:
                super().keyPressEvent(event)
                return
            event.accept()
            return
        if (
            modifiers == Qt.KeyboardModifier.NoModifier
            and self.stack.currentWidget() is self.player_page
            and key in (Qt.Key.Key_Left, Qt.Key.Key_Right)
        ):
            delta = -5.0 if key == Qt.Key.Key_Left else 5.0
            self.playback.seek(max(0.0, self.playback.position + delta))
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ 桌面集成

    def show_normal(self) -> None:
        """从托盘/通知回到主窗口。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self) -> None:
        """托盘「退出」：绕过关闭到托盘的拦截，走完整的退出清理。"""
        self._force_quit = True
        self.close()

    def toggle_desktop_lyrics(self, force: bool | None = None) -> None:
        if not hasattr(self, "desktop_lyrics"):
            return
        if force is True:
            self.desktop_lyrics.open_lyrics()
        elif force is False:
            self.desktop_lyrics.hide_lyrics()
        else:
            self.desktop_lyrics.toggle()
        try:
            self.tray.sync_lyrics(self.desktop_lyrics.isVisible())
        except (AttributeError, RuntimeError):
            pass

    def _restore_desktop_lyrics(self) -> None:
        """上次退出时开着就恢复；自检/截图不弹额外的窗口。"""
        if self.defer_page_loads:
            return
        if self.cfg.get("player.desktop_lyrics.enabled", False):
            self.desktop_lyrics.open_lyrics()

    def _on_desktop_lyrics_closed(self) -> None:
        try:
            self.tray.sync_lyrics(False)
        except (AttributeError, RuntimeError):
            pass

    def _on_playback_error(self, message: str) -> None:
        self.toast(f"播放失败：{message}")

    def _on_player_exited(self, code: int) -> None:
        self.toast("播放器已退出" + (f"（代码 {code}）" if code else ""))

    def check_updates_on_start(self) -> None:
        """启动后检查一次新版；发现新版直接弹窗问用户要不要安装。

        检查本身失败仍然静默，不拿网络问题打扰启动。
        """
        if not self.cfg.get("update", {}).get("check_on_start", True):
            return
        page = self.pages.get("settings")
        hook = getattr(page, "check_updates", None)
        if callable(hook):
            try:
                hook(silent=True, prompt=True)
            except (RuntimeError, TypeError):
                pass

    # ============================================================== 作业调度

    def submit(
        self,
        title: str,
        argv: list[str],
        confirm: tuple[str, str] | None = None,
        cwd: str | None = None,
        intro: list[str] | None = None,
        tagger: Callable[[str], str | None] | None = None,
        on_finish: Callable[["ConsolePanel", int], None] | None = None,
        sensitive_input: bool = False,
    ):
        """启动一个作业。

        :param title: 控制台标签页标题
        :param argv: 传给 ncmctl 的参数数组（不含可执行文件本身）
        :param confirm: ``(标题, 正文)``，用于会改动账号状态或不可逆的操作
        :param intro: 进程开跑前先写进控制台的行。ncmctl 自己不会交代作业背景
            （例如「这批里有几首要会员」），这类信息只能由调用方带进来。
        :param tagger: 可选的「行变换器」，用来给运行中的输出行加标记。
            返回改写后的整行就按标记色显示，返回 None 表示跟它无关。
            下载页用它把 VIP 的进度行标出来，见
            :class:`~ncmgui.resolver.FeeTagger`。
        :param on_finish: 作业结束后回调 ``(面板, 退出码)``。**在主线程里调**，
            所以可以放心开新作业、动界面。下载页用它接一个「下完再抓歌词」的
            后处理 —— 后处理需要往同一个面板里写输出，而面板是这里创建的，
            调用方拿不到，只能这样传回去。
        :param sensitive_input: 这个作业的 stdin 是敏感内容（短信验证码、
            密码、令牌）。输入框改用密码回显，回显行与导出日志都不记原文。
        :return: 启动成功返回 :class:`~ncmgui.runner.JobRunner`，否则返回 None
        """
        from .runner import JobRunner

        if confirm and not self._confirm(confirm[0], confirm[1]):
            return None

        if not self.binary:
            QMessageBox.critical(
                self,
                "找不到 ncmctl",
                "未能在 PATH 或常见安装位置找到 <b>ncmctl</b> 可执行文件。<br><br>"
                "请到「设置」页手动指定它的完整路径。",
            )
            return None

        runner = JobRunner(self.binary, argv, cwd=cwd, parent=self)
        panel = ConsolePanel(title, self)
        panel.bind(runner)
        panel.set_tagger(tagger)
        panel.set_sensitive_input(sensitive_input)
        for line in intro or []:
            panel.view.append_line(line, classify(line))

        index = self.console_tabs.addTab(panel, title)
        self.console_tabs.setCurrentIndex(index)
        self._set_console_visible(True)
        self._update_console_hint()

        runner.output.connect(lambda _line, p=panel: self._mark_activity(p))
        panel.jobFinished.connect(lambda code, p=panel: self._on_job_finished(p, code))
        panel.closeRequested.connect(lambda p=panel: self._close_panel(p))
        if on_finish is not None:
            # 接在 _on_job_finished 之后（连接顺序即调用顺序），标签页标题已经
            # 改成「完成 / 失败」了，后处理再往上写输出不会把它盖回去
            panel.jobFinished.connect(lambda code, p=panel: on_finish(p, code))

        runner.start()
        return runner

    def _confirm(self, title: str, body: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(body)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Ok).setText("确认执行")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    def _on_job_finished(self, panel: ConsolePanel, code: int) -> None:
        index = self.console_tabs.indexOf(panel)
        if index < 0:
            return
        self.console_tabs.setTabText(index, f"{panel.title} · {'完成' if code == 0 else f'失败({code})'}")
        self.console_tabs.tabBar().setTabTextColor(
            index, QColor(theme.TEXT_DIM if code == 0 else theme.RED)
        )
        # 登录类作业结束后，登录状态可能变了
        self.refresh_login()

    def _close_panel(self, panel: ConsolePanel) -> None:
        index = self.console_tabs.indexOf(panel)
        if index < 0:
            return
        self.console_tabs.removeTab(index)
        panel.deleteLater()
        if self.console_tabs.count() == 0:
            self._set_console_visible(False)
        self._update_console_hint()

    def _mark_activity(self, panel: ConsolePanel) -> None:
        """非当前标签页有新输出时，把标题点亮，提示用户去看。"""
        index = self.console_tabs.indexOf(panel)
        if index < 0 or index == self.console_tabs.currentIndex():
            return
        if self.console_tabs.tabText(index).startswith("● "):
            return
        self.console_tabs.setTabText(index, f"● {panel.title}")
        self.console_tabs.tabBar().setTabTextColor(index, QColor(theme.CYAN))

    def _on_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        panel = self.console_tabs.widget(index)
        if isinstance(panel, ConsolePanel):
            self.console_tabs.setTabText(index, panel.title)
        self.console_tabs.tabBar().setTabTextColor(index, QColor(theme.TEXT))

    def _update_console_hint(self) -> None:
        count = self.console_tabs.count()
        running = sum(
            1
            for i in range(count)
            if isinstance(self.console_tabs.widget(i), ConsolePanel)
            and self.console_tabs.widget(i).runner
            and self.console_tabs.widget(i).runner.is_running
        )
        if count == 0:
            self.console_hint.setText("尚无作业")
        elif running:
            self.console_hint.setText(f"{running} 个作业运行中 · 共 {count} 个")
        else:
            self.console_hint.setText(f"共 {count} 个作业")

    def _set_console_visible(self, visible: bool) -> None:
        self.console_dock.setVisible(visible)
        if visible:
            self.splitter.setSizes([520, 260])
            self.collapse_button.setText("折叠")
            self._console_collapsed = False

    def _toggle_console(self) -> None:
        self._console_collapsed = not self._console_collapsed
        if self._console_collapsed:
            self.console_dock.setVisible(False)
            self.collapse_button.setText("展开")
        else:
            self.console_dock.setVisible(True)
            self.splitter.setSizes([520, 260])
            self.collapse_button.setText("折叠")

    # ================================================================ 登录状态

    def global_opts(self) -> ncmctl.GlobalOpts:
        return ncmctl.GlobalOpts(
            home=self.cfg.get("home", ""),
            config=self.cfg.get("config_file", ""),
            debug=bool(self.cfg.get("debug", False)),
        )

    def is_logged_in(self) -> bool:
        return self._logged_in

    def require_login_silent(self, action: str = "该操作") -> bool:
        """和页面基类的 ``require_login`` 一样，但不需要页面实例。"""
        if self._logged_in:
            return True
        QMessageBox.warning(
            self,
            "需要登录",
            f"{action}需要登录网易云账号。<br><br>"
            "请先到左侧「账号」页完成登录（推荐扫码，最简单）。",
        )
        return False

    def _api_proxy(self) -> str:
        """接口请求跟随「下载代理」设置；direct 表示直连。"""
        try:
            return str(self.cfg.get("update.proxy", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _cookie_signature(self) -> tuple[int, int] | None:
        try:
            stat = ncmctl.cookie_path(self.cfg.get("home", "")).stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def _set_logged_in(self, current: bool) -> None:
        if current == self._logged_in:
            return
        self._logged_in = current
        self._refresh_login_ui()
        if not current:
            # 登出后红心歌单没了，正在播的队列也不该继续
            self.playback.stop()
            self.playback.clear_queue()
            self.actions.clear_liked()
        self.loginChanged.emit(current)
        for page in (*self.pages.values(), self.detail_host):
            hook = getattr(page, "on_login_changed", None)
            if callable(hook):
                hook(current)
        if current:
            self.actions.ensure_liked()

    def refresh_login(self) -> None:
        """文件层面探测登录状态；Cookie 变化时再做一次在线校验。

        启动后不立即联网。登录成功（cookie.json 被 ncmctl 写入）或用户
        手动删掉凭据时，文件签名会变化，这里据此触发。
        """
        signature = self._cookie_signature()
        exists = signature is not None
        changed = signature != self._cookie_stamp
        self._cookie_stamp = signature

        if not exists:
            self._set_logged_in(False)
            return
        if not self._logged_in:
            # 先乐观显示已登录，在线校验失败会再改回来
            self._set_logged_in(True)
        if changed and not self.defer_page_loads:
            self._probe_account()

    def refresh_profile(self) -> None:
        """重新读取在线账号资料（头像/昵称/UID）。"""
        self._probe_account()

    def _probe_account(self) -> None:
        """在线校验登录态。网络故障时保持现状，避免误判为登出。"""
        if self._login_task is not None and self._login_task.is_running:
            return
        if not ncmctl.is_logged_in(self.cfg.get("home", "")):
            return
        task = BackgroundTask(lambda: self.client.refresh_account(), parent=self)
        self._login_task = task

        def done(profile) -> None:
            if self.defer_page_loads:
                return
            valid = isinstance(profile, dict) and bool(profile.get("userId"))
            if valid:
                self._set_logged_in(True)
                self._refresh_login_ui()
                self.profileChanged.emit(dict(profile))
            else:
                self._set_logged_in(False)
                self.profileChanged.emit({})

        def failed(message: str) -> None:
            # 断网、代理错误、接口超时都不等于登录失效；保留 Cookie 状态，
            # 页面需要登录时再给出具体错误
            if isinstance(message, str) and message:
                pass

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _refresh_login_ui(self) -> None:
        self.profile_card.set_logged_in(self._logged_in)
        self.profile_card.set_profile(self.client.profile)
        self._update_status_right()

    def _update_status_right(self) -> None:
        state = "已登录" if self._logged_in else "未登录"
        self.status_right.setText(f"ncmctl · {state}")

    def _poll_login(self) -> None:
        """低频轮询登录状态。

        ncmctl 的二维码/手机登录会自己写 cookie.json，界面无法直接收到通知，
        所以用轮询来兜住「用户在控制台手动完成登录」的情况。
        """
        self.refresh_login()
        self._update_console_hint()
        QTimer.singleShot(3000, self._poll_login)

    # ==================================================================== 杂项

    def toast(self, message: str, kind: str = "info") -> None:
        self.status_message.setText(message)
        QTimer.singleShot(5000, lambda: self.status_message.setText(""))

    def reload_api(self) -> None:
        """设置里改了状态目录或代理后，重建接口客户端。"""
        self.client = ApiClient(self.cfg.get("home", ""), proxy=self._api_proxy())
        self._cookie_stamp = self._cookie_signature()
        self._refresh_login_ui()
        if not self.defer_page_loads:
            self._probe_account()

    def reload_binary(self) -> None:
        """配置里改了 ncmctl 路径后重新探测。"""
        self.binary = ncmctl.find_binary(self.cfg.get("ncmctl_path", ""))

    def save_all_config(self) -> None:
        """把各页面的当前设置写回配置文件。

        控件可能已经被销毁（强制退出、会话注销等情况下 closeEvent 才会跑到，
        此时子控件可能已经不在了），所以这里容忍 RuntimeError —— 这种情况下
        设置本来就无从采集，不该让退出流程崩掉。
        """
        # 播放页不在导航里（self.pages 里没有），单独收一次分栏比例
        for page in (*self.pages.values(), self.player_page):
            saver = getattr(page, "save_config", None)
            if not callable(saver):
                continue
            try:
                saver()
            except RuntimeError:
                pass
        try:
            self.cfg.save()
        except RuntimeError:
            pass

    def _running_panels(self) -> list[ConsolePanel]:
        """当前还在运行的作业面板。"""
        panels: list[ConsolePanel] = []
        for index in range(self.console_tabs.count()):
            panel = self.console_tabs.widget(index)
            if (
                isinstance(panel, ConsolePanel)
                and panel.runner is not None
                and panel.runner.is_running
            ):
                panels.append(panel)
        return panels

    def _wait_critical_tasks(self) -> bool:
        """等后台的歌词/封面写入收尾；返回 False 表示用户选择继续运行。"""
        pending = BackgroundTask.pending_critical()
        if not pending:
            return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("正在写入歌词/封面")
        box.setText(f"还有 {len(pending)} 个后台任务正在写入文件。")
        box.setInformativeText(
            "退出会先请求它们停下，最多等 5 秒。写入是原子的，最多是少处理几首，"
            "不会留下半写文件。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        box.button(QMessageBox.StandardButton.Ok).setText("等它结束再退出")
        box.button(QMessageBox.StandardButton.Cancel).setText("继续运行")
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return False

        for task in pending:
            task.cancel()

        deadline = time.monotonic() + 5.0
        while BackgroundTask.pending_critical() and time.monotonic() < deadline:
            # 封面任务结束后会接着启动歌词任务；每轮都对新出现的任务补一次
            # cancel，否则那个任务会在关窗途中继续跑
            for task in BackgroundTask.pending_critical():
                task.cancel()
            # 关键任务结束时要发 done/failed，槽函数在主线程；不转事件循环
            # 它们会一直排队，界面看起来像卡死
            QApplication.processEvents()
            time.sleep(0.02)

        if not BackgroundTask.pending_critical():
            return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("后台任务仍未结束")
        box.setText("等了 5 秒仍有任务在写文件。")
        box.setInformativeText(
            "现在退出不会损坏文件（写入是原子的），但当前这一批可能没处理完。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        box.button(QMessageBox.StandardButton.Ok).setText("仍然退出")
        box.button(QMessageBox.StandardButton.Cancel).setText("继续等待")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 「关闭到托盘」只在托盘真的可用时生效；否则窗口藏起来就找不回来
        if (
            not getattr(self, "_force_quit", False)
            and getattr(self, "tray_available", False)
            and bool(self.cfg.get("ui.close_to_tray", False))
        ):
            event.ignore()
            self.hide()
            try:
                self.tray.showMessage(
                    APP_NAME, "已最小化到托盘，点托盘图标可以回来。"
                )
            except (AttributeError, RuntimeError):
                pass
            return

        if not self._wait_critical_tasks():
            event.ignore()
            return

        running = self._running_panels()
        if running:
            names = "、".join(panel.title for panel in running)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("仍有作业在运行")
            box.setText(f"还有 {len(running)} 个作业没有结束：<b>{names}</b>")
            box.setInformativeText("现在退出会中断它们。下载到一半的文件可能需要重新下载。")
            box.setStandardButtons(
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
            )
            box.button(QMessageBox.StandardButton.Ok).setText("仍然退出")
            box.button(QMessageBox.StandardButton.Cancel).setText("继续运行")
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Ok:
                event.ignore()
                return

            # 先请求停止，再给它们一点收尾时间，避免留下僵死的子进程
            for panel in running:
                panel.runner.stop(grace_ms=600)
            for panel in running:
                panel.runner.wait_for_finish(700)

        # 先收掉后台进程（播放器），再写配置 —— 播放器退出时会改音量之外的
        # 状态，配置采集必须排在其后。任一页面清理失败都不能阻断退出。
        try:
            # 先把当前这次播放补写成完整记录，再停播放器
            self.history.shutdown()
        except Exception:  # noqa: BLE001 - 统计失败不能挡住退出
            pass
        try:
            # 排在刚补发的结算日志之后，让队列里的任务先走完
            self.scrobble.shutdown()
        except Exception:  # noqa: BLE001 - 上报失败不能挡住退出
            pass
        try:
            self.mpris.shutdown()
        except Exception:  # noqa: BLE001 - D-Bus 不可用不能挡住退出
            pass
        try:
            self.playback.shutdown()
        except Exception:  # noqa: BLE001 - 退出流程不能被播放器拖垮
            pass
        try:
            self.desktop_lyrics.shutdown()
        except Exception:  # noqa: BLE001 - 桌面歌词保存失败不该挡住退出
            pass
        try:
            self.lyrics.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tray.shutdown()
        except Exception:  # noqa: BLE001
            pass
        for page in self.pages.values():
            hook = getattr(page, "shutdown", None)
            if not callable(hook):
                continue
            try:
                hook()
            except Exception:  # noqa: BLE001 - 退出流程不能被单个页面拖垮
                pass

        try:
            self.playback.save_config()
        except Exception:  # noqa: BLE001
            pass
        self._save_window_size()
        self.save_all_config()
        try:
            self.library.close()
        except Exception:  # noqa: BLE001 - 曲库关闭失败不该挡住退出
            pass
        super().closeEvent(event)
