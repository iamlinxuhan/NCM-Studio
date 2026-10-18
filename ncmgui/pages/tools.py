"""调试工具页：API 载荷加解密，以及直接调用导出的 Go API 方法。"""

from __future__ import annotations

from .. import ncmctl
from ..widgets.card import accent_button, ghost_button
from ..widgets.form import FormCard, combo_data
from .base import BasePage

ENCODING_LABELS = [
    ("字符串", "string"),
    ("hex", "hex"),
    ("base64", "base64"),
]


class ToolsPage(BasePage):
    PAGE_TITLE = "调试工具"
    PAGE_SUBTITLE = "本地加解密 NetEase API 载荷，或直接调用 ncmctl 导出的 API 方法。"

    def build(self) -> None:
        self._build_encrypt()
        self._build_decrypt()
        self._build_curl()

    # ------------------------------------------------------------------ 加密

    def _build_encrypt(self) -> None:
        card = FormCard(
            "加密载荷",
            "把一段 JSON 加密成 WEAPI / EAPI / Linux API 格式。EAPI 必须填写请求路由。",
        )
        self.enc_kind = card.add_combo("格式", ncmctl.ENCRYPT_KINDS, "weapi")
        self.enc_url = card.add_line(
            "请求路由", placeholder="/eapi/v3/song/detail",
            hint="仅 EAPI 需要。路由参与摘要计算，必须和实际请求一致。",
        )
        self.enc_payload = card.add_text(
            "JSON",
            value='{"key":"value"}',
            height=90,
            hint="也可以填一个包含 JSON 的文件路径。",
        )
        self.enc_output = card.add_path(
            "输出文件", mode="save", placeholder="留空则打印到控制台",
        )

        run = accent_button("加密")
        run.clicked.connect(self._run_encrypt)
        demo = ghost_button("填入示例")
        demo.clicked.connect(lambda: self.enc_payload.setPlainText('{"key":"value"}'))
        card.add_row_of(run, demo)
        self.content.addWidget(card)

    def _run_encrypt(self) -> None:
        kind = combo_data(self.enc_kind, "weapi")
        url = self.enc_url.text().strip()
        if kind == "eapi" and not url:
            self.warn("EAPI 加密必须填写请求路由，例如 /eapi/v3/song/detail。")
            return
        if not self.check_path(self.enc_output, "输出文件"):
            return
        try:
            argv = ncmctl.build_crypto_encrypt(
                self.enc_payload.toPlainText(),
                kind=kind,
                url=url,
                output=self.enc_output.path(),
                opts=self.opts(),
            )
        except ValueError as exc:
            self.warn(str(exc))
            return
        self.save_config()
        self.submit("加密载荷", argv)

    # ------------------------------------------------------------------ 解密

    def _build_decrypt(self) -> None:
        card = FormCard(
            "解密载荷",
            "解密 EAPI / XEAPI 载荷，也可以直接丢一个 HAR 抓包文件进来按 URL 过滤。",
        )
        self.dec_kind = card.add_combo("格式", ncmctl.DECRYPT_KINDS, "eapi")
        self.dec_encode = card.add_combo(
            "输入编码", ncmctl.DECRYPT_ENCODINGS, "hex",
            hint="直接粘贴密文时用它说明密文是怎么编码的；HAR 文件不需要。",
        )
        self.dec_target = card.add_combo(
            "解密目标", ncmctl.DECRYPT_TARGETS, "auto",
            hint="both 只能用于 HAR。",
        )
        self.dec_url = card.add_line(
            "URL 过滤", value="*",
            hint="HAR 模式下用来筛选条目，例如 /xeapi/*。",
        )
        self.dec_dynamic_key = card.add_line(
            "XEAPI 动态密钥", placeholder="仅 XEAPI 请求 B 需要",
            password=True,
        )
        self.dec_dynamic_encode = card.add_combo(
            "密钥编码", ENCODING_LABELS, "string",
        )
        self.dec_cipher = card.add_text(
            "密文 / HAR", height=100,
            hint="直接粘贴密文，或填一个 .har 文件的路径。",
        )
        self.dec_output = card.add_path(
            "输出文件", mode="save", placeholder="留空则打印到控制台",
        )

        run = accent_button("解密")
        run.clicked.connect(self._run_decrypt)
        card.add_row_of(run)
        self.content.addWidget(card)

    def _run_decrypt(self) -> None:
        if not self.check_path(self.dec_output, "输出文件"):
            return
        try:
            argv = ncmctl.build_crypto_decrypt(
                self.dec_cipher.toPlainText(),
                kind=combo_data(self.dec_kind, "eapi"),
                encode=combo_data(self.dec_encode, "hex"),
                dynamic_key=self.dec_dynamic_key.text().strip(),
                dynamic_key_encode=combo_data(self.dec_dynamic_encode, "string"),
                target=combo_data(self.dec_target, "auto"),
                url=self.dec_url.text().strip() or "*",
                output=self.dec_output.path(),
                opts=self.opts(),
            )
        except ValueError as exc:
            self.warn(str(exc))
            return
        self.save_config()
        self.submit("解密载荷", argv)

    # ------------------------------------------------------------------ curl

    def _build_curl(self) -> None:
        card = FormCard(
            "调用 API 方法",
            "这里的「方法」是 ncmctl 导出的 Go 方法名，不是 HTTP 动词。"
            "调用前请确认该接口是否会产生账号副作用。",
        )
        self.curl_method = card.add_line(
            "方法名", value=self.cfg.get("curl.method", ""),
            placeholder="GetUserInfo",
        )
        self.curl_kind = card.add_combo(
            "接口包", ncmctl.CURL_KINDS, self.cfg.get("curl.kind", "weapi"),
        )
        self.curl_data = card.add_text(
            "请求参数（JSON）", value=self.cfg.get("curl.data", "{}"), height=90,
            hint="会解码成该方法的请求结构体，未知字段会被拒绝。",
        )
        self.curl_timeout = card.add_line(
            "超时", value=self.cfg.get("curl.timeout", "15s"),
            placeholder="15s / 1m",
        )
        self.curl_output = card.add_path(
            "输出文件", mode="save", value=self.cfg.get("curl.output", ""),
            placeholder="留空则打印到控制台",
        )

        run = accent_button("调用")
        run.clicked.connect(self._run_curl)
        whoami = ghost_button("查询当前账号信息")
        whoami.setToolTip("以 weapi 调用 GetUserInfo，参数为空")
        whoami.clicked.connect(self._quick_userinfo)
        card.add_row_of(run, whoami)
        self.content.addWidget(card)

    def _run_curl(self) -> None:
        if not self.check_path(self.curl_output, "输出文件"):
            return
        try:
            argv = ncmctl.build_curl(
                self.curl_method.text(),
                kind=combo_data(self.curl_kind, "weapi"),
                data=self.curl_data.toPlainText().strip() or "{}",
                output=self.curl_output.path(),
                timeout=self.curl_timeout.text().strip() or "15s",
                opts=self.opts(),
            )
        except ValueError as exc:
            self.warn(str(exc))
            return
        self.save_config()
        self.submit(f"API {self.curl_method.text().strip()}", argv)

    def _quick_userinfo(self) -> None:
        if not self.require_login("查询账号信息"):
            return
        self.curl_method.setText("GetUserInfo")
        self.curl_kind.setCurrentIndex(max(0, self.curl_kind.findData("weapi")))
        self.curl_data.setPlainText("{}")
        self.save_config()
        argv = ncmctl.build_curl(
            "GetUserInfo", kind="weapi", data="{}", timeout="15s", opts=self.opts()
        )
        self.submit("账号信息", argv)

    # ------------------------------------------------------------------ 配置

    def save_config(self) -> None:
        self.cfg.set("curl.method", self.curl_method.text().strip())
        self.cfg.set("curl.kind", combo_data(self.curl_kind, "weapi"))
        self.cfg.set("curl.data", self.curl_data.toPlainText())
        self.cfg.set("curl.timeout", self.curl_timeout.text().strip())
        self.cfg.set("curl.output", self.curl_output.path())
