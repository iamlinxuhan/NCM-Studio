"""NCM 解码页：把 .ncm 加密文件还原成 .mp3 / .flac。

这一步完全在本地完成，不需要登录，也不需要联网。
"""

from __future__ import annotations

from .. import ncmctl
from ..config import default_ncm_dir
from ..widgets.card import accent_button, ghost_button
from ..widgets.form import FormCard
from .base import BasePage


class NcmPage(BasePage):
    PAGE_TITLE = "NCM 解码"
    PAGE_SUBTITLE = "把网易云客户端下载的 .ncm 文件还原为 .mp3 / .flac。纯本地操作，无需登录。"

    def build(self) -> None:
        sources = FormCard(
            "输入文件",
            "可以直接加单个 .ncm 文件，也可以整个文件夹一起加（ncmctl 最多向下扫描三层）。",
        )
        self.inputs = sources.add_pathlist("", hint="支持一次选择多个文件；重复项会自动忽略。")
        self.inputs.set_paths(self.cfg.get("ncm.inputs", []) or [])
        self.content.addWidget(sources)

        options = FormCard("解码设置")
        self.output = options.add_path(
            "输出目录",
            mode="dir",
            value=self.cfg.get("ncm.output", ""),
            default=str(default_ncm_dir()),
            hint="还原出来的音频文件会写到这里。",
        )
        self.parallel = options.add_spin(
            "并发数",
            int(self.cfg.get("ncm.parallel", 10)),
            1, 50,
            hint="解码是本地 CPU 操作，并发设高些通常更快。",
        )
        self.no_tag = options.add_switch(
            "不写入音频标签（--tag）",
            bool(self.cfg.get("ncm.no_tag", False)),
            hint="默认会写入标签。仅在标签写入出问题或你不需要时才打开。",
        )
        self.content.addWidget(options)

        actions = FormCard()
        self.start_button = accent_button("开始解码")
        self.start_button.clicked.connect(self._start)
        clear_button = ghost_button("清空列表")
        clear_button.clicked.connect(self.inputs.clear)
        open_button = ghost_button("打开输出目录")
        open_button.clicked.connect(self.output.reveal)
        actions.add_row_of(self.start_button, clear_button, open_button)
        self.content.addWidget(actions)

    # ------------------------------------------------------------------ 内部

    def _start(self) -> None:
        inputs = self.inputs.paths()
        if not inputs:
            self.warn("请先添加至少一个 .ncm 文件或文件夹。")
            return
        if not self.check_path(self.output, "输出目录"):
            return

        self.save_config()
        argv = ncmctl.build_ncm(
            inputs,
            output=self.output.path(),
            parallel=self.parallel.value(),
            no_tag=self.no_tag.isChecked(),
            opts=self.opts(),
        )
        title = "NCM 解码" if len(inputs) == 1 else f"NCM 解码 {len(inputs)} 项"
        self.submit(title, argv)

    def save_config(self) -> None:
        self.cfg.set("ncm.inputs", self.inputs.paths())
        self.cfg.set("ncm.output", self.output.path())
        self.cfg.set("ncm.parallel", self.parallel.value())
        self.cfg.set("ncm.no_tag", self.no_tag.isChecked())
