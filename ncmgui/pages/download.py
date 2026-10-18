"""下载页：歌曲 ID / 链接 → 本地音乐文件。

除了把 ncmctl 的 download 子命令表单化，这一页还补了三件 ncmctl 自己不做的
事（前两件见 :mod:`ncmgui.resolver`）：

* **查重** —— ncmctl 遇到同名文件只会改写成 ``歌曲(1).flac`` 接着下，
  不会跳过。这里在下载前先把目标展开成歌曲清单，比对输出目录，把已有的剔掉。
* **下载「我喜欢的音乐」** —— 一键把整个红心歌单搬回本地。
* **歌词与封面** —— ncmctl 只下音频，标签一个都不写（``--tag`` 是空实现），
  歌词、封面自然也没有。下完之后按设置补一轮，见 :mod:`ncmgui.lyrics` 与
  :mod:`ncmgui.covers`。
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMessageBox

from .. import covers, images, lyrics, ncmctl
from ..config import default_music_dir
from ..resolver import PLAYLIST_URL, FeeTagger, Plan, Resolver, Song
from ..widgets.card import Card, accent_button, ghost_button
from ..widgets.console import ConsoleView, classify
from ..widgets.form import FormCard, combo_data
from .base import BasePage

PLACEHOLDER = (
    "2161154646\n"
    "https://music.163.com/song?id=1820944399\n"
    "https://music.163.com/playlist?id=593617579\n"
    "https://music.163.com/album?id=32311"
)

#: 解析任务当前的意图，决定拿到计划之后做什么
MODE_PREVIEW = "preview"
MODE_DOWNLOAD = "download"
MODE_LIKED = "liked"

#: 抓歌词时的并发路数。每首要起一个 ncmctl 子进程，开太多只是让接口先限流我们；
#: 这个环节本来就只有几秒到几十秒，不值得为它把并发拉满。
LYRIC_PARALLEL = 4

#: 歌词接口的超时。比解析用的 30s 短 —— 一首歌的歌词就一个请求，卡住就是卡住，
#: 拖着不返回只会白占一个并发位。
LYRIC_TIMEOUT = "20s"


def _say(panel, text: str, progress: bool = False) -> None:
    """往某个作业面板里写一行（或刷新一行）。

    面板可能已经被用户关掉了 —— 歌词是下载结束后才接上去跑的一轮，用户完全
    可能在这期间把标签页关掉。此时访问它的控件会抛 ``RuntimeError``，忽略即可。
    """
    try:
        if progress:
            panel.view.append_progress(text)
        else:
            panel.view.append_line(text, classify(text))
    except RuntimeError:
        pass


class _LyricSignals(QObject):
    """把歌词后处理线程里的输出送回界面。

    :class:`~ncmgui.workers.BackgroundTask` 只有一个 ``progress(str)`` 通道，
    而这里要区分「不断刷新的计数行」和「值得留一行的说明」，所以在页面内部另开
    一个两通道的信号对象。信号是队列投递的，槽函数仍在主线程执行。
    """

    progress = pyqtSignal(str)
    note = pyqtSignal(str)


class DownloadPage(BasePage):
    PAGE_TITLE = "下载"
    PAGE_SUBTITLE = "解析歌曲 ID、专辑 / 歌手 / 歌单链接并下载到本地。需要登录。"

    # --------------------------------------------------------------- 生命周期

    def __init__(self, window) -> None:
        # 必须在 build() 之前就位：build() 里会用到它们
        self._task = None
        self._plan: Plan | None = None
        self._plan_mode = ""
        #: 解析失败时退而求其次的动作（见 _start_task）
        self._fallback: Callable[[], None] | None = None
        #: 正在跑的歌词 / 封面任务，仅用于避免重复启动
        self._lyric_task = None
        self._cover_task = None
        super().__init__(window)

    # -------------------------------------------------------------------- 构建

    def build(self) -> None:
        self._build_targets()
        self._build_quick()
        self._build_options()
        self._build_report()
        self._build_actions()
        self._sync_buttons()

    def _build_targets(self) -> None:
        targets_card = FormCard(
            "下载目标",
            "每行一个，也可以用空格或逗号分隔。支持歌曲 ID 以及歌曲 / 专辑 / 歌手 / 歌单链接。",
        )
        self.targets = targets_card.add_text(
            "", value="", placeholder=PLACEHOLDER, height=150
        )
        self.target_count = QLabel("尚未识别到目标")
        self.target_count.setObjectName("Hint")
        targets_card.add_widget(self.target_count)
        self.targets.textChanged.connect(self._on_targets_changed)
        self.content.addWidget(targets_card)

    def _build_quick(self) -> None:
        card = Card("快捷下载", "不用手抄歌单 ID，直接从账号里取。")

        self.liked_button = accent_button("下载「我喜欢的音乐」全部歌曲")
        self.liked_button.clicked.connect(self._download_liked)

        hint = QLabel(
            "先读取红心歌单的曲目，再按下面的「查重」设置决定跳过哪些，"
            "最后只把缺的交给 ncmctl。"
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)

        card.add(self.liked_button)
        card.add(hint)
        self.content.addWidget(card)

    def _build_options(self) -> None:
        options = FormCard("下载设置")
        self.level = options.add_combo(
            "音质",
            ncmctl.DOWNLOAD_LEVELS,
            self.cfg.get("download.level", "lossless"),
            hint="该音质不可得时默认自动降级；想严格按所选音质下载请打开下面的开关。",
        )
        self.strict = options.add_switch(
            "严格音质（音质不可得时跳过，而不是降级）",
            bool(self.cfg.get("download.strict", False)),
        )
        self.skip_existing = options.add_switch(
            "跳过输出目录里已存在的歌曲（查重）",
            bool(self.cfg.get("download.skip_existing", True)),
            hint=(
                "下载前先解析出每首歌的文件名并比对输出目录，已存在的直接跳过。"
                "ncmctl 自身不会跳过，重名时会写成「歌曲(1).flac」再下一遍。"
            ),
        )
        self.lyric = options.add_combo(
            "歌词",
            lyrics.MODES,
            # 配置里是个认不出的值时 set_combo_data 会保持第 0 项，而第 0 项
            # 就是默认的「独立 .lrc 文件」，正好是我们想要的兜底
            self.cfg.get("download.lyric", lyrics.MODE_LRC),
            hint=(
                "ncmctl 只下音频，一个标签都不写，歌词更没有。「独立 .lrc 文件」"
                "会在音频旁边放一个同名的 .lrc；「写入音频标签」直接写进音频文件"
                "（会改写文件本身）；外文歌会自动把中文翻译逐行插在原文下面。"
                "已经带歌词的会跳过，重复跑不会做无用功。"
            ),
        )
        self.cover = options.add_combo(
            "封面",
            covers.MODES,
            self.cfg.get("download.cover", covers.MODE_FILE),
            hint=(
                "「保存为同名图片」会在音频旁边放一张同名 .jpg，不改动音频文件；"
                "「写入音频标签」直接嵌进音频文件（会改写文件本身）。"
                "本地音乐播放时会优先用这张图。已经有封面的会跳过。"
            ),
        )
        self.parallel = options.add_spin(
            "并发数",
            int(self.cfg.get("download.parallel", 5)),
            1, 20,
            hint="同时下载的曲目数量，网络不稳定时建议调小。",
        )
        self.output = options.add_path(
            "输出目录",
            mode="dir",
            value=self.cfg.get("download.output", ""),
            default=str(default_music_dir()),
            hint="下载完成的文件会写到这里，MD5 校验通过后才算完成。",
        )
        # 输出目录变了，之前的查重结论就作废
        self.output.pathChanged.connect(lambda _path: self._invalidate_plan())
        self.content.addWidget(options)

    def _build_report(self) -> None:
        card = Card("查重结果", "点「开始下载」或「仅查重」后，这里会列出跳过与待下载的曲目。")

        self.report_label = QLabel("还没有查重结果。")
        self.report_label.setObjectName("Hint")
        self.report_label.setWordWrap(True)

        self.report_view = ConsoleView()
        self.report_view.setFixedHeight(150)
        self.report_view.setVisible(False)

        self.details_button = ghost_button("显示明细", compact=True)
        self.details_button.setVisible(False)
        self.details_button.clicked.connect(self._toggle_details)

        # 按钮单独一行并靠左，别被卡片拉成通栏
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.addWidget(self.details_button)
        toggle_row.addStretch(1)

        card.add(self.report_label)
        card.add(toggle_row)
        card.add(self.report_view)
        self.content.addWidget(card)

    def _build_actions(self) -> None:
        actions = FormCard()
        self.start_button = accent_button("开始下载")
        self.start_button.clicked.connect(self._start)

        self.preview_button = ghost_button("仅查重")
        self.preview_button.clicked.connect(self._preview)

        clear_button = ghost_button("清空目标")
        clear_button.clicked.connect(lambda: self.targets.setPlainText(""))

        open_button = ghost_button("打开输出目录")
        open_button.clicked.connect(self.output.reveal)

        actions.add_row_of(self.start_button, self.preview_button)
        actions.add_row_of(clear_button, open_button)
        self.content.addWidget(actions)

    # ------------------------------------------------------------------ 交互

    def _on_targets_changed(self) -> None:
        count = len(ncmctl.parse_targets(self.targets.toPlainText()))
        self.target_count.setText(
            f"已识别 {count} 个目标" if count else "尚未识别到目标"
        )
        self._invalidate_plan()

    def _invalidate_plan(self) -> None:
        """输入或输出目录变了，上次的查重结论就不算数了。"""
        self._plan = None

    def _sync_buttons(self) -> None:
        """按钮可用状态只在这里决定，避免各处各改一套。"""
        busy = bool(self._task is not None and self._task.is_running)
        self.start_button.setEnabled(not busy)
        self.preview_button.setEnabled(not busy)
        self.liked_button.setEnabled(not busy and self.is_logged_in())
        self.setCursor(
            Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.ArrowCursor
        )

    def _toggle_details(self) -> None:
        visible = not self.report_view.isVisible()
        self.report_view.setVisible(visible)
        self.details_button.setText("隐藏明细" if visible else "显示明细")

    def on_login_changed(self, logged_in: bool) -> None:
        self._sync_buttons()

    # ------------------------------------------------------------------ 查重

    def _output_dir(self) -> str:
        return self.output.path()

    def _check_output(self) -> bool:
        return self.check_path(self.output, "输出目录")

    def _start_task(self, work, busy_text: str, mode: str, fallback=None) -> None:
        """在后台跑一次解析。

        :param work: 接收一个 :class:`Resolver`、返回结果的函数。**不得触碰界面。**
        :param mode: 见 ``MODE_*``，决定结果回来之后做什么
        :param fallback: 解析失败时改跑的动作。只为「下载」准备 —— 解析不出来
            只该影响查重和歌词，不该把整个下载拦下来，ncmctl 自己未必解析不了
            那个目标。
        """
        if self._task is not None and self._task.is_running:
            self.toast("上一次解析还没结束，请稍候")
            return

        from ..workers import BackgroundTask

        resolver = Resolver(self.window.binary, opts=self.opts())
        task = BackgroundTask(lambda: work(resolver), parent=self)
        resolver.progress = task.progress.emit

        self._task = task
        self._plan_mode = mode
        self._fallback = fallback
        task.progress.connect(self._on_progress)
        task.done.connect(self._on_plan_ready)
        task.failed.connect(self._on_task_failed)

        self.report_label.setText(busy_text)
        self._sync_buttons()
        task.start()

    def _on_progress(self, message: str) -> None:
        self.report_label.setText(message)

    def _on_task_failed(self, message: str) -> None:
        mode, self._plan_mode = self._plan_mode, ""
        fallback, self._fallback = self._fallback, None
        self._sync_buttons()
        self.report_label.setText("解析失败，未做任何下载。")

        if mode == MODE_DOWNLOAD and fallback is not None:
            # 走到这里说明是「关掉查重 + 要抓歌词」那一档：解析只是为了拿歌词和
            # 文件名，没有它照样能下载。ncmctl 自己未必解析不了这个目标，所以
            # 退回去直接下，只是这一轮没有歌词。
            self.warn(
                f"{message}\n\n将跳过查重、歌词与封面，把目标直接交给 ncmctl 下载。",
                "解析失败",
            )
            fallback()
            return

        self.warn(message, "解析失败")

    def _on_plan_ready(self, plan: object) -> None:
        mode, self._plan_mode = self._plan_mode, ""
        self._sync_buttons()

        if not isinstance(plan, Plan):
            return

        if mode == MODE_LIKED and plan.sources:
            # 把解析出来的歌单链接回填到目标框，让用户看得见到底下了什么。
            # 这一步会触发 textChanged → 作废计划，所以计划要等它之后再存。
            self.targets.setPlainText("\n".join(plan.sources))
            self._update_count_only()

        self._plan = plan
        self._show_plan(plan)

        if mode == MODE_DOWNLOAD:
            # 计划里存着产生它的原始目标，关掉查重时下载仍旧用它们（见 _execute_plan）
            self._execute_plan(plan, list(plan.sources))
        elif mode == MODE_LIKED:
            self._execute_plan(plan)
        elif plan.warnings:
            self.toast(f"有 {len(plan.warnings)} 个目标没能解析，展开明细可以看到")

    def _update_count_only(self) -> None:
        """回填目标框时刷新计数，但不作废刚拿到的计划。"""
        count = len(ncmctl.parse_targets(self.targets.toPlainText()))
        self.target_count.setText(f"已识别 {count} 个目标")

    def _show_plan(self, plan: Plan) -> None:
        self.report_label.setText(plan.report())
        self.report_view.clear_log()
        for line in plan.details().splitlines():
            self.report_view.append_line(line, classify(line))
        self.details_button.setVisible(bool(plan.present or plan.warnings))

    # ------------------------------------------------------------------ 下载

    def _plan_is_fresh(self, targets: list[str]) -> bool:
        return isinstance(self._plan, Plan) and self._plan.matches(
            targets, self._output_dir()
        )

    def _preview(self) -> None:
        targets = ncmctl.parse_targets(self.targets.toPlainText())
        if not targets:
            self.warn("请至少填写一个歌曲 ID 或链接。")
            return
        if not self.require_login("查重"):
            return
        if not self._check_output():
            return
        self.save_config()
        output = self._output_dir()
        self._start_task(
            lambda r: r.plan(targets, output),
            "正在解析目标并对照输出目录查重…",
            MODE_PREVIEW,
        )

    def _start(self) -> None:
        targets = ncmctl.parse_targets(self.targets.toPlainText())
        if not targets:
            self.warn("请至少填写一个歌曲 ID 或链接。")
            return
        if not self.require_login("下载"):
            return
        if not self._check_output():
            return

        if self.level.currentData() == "hires":
            QMessageBox.information(
                self,
                "关于 Hi-Res",
                "Hi-Res 只有部分曲目提供。若长时间没有进展，"
                "可以改用「无损 FLAC」，或打开「严格音质」之外的降级行为。",
            )

        self.save_config()

        checking = self.skip_existing.isChecked()
        wants_lyric = combo_data(self.lyric, lyrics.MODE_LRC) != lyrics.MODE_NONE
        wants_cover = combo_data(self.cover, covers.MODE_FILE) != covers.MODE_NONE

        # 既不查重、也不要歌词和封面：直接把原始目标交给 ncmctl，一个接口请求都
        # 不用发，行为与以前完全一致
        if not checking and not wants_lyric and not wants_cover:
            self._submit_download(targets, self._title_for(targets))
            return

        # 「仅查重」刚跑过且条件没变，直接复用结果
        if self._plan_is_fresh(targets):
            self._show_plan(self._plan)
            self._execute_plan(self._plan, targets)
            return

        output = self._output_dir()
        if checking:
            busy = "正在解析目标并对照输出目录查重…"
            fallback = None
        else:
            # 这一趟解析纯粹是为了拿歌曲 ID / 封面地址，好在下完后补歌词和封面，
            # 不参与下载决策
            busy = "正在解析目标（为了取歌词与封面）…"
            fallback = lambda: self._submit_download(targets, self._title_for(targets))

        self._start_task(
            lambda r: r.plan(targets, output),
            busy,
            MODE_DOWNLOAD,
            fallback=fallback,
        )

    def _download_liked(self) -> None:
        if not self.require_login("下载我喜欢的音乐"):
            return
        if not self._check_output():
            return
        self.save_config()
        output = self._output_dir()

        def work(resolver: Resolver) -> Plan:
            playlist_id, name = resolver.liked_playlist()
            url = PLAYLIST_URL.format(playlist_id)
            resolver.notify(f"「{name}」正在读取曲目列表…")
            plan = resolver.plan([url], output)
            plan.label = name
            return plan

        self._start_task(work, "正在读取「我喜欢的音乐」…", MODE_LIKED)

    def _execute_plan(self, plan: Plan, raw_targets: list[str] | None = None) -> None:
        """按计划开跑。

        :param raw_targets: 产生这份计划的原始目标（链接 / ID）。关掉查重时用它，
            这样交给 ncmctl 的 argv 与「直接用 ncmctl」完全一致 —— 这一条是对外
            承诺过的。拿不到原始目标时（「我喜欢的音乐」是先解析才知道歌单 ID 的）
            退回按歌曲 ID 下载，也就是一直以来的行为。
        """
        if self.skip_existing.isChecked():
            songs = plan.missing
            targets = [str(song.id) for song in songs]
        else:
            songs = plan.songs
            targets = list(raw_targets or []) or [str(song.id) for song in songs]

        label = f"「{plan.label}」" if plan.label else ""
        if not songs:
            self.toast(f"{label}{plan.total} 首歌曲都已存在于输出目录，无需下载")
            return

        title = f"下载{label} {len(songs)} 首" if label else f"下载 {len(songs)} 首"
        self._submit_download(
            targets,
            title,
            intro=self._intro_for(plan, songs),
            songs=songs,
        )

    @staticmethod
    def _intro_for(plan: Plan, songs: list[Song]) -> list[str]:
        """开跑前在控制台里交代一句：这批里哪些是要会员的。

        ncmctl 的下载日志只有进度条和一句 report，从不提付费情况 —— 而拉不到
        音质的往往正是这几首。所以把清单摆在日志最前面，出错时好对照。
        """
        paid = [song for song in songs if song.is_paid]
        if not paid:
            return []

        if plan.vip is True:
            tail = "当前账号是会员，通常能下"
        elif plan.vip is False:
            tail = "而当前账号不是会员，这些多半会失败"
        else:
            tail = "下载失败多半是它们"

        lines = [
            f"── 本批 {len(songs)} 首里有 {len(paid)} 首需要会员或购买，{tail} ──"
        ]
        limit = 20
        lines.extend(f"   {song.marked}" for song in paid[:limit])
        if len(paid) > limit:
            lines.append(f"   … 还有 {len(paid) - limit} 首")
        return lines

    def _submit_download(
        self,
        targets: list[str],
        title: str,
        intro: list[str] | None = None,
        songs: list[Song] | None = None,
    ) -> None:
        """提交下载作业。

        :param songs: 这批目标对应的歌曲。给了它就能顺带装上付费标注 ——
            ncmctl 的进度行会被改写成 ``[VIP]歌手 - 歌名  [进度条]`` 并标黄，
            让人一眼看出哪几首可能拉不到音质；同时也能在下载结束后接着抓歌词
            （歌词要靠 ``song.base`` 才知道该写到哪个文件上）。跳过查重时拿不到，
            就两样都不做。
        """
        argv = ncmctl.build_download(
            targets,
            level=combo_data(self.level, "lossless"),
            output=self._output_dir(),
            parallel=self.parallel.value(),
            strict=self.strict.isChecked(),
            opts=self.opts(),
        )
        self.submit(
            title,
            argv,
            intro=intro,
            tagger=FeeTagger(songs) if songs else None,
            on_finish=self._post_hook(songs),
        )

    def _post_hook(self, songs: list[Song] | None):
        """造一个「下载结束后补封面和歌词」的回调；都不需要时返回 ``None``。"""
        lyric_mode = combo_data(self.lyric, lyrics.MODE_LRC)
        cover_mode = combo_data(self.cover, covers.MODE_FILE)
        if not songs or (lyric_mode == lyrics.MODE_NONE and cover_mode == covers.MODE_NONE):
            return None

        # 封面地址在解析结果里，顺手升到 600×600；歌词要的是歌曲 ID
        cover_pairs = [
            (song.base, images.upgrade_url(song.pic_url)) for song in songs
        ]
        lyric_pairs = [(song.base, song.id) for song in songs]
        output = self._output_dir()

        def hook(panel, code: int) -> None:
            if code != 0:
                _say(panel, f"下载没有正常结束（退出码 {code}），跳过封面与歌词")
                return

            def then_lyrics() -> None:
                if lyric_mode != lyrics.MODE_NONE:
                    self._start_lyrics(panel, lyric_pairs, output, lyric_mode)

            # 先封面后歌词：两件事都会往同一个面板里写进度，串行起来看得清
            self._start_covers(panel, cover_pairs, output, cover_mode, then_lyrics)

        return hook

    def _start_covers(
        self,
        panel,
        pairs: list[tuple[str, str]],
        output: str,
        mode: str,
        then: Callable[[], None],
    ) -> None:
        """下载结束后补一轮封面，做完再接着跑 ``then``（歌词）。"""
        if mode == covers.MODE_NONE:
            then()
            return

        from ..workers import BackgroundTask

        signals = _LyricSignals(self)

        _say(panel, "")
        _say(panel, f"── 抓取封面：{len(pairs)} 首 · {covers.mode_label(mode)} ──")
        signals.progress.connect(lambda text, p=panel: _say(p, text, progress=True))
        signals.note.connect(lambda text, p=panel: _say(p, text))

        def work() -> covers.Report:
            return covers.collect(
                pairs,
                images.fetch,
                output,
                mode,
                parallel=LYRIC_PARALLEL,
                on_progress=lambda done, total, base: signals.progress.emit(
                    f"封面 {done}/{total} · {base}"
                ),
                on_note=signals.note.emit,
                should_stop=task.cancel_event.is_set,
            )

        # critical：关窗时最多等它 5 秒，别让标签写到一半就退出
        task = BackgroundTask(work, parent=self, critical=True)
        self._cover_task = task
        task.done.connect(lambda report, p=panel: self._on_covers_done(p, report, then))
        task.failed.connect(lambda message, p=panel: self._on_covers_failed(p, message, then))
        task.start()

    def _on_covers_done(self, panel, report: object, then: Callable[[], None]) -> None:
        self._cover_task = None
        if isinstance(report, covers.Report):
            _say(panel, "")
            _say(panel, f"── 封面：{report.summary()} ──")
            if report.failed or report.missing:
                self.toast(f"封面：{report.summary()}")
        then()

    def _on_covers_failed(self, panel, message: str, then: Callable[[], None]) -> None:
        self._cover_task = None
        # 封面和歌词一样是锦上添花：音频已经下好了，失败不该让人以为下载出了问题
        _say(panel, f"封面下载失败：{message}（音频文件不受影响）")
        then()

    def _start_lyrics(self, panel, pairs: list[tuple[str, int]], output: str, mode: str) -> None:
        """下载结束后补一轮歌词。

        跑在后台线程里：一首歌一个 ``ncmctl curl``，几百首的歌单就是一长串请求。
        输出直接写在**下载作业那个面板**里，紧接在 ``── 进程结束 ──`` 之后 ——
        这样「下了哪些、歌词跟上了没有」在同一处看得到，不用再去翻第二个标签页。
        """
        from ..workers import BackgroundTask

        binary = self.window.binary
        opts = self.opts()
        signals = _LyricSignals(self)

        _say(panel, "")
        _say(panel, f"── 抓取歌词：{len(pairs)} 首 · {lyrics.mode_label(mode)} ──")
        signals.progress.connect(lambda text, p=panel: _say(p, text, progress=True))
        signals.note.connect(lambda text, p=panel: _say(p, text))

        def work() -> lyrics.Report:
            resolver = Resolver(binary, opts=opts, timeout=LYRIC_TIMEOUT)
            return lyrics.collect(
                pairs,
                resolver.lyric,
                output,
                mode,
                parallel=LYRIC_PARALLEL,
                on_progress=lambda done, total, base: signals.progress.emit(
                    f"歌词 {done}/{total} · {base}"
                ),
                on_note=signals.note.emit,
                should_stop=task.cancel_event.is_set,
            )

        task = BackgroundTask(work, parent=self, critical=True)
        self._lyric_task = task
        task.done.connect(lambda report, p=panel: self._on_lyrics_done(p, report))
        task.failed.connect(lambda message, p=panel: self._on_lyrics_failed(p, message))
        task.start()

    def _on_lyrics_done(self, panel, report: object) -> None:
        self._lyric_task = None
        if not isinstance(report, lyrics.Report):
            return
        _say(panel, "")
        _say(panel, f"── 歌词：{report.summary()} ──")
        if report.failed or report.missing:
            self.toast(f"歌词：{report.summary()}")

    def _on_lyrics_failed(self, panel, message: str) -> None:
        self._lyric_task = None
        # 歌词是锦上添花：音频已经下好了，这一步失败不该让人以为下载出了问题
        _say(panel, f"歌词抓取失败：{message}（音频文件不受影响）")
        self.toast("歌词抓取失败，音频文件不受影响")

    @staticmethod
    def _title_for(targets: list[str]) -> str:
        return f"下载 {targets[0]}" if len(targets) == 1 else f"下载 {len(targets)} 个目标"

    # ------------------------------------------------------------------ 配置

    def save_config(self) -> None:
        self.cfg.set("download.level", combo_data(self.level, "lossless"))
        self.cfg.set("download.strict", self.strict.isChecked())
        self.cfg.set("download.skip_existing", self.skip_existing.isChecked())
        self.cfg.set("download.lyric", combo_data(self.lyric, lyrics.MODE_LRC))
        self.cfg.set("download.cover", combo_data(self.cover, covers.MODE_FILE))
        self.cfg.set("download.parallel", self.parallel.value())
        self.cfg.set("download.output", self.output.path())
