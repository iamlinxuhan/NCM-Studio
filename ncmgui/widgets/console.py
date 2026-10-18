"""终端风格的作业输出面板。

每个作业（一次 ncmctl 调用）对应一个 ``ConsolePanel``，由主窗口放进底部标签页。
面板自己管理配色、状态、耗时和 stdin 输入，不依赖任何具体业务页面。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..runner import JobRunner
from .card import Chip, ghost_button

# 日志着色用的关键词。顺序有意义：先判错误，再判成功，最后判警告。
_ERROR_HINTS = (
    "error", "failed", "failure", "fatal", "panic", "traceback",
    "错误", "失败", "异常", "拒绝", "不可用",
)
_SUCCESS_HINTS = (
    "success", "done", "finished", "complete", "✓", "已保存", "已完成",
    "成功", "下载完成", "登录成功",
)
_WARN_HINTS = ("warn", "警告", "跳过", "skip", "retry", "重试", "注意", "deprecated")

#: 付费歌曲的标记。与 :mod:`ncmgui.resolver` 的 ``_FEE_LABELS`` 一一对应，
#: 改那边记得同步这里 —— 标黄就是为了让人一眼看出「这几首可能要会员」。
_PAID_MARKS = ("[VIP]", "[付费专辑]", "[VIP音质]")


def classify(text: str) -> str:
    """根据内容猜测一行日志该用什么颜色。

    终端区在两种主题里都是深底（见 ``theme`` 里的 CONSOLE_*），所以这里取
    终端专用色，而不是普通界面的强调色 —— 后者在亮色主题里会压暗到看不清。
    """
    lowered = text.lower()
    if any(hint in lowered for hint in _ERROR_HINTS):
        return theme.CONSOLE_RED
    if any(hint in lowered for hint in _SUCCESS_HINTS):
        return theme.CONSOLE_GREEN
    # 分组小标题（明细里的 ▸）优先于「跳过」这类词，否则标题会被判成警告色
    if text.startswith("▸"):
        return theme.CONSOLE_CYAN
    if any(mark in text for mark in _PAID_MARKS):
        return theme.CONSOLE_AMBER
    if any(hint in lowered for hint in _WARN_HINTS):
        return theme.CONSOLE_AMBER
    if text.startswith("$") or text.startswith(">"):
        return theme.CONSOLE_CYAN
    if text.startswith("─") or text.startswith("#"):
        return theme.CONSOLE_DIM
    return theme.CONSOLE_TEXT


class ConsoleView(QPlainTextEdit):
    """只读的彩色日志视图。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Console")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setUndoRedoEnabled(False)
        # 长任务会输出很多行，限制块数避免内存无限增长
        self.setMaximumBlockCount(6000)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        font = QFont(theme.mono_family(), 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)

        self._progress_active = False

    # ------------------------------------------------------------------ 写入

    def append_line(self, text: str, color: str | None = None) -> None:
        """追加一整行。"""
        self._progress_active = False
        document = self.document()
        cursor = QTextCursor(document.lastBlock())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not document.isEmpty():
            cursor.insertBlock()

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color or theme.CONSOLE_TEXT))
        cursor.insertText(text, fmt)
        self._autoscroll()

    def append_progress(self, text: str, color: str | None = None) -> None:
        """进度刷新：覆盖最后一行，而不是不断追加。"""
        document = self.document()
        cursor = QTextCursor(document.lastBlock())
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color or theme.CONSOLE_DIM))

        if self._progress_active:
            # 选中最后一行的内容（不含段落分隔符）后整体替换
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
            )
            cursor.removeSelectedText()
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if not document.isEmpty():
                cursor.insertBlock()
            self._progress_active = True

        cursor.insertText(text, fmt)
        self._autoscroll()

    def _autoscroll(self) -> None:
        """只有用户本来就停在底部时才自动滚动，避免打断向上翻看。"""
        bar = self.verticalScrollBar()
        if getattr(self, "_stick_to_bottom", True):
            bar.setValue(bar.maximum())

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().wheelEvent(event)
        bar = self.verticalScrollBar()
        # 离底部超过 4 行就认为用户想停在这里看历史
        self._stick_to_bottom = bar.value() >= bar.maximum() - 4

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.addAction("复制全部", self.copy_all)
        menu.addAction("全选", self.selectAll)
        menu.addSeparator()
        menu.addAction("清空", self.clear_log)
        menu.exec(event.globalPos())

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.toPlainText())

    def clear_log(self) -> None:
        self.clear()
        self._progress_active = False


