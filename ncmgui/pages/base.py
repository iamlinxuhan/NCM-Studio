"""页面基类。

统一三件事：

* 内容区可滚动（参数表单通常比窗口高）
* 页面标题样式一致
* 提交作业、读取配置、登录校验这些跨页面动作都有统一的入口
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from PyQt6.QtWidgets import (
    QFrame,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import ncmctl
from ..widgets.card import page_header

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免循环导入
    from ..window import MainWindow


class BasePage(QWidget):
    """所有功能页的父类。"""

    PAGE_TITLE = ""
    PAGE_SUBTITLE = ""

    #: 内容是否包在 QScrollArea 里。表格页必须设 ``False``：表格自己有滚动条，
    #: 再套一层会出现「滚轮到底后整页跟着动」、表格高度被内容撑开之类的怪现象。
    SCROLLABLE = True

    #: build() 之后是否在末尾加弹簧。滚动页需要它把内容顶到上方；
    #: 非滚动页要让表格自己吃掉剩余高度，不能再加。
    TRAILING_STRETCH = True

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.cfg = window.cfg

        inner = QWidget()
        self.content = QVBoxLayout(inner)
        self.content.setContentsMargins(26, 22, 26, 26)
        self.content.setSpacing(16)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        if self.SCROLLABLE:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(inner)
            root.addWidget(scroll)
        else:
            root.addWidget(inner, 1)

        if self.PAGE_TITLE:
            self.content.addWidget(page_header(self.PAGE_TITLE, self.PAGE_SUBTITLE))

        self.build()
        if self.TRAILING_STRETCH:
            self.content.addStretch(1)

    # ------------------------------------------------------------ 子类钩子

    def build(self) -> None:
        """构建页面内容。子类必须实现。"""

    def save_config(self) -> None:
        """窗口关闭前把界面上的设置写回配置。子类按需覆盖。"""

    def on_login_changed(self, logged_in: bool) -> None:
        """登录状态变化时的回调。子类按需覆盖。"""

    def on_show(self) -> None:
        """页面被切到前台时调用。子类按需覆盖（例如懒加载列表）。"""

    def shutdown(self) -> None:
        """窗口关闭前的清理。子类按需覆盖（例如关掉播放器子进程）。"""

    # ------------------------------------------------------------ 便捷方法

    def opts(self) -> ncmctl.GlobalOpts:
        """当前生效的全局参数（--home / -c / --debug）。"""
        return self.window.global_opts()

    def submit(
        self,
        title: str,
        argv: list[str],
        confirm: tuple[str, str] | None = None,
        intro: list[str] | None = None,
        tagger: Callable[[str], str | None] | None = None,
        on_finish: Callable[[object, int], None] | None = None,
        sensitive_input: bool = False,
    ):
        """提交一个作业，返回 JobRunner 或 None。

        :param intro: 见 :meth:`MainWindow.submit` —— 进程开跑前先写进控制台的行
        :param tagger: 见 :meth:`MainWindow.submit` —— 给运行中的输出行加标记
        :param on_finish: 见 :meth:`MainWindow.submit` —— 作业结束后的回调，
            参数是 ``(控制台面板, 退出码)``
        :param sensitive_input: 见 :meth:`MainWindow.submit` —— stdin 是否敏感
            （短信验证码、密码、令牌）
        """
        return self.window.submit(
            title,
            argv,
            confirm,
            intro=intro,
            tagger=tagger,
            on_finish=on_finish,
            sensitive_input=sensitive_input,
        )

    def is_logged_in(self) -> bool:
        return self.window.is_logged_in()

    def require_login(self, action: str = "该操作") -> bool:
        """需要登录的动作在未登录时给出明确引导。"""
        if self.window.is_logged_in():
            return True
        QMessageBox.warning(
            self,
            "需要登录",
            f"{action}需要登录网易云账号。<br><br>"
            "请先到左侧「账号」页完成登录（推荐扫码，最简单）。",
        )
        return False

    def warn(self, message: str, title: str = "无法执行") -> None:
        QMessageBox.warning(self, title, message)

    def check_path(self, picker, label: str, require_exists: bool = False) -> bool:
        """提交前检查一个 PathPicker；有问题时弹提示并返回 False。

        :param require_exists: 路径必须已经存在（上传源、证书、配置文件）。
        """
        checker = getattr(picker, "problem", None)
        problem = checker(require_exists=require_exists) if callable(checker) else ""
        if not problem:
            return True
        self.warn(f"{label}：{problem}")
        edit = getattr(picker, "edit", None)
        if edit is not None:
            edit.setFocus()
        return False

    def toast(self, message: str) -> None:
        self.window.toast(message)


class TabbedPage(BasePage):
    """把几个关系紧密的页面收进一个标签页，缩短侧边栏。

    ``TABS`` 里是 ``(key, 标签文字, 页面类)``。子页面仍然各自是完整的
    :class:`BasePage`（滚动、表格、懒加载都不变），只是把标题让给外层，
    避免「标签名 + 大标题」重复两遍。

    子页面不进主窗口的 stack，但会被注册到 ``window.pages`` 里，所以
    ``window.show_page("history")``、``window.pages["download"]`` 这类旧入口
    照常可用；主窗口负责把 ``show_page`` 翻译成「切到这个宿主页的对应标签」。
    """

    SCROLLABLE = False
    TRAILING_STRETCH = False
    TABS: tuple[tuple[str, str, type[BasePage]], ...] = ()

    def __init__(self, window: "MainWindow") -> None:
        self.tabs: dict[str, BasePage] = {}
        self._switching = False
        super().__init__(window)

    def build(self) -> None:
        self.tab_widget = QTabWidget()
        for key, label, page_cls in self.TABS:
            # 动态子类只改标题：页面内容一行都不用动，标题也不会重复显示
            embedded_cls = type(
                page_cls.__name__,
                (page_cls,),
                {"PAGE_TITLE": "", "PAGE_SUBTITLE": ""},
            )
            child = embedded_cls(self.window)
            self.tabs[key] = child
            self.tab_widget.addTab(child, label)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        self.content.addWidget(self.tab_widget)

    # ------------------------------------------------------------ 标签切换

    def current_key(self) -> str:
        index = self.tab_widget.currentIndex()
        if 0 <= index < len(self.TABS):
            return self.TABS[index][0]
        return ""

    def select_tab(self, key: str) -> bool:
        """切到指定子页面；找不到时返回 False。"""
        for index, (tab_key, _label, _cls) in enumerate(self.TABS):
            if tab_key != key:
                continue
            if self.tab_widget.currentIndex() != index:
                # 由 show_page 统一触发 on_show，避免一次切换调用两遍
                self._switching = True
                self.tab_widget.setCurrentIndex(index)
                self._switching = False
            return True
        return False

    def on_show(self) -> None:
        child = self.tabs.get(self.current_key())
        hook = getattr(child, "on_show", None)
        if callable(hook):
            try:
                hook()
            except RuntimeError:
                pass

    def _on_tab_changed(self, _index: int) -> None:
        if self._switching or not self.isVisible():
            return
        self.on_show()
