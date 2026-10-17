"""账号页：四种登录方式 + 登出 + 登录状态。

二维码登录的流程值得说明一下：

1. ``ncmctl login qrcode --dir <临时目录>`` 会在该目录写出 ``qrcode.png``
2. 界面用定时器轮询这个文件，一旦出现就加载进来显示
3. 主窗口另外在轮询 ``cookie.json``，登录成功后会自动翻转状态

ncmctl 在控制台里也会打印二维码内容，所以万一 PNG 没生成，
我们还能用 ``qrcode`` 库自己把那段内容渲染出来（该库缺失时降级为文字提示）。
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import ncmctl
from ..widgets.card import Chip, accent_button, danger_button, ghost_button
from ..widgets.form import FormCard
from .base import BasePage


def _restrict_to_owner(fd: int) -> None:
    """把临时文件的权限收成「只有本人可读写」。

    Windows 上压根没有 ``os.fchmod``（那边不存在 POSIX 权限位），但临时文件
    本来就落在用户自己的 ``%TEMP%`` 下，不处理也不会被别人读到。
    """
    if sys.platform != "win32":
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)

QR_SIZE = 232

COOKIE_FORMATS = [
    ("自动识别", ""),
    ("header 文本", "header"),
    ("json", "json"),
    ("netscape 文件", "netscape"),
]


class AccountPage(BasePage):
    PAGE_TITLE = "账号"
    PAGE_SUBTITLE = "登录网易云账号。多数功能（下载、云盘、任务）都需要先登录。"

    def build(self) -> None:
        self._qr_dir: str | None = None
        self._qr_runner = None
        self._qr_from_stdout: str | None = None
        self._qr_shown = False

        self._build_status_card()
        self._build_qrcode_card()
        self._build_phone_card()
        self._build_cookie_card()
        self._build_cookiecloud_card()

        self._qr_timer = QTimer(self)
        self._qr_timer.setInterval(400)
        self._qr_timer.timeout.connect(self._poll_qrcode)

        self._refresh_status()

    # ------------------------------------------------------------ 登录状态

    def _build_status_card(self) -> None:
        card = FormCard("登录状态")

        self.status_chip = Chip("未登录", "off")
        self.status_detail = QLabel("")
        self.status_detail.setObjectName("Hint")
        self.status_detail.setWordWrap(True)

        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(12)
        holder_layout.addWidget(self.status_chip)
        holder_layout.addWidget(self.status_detail, 1)
        card.add_widget(holder)

        refresh = ghost_button("刷新状态")
        refresh.clicked.connect(self._refresh_status)
        info = ghost_button("查询账号信息")
        info.clicked.connect(self._query_userinfo)
        self.logout_btn = danger_button("登出")
        self.logout_btn.clicked.connect(self._logout)
        card.add_row_of(refresh, info, self.logout_btn)
        self.content.addWidget(card)

    def _refresh_status(self) -> None:
        home = self.cfg.get("home", "")
        logged_in = ncmctl.is_logged_in(home)

        # 没登录就没什么可登出的。这个按钮原来是常亮的红色，未登录时点下去
        # 只会去调一次注定失败的接口 —— 状态写着"未登录"、旁边却摆着「登出」，
        # 本身就是在骗人。禁用比弹一句"请先登录"更直接
        self.logout_btn.setEnabled(logged_in)
        self.logout_btn.setToolTip("" if logged_in else "还没有登录，无需登出")

        if logged_in:
            self.status_chip.setText("已登录")
            self.status_chip.set_state("on")
            stamp = ncmctl.login_timestamp(home)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "未知"
            self.status_detail.setText(
                f"凭据文件：{ncmctl.cookie_path(home)}<br>最近一次写入：{when}"
            )
        else:
            self.status_chip.setText("未登录")
            self.status_chip.set_state("off")
            self.status_detail.setText(
                f"未找到 {ncmctl.cookie_path(home)}<br>"
                "推荐用下面的「扫码登录」，最省事也最安全。"
            )

    def on_login_changed(self, logged_in: bool) -> None:
        self._refresh_status()
        if logged_in:
            self._stop_qrcode("登录成功")
            self.qr_status.setText("✅ 登录成功，凭据已保存。")

    def _query_userinfo(self) -> None:
        if not self.require_login("查询账号信息"):
            return
        argv = ncmctl.build_curl(
            "GetUserInfo", kind="weapi", data="{}", timeout="15s", opts=self.opts()
        )
        self.submit("账号信息", argv)

    def _logout(self) -> None:
        # 按钮已经按登录状态禁用，这里再拦一道：状态可能是刚变的（比如凭据文件被
        # 外部删掉），而窗口还没来得及刷新
        if not self.require_login("登出"):
            return
        if not self.window.submit(
            "登出",
            ncmctl.build_logout(False, self.opts()),
            confirm=(
                "确认登出？",
                "登出会调用网易云接口并删除本地保存的凭据文件。"
                "之后需要重新登录才能使用下载、云盘和账号任务功能。",
            ),
        ):
            return

    # ------------------------------------------------------------ 扫码登录

    def _build_qrcode_card(self) -> None:
        card = FormCard(
            "扫码登录",
            "用网易云音乐 App 扫码确认，无需输入密码，凭据也不会经过本程序。",
        )

        # 左：二维码，右：说明
        self.qr_label = QLabel("点「生成二维码」\n二维码会显示在这里")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setFixedSize(QR_SIZE, QR_SIZE)
        self.qr_label.setObjectName("CardSubtitle")
        self.qr_label.setStyleSheet("border: 1px dashed #2b3c60; border-radius: 8px;")

        self.qr_status = QLabel("尚未开始。")
        self.qr_status.setObjectName("Hint")
        self.qr_status.setWordWrap(True)

        steps = QLabel(
            "1. 点下方「生成二维码」<br>"
            "2. 打开手机网易云音乐 → 左上角菜单 → 扫一扫<br>"
            "3. 扫描后手机上确认登录<br><br>"
            "二维码有效期默认 5 分钟，过期后重新生成即可。"
        )
        steps.setObjectName("Hint")
        steps.setWordWrap(True)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_layout.addWidget(self.qr_status)
        right_layout.addWidget(steps)
        right_layout.addStretch(1)

        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(18)
        holder_layout.addWidget(self.qr_label)
        holder_layout.addWidget(right, 1)
        card.add_widget(holder)

        self.qr_start = accent_button("生成二维码")
        self.qr_start.clicked.connect(self._start_qrcode)
        self.qr_stop = danger_button("停止等待")
        self.qr_stop.setEnabled(False)
        self.qr_stop.clicked.connect(lambda: self._stop_qrcode("已停止等待"))
        open_qr_dir = ghost_button("打开二维码目录")
        open_qr_dir.clicked.connect(self._reveal_qr_dir)
        card.add_row_of(self.qr_start, self.qr_stop, open_qr_dir)
        self.content.addWidget(card)

    def _start_qrcode(self) -> None:
        if self._qr_runner and self._qr_runner.is_running:
            self.warn("二维码已在等待确认中，请先在手机上操作，或点「停止等待」。")
            return

        self._cleanup_qr_dir()
        self._qr_dir = tempfile.mkdtemp(prefix="ncmctl-qr-")
        self._qr_shown = False
        self._qr_from_stdout = None

        self.qr_label.setPixmap(QPixmap())
        self.qr_label.setText("正在生成二维码…")
        self.qr_status.setText("已请求 ncmctl 生成二维码，请稍候…")

        runner = self.window.submit(
            "二维码登录",
            ncmctl.build_login_qrcode(self._qr_dir, timeout="5m", opts=self.opts()),
        )
        if not runner:
            self._cleanup_qr_dir()
            self.qr_label.setText("生成失败")
            self.qr_status.setText("作业未能启动。")
            return

        self._qr_runner = runner
        runner.output.connect(self._watch_qr_output)
        runner.finished.connect(self._on_qr_finished)
        self.qr_stop.setEnabled(True)
        self._qr_timer.start()

    def _watch_qr_output(self, line: str) -> None:
        """兜底：从控制台输出里捞二维码内容，供自行渲染。"""
        stripped = line.strip()
        if stripped.startswith("http") and ("login" in stripped or "163" in stripped):
            if self._qr_from_stdout is None:
                self._qr_from_stdout = stripped

    def _poll_qrcode(self) -> None:
        if not self._qr_dir:
            return

        image = Path(self._qr_dir) / "qrcode.png"
        if image.is_file():
            pixmap = QPixmap(str(image))
            if not pixmap.isNull():
                self.qr_label.setPixmap(
                    pixmap.scaled(
                        QR_SIZE, QR_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                if not self._qr_shown:
                    self._qr_shown = True
                    self.qr_status.setText("二维码已生成 —— 请用手机网易云音乐扫码并在手机上确认。")
                return

        # PNG 还没出现：如果已经从输出里拿到了内容，就自己渲染一张
        if not self._qr_shown and self._qr_from_stdout:
            if self._render_qr_from_text(self._qr_from_stdout):
                self._qr_shown = True
                self.qr_status.setText("二维码已生成（由终端内容渲染）—— 请用手机扫码确认。")

    def _render_qr_from_text(self, text: str) -> bool:
        """用 qrcode 库把二维码内容渲染成图片。缺库时返回 False。"""
        try:
            import io

            import qrcode
            from PyQt6.QtGui import QImage
        except ImportError:
            return False

        try:
            image = qrcode.make(text)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            qt_image = QImage()
            qt_image.loadFromData(buffer.getvalue(), "PNG")
            if qt_image.isNull():
                return False
            self.qr_label.setPixmap(
                QPixmap.fromImage(qt_image).scaled(
                    QR_SIZE, QR_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            return True
        except Exception:
            return False

    def _on_qr_finished(self, code: int, _seconds: float) -> None:
        self.qr_stop.setEnabled(False)
        self._qr_timer.stop()

        if not self._qr_shown:
            self.qr_status.setText(
                "ncmctl 已退出，但没有生成二维码图片。"
                "如果在控制台里看到了二维码内容，可以直接用手机扫描。"
            )
        elif code != 0:
            self.qr_status.setText("等待被中断或超时。可以重新生成二维码。")

        # 稍等一下再删目录，避免刚写入的图片被立刻清掉
        QTimer.singleShot(1500, self._cleanup_qr_dir)
        self.refresh_login()

    def refresh_login(self) -> None:
        self.window.refresh_login()

    def _stop_qrcode(self, reason: str) -> None:
        self._qr_timer.stop()
        if self._qr_runner and self._qr_runner.is_running:
            self._qr_runner.stop()
            self.qr_status.setText(f"{reason}，正在终止等待…")
        else:
            self.qr_status.setText(reason)
        self.qr_stop.setEnabled(False)

    def _reveal_qr_dir(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        if self._qr_dir and Path(self._qr_dir).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._qr_dir))
        else:
            self.toast("当前没有二维码目录（尚未生成过二维码）。")

    def _cleanup_qr_dir(self) -> None:
        if self._qr_dir:
            shutil.rmtree(self._qr_dir, ignore_errors=True)
            self._qr_dir = None

    # ------------------------------------------------------------ 手机号登录

    def _build_phone_card(self) -> None:
        card = FormCard(
            "手机号登录",
            "不给密码就走短信验证码：ncmctl 发码后会在控制台等待输入，"
            "在底部控制台的输入框里填验证码即可。",
        )
        self.phone_number = card.add_line(
            "手机号", value=self.cfg.get("login.phone", ""), placeholder="18800008888",
        )
        self.phone_country = card.add_line(
            "国家代码", value=str(self.cfg.get("login.country", "86")), placeholder="86",
        )
        self.phone_password = card.add_line(
            "密码", placeholder="留空则改用短信验证码", password=True,
            hint="密码会以明文出现在进程参数里（ncmctl 本身的限制），介意的话请用扫码登录。",
        )

        run = accent_button("登录")
        run.clicked.connect(self._login_phone)
        card.add_row_of(run)
        self.content.addWidget(card)

    def _login_phone(self) -> None:
        number = self.phone_number.text().strip()
        if not number:
            self.warn("请填写手机号。")
            return
        try:
            country = int(self.phone_country.text().strip() or "86")
        except ValueError:
            self.warn("国家代码需要是数字，例如 86。")
            return

        self.save_config()
        password = self.phone_password.text()
        argv = ncmctl.build_login_phone(
            number, countrycode=country, password=password, timeout="10m", opts=self.opts()
        )
        self.submit("手机号登录", argv)

    # ------------------------------------------------------------- Cookie

    def _build_cookie_card(self) -> None:
        card = FormCard(
            "Cookie 登录",
            "从已登录的浏览器里导出 Cookie 再导入。内容里必须含 MUSIC_U。"
            "粘贴的内容会先写进 0600 权限的临时文件，避免出现在进程参数里。",
        )
        self.cookie_file = card.add_path(
            "Cookie 文件", mode="file",
            placeholder="选择导出的 cookie 文件（优先使用）",
        )
        self.cookie_text = card.add_text(
            "或粘贴内容", height=90,
            placeholder="MUSIC_U=xxxx; __csrf=yyyy",
            hint="填写了文件时以文件为准。",
        )
        self.cookie_format = card.add_combo("格式", COOKIE_FORMATS, "")

        run = accent_button("导入并登录")
        run.clicked.connect(self._login_cookie)
        card.add_row_of(run)
        self.content.addWidget(card)

    def _login_cookie(self) -> None:
        file_path = self.cookie_file.path()
        content = self.cookie_text.toPlainText().strip()
        if not file_path and not content:
            self.warn("请选择 Cookie 文件，或直接粘贴 Cookie 内容。")
            return

        temp_file: str | None = None
        if not file_path and content:
            temp_file = self._write_secure_temp(content)
            if not temp_file:
                self.warn("无法写入临时文件，请改用文件导入。")
                return
            file_path = temp_file

        argv = ncmctl.build_login_cookie(
            file=file_path,
            fmt=self.cookie_format.currentData() or "",
            opts=self.opts(),
        )
        runner = self.submit("Cookie 登录", argv)
        if runner and temp_file:
            # 进程读完就可以删了；留一点余量
            runner.finished.connect(lambda *_: self._remove_temp(temp_file))

    @staticmethod
    def _write_secure_temp(content: str) -> str | None:
        """把 Cookie 内容写进只有本人可读的临时文件。"""
        handle = -1
        try:
            handle, path = tempfile.mkstemp(prefix="ncmctl-cookie-", suffix=".txt")
            _restrict_to_owner(handle)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                handle = -1  # 交给 stream 管了，别再自己 close
                stream.write(content)
            return path
        except OSError:
            if handle >= 0:
                os.close(handle)
            return None

    @staticmethod
    def _remove_temp(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    # --------------------------------------------------------- CookieCloud

    def _build_cookiecloud_card(self) -> None:
        card = FormCard(
            "CookieCloud 登录",
            "从自建的 CookieCloud 服务同步浏览器 Cookie。需要 UUID 与密码。",
        )
        self.cc_uuid = card.add_line("UUID", value=self.cfg.get("login.cc_uuid", ""))
        self.cc_password = card.add_line(
            "密码", placeholder="CookieCloud 端的密码", password=True,
            hint="会以明文出现在进程参数中。",
        )
        self.cc_server = card.add_line(
            "服务器", value=self.cfg.get("login.cc_server", "http://127.0.0.1:8088"),
        )
        self.cc_headers = card.add_line(
            "额外请求头", placeholder="k1=v1,k2=v2",
        )

        run = accent_button("同步并登录")
        run.clicked.connect(self._login_cookiecloud)
        card.add_row_of(run)
        self.content.addWidget(card)

    def _login_cookiecloud(self) -> None:
        uuid = self.cc_uuid.text().strip()
        password = self.cc_password.text()
        if not uuid or not password:
            self.warn("CookieCloud 的 UUID 与密码都是必填项。")
            return

        self.save_config()
        argv = ncmctl.build_login_cookiecloud(
            uuid,
            password,
            server=self.cc_server.text().strip(),
            headers=self.cc_headers.text().strip(),
            opts=self.opts(),
        )
        self.submit("CookieCloud 登录", argv)

    # ------------------------------------------------------------------ 配置

    def save_config(self) -> None:
        self.cfg.set("login.phone", self.phone_number.text().strip())
        self.cfg.set("login.country", self.phone_country.text().strip())
        self.cfg.set("login.cc_uuid", self.cc_uuid.text().strip())
        self.cfg.set("login.cc_server", self.cc_server.text().strip())
