"""账号任务页：签到、听歌、音乐合伙人，以及把它们挂成定时服务。

ncmctl 的 help 明确提示 scrobble 与 partner 有账号风控风险，
所以这两项在执行前都会弹二次确认。
"""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QWidget

from .. import ncmctl
from ..widgets.card import accent_button, danger_button
from ..widgets.form import FormCard
from .base import BasePage

LOCATIONS = [
    ("Asia/Shanghai（北京时间）", "Asia/Shanghai"),
    ("Asia/Tokyo", "Asia/Tokyo"),
    ("Asia/Singapore", "Asia/Singapore"),
    ("UTC", "UTC"),
]


class TasksPage(BasePage):
    PAGE_TITLE = "账号任务"
    PAGE_SUBTITLE = "每日签到、听歌打卡、音乐合伙人评测，以及按 cron 定时执行的服务。"

    def build(self) -> None:
        self._service_runner = None

        self._build_sign_card()
        self._build_scrobble_card()
        self._build_partner_card()
        self._build_service_card()

    # --------------------------------------------------------------- 每日签到

    def _build_sign_card(self) -> None:
        card = FormCard("每日签到", "云贝签到 + VIP 签到，各执行一次。")
        self.sign_automatic = card.add_switch(
            "同时领取云贝与 VIP 奖励",
            bool(self.cfg.get("task.sign_automatic", False)),
            hint="会额外执行领取动作，账号风控暴露略微增加。",
        )
        run = accent_button("立即签到")
        run.clicked.connect(self._run_sign)
        card.add_row_of(run)
        self.content.addWidget(card)

    def _run_sign(self) -> None:
        if not self.require_login("签到"):
            return
        self.save_config()
        argv = ncmctl.build_sign(self.sign_automatic.isChecked(), self.opts())
        self.submit("签到", argv)

    # --------------------------------------------------------------- 听歌任务

    def _build_scrobble_card(self) -> None:
        card = FormCard(
            "听歌任务",
            "提交播放记录以提高账号的累计听歌数。使用本地数据库避免重复提交同一首歌。",
        )
        self.scrobble_num = card.add_spin(
            "提交数量",
            int(self.cfg.get("task.scrobble_num", 300)),
            1, 300,
            hint="单次上限 300；可用曲目不足时实际提交数会少于这个值。",
        )
        warn = QLabel("⚠ ncmctl 官方提示：该自动化行为有较高的账号受限风险。")
        warn.setObjectName("Hint")
        card.add_widget(warn)

        run = danger_button("立即执行听歌任务")
        run.clicked.connect(self._run_scrobble)
        card.add_row_of(run)
        self.content.addWidget(card)

    def _run_scrobble(self) -> None:
        if not self.require_login("听歌任务"):
            return
        self.save_config()
        argv = ncmctl.build_scrobble(self.scrobble_num.value(), self.opts())
        self.window.submit(
            "听歌任务",
            argv,
            confirm=(
                "确认执行听歌任务？",
                f"将向账号提交最多 <b>{self.scrobble_num.value()}</b> 条播放记录。<br><br>"
                "ncmctl 对此行为标注了较高的账号受限风险，请自行斟酌使用频率。",
            ),
        )

    # ----------------------------------------------------------- 音乐合伙人

    def _build_partner_card(self) -> None:
        card = FormCard(
            "音乐合伙人",
            "提交音乐合伙人评测。每首之间会等待 15-24 秒，属于正常的慢速任务。",
        )
        self.partner_star = self._score_row(
            card, "基础评分", self.cfg.get("task.partner_star", [3, 4]),
            hint="每次从勾选的分数里随机取一个作为基础分。",
        )
        self.partner_extra = self._score_row(
            card, "附加评分", self.cfg.get("task.partner_extra", [2, 3, 4]),
            hint="附加评测环节使用的分数候选。",
        )
        self.partner_num = card.add_line(
            "额外评测数",
            value=str(self.cfg.get("task.partner_num", "random")),
            placeholder="random",
            hint="填 random（2-7 之间随机）或 0-15 的整数。",
        )
        warn = QLabel("⚠ 该操作会改动账号状态，且同样存在风控风险。")
        warn.setObjectName("Hint")
        card.add_widget(warn)

        run = danger_button("立即提交评测")
        run.clicked.connect(self._run_partner)
        card.add_row_of(run)
        self.content.addWidget(card)

    def _score_row(self, card: FormCard, label: str, defaults, hint: str) -> list[QCheckBox]:
        """1-5 分的多选行。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)

        boxes: list[QCheckBox] = []
        defaults = list(defaults or [])
        for score in range(1, 6):
            box = QCheckBox(str(score))
            box.setChecked(score in defaults)
            boxes.append(box)
            row.addWidget(box)
        row.addStretch(1)

        card.add_row(label, holder, hint)
        return boxes

    @staticmethod
    def _checked_scores(boxes: list[QCheckBox]) -> list[int]:
        return [index + 1 for index, box in enumerate(boxes) if box.isChecked()]

    def _run_partner(self) -> None:
        if not self.require_login("音乐合伙人评测"):
            return

        star = self._checked_scores(self.partner_star)
        extra = self._checked_scores(self.partner_extra)
        num = self.partner_num.text().strip() or "random"

        if not star:
            self.warn("请至少勾选一个基础评分。")
            return
        if not extra:
            self.warn("请至少勾选一个附加评分。")
            return

        self.save_config()
        argv = ncmctl.build_partner(star, extra, num, self.opts())
        self.window.submit(
            "音乐合伙人",
            argv,
            confirm=(
                "确认提交评测？",
                "该操作会真实提交评测并改动账号状态，"
                "且 ncmctl 提示存在风控风险。<br><br>执行过程较慢（每首间隔 15-24 秒），请耐心等待。",
            ),
        )

    # ------------------------------------------------------------- 定时服务

    def _build_service_card(self) -> None:
        card = FormCard(
            "定时任务服务",
            "把任务按 cron 表达式挂成常驻服务，启动后会一直运行直到你手动停止。",
        )

        self.task_sign = QCheckBox("签到")
        self.task_sign.setChecked(bool(self.cfg.get("task.service_sign", True)))
        self.task_scrobble = QCheckBox("听歌")
        self.task_scrobble.setChecked(bool(self.cfg.get("task.service_scrobble", False)))
        self.task_partner = QCheckBox("音乐合伙人")
        self.task_partner.setChecked(bool(self.cfg.get("task.service_partner", False)))

        selector = QWidget()
        selector_row = QHBoxLayout(selector)
        selector_row.setContentsMargins(0, 0, 0, 0)
        selector_row.setSpacing(20)
        for box in (self.task_sign, self.task_scrobble, self.task_partner):
            selector_row.addWidget(box)
        selector_row.addStretch(1)
        card.add_row("启用任务", selector, "一个都不勾选时，等价于全部启用（--runAll）。")

        self.sign_cron = card.add_line(
            "签到 cron",
            value=self.cfg.get("task.sign_cron", "0 10 * * *"),
            hint="五段式 cron：分 时 日 月 周。默认每天 10:00。",
        )
        self.scrobble_cron = card.add_line(
            "听歌 cron",
            value=self.cfg.get("task.scrobble_cron", "0 18 * * *"),
            hint="默认每天 18:00。",
        )
        self.partner_cron = card.add_line(
            "合伙人 cron",
            value=self.cfg.get("task.partner_cron", "0 18 * * *"),
            hint="默认每天 18:00。",
        )
        self.location = card.add_combo(
            "时区",
            LOCATIONS,
            self.cfg.get("task.location", "Asia/Shanghai"),
            hint="cron 表达式按此时区解释。",
        )

        start = accent_button("启动定时服务")
        start.clicked.connect(self._start_service)
        self.stop_service_button = danger_button("停止服务")
        self.stop_service_button.setEnabled(False)
        self.stop_service_button.clicked.connect(self._stop_service)
        card.add_row_of(start, self.stop_service_button)
        self.content.addWidget(card)

    @staticmethod
    def _cron_ok(expr: str) -> bool:
        return len((expr or "").split()) == 5

    def _start_service(self) -> None:
        if not self.require_login("定时任务服务"):
            return
        if self._service_runner and self._service_runner.is_running:
            self.warn("定时服务已经在运行了。")
            return

        for label, edit in (
            ("签到", self.sign_cron),
            ("听歌", self.scrobble_cron),
            ("合伙人", self.partner_cron),
        ):
            if not self._cron_ok(edit.text()):
                self.warn(f"{label} 的 cron 表达式不是五段式，请检查（例：0 10 * * *）。")
                return

        spec = ncmctl.TaskSpec(
            sign=self.task_sign.isChecked(),
            scrobble=self.task_scrobble.isChecked(),
            partner=self.task_partner.isChecked(),
            sign_cron=self.sign_cron.text().strip(),
            scrobble_cron=self.scrobble_cron.text().strip(),
            partner_cron=self.partner_cron.text().strip(),
            location=self.location.currentData() or "Asia/Shanghai",
            sign_automatic=self.sign_automatic.isChecked(),
            scrobble_num=self.scrobble_num.value(),
            partner_num=self.partner_num.text().strip() or "random",
        )

        self.save_config()
        runner = self.window.submit("定时任务服务", ncmctl.build_task(spec, self.opts()))
        if runner:
            self._service_runner = runner
            self.stop_service_button.setEnabled(True)
            runner.finished.connect(self._on_service_stopped)

    def _stop_service(self) -> None:
        if self._service_runner and self._service_runner.is_running:
            self._service_runner.stop()
            self.stop_service_button.setEnabled(False)
            self.toast("已请求停止定时服务。")

    def _on_service_stopped(self, *_args) -> None:
        self.stop_service_button.setEnabled(False)

    # ------------------------------------------------------------------ 配置

    def save_config(self) -> None:
        self.cfg.set("task.sign_automatic", self.sign_automatic.isChecked())
        self.cfg.set("task.scrobble_num", self.scrobble_num.value())
        self.cfg.set("task.partner_star", self._checked_scores(self.partner_star))
        self.cfg.set("task.partner_extra", self._checked_scores(self.partner_extra))
        self.cfg.set("task.partner_num", self.partner_num.text().strip())
        self.cfg.set("task.service_sign", self.task_sign.isChecked())
        self.cfg.set("task.service_scrobble", self.task_scrobble.isChecked())
        self.cfg.set("task.service_partner", self.task_partner.isChecked())
        self.cfg.set("task.sign_cron", self.sign_cron.text().strip())
        self.cfg.set("task.scrobble_cron", self.scrobble_cron.text().strip())
        self.cfg.set("task.partner_cron", self.partner_cron.text().strip())
        self.cfg.set("task.location", self.location.currentData() or "Asia/Shanghai")