class ConsolePanel(QWidget):
    """一个作业的完整输出面板：标题 + 状态 + 停止/清空/导出 + stdin 输入。"""

    #: 参数为退出码；主窗口据此更新标签页标题
    jobFinished = pyqtSignal(int)
    #: 用户点击关闭
    closeRequested = pyqtSignal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ConsoleDock")

        self.title = title
        self.runner: JobRunner | None = None
        self.command_line = ""
        self._raw: list[str] = []
        self._started_at = 0.0
        #: 可选的「行变换器」。返回改写后的整行就按标记色显示，返回 None
        #: 表示这行跟它无关、走默认配色。下载页用它给 VIP 的进度行打标记。
        self._tagger: Callable[[str], str | None] | None = None

        # ------------------------------------------------------------ 顶部栏
        self.title_label = QLabel(title)
        self.title_label.setObjectName("ConsoleTitle")

        self.status_chip = Chip("排队中", "off")

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("Hint")

        self.stop_button = ghost_button("停止", compact=True)
        self.stop_button.setEnabled(False)
        self.copy_button = ghost_button("复制命令", compact=True)
        self.clear_button = ghost_button("清空", compact=True)
        self.export_button = ghost_button("导出日志", compact=True)
        self.close_button = ghost_button("关闭", compact=True)
        self.close_button.setVisible(False)

        head = QWidget()
        head.setObjectName("ConsoleHead")
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(12, 7, 12, 7)
        head_layout.setSpacing(10)
        head_layout.addWidget(self.title_label)
        head_layout.addWidget(self.status_chip)
        head_layout.addWidget(self.meta_label)
        head_layout.addStretch(1)
        head_layout.addWidget(self.copy_button)
        head_layout.addWidget(self.clear_button)
        head_layout.addWidget(self.export_button)
        head_layout.addWidget(self.stop_button)
        head_layout.addWidget(self.close_button)

        # ------------------------------------------------------------ 输出区
        self.view = ConsoleView()

        # ------------------------------------------------------------ stdin
        self.stdin_bar = QLineEdit()
        self.stdin_bar.setObjectName("StdinBar")
        self.stdin_bar.setPlaceholderText(
            "向进程输入一行内容后回车（例如短信验证码）—— 仅交互式命令需要"
        )
        self.stdin_bar.returnPressed.connect(self._send_stdin)
        self.stdin_bar.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(head)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.stdin_bar)

        # ------------------------------------------------------------ 连接
        self.stop_button.clicked.connect(self._on_stop)
        self.copy_button.clicked.connect(self._copy_command)
        self.clear_button.clicked.connect(self.view.clear_log)
        self.export_button.clicked.connect(self._export)
        self.close_button.clicked.connect(self.closeRequested.emit)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_meta)

    # ------------------------------------------------------------------ 绑定

    def set_tagger(self, tagger: Callable[[str], str | None] | None) -> None:
        """装一个行变换器，用来给特定输出行加标记（见 ``_tagger``）。"""
        self._tagger = tagger

    def bind(self, runner: JobRunner) -> None:
        """连接一个已启动（或即将启动）的作业。"""
        self.runner = runner
        self.command_line = runner.command_line
        self._started_at = time.monotonic()

        runner.started.connect(self._on_started)
        runner.output.connect(self._on_output)
        runner.progress.connect(self._on_progress)
        runner.finished.connect(self._on_finished)
        runner.failed.connect(self._on_failed)

        self.view.append_line(f"$ {self.command_line}", theme.CONSOLE_CYAN)
        self.view.append_line("")

    # ------------------------------------------------------------------ 槽

    def _on_started(self) -> None:
        self.status_chip.setText("运行中")
        self.status_chip.set_state("warn")
        self.stop_button.setEnabled(True)
        self.stdin_bar.setVisible(True)
        self._tick.start()
        self._update_meta()

    def _on_output(self, text: str) -> None:
        # 进度条实际是走 output 过来的（pb 打的是 "\r<行>\n"），所以这里也要过
        # 一遍标注器 —— 下载时的 VIP 标记主要就是在这条路径上加的
        marked = self._tagger(text) if self._tagger else None
        if marked is not None:
            self._raw.append(marked)
            self.view.append_line(marked, theme.CONSOLE_AMBER)
            return
        self._raw.append(text)
        self.view.append_line(text, classify(text))

    def _on_progress(self, text: str) -> None:
        marked = self._tagger(text) if self._tagger else None
        if marked is not None:
            self.view.append_progress(marked, theme.CONSOLE_AMBER)
            return
        self.view.append_progress(text)

    def _on_failed(self, message: str) -> None:
        self._raw.append(message)
        self.view.append_line(message, theme.CONSOLE_RED)

    def _on_finished(self, code: int, seconds: float) -> None:
        self._tick.stop()
        self.stop_button.setEnabled(False)
        self.stdin_bar.setVisible(False)
        self.close_button.setVisible(True)

        if code == 0:
            self.status_chip.setText("已完成")
            self.status_chip.set_state("on")
        else:
            self.status_chip.setText(f"退出码 {code}")
            self.status_chip.set_state("off")

        self.meta_label.setText(f"耗时 {self._format_duration(seconds)}")
        self.view.append_line("")
        self.view.append_line(
            f"── 进程结束，退出码 {code}，耗时 {self._format_duration(seconds)} ──",
            theme.CONSOLE_DIM if code == 0 else theme.CONSOLE_RED,
        )
        self.jobFinished.emit(code)

    # ------------------------------------------------------------------ 操作

    def _on_stop(self) -> None:
        if self.runner and self.runner.is_running:
            self.stop_button.setEnabled(False)
            self.stop_button.setText("停止中…")
            self.view.append_line("─ 正在请求停止…", theme.CONSOLE_AMBER)
            self.runner.stop()

    def _send_stdin(self) -> None:
        text = self.stdin_bar.text()
        if self.runner and self.runner.is_running:
            self.runner.send_line(text)
            # 回显，让用户确认输入确实送进去了
            self.view.append_line(f"‹ 输入 › {text}", theme.CONSOLE_PURPLE)
            self.stdin_bar.clear()

    def _copy_command(self) -> None:
        QApplication.clipboard().setText(self.command_line)
        self.copy_button.setText("已复制")
        QTimer.singleShot(1200, lambda: self.copy_button.setText("复制命令"))

    def _export(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suggested = f"ncmctl-{self.title}-{stamp}.log"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", str(Path.home() / suggested), "日志文件 (*.log);;所有文件 (*)"
        )
        if not path:
            return
        try:
            Path(path).write_text("\n".join(self._raw) + "\n", encoding="utf-8")
        except OSError as exc:
            self.view.append_line(f"导出失败：{exc}", theme.CONSOLE_RED)
        else:
            self.view.append_line(f"日志已导出到 {path}", theme.CONSOLE_GREEN)

    def _update_meta(self) -> None:
        if self._started_at:
            elapsed = time.monotonic() - self._started_at
            self.meta_label.setText(f"已运行 {self._format_duration(elapsed)}")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds} 秒"
        minutes, sec = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} 分 {sec} 秒"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分"
