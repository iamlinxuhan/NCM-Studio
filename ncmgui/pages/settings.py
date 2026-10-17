"""设置页：ncmctl 定位、全局运行参数、配置持久化与自更新。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QLabel

from .. import NCMCTL_VERSION, ncmctl, player
from ..config import config_dir
from ..widgets.card import accent_button, danger_button, ghost_button
from ..widgets.form import FormCard
from ..workers import BackgroundTask
from .base import BasePage


def _same_file(left: str | None, right: str | None) -> bool:
    """两个路径是否指向同一个文件。任一为空则视为不同。"""
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


class SettingsPage(BasePage):
    PAGE_TITLE = "设置"
    PAGE_SUBTITLE = "ncmctl 可执行文件位置、全局参数与程序配置。"

    def build(self) -> None:
        self._build_binary_card()
        self._build_player_card()
        self._build_global_card()
        self._build_update_card()
        self._build_about_card()
        # _build_binary_card 里的 _detect 跑在更新卡片存在之前，
        # 那一次同步不到按钮，这里补一次
        self._sync_update_button()

    # --------------------------------------------------------- ncmctl 定位

    def _build_binary_card(self) -> None:
        card = FormCard(
            "ncmctl 位置",
            f"留空时优先使用随本程序分发的 ncmctl v{NCMCTL_VERSION}，"
            "其次在 PATH 与常见安装位置（~/go/bin 等）查找。"
            "想让本程序改用你自己装的那份，在下面填它的完整路径。",
        )
        self.binary_path = card.add_path(
            "可执行文件", mode="file",
            value=self.cfg.get("ncmctl_path", ""),
            placeholder="留空表示使用随程序分发的版本",
            hint="本程序已内置 ncmctl，一般不需要填这一项。",
        )

        self.detected = QLabel("")
        self.detected.setObjectName("Hint")
        self.detected.setWordWrap(True)
        card.add_widget(self.detected)

        detect = ghost_button("重新探测")
        detect.clicked.connect(self._detect)
        card.add_row_of(detect)
        self.content.addWidget(card)

        #: 当前解析到的是不是随包分发的那份。自更新按钮据此决定是否可用
        self._using_bundled = False

        self._detect()

    def _detect(self) -> None:
        found = ncmctl.find_binary(self.binary_path.path())
        bundled = ncmctl.bundled_binary()
        self._using_bundled = _same_file(found, bundled)

        if not found:
            self._using_bundled = False
            self.detected.setText(
                "⚠ 未找到 ncmctl。请在上面指定完整路径。"
            )
        elif self._using_bundled:
            self.detected.setText(
                f"✅ 当前使用：随程序分发的 v{NCMCTL_VERSION}"
                f"<br><code>{found}</code>"
            )
        else:
            # 外部版本只有真问过才知道，异步去问，别把界面卡住
            self.detected.setText(f"✅ 当前使用：<code>{found}</code>（查询版本…）")
            self._probe_version(found)

        self._sync_update_button()

    def _probe_version(self, binary: str) -> None:
        """后台跑 ``ncmctl --version``，回来后补进提示文字。"""
        self._probe = BackgroundTask(
            lambda: ncmctl.probe_version(binary, self.opts()), parent=self
        )

        def done(version: object) -> None:
            # 探测期间用户可能又点了「重新探测」，那就别用旧结果盖上去
            if not _same_file(ncmctl.find_binary(self.binary_path.path()), binary):
                return
            text = f"v{version}" if version else "版本未知"
            self.detected.setText(f"✅ 当前使用：{text}<br><code>{binary}</code>")

        self._probe.done.connect(done)
        self._probe.failed.connect(
            lambda _msg: self.detected.setText(
                f"✅ 当前使用：<code>{binary}</code>（版本未知）"
            )
        )
        self._probe.start()

    # -------------------------------------------------------------- 播放器

    def _build_player_card(self) -> None:
        card = FormCard(
            "播放器（mpv）",
            "「我喜欢的音乐」用随程序分发的 mpv 播放。Linux / Windows 的发行包已内置，"
            "macOS 首版需要先执行 brew install mpv。留空表示依次尝试"
            "「随包 → PATH → 常见安装目录」。",
        )
        self.mpv_path = card.add_path(
            "mpv 可执行文件", mode="file",
            value=self.cfg.get("player.mpv_path", ""),
            placeholder="留空表示自动探测",
            hint="填的路径不存在时会退回自动探测；当前实际使用的是哪一个见下方。",
        )

        self.mpv_detected = QLabel("")
        self.mpv_detected.setObjectName("Hint")
        self.mpv_detected.setWordWrap(True)
        card.add_widget(self.mpv_detected)

        detect = ghost_button("重新探测")
        detect.clicked.connect(self._detect_mpv)
        card.add_row_of(detect)
        self.content.addWidget(card)

        self._detect_mpv()

    def _detect_mpv(self) -> None:
        binary = player.find_mpv(self.mpv_path.path())
        if not binary:
            self.mpv_detected.setText(
                "⚠ 未找到 mpv。流媒体播放将不可用：Linux 可执行 "
                "<code>sudo apt install mpv</code>，"
                "或在上面指定 mpv 的完整路径。"
            )
            return

        self.mpv_detected.setText(f"✅ 当前使用：<code>{binary}</code>（查询版本…）")

        # mpv --version 要起一个子进程，放后台跑；异常退出时保持「版本未知」
        task = BackgroundTask(lambda: player.probe_mpv(binary), parent=self)

        def done(result: object) -> None:
            if not _same_file(player.find_mpv(self.mpv_path.path()), binary):
                return  # 探测期间用户改了路径
            ok, output = result  # type: ignore[misc]
            if ok:
                self.mpv_detected.setText(
                    f"✅ 当前使用：<code>{binary}</code><br>{output or '版本未知'}"
                )
            else:
                # 文件在、却跑不起来：多半是缺共享库（打包时漏收 .so）
                self.mpv_detected.setText(
                    f"⚠ 找到了 <code>{binary}</code>，但执行失败：{output or '无输出'}"
                )

        task.done.connect(done)
        task.failed.connect(
            lambda msg: self.mpv_detected.setText(
                f"⚠ 找到了 <code>{binary}</code>，但无法执行：{msg}"
            )
        )
        task.start()

    # -------------------------------------------------------------- 全局参数

    def _build_global_card(self) -> None:
        card = FormCard(
            "全局运行参数",
            "对应 ncmctl 的 --home / -c / --debug，会附加到每一条命令后面。",
        )
        self.home = card.add_path(
            "状态目录（--home）", mode="dir",
            value=self.cfg.get("home", ""),
            placeholder="留空表示使用系统家目录",
            hint="ncmctl 会在这里的 .ncmctl/ 子目录下保存登录凭据与缓存。",
        )
        self.config_file = card.add_path(
            "配置文件（-c）", mode="file",
            value=self.cfg.get("config_file", ""),
            placeholder="留空表示不指定，使用 ncmctl 默认查找逻辑",
        )
        self.debug = card.add_switch(
            "输出详细日志（--debug）",
            bool(self.cfg.get("debug", False)),
            hint="会打印 API 请求头与响应体，其中可能包含敏感数据。排查问题时才建议开启。",
        )

        save = accent_button("保存设置")
        save.clicked.connect(self._save)
        reset = ghost_button("恢复默认")
        reset.clicked.connect(self._reset)
        card.add_row_of(save, reset)
        self.content.addWidget(card)

    def _save(self) -> None:
        self.save_config()
        self.cfg.save()
        self.window.reload_binary()
        self._detect()
        self.toast("设置已保存，并已重新探测 ncmctl。")

    def _reset(self) -> None:
        self.binary_path.set_path("")
        self.home.set_path("")
        self.config_file.set_path("")
        self.debug.setChecked(False)
        self.save_config()
        self.cfg.save()
        self.window.reload_binary()
        self._detect()
        self.toast("已恢复默认设置。")

    # ---------------------------------------------------------------- 更新

    def _build_update_card(self) -> None:
        card = FormCard(
            "更新 ncmctl",
            "从 GitHub Release 下载最新版本，校验 SHA-256 后原地替换当前可执行文件。"
            "已安装的版本不会被降级。",
        )
        self.update_proxy = card.add_line(
            "下载代理", value=self.cfg.get("update.proxy", ""),
            placeholder="留空使用内置代理链；填 direct 强制直连",
            hint="国内网络直连 GitHub 常常失败，留空通常最省事。",
        )

        self.update_button = danger_button("立即更新")
        self.update_button.clicked.connect(self._update)
        card.add_row_of(self.update_button)

        self.update_note = QLabel("")
        self.update_note.setObjectName("Hint")
        self.update_note.setWordWrap(True)
        card.add_widget(self.update_note)

        self.content.addWidget(card)

    def _sync_update_button(self) -> None:
        """随包分发的那份不能自更新，把按钮关掉并说明原因。

        内置副本要么在 AppImage 的只读挂载里，要么在单文件包的临时解包目录里，
        ``ncmctl update`` 想原地覆盖自己必然会失败、或者改了也在退出时被丢掉。
        与其让用户点了之后收到一句莫名其妙的写入错误，不如直接说清楚。
        """
        if getattr(self, "update_button", None) is None:
            return
        blocked = self._using_bundled
        self.update_button.setEnabled(not blocked)
        if blocked:
            self.update_note.setText(
                f"当前使用的是随本程序分发的 ncmctl v{NCMCTL_VERSION}，"
                "它随本程序一同更新，无法单独升级。<br>"
                "想用自己的 ncmctl 并自行更新，请在上方「可执行文件」里指定它的路径。"
            )
        else:
            self.update_note.setText("")

    def _update(self) -> None:
        proxy = self.update_proxy.text().strip()
        if proxy.lower() == "direct":
            proxy = ""
        self.cfg.set("update.proxy", self.update_proxy.text().strip())

        self.window.submit(
            "更新 ncmctl",
            ncmctl.build_update(proxy, self.opts()),
            confirm=(
                "确认更新 ncmctl？",
                "更新会下载新版本并<b>原地覆盖</b>当前的 ncmctl 可执行文件。"
                "更新期间请勿运行其他 ncmctl 作业。<br><br>"
                "下载来源为 GitHub Release，并会校验 SHA-256。",
            ),
        )

    # ---------------------------------------------------------------- 关于

    def _build_about_card(self) -> None:
        card = FormCard("配置与关于")

        path = QLabel(f"本程序的配置文件：<code>{config_dir() / 'config.json'}</code>")
        path.setObjectName("Hint")
        path.setWordWrap(True)
        card.add_widget(path)

        note = QLabel(
            "本程序只是 ncmctl 的图形前端，不保存你的账号密码。<br>"
            "登录凭据由 ncmctl 自己写在 <code>~/.ncmctl/</code> 下，本程序只读取状态用于显示。"
        )
        note.setObjectName("Hint")
        note.setWordWrap(True)
        card.add_widget(note)

        bundled = QLabel(
            f"随本程序分发的 ncmctl 为 <b>v{NCMCTL_VERSION}</b>，"
            "以 MIT 许可发布，版权归 chaunsin 所有。"
        )
        bundled.setObjectName("Hint")
        bundled.setWordWrap(True)
        card.add_widget(bundled)

        open_dir = ghost_button("打开配置目录")
        open_dir.clicked.connect(self._open_config_dir)
        link = ghost_button("打开 ncmctl 项目主页")
        link.clicked.connect(self._open_project)
        card.add_row_of(open_dir, link)
        self.content.addWidget(card)

    def _open_config_dir(self) -> None:
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _open_project(self) -> None:
        QDesktopServices.openUrl(QUrl("https://github.com/chaunsin/netease-cloud-music"))

    # ------------------------------------------------------------------ 配置

    def save_config(self) -> None:
        self.cfg.set("ncmctl_path", self.binary_path.path())
        self.cfg.set("player.mpv_path", self.mpv_path.path())
        self.cfg.set("home", self.home.path())
        self.cfg.set("config_file", self.config_file.path())
        self.cfg.set("debug", self.debug.isChecked())
        self.cfg.set("update.proxy", self.update_proxy.text().strip())
