"""主窗口：侧边栏导航 + 页面栈 + 底部作业控制台。"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, VERSION, ncmctl, theme
from .config import Config
from .images import CoverStore
from .pages.account import AccountPage
from .pages.cloud import CloudPage
from .pages.download import DownloadPage
from .pages.liked import LikedPage
from .pages.local import LocalPage
from .pages.ncm import NcmPage
from .pages.player import PlayerPage
from .pages.proxy import ProxyPage
from .pages.settings import SettingsPage
from .pages.tasks import TasksPage
from .pages.tools import ToolsPage
from .playback import PlaybackController
from .widgets.card import Chip
from .widgets.console import ConsolePanel, classify
from .widgets.playbackbar import PlaybackBar

#: (键, 图标, 名称, 页面类)
NAV_ITEMS = (
    ("account", "◈", "账号", AccountPage),
    ("liked", "♡", "我喜欢的音乐", LikedPage),
    ("local", "♪", "本地音乐", LocalPage),
    ("download", "▼", "下载", DownloadPage),
    ("ncm", "◇", "NCM 解码", NcmPage),
    ("cloud", "☁", "云盘上传", CloudPage),
    ("tasks", "◑", "账号任务", TasksPage),
    ("proxy", "⇄", "监控代理", ProxyPage),
    ("tools", "▧", "调试工具", ToolsPage),
    ("settings", "⚙", "设置", SettingsPage),
)


class MainWindow(QMainWindow):
    """应用主窗口。"""

    #: 登录状态发生变化
    loginChanged = pyqtSignal(bool)

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.binary = ncmctl.find_binary(cfg.get("ncmctl_path", ""))
        self._logged_in = ncmctl.is_logged_in(cfg.get("home", ""))
        #: 截图/自检时置真，切页面不触发联网加载
        self.defer_page_loads = False

        # 播放器与封面缓存要在建页之前就位：各页面在 build() 里直接订阅它们
        self.covers = CoverStore(self)
        self.playback = PlaybackController(
            cfg,
            binary=lambda: self.binary,
            opts=self.global_opts,
            parent=self,
        )
        #: 从播放页「返回」时回到的导航页
        self._last_page_key = "account"

        self.setWindowTitle(f"{APP_NAME} · 第三方音乐播放器")
        self.resize(1180, 780)
        self.setMinimumSize(920, 620)

        self._build_ui()
        self._refresh_login_ui()
        self._poll_login()

    # ==================================================================== 构建

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
        sidebar.setFixedWidth(206)

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

        self.login_chip = Chip("未登录", "off")
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 10, 0, 0)
        chip_row.addWidget(self.login_chip)
        chip_row.addStretch(1)
        brand_layout.addLayout(chip_row)

        layout.addWidget(brand)

        # 导航
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_index: dict[str, int] = {}

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}

        for index, (key, glyph, label, page_cls) in enumerate(NAV_ITEMS):
            button = QPushButton(f"  {theme.safe_glyph(glyph, '·')}   {label}")
            button.setObjectName("Nav")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, k=key: self.show_page(k))

            self._nav_group.addButton(button)
            self._nav_buttons[key] = button
            self._nav_index[key] = index
            layout.addWidget(button)

            page = page_cls(self)
            self.pages[key] = page
            self.stack.addWidget(page)

        # 播放页覆盖内容区但不是导航项：放在所有导航页之后，_nav_index 不变
        self.player_page = PlayerPage(self)
        self.stack.addWidget(self.player_page)

        if "account" in self._nav_buttons:
            self._nav_buttons["account"].setChecked(True)
        self.playback.set_local_provider(self.pages["liked"].local_path)
        self.playback.errorRaised.connect(self._on_playback_error)
        self.playback.playerExited.connect(self._on_player_exited)
        self.player_page.backRequested.connect(self.return_from_player)

        layout.addStretch(1)

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

    def show_page(self, key: str) -> None:
        index = self._nav_index.get(key)
        if index is None:
            return
        self._last_page_key = key
        self.stack.setCurrentIndex(index)
        self._nav_buttons[key].setChecked(True)
        # 懒加载页面（如红心歌单）在这里才去取数据，启动时不必联网
        page = self.pages.get(key)
        hook = getattr(page, "on_show", None)
        if callable(hook) and not self.defer_page_loads:
            try:
                hook()
            except RuntimeError:
                pass

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
        self.show_page(self._last_page_key or "account")

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if (
            event.key() == Qt.Key.Key_Escape
            and self.stack.currentWidget() is self.player_page
        ):
            self.return_from_player()
            event.accept()
            return
        super().keyPressEvent(event)

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

    def refresh_login(self) -> None:
        """重新探测登录状态，变化时通知各页面。"""
        current = ncmctl.is_logged_in(self.cfg.get("home", ""))
        if current == self._logged_in:
            return
        self._logged_in = current
        self._refresh_login_ui()
        if not current:
            # 登出后红心歌单没了，正在播的队列也不该继续
            self.playback.stop()
            self.playback.clear_queue()
        self.loginChanged.emit(current)
        for page in self.pages.values():
            hook = getattr(page, "on_login_changed", None)
            if callable(hook):
                hook(current)

    def _refresh_login_ui(self) -> None:
        if self._logged_in:
            self.login_chip.setText("已登录")
            self.login_chip.set_state("on")
        else:
            self.login_chip.setText("未登录")
            self.login_chip.set_state("off")
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

    def reload_binary(self) -> None:
        """配置里改了 ncmctl 路径后重新探测。"""
        self.binary = ncmctl.find_binary(self.cfg.get("ncmctl_path", ""))

    def save_all_config(self) -> None:
        """把各页面的当前设置写回配置文件。

        控件可能已经被销毁（强制退出、会话注销等情况下 closeEvent 才会跑到，
        此时子控件可能已经不在了），所以这里容忍 RuntimeError —— 这种情况下
        设置本来就无从采集，不该让退出流程崩掉。
        """
        for page in self.pages.values():
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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
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
            self.playback.shutdown()
        except Exception:  # noqa: BLE001 - 退出流程不能被播放器拖垮
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
        self.save_all_config()
        super().closeEvent(event)
