"""云盘上传页：把本地音乐传到账号的音乐云盘。

上传会真实改动账号状态，所以这里加了一道二次确认。
"""

from __future__ import annotations

from .. import ncmctl
from ..widgets.card import accent_button, ghost_button
from ..widgets.form import FormCard
from .base import BasePage

MINSIZE_PRESETS = [
    ("不过滤", ""),
    ("512 KB", "512KB"),
    ("1 MB", "1MB"),
    ("5 MB", "5MB"),
    ("10 MB", "10MB"),
]

REGEXP_PRESETS = [
    ("所有识别的音频", ""),
    ("仅 FLAC", r".*\.flac$"),
    ("仅 MP3", r".*\.mp3$"),
    ("排除 live 曲目", r"^(?!.*[Ll]ive).*$"),
]


class CloudPage(BasePage):
    PAGE_TITLE = "云盘上传"
    PAGE_SUBTITLE = "上传歌曲到网易云音乐云盘。单文件上限 500 MB，目录递归扫描三层。"

    def build(self) -> None:
        source = FormCard(
            "上传来源",
            "选一个音乐文件，或者选一个文件夹批量上传。",
        )
        self.source = source.add_path(
            "文件或目录",
            mode="any",
            value=self.cfg.get("cloud.source", ""),
            hint="点「选择…」可以选文件，也可以选整个文件夹。",
        )
        self.content.addWidget(source)

        options = FormCard("上传设置")
        self.minsize = options.add_combo(
            "体积下限",
            MINSIZE_PRESETS,
            self.cfg.get("cloud.minsize", ""),
            hint="小于该体积的文件会被跳过，用来滤掉试听片段。",
        )
        self.regexp = options.add_combo(
            "文件名过滤",
            REGEXP_PRESETS,
            self.cfg.get("cloud.regexp", ""),
            hint="按正则匹配文件路径；仅在来源是文件夹时生效。",
        )
        self.parallel = options.add_spin(
            "并发数",
            int(self.cfg.get("cloud.parallel", 3)),
            1, 10,
            hint="并发过高容易被限流，建议不超过 5。",
        )
        self.content.addWidget(options)

        actions = FormCard()
        self.start_button = accent_button("开始上传")
        self.start_button.clicked.connect(self._start)
        open_button = ghost_button("打开来源位置")
        open_button.clicked.connect(self.source.reveal)
        actions.add_row_of(self.start_button, open_button)
        self.content.addWidget(actions)

    # ------------------------------------------------------------------ 内部

    def _start(self) -> None:
        source = self.source.path()
        if not source:
            self.warn("请先选择要上传的文件或文件夹。")
            return
        if not self.check_path(self.source, "上传源", require_exists=True):
            return
        if not self.require_login("上传到云盘"):
            return

        self.save_config()
        argv = ncmctl.build_cloud(
            source,
            minsize=self.minsize.currentData() or "",
            parallel=self.parallel.value(),
            regexp=self.regexp.currentData() or "",
            opts=self.opts(),
        )

        if not self.window.submit(
            "云盘上传",
            argv,
            confirm=(
                "确认上传到云盘？",
                f"将把以下内容上传到你的网易云音乐云盘：<br><br>"
                f"<code>{source}</code><br><br>"
                "这会真实修改账号的云盘内容，并占用云盘容量。",
            ),
        ):
            return

    def save_config(self) -> None:
        self.cfg.set("cloud.source", self.source.path())
        self.cfg.set("cloud.minsize", self.minsize.currentData() or "")
        self.cfg.set("cloud.regexp", self.regexp.currentData() or "")
        self.cfg.set("cloud.parallel", self.parallel.value())
