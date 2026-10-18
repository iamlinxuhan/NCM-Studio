"""输出目录 / 文件选择器。

这是本程序最核心的交互之一：ncmctl 的每条命令几乎都要一个 ``--output``，
手敲路径既繁琐又容易出错。这个控件把「选择 → 校验 → 调起文件管理器确认结果」
串成一步，并且能被全站复用。

支持三种模式：

* ``dir``  选一个目录（下载、NCM 解码、crypto/curl 输出目录）
* ``file`` 选一个已存在的文件（Cookie 文件、CA 证书）
* ``save`` 选一个待写入的文件路径（``-o response.json``）
* ``any``  文件或目录都行（云盘上传源既可以是单曲也可以是整个文件夹）
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QVBoxLayout,
    QWidget,
)

from .card import ghost_button


class PathPicker(QWidget):
    """一行式路径选择控件：输入框 + 选择 + 打开 + 恢复默认。"""

    pathChanged = pyqtSignal(str)

    def __init__(
        self,
        mode: str = "dir",
        value: str = "",
        placeholder: str = "",
        default: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if mode not in {"dir", "file", "save", "any"}:
            raise ValueError(f"PathPicker 不支持的模式：{mode}")

        self.mode = mode
        self._default = default
        self._note: QLabel | None = None

        self.edit = QLineEdit(value)
        self.edit.setPlaceholderText(placeholder or self._placeholder())
        self.edit.setClearButtonEnabled(True)

        self.browse_button = ghost_button("选择…", compact=True)
        if mode == "any":
            self.browse_button.setToolTip("可以选择单个文件，也可以选择整个文件夹")
            self._attach_any_menu()
        self.reveal_button = ghost_button("打开", compact=True)
        self.reveal_button.setToolTip("在文件管理器中打开")
        self.reset_button = ghost_button("默认", compact=True)
        self.reset_button.setToolTip("恢复默认目录")
        self.reset_button.setVisible(bool(default))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.edit, 1)
        row.addWidget(self.browse_button)
        row.addWidget(self.reveal_button)
        row.addWidget(self.reset_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(row)

        self.browse_button.clicked.connect(self._on_browse)
        self.reveal_button.clicked.connect(self._on_reveal)
        self.reset_button.clicked.connect(self._on_reset)
        self.edit.textChanged.connect(self._on_text_changed)

        self._on_text_changed(self.edit.text())

    # ------------------------------------------------------------------ 对外接口

    def path(self) -> str:
        """返回展开 ``~`` 后的路径；为空时返回空串。"""
        raw = self.edit.text().strip()
        return os.path.expanduser(raw) if raw else ""

    def set_path(self, value: str) -> None:
        self.edit.setText(value or "")

    def is_valid(self) -> bool:
        """当前路径是否会引发问题（不存在、类型不符、不可写）。"""
        return self._state()[0] != "bad"

    def problem(self, require_exists: bool = False) -> str:
        """返回阻止提交的原因；空串表示可以用。

        :param require_exists: 即使控件本身允许「运行前自动创建」，也要求路径
            已经存在（例如上传源、CA 证书这类不能由程序代建的输入）。
        """
        state, message = self._state()
        if state == "bad":
            return message
        raw = self.edit.text().strip()
        if require_exists and raw:
            path = Path(os.path.expanduser(raw))
            if not path.exists():
                return "路径不存在"
        return ""

    def reveal(self) -> None:
        """在文件管理器中打开当前路径。"""
        self._on_reveal()

    # ------------------------------------------------------------------ 内部

    def _placeholder(self) -> str:
        return {
            "dir": "留空则使用 ncmctl 默认目录",
            "file": "选择文件…",
            "save": "留空则打印到控制台",
            "any": "选择文件或文件夹…",
        }[self.mode]

    def _state(self) -> tuple[str, str]:
        """返回 ``(状态, 提示)``，状态取值 ok / warn / bad / 空串。"""
        raw = self.edit.text().strip()
        if not raw:
            return "", ""

        path = Path(os.path.expanduser(raw))

        if self.mode == "dir":
            if path.exists() and not path.is_dir():
                return "bad", "这是一个文件，请选择目录"
            if not path.exists():
                return "warn", "目录尚不存在，运行前会自动创建"
            if not os.access(path, os.W_OK):
                return "bad", "目录不可写，请检查权限"
            return "ok", ""

        if self.mode == "file":
            if not path.exists():
                return "bad", "文件不存在"
            if path.is_dir():
                return "bad", "这是一个目录，请选择文件"
            return "ok", ""

        if self.mode == "any":
            if not path.exists():
                return "bad", "路径不存在"
            return "ok", ""

        # save 模式
        if path.exists():
            return "ok", ""
        if not path.parent.exists():
            return "bad", "上级目录不存在"
        if not os.access(path.parent, os.W_OK):
            return "bad", "上级目录不可写，请检查权限"
        return "ok", ""

    def _on_text_changed(self, _text: str) -> None:
        state, message = self._state()

        self.edit.setProperty("state", state)
        self.edit.style().unpolish(self.edit)
        self.edit.style().polish(self.edit)
        self.edit.setToolTip(message)

        if message:
            if self._note is None:
                self._note = QLabel()
                self._note.setObjectName("Hint")
                self._note.setWordWrap(True)
                self.layout().addWidget(self._note)
            self._note.setText(message)
            self._note.setVisible(True)
        elif self._note is not None:
            self._note.setVisible(False)

        # 只有路径确实存在时「打开」才有意义
        exists = bool(self.edit.text().strip()) and Path(
            os.path.expanduser(self.edit.text().strip())
        ).exists()
        self.reveal_button.setEnabled(exists)

        self.pathChanged.emit(self.path())

    def _start_dir(self) -> str:
        """文件对话框的起始目录。"""
        current = self.edit.text().strip()
        if current:
            path = Path(os.path.expanduser(current))
            target = path if path.is_dir() else path.parent
            if target.is_dir():
                return str(target)
        return str(Path.home())

    def _attach_any_menu(self) -> None:
        """``any`` 模式下把「选择…」变成一个小菜单。"""
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        pick_file = QAction("选择单个文件…", self)
        pick_file.triggered.connect(self._browse_file)
        pick_dir = QAction("选择文件夹…", self)
        pick_dir.triggered.connect(self._browse_dir)
        menu.addAction(pick_file)
        menu.addAction(pick_dir)
        self.browse_button.setMenu(menu)

    def _on_browse(self) -> None:
        if self.mode == "dir":
            self._browse_dir()
        elif self.mode == "file":
            self._browse_file()
        elif self.mode == "save":
            self._browse_save()
        # any 模式由菜单驱动，不在这里处理

    def _apply(self, chosen: str) -> None:
        if chosen:
            self.edit.setText(chosen)
            self.pathChanged.emit(self.path())

    def _browse_dir(self) -> None:
        self._apply(
            QFileDialog.getExistingDirectory(
                self, "选择文件夹", self._start_dir(),
                QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
            )
        )

    def _browse_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "选择文件", self._start_dir())
        self._apply(chosen)

    def _browse_save(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "选择输出文件", self._start_dir())
        self._apply(chosen)

    def _on_reveal(self) -> None:
        """在系统文件管理器里定位到该路径。"""
        raw = self.edit.text().strip()
        if not raw:
            return
        path = Path(os.path.expanduser(raw))
        target = path if path.exists() else path.parent
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _on_reset(self) -> None:
        if self._default:
            self.edit.setText(self._default)


class PathList(QWidget):
    """多路径列表，供 ``ncmctl ncm`` 这类接受多个输入的场合使用。

    文件路径里可能带空格，用换行或空格分隔再解析并不可靠，
    所以这里用列表明确保存每一项。
    """

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.list = QListWidget()
        self.list.setMinimumHeight(96)
        self.list.setAlternatingRowColors(False)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

        add_files = ghost_button("添加文件…", compact=True)
        add_dir = ghost_button("添加文件夹…", compact=True)
        remove = ghost_button("移除选中", compact=True)
        clear = ghost_button("清空", compact=True)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        buttons.addWidget(add_files)
        buttons.addWidget(add_dir)
        buttons.addStretch(1)
        buttons.addWidget(remove)
        buttons.addWidget(clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.list)
        layout.addLayout(buttons)

        add_files.clicked.connect(self._add_files)
        add_dir.clicked.connect(self._add_dir)
        remove.clicked.connect(self._remove_selected)
        clear.clicked.connect(self.clear)

    def paths(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def add(self, path: str) -> None:
        path = path.strip()
        if path and path not in self.paths():
            self.list.addItem(path)
            self.changed.emit()

    def set_paths(self, paths: list[str]) -> None:
        self.list.clear()
        for path in paths:
            if path.strip():
                self.list.addItem(path.strip())
        self.changed.emit()

    def clear(self) -> None:
        self.list.clear()
        self.changed.emit()

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 .ncm 文件", str(Path.home()), "NCM 文件 (*.ncm);;所有文件 (*)"
        )
        for path in files:
            self.add(path)

    def _add_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择包含 .ncm 的文件夹", str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if directory:
            self.add(directory)

    def _remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.changed.emit()
