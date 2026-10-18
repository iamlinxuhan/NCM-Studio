"""监控代理页：抓取并（默认脱敏地）展示网易云 App 的 HTTP(S) 请求。"""

from __future__ import annotations

from PyQt6.QtWidgets import QLabel

from .. import ncmctl
from ..widgets.card import accent_button, danger_button
from ..widgets.form import FormCard
from .base import BasePage

MAX_BODY_PRESETS = [
    ("64 KB", "64KB"),
    ("256 KB", "256KB"),
    ("1 MB", "1MB"),
    ("4 MB", "4MB"),
    ("不限", "0"),
]


class ProxyPage(BasePage):
    PAGE_TITLE = "监控代理"
    PAGE_SUBTITLE = "启动一个本机 HTTP(S) 代理，捕获网易云客户端的接口流量并做脱敏展示。"

    def build(self) -> None:
        self._runner = None

        listen = FormCard("监听设置")
        self.listen = listen.add_line(
            "监听地址",
            value=self.cfg.get("proxy.listen", "127.0.0.1:9000"),
            placeholder="127.0.0.1:9000",
            hint="保持 127.0.0.1 只对本机开放。改成 0.0.0.0 会在局域网暴露一个无认证代理，"
                 "仅限可信网络使用。",
        )
        self.max_body = listen.add_combo(
            "展示体积上限",
            MAX_BODY_PRESETS,
            self.cfg.get("proxy.max_body", "1MB"),
            hint="仅影响控制台里展示的报文长度，不影响转发。",
        )
        self.show_sensitive = listen.add_switch(
            "显示敏感信息（关闭脱敏）",
            bool(self.cfg.get("proxy.show_sensitive", False)),
            hint="开启后账号标识、凭据等会明文出现在日志里。",
        )
        self.content.addWidget(listen)

        ca = FormCard(
            "CA 证书（可选）",
            "不填时 ncmctl 会自行生成并复用一份 CA。注意：本程序不会修改系统信任库，"
            "需要你自己把 CA 装进被代理客户端的信任列表。",
        )
        self.ca_cert = ca.add_path(
            "证书", mode="file", value=self.cfg.get("proxy.ca_cert", ""),
            placeholder="ca.crt（可留空）",
        )
        self.ca_key = ca.add_path(
            "私钥", mode="file", value=self.cfg.get("proxy.ca_key", ""),
            placeholder="ca.key（可留空）",
        )
        hint = QLabel("两者必须同时提供或同时留空。")
        hint.setObjectName("Hint")
        ca.add_widget(hint)
        self.content.addWidget(ca)

        xeapi = FormCard(
            "XEAPI 会话（可选）",
            "用于从既有的抓包状态里预置动态密钥，一般不需要手动填。",
        )
        self.xeapi_id = xeapi.add_line(
            "Session ID", value=self.cfg.get("proxy.xeapi_id", ""),
            placeholder="留空则从响应头动态学习",
        )
        self.xeapi_key = xeapi.add_line(
            "Session Key", value=self.cfg.get("proxy.xeapi_key", ""),
            placeholder="16 / 24 / 32 字节的 ASCII 密钥", password=True,
            hint="⚠️ Session Key 会作为命令行参数传递，可能被同机其他用户、"
                 "进程监控工具或系统日志读取。请只在可信的本机环境使用。",
        )
        self.xeapi_state = xeapi.add_path(
            "状态文件", mode="file", value=self.cfg.get("proxy.xeapi_state", ""),
            placeholder="~/.ncmctl/xeapi.yaml（可留空）",
        )
        self.content.addWidget(xeapi)

        actions = FormCard()
        self.start_button = accent_button("启动代理")
        self.start_button.clicked.connect(self._start)
        self.stop_button = danger_button("停止代理")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        actions.add_row_of(self.start_button, self.stop_button)
        self.content.addWidget(actions)

    # ------------------------------------------------------------------ 内部

    def _start(self) -> None:
        if self._runner and self._runner.is_running:
            self.warn("代理已经在运行了。")
            return

        if self.ca_cert.path() and not self.check_path(
            self.ca_cert, "CA 证书", require_exists=True
        ):
            return
        if self.ca_key.path() and not self.check_path(
            self.ca_key, "CA 私钥", require_exists=True
        ):
            return
        if self.xeapi_state.path() and not self.check_path(
            self.xeapi_state, "XEAPI 状态文件", require_exists=True
        ):
            return

        spec = ncmctl.ProxySpec(
            listen=self.listen.text().strip(),
            ca_cert=self.ca_cert.path(),
            ca_key=self.ca_key.path(),
            max_body=self.max_body.currentData() or "1MB",
            show_sensitive=self.show_sensitive.isChecked(),
            xeapi_session_id=self.xeapi_id.text().strip(),
            xeapi_session_key=self.xeapi_key.text().strip(),
            xeapi_state_file=self.xeapi_state.path(),
        )

        try:
            argv = ncmctl.build_proxy(spec, self.opts())
        except ValueError as exc:
            self.warn(str(exc))
            return

        self.save_config()
        runner = self.window.submit("监控代理", argv)
        if runner:
            self._runner = runner
            self.stop_button.setEnabled(True)
            runner.finished.connect(self._on_stopped)

    def _stop(self) -> None:
        if self._runner and self._runner.is_running:
            self._runner.stop()
            self.stop_button.setEnabled(False)
            self.toast("已请求停止代理。")

    def _on_stopped(self, *_args) -> None:
        self.stop_button.setEnabled(False)

    def save_config(self) -> None:
        self.cfg.set("proxy.listen", self.listen.text().strip())
        self.cfg.set("proxy.max_body", self.max_body.currentData() or "1MB")
        self.cfg.set("proxy.show_sensitive", self.show_sensitive.isChecked())
        self.cfg.set("proxy.ca_cert", self.ca_cert.path())
        self.cfg.set("proxy.ca_key", self.ca_key.path())
        self.cfg.set("proxy.xeapi_id", self.xeapi_id.text().strip())
        self.cfg.set("proxy.xeapi_key", self.xeapi_key.text().strip())
        self.cfg.set("proxy.xeapi_state", self.xeapi_state.path())
