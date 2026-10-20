#!/usr/bin/env python3
"""NCM Studio 启动入口。

用法::

    .venv/bin/python main.py                     # 正常启动
    .venv/bin/python main.py --self-test         # 无头冒烟测试，不打开窗口
    .venv/bin/python main.py --screenshot 目录   # 把每个页面渲染成 PNG 后退出

``--self-test`` 会构建整个界面、逐个页面实例化、并让控制台跑一遍输入输出逻辑，
用于在无法开窗的环境（CI、SSH）里确认没有导入/布局错误。

``--screenshot`` 走 Qt 自己的渲染，不依赖 X11/Wayland 抓屏工具，
因此在 Wayland 会话或 offscreen 平台下同样可用。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 允许直接 `python main.py` 而不必先安装成包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import QLockFile, QTimer, Qt  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox  # noqa: E402

from ncmgui import APP_ID, APP_NAME, VERSION, ensure_utf8_stdout, theme  # noqa: E402
from ncmgui import updater  # noqa: E402
from ncmgui.config import Config, config_dir, default_music_dir  # noqa: E402
from ncmgui.lyrics import TimedLine, Word  # noqa: E402
from ncmgui.resources import ICON_PATH  # noqa: E402
from ncmgui.window import MainWindow  # noqa: E402


def _icon_ink(icon: QIcon) -> QColor | None:
    """取图标里最实的一个像素，用来判断它是按哪个主题画的。"""
    image = icon.pixmap(40, 40, QIcon.Mode.Normal, QIcon.State.Off).toImage()
    best = None
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() == 0:
                continue
            if best is None or color.alpha() > best.alpha():
                best = color
    return best


def _self_test(window: MainWindow) -> int:
    """构建完成后做一遍结构性检查。"""
    from ncmgui.widgets.console import ConsolePanel, ConsoleView

    # 自检只要结构，不该联网。窗口建好后会在事件循环第一拍加载发现页；
    # 这里先关掉，免得自检里抛出网络相关的后台任务错误。
    window.defer_page_loads = True

    problems: list[str] = []

    expected = {
        "discover",
        "search",
        "liked",
        "favorites",
        "local",
        "history",
        "stats",
        "download",
        "ncm",
        "cloud",
        "streaming",
        "tasks",
        "proxy",
        "tools",
        "account",
        "settings",
    }
    missing = expected - set(window.pages)
    if missing:
        problems.append(f"缺少页面：{sorted(missing)}")

    for key, page in window.pages.items():
        if page.content.count() == 0:
            problems.append(f"页面 {key} 没有任何内容")

    # 侧边栏只放 NAV_ITEMS；合并页的子页与搜索页都要映射到某个按钮，
    # 且必须能通过 show_page 找到 stack 位置
    from ncmgui.window import EXTRA_PAGES, NAV_ITEMS, PAGE_NAV

    nav_keys = [item[0] for item in NAV_ITEMS]
    if list(window._nav_buttons) != nav_keys:
        problems.append("侧边栏按钮与 NAV_ITEMS 不一致")
    for key in window.pages:
        if PAGE_NAV.get(key, key) not in window._nav_buttons:
            problems.append(f"页面 {key} 没有对应的侧边栏入口")
        if key not in window._nav_index:
            problems.append(f"页面 {key} 没有 stack 索引")
    for key, *_ in EXTRA_PAGES:
        if key in window._nav_buttons:
            problems.append(f"不在侧边栏的页面 {key} 混进了导航按钮")

    # 登出按钮的可用状态必须和登录状态一致。用户实际撞见过反例：状态写着「未登录」，
    # 旁边却亮着红色的「登出」。断言的是两者**永远相等**，所以这句在本机已登录、
    # CI 上未登录时都成立，不依赖具体环境
    account = window.pages.get("account")
    button = getattr(account, "logout_btn", None)
    if button is None:
        problems.append("账号页没有登出按钮（改名了？自检要跟着改）")
    elif button.isEnabled() != window.is_logged_in():
        problems.append(
            "登出按钮的可用状态与登录状态不一致"
            f"（按钮 {'可用' if button.isEnabled() else '禁用'}，"
            f"当前 {'已登录' if window.is_logged_in() else '未登录'}）"
        )

    # 侧边栏账号卡要能显示头像/昵称；账号页也要有同一份资料卡
    profile_card = getattr(window, "profile_card", None)
    if profile_card is None:
        problems.append("侧边栏没有账号卡")
    else:
        if profile_card._logged_in != window.is_logged_in():
            problems.append("账号卡的登录状态与窗口不一致")
        if profile_card.avatar.pixmap() is None or profile_card.avatar.pixmap().isNull():
            problems.append("账号卡没有头像占位图")
    account_card = getattr(account, "profile_card", None)
    if account_card is None:
        problems.append("账号页没有资料卡")

    # 发现页顶部现在是搜索入口与快捷块；搜索页不再占侧边栏
    discover = window.pages.get("discover")
    if discover is not None:
        if not getattr(discover, "quick_search", None):
            problems.append("发现页没有搜索框")
        if not getattr(discover, "greeting", None):
            problems.append("发现页没有问候语")

    # 红心歌单页在启动时**不应该**联网、也不应该拉起播放器 —— 用户可能只是想
    # 下载几首歌，没必要为一个没打开过的页面付出 20 秒的接口等待
    liked = window.pages.get("liked")
    if liked is not None:
        table = getattr(liked, "table", None)
        model = table.model() if table is not None else None
        if model is None:
            problems.append("红心歌单页没有表格模型")
        else:
            headers = [model.headerData(i, Qt.Orientation.Horizontal) for i in range(model.columnCount())]
            if len(headers) != 6:
                problems.append(f"红心歌单表格应为 6 列，实际 {len(headers)} 列：{headers}")
            elif headers[:3] != ["#", "歌名", "歌手"]:
                problems.append(f"红心歌单表头不对：{headers}")
        if getattr(liked, "_player", None) is not None:
            problems.append("自检时不该启动播放器")

    # 本地曲库页不联网，表格列数与目录配置都是纯逻辑
    local_page = window.pages.get("local")
    if local_page is not None:
        model = getattr(local_page, "model", None)
        if model is None:
            problems.append("本地曲库页没有歌曲表格模型")
        elif model.columnCount() != 6:
            problems.append(f"本地曲库表格应为 6 列，实际 {model.columnCount()} 列")
        else:
            headers = [
                model.headerData(i, Qt.Orientation.Horizontal)
                for i in range(model.columnCount())
            ]
            if headers != ["#", "歌名", "歌手", "专辑", "时长", "音质"]:
                problems.append(f"本地曲库表头不对：{headers}")
        dirs = getattr(local_page, "_dirs", None)
        if not dirs:
            problems.append("本地曲库页没有默认目录")
        if getattr(local_page, "_scan_task", None) is not None:
            problems.append("自检时不该启动扫描")
        if window.library.query_one("SELECT COUNT(*) AS n FROM tracks") is None:
            problems.append("本地曲库数据库不可用")

    # 播放历史与统计都只读本地记录，自检时可以直接加载
    history_page = window.pages.get("history")
    if history_page is not None:
        table = getattr(history_page, "table", None)
        if table is None:
            problems.append("播放历史页没有表格")
        elif table.columnCount() != 7:
            problems.append(f"播放历史表格应为 7 列，实际 {table.columnCount()} 列")
        else:
            headers = [table.horizontalHeaderItem(i).text() for i in range(7)]
            if headers != ["时间", "歌名", "歌手", "专辑", "时长", "来源", "状态"]:
                problems.append(f"播放历史表头不对：{headers}")
        history_page.on_show()

    stats_page = window.pages.get("stats")
    if stats_page is not None:
        stats_page.on_show()
        data = getattr(stats_page, "_data", None)
        if data is None or len(getattr(data, "days", ())) != 90:
            problems.append("听歌统计没有生成 90 天窗口")
        if not getattr(stats_page, "_rank_tables", None):
            problems.append("听歌统计缺少排行表格")

    # 播放工具窗：能力探测与控件范围都不能依赖真实播放
    from ncmgui.dialogs.playback import PlaybackToolsDialog

    try:
        tools = PlaybackToolsDialog(window)
        if tools.tabs.count() != 3:
            problems.append(f"播放工具窗应有 3 个标签页，实际 {tools.tabs.count()}")
        if (tools.speed_slider.minimum(), tools.speed_slider.maximum()) != (50, 200):
            problems.append(
                "倍速滑杆范围应为 50–200，实际 "
                f"{tools.speed_slider.minimum()}–{tools.speed_slider.maximum()}"
            )
        if len(tools.eq_sliders) != 10:
            problems.append(f"均衡器应有 10 段，实际 {len(tools.eq_sliders)}")
        tools._sync_speed(1.25)
        if tools.speed_value.text() != "1.25×":
            problems.append(f"倍速标签不对：{tools.speed_value.text()}")
        tools._sync_ab(3.0, 5.0)
        if "00:03" not in tools.ab_label.text() or "00:05" not in tools.ab_label.text():
            problems.append(f"A-B 标签不对：{tools.ab_label.text()}")
        tools._sync_sleep(90, False)
        if "01:30" not in tools.sleep_label.text():
            problems.append(f"定时关闭倒计时不对：{tools.sleep_label.text()}")
        tools.close()
    except Exception as exc:  # noqa: BLE001 - 自检只收集问题，不在这里中断
        problems.append(f"播放工具窗构造失败：{exc}")

    queue_view = getattr(getattr(window, "player_page", None), "queue_view", None)
    if queue_view is None:
        problems.append("播放页没有队列视图")
    elif not queue_view.dragEnabled() or not queue_view.acceptDrops():
        problems.append("队列视图没有启用拖拽排序")

    # 流媒体：配置为空时页面也要能打开（只是禁用列表），适配器与歌曲映射
    # 是纯逻辑，不需要真的连服务器
    streaming_page = window.pages.get("streaming")
    registry = getattr(window, "streaming", None)
    if streaming_page is None or registry is None:
        problems.append("缺少流媒体页面或注册表")
    else:
        from ncmgui.streaming.registry import song_id_for, split_stream_id

        if streaming_page.tabs.count() != 4:
            problems.append("流媒体页应当有歌曲/专辑/歌手/歌单四个 Tab")
        demo_id = song_id_for("demo", "t1")
        if demo_id >= 0 or demo_id != song_id_for("demo", "t1"):
            problems.append("流媒体歌曲 ID 应当稳定且为负")
        if split_stream_id("demo:t1") != ("demo", "t1"):
            problems.append("流媒体歌曲标识拆分不正确")

    # 桌面歌词窗：置顶/透明这些窗口标志无法在离屏环境验证，但至少确认
    # 绘制路径、翻译与时间轴定位不抛异常
    desktop = getattr(window, "desktop_lyrics", None)
    if desktop is None:
        problems.append("主窗口没有桌面歌词窗")
    else:
        demo = [
            TimedLine(start=0.0, end=2.0, text="桌面歌词第一句"),
            TimedLine(start=2.0, end=4.0, text="桌面歌词第二句"),
        ]
        desktop.set_demo(demo, 2.5)
        if desktop.current_line() is None or desktop.current_line().text != "桌面歌词第二句":
            problems.append("桌面歌词没有按位置定位当前行")
        if desktop.grab().isNull():
            problems.append("桌面歌词窗渲染失败")
        desktop.hide()

    # 歌词服务与播放页共用同一份偏移：正偏移必须让歌词晚出现
    service = getattr(window, "lyrics", None)
    if service is None:
        problems.append("主窗口没有歌词服务")
    elif abs(service.display_position(10.0) - 10.0) > 0.001:
        problems.append("默认歌词偏移不该改变播放位置")
    elif not hasattr(window, "toggle_desktop_lyrics"):
        problems.append("主窗口没有桌面歌词开关")

    # 播放页不是导航页：它在栈里，但启动时不该是当前页，也不该拉起 mpv
    if getattr(window, "player_page", None) is None:
        problems.append("主窗口没有播放页")
    elif window.stack.currentWidget() is window.player_page:
        problems.append("启动时不该停在播放页")
    playback = getattr(window, "playback", None)
    if playback is None:
        problems.append("主窗口没有播放控制器")
    elif playback.player_running:
        problems.append("自检时不该启动 mpv")
    else:
        # 播放顺序的四种取值都是纯逻辑，离线可验
        for mode in ("order", "loop", "one", "shuffle"):
            playback.set_mode(mode)
            if playback.mode != mode:
                problems.append(f"播放顺序切换到 {mode} 没有生效")
        playback.set_mode("loop")

    # 自绘图标必须跟着主题重画。用户撞见过暗色启动切到亮色后，上一首、下一首、
    # 播放顺序、播放列表仍留着旧主题的近白色，落在浅底上几乎看不见 —— 只有
    # 播放/暂停和音量因为状态一变就重画，看起来还是好的
    bar = getattr(window, "playback_bar", None)
    app = QApplication.instance()
    if bar is None or app is None:
        problems.append("主窗口没有播放条，主题图标自检无法进行")
    else:
        original = "dark" if theme.is_dark() else "light"
        try:
            for target in ("dark", "light"):
                theme.apply_theme(app, target)
                window._refresh_theme_dependents()
                for name, button in (
                    ("上一首", bar.prev_btn),
                    ("下一首", bar.next_btn),
                    ("播放顺序", bar.mode_btn),
                    ("播放列表", bar.queue_btn),
                ):
                    ink = _icon_ink(button.icon())
                    expect = QColor(theme.TEXT)
                    if ink is None or max(
                        abs(ink.red() - expect.red()),
                        abs(ink.green() - expect.green()),
                        abs(ink.blue() - expect.blue()),
                    ) > 10:
                        shown = ink.name() if ink is not None else "空"
                        problems.append(
                            f"{target} 主题下{name}图标没有按 theme.TEXT 重画"
                            f"（实际 {shown}，期望 {expect.name()}）"
                        )
        finally:
            theme.apply_theme(app, original)
            window._refresh_theme_dependents()

    # 切歌后歌词必须回到顶部。用户撞见过上一首的滚动位置被新歌词继承，
    # 新歌前奏期间歌词停在中间；上一首没跑完的滚动动画也会把位置继续往里带
    player_page = getattr(window, "player_page", None)
    if player_page is None:
        problems.append("主窗口没有播放页，歌词滚动自检无法进行")
    else:
        demo_lines = [
            TimedLine(start=30.0 + i * 3, end=33.0 + i * 3, text=f"第 {i} 句")
            for i in range(20)
        ]
        page = player_page
        bar = page.lyric_scroll.verticalScrollBar()
        page._build_lyrics(demo_lines)
        QApplication.processEvents()
        # 歌词区在自检时没有真实滚动范围，手动给一个，专门验「重建时归零」
        bar.setRange(0, 1000)
        bar.setValue(500)
        page._clear_lyrics("正在加载歌词…")
        if bar.value() != bar.minimum():
            problems.append(
                f"清空歌词后滚动条没有回到顶部（实际 {bar.value()}）"
            )

        page._build_lyrics(demo_lines)
        QApplication.processEvents()
        bar.setRange(0, 1000)
        bar.setValue(500)
        page._current_line = 5
        page._update_current_line(0.0)
        if bar.value() != bar.minimum():
            problems.append(
                f"位置回到第一句之前时歌词没有回顶（实际 {bar.value()}）"
            )
        page._clear_lyrics()

    # 当前行换行成两个视觉行时，幕布只扫到第一行，第二行没唱到的字必须还是
    # 浅色。旧实现只对高亮区域做 DestinationIn，没被画到的第二行保持深色，
    # 于是窗口一窄、歌词一换行，未播放的部分整块变深
    try:
        from ncmgui.widgets.lyricline import LyricLineWidget, PAD_Y

        text = "Why not something like your old man"
        words = []
        acc = ""
        for index, word in enumerate(text.split(" ")):
            token = word if index == 0 else " " + word
            start = len(acc)
            acc += token
            words.append(
                Word(
                    start=index * 0.5,
                    end=(index + 1) * 0.5,
                    text=token,
                    char_start=start,
                    char_end=len(acc),
                )
            )
        wrapper = LyricLineWidget(
            TimedLine(start=0.0, end=3.5, text=text, words=tuple(words))
        )
        wrapper.set_current(True)
        # 不真正弹窗，只让它完成排版和绘制
        wrapper.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        wrapper.resize(280, 120)
        wrapper.show()
        QApplication.processEvents()
        # 停在行首：不同平台的字体度量会让换行位置不同，但无论断在哪，
        # 第二视觉行都一定是「还没唱到」的部分
        wrapper.set_position(0.1)
        QApplication.processEvents()

        if wrapper._layout is None or wrapper._layout.lineCount() < 2:
            problems.append("歌词换行自检用例没有换行（字体度量变了？）")
        else:
            image = wrapper.grab().toImage()
            second = wrapper._layout.lineAt(1)
            dpr = image.devicePixelRatio()
            top = int((PAD_Y + second.y()) * dpr)
            bottom = int((PAD_Y + second.y() + second.height()) * dpr)
            best = None
            for y in range(max(0, top), min(image.height(), bottom)):
                for x in range(image.width()):
                    color = image.pixelColor(x, y)
                    if color.alpha() and (best is None or color.alpha() > best.alpha()):
                        best = color
            sung = QColor(theme.LYRIC_SUNG)
            unsung = QColor(theme.LYRIC_UNSUNG)

            def distance(color: QColor, ref: QColor) -> int:
                return (
                    (color.red() - ref.red()) ** 2
                    + (color.green() - ref.green()) ** 2
                    + (color.blue() - ref.blue()) ** 2
                )

            if best is None:
                problems.append("歌词换行自检取不到第二视觉行的像素")
            elif distance(best, unsung) >= distance(best, sung):
                problems.append(
                    f"换行后未播放的歌词用成了已播放的深色（实际 {best.name()}）"
                )
        wrapper.close()
        wrapper.deleteLater()
    except Exception as exc:  # noqa: BLE001 - 自检要报告而不是崩掉
        problems.append(f"歌词换行渲染自检失败：{exc!r}")

    # 分栏比例只该记住用户真正拖过的位置。用户撞见过长歌手名把歌词列撑宽、
    # 黑胶被布局压到最小宽度，QSplitter 为那次自动压缩发出的 splitterMoved
    # 被当成拖动结果存进了配置，下次启动黑胶就一直是窄的；换行反而被挤没了
    page = getattr(window, "player_page", None)
    if page is None:
        problems.append("主窗口没有播放页，分栏比例自检无法进行")
    else:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_cfg = Config(Path(temp_dir) / "config.json")
            temp_cfg.set("player.split", [0.3044, 0.6956])
            temp_cfg.set("player.split_rev", 0)
            original_cfg = page.cfg
            original_applied = page._split_applied
            original_dirty = page._split_dirty
            try:
                page.cfg = temp_cfg
                page._split_applied = False
                page._restore_split()
                if temp_cfg.get("player.split"):
                    problems.append("旧版本被布局压出来的分栏比例没有被丢弃")
                # 没有按住左键：布局自己压缩时发出的 splitterMoved 不算拖动
                page._split_dirty = False
                page._on_split_moved(0, 1)
                if page._split_dirty:
                    problems.append("布局自动压缩被当成了用户拖动分栏")
            finally:
                page._split_timer.stop()
                page.cfg = original_cfg
                page._split_applied = original_applied
                page._split_dirty = original_dirty

    # 右上角的音质必须写**实际**播放的音质。用户撞见过「标签写着极高 320 kbps、
    # 实际播的是无损文件」：本地文件按文件格式显示，流媒体按直链接口返回的
    # 档位（可能降级）显示，请求档位只在取链结果回来之前兜底
    from ncmgui.pages.player import quality_text
    from ncmgui.resolver import Song

    quality_cases = [
        (
            "本地文件用文件音质",
            quality_text(
                "",
                Song(id=1, name="A", path="/x.flac", quality="无损 44.1kHz/16bit"),
                "exhigh",
            ),
            "无损 44.1kHz/16bit",
        ),
        (
            "流媒体用接口返回的档位",
            quality_text("lossless", Song(id=2, name="B"), "exhigh"),
            "无损 FLAC",
        ),
        (
            "取链未回时退回请求档位",
            quality_text("", Song(id=3, name="C"), "exhigh"),
            "极高 320 kbps",
        ),
        (
            "本地文件读不出格式时不冒充流媒体档位",
            quality_text("", Song(id=4, name="D", path="/x.flac"), "exhigh"),
            "未知",
        ),
        ("未播放", quality_text("", None, "exhigh"), "—"),
    ]
    for name, got, want in quality_cases:
        if got != want:
            problems.append(f"音质标签：{name} 应为 {want!r}，实际 {got!r}")

    # 红心表格第 0 列的 ▶/❚❚ 只由模型状态决定，和当前有没有 mpv 无关
    source = getattr(liked, "model", None)
    if source is not None:
        source.set_songs([Song(id=1, name="自检", artists=("测试",))])
        source.set_playing(1)
        source.set_paused(False)
        playing_marker = source.data(source.index(0, 0), Qt.ItemDataRole.DisplayRole)
        source.set_paused(True)
        paused_marker = source.data(source.index(0, 0), Qt.ItemDataRole.DisplayRole)
        if playing_marker != "❚❚":
            problems.append(f"播放中的行标记应为 ❚❚，实际 {playing_marker!r}")
        if paused_marker != "▶":
            problems.append(f"暂停时的行标记应为 ▶，实际 {paused_marker!r}")
        source.set_playing(None)
        source.set_paused(False)
        source.set_songs([])

    # 播放器 IPC 的编解码是纯 Python，离线验得到；mpv 二进制本身由 CI 另行冒烟
    try:
        from ncmgui import player as player_mod

        line = player_mod.encode_command(["loadfile", "https://example.com/a.mp3", "replace"])
        message = player_mod.parse_message(line.decode("utf-8"))
        if message is None or message.raw.get("command") is None:
            problems.append("播放器命令编码/解析往返失败")
        elif message.raw["command"][2] != "replace":
            problems.append("播放器命令参数在往返中被改动")
        event = player_mod.parse_message('{"event": "end-file", "reason": "eof"}')
        if event is None or event.kind != "event" or player_mod.end_reason(event) != "eof":
            problems.append("播放器 end-file 事件解析异常")
        if player_mod.format_clock(222) != "03:42":
            problems.append("播放器时长格式化异常（222 秒不是 03:42）")
    except Exception as exc:  # pragma: no cover - 自检路径
        problems.append(f"播放器模块自检失败：{exc!r}")

    # 控制台的着色与进度覆盖逻辑是最容易写错的部分，单独跑一遍
    try:
        view = ConsoleView()
        view.append_line("$ ncmctl download 1")
        view.append_line("开始下载", theme.CONSOLE_CYAN)
        for percent in ("10%", "45%", "80%"):
            view.append_progress(f"进度 {percent}")
        view.append_line("完成 100%")
        text = view.toPlainText()
        if "进度 10%" in text or "进度 45%" in text:
            problems.append("进度行没有被覆盖，仍在累积旧内容")
        if "进度 80%" not in text:
            problems.append("最后一次进度没有被保留")
        if "完成 100%" not in text:
            problems.append("正常行在进度行之后没有正确追加")

        panel = ConsolePanel("自检")
        panel.view.append_line("panel ok")

        # 敏感 stdin（短信验证码）不能出现在控制台正文与导出日志里。用假 runner
        # 走一遍真实的 _send_stdin 路径，不启动任何进程
        class _FakeRunner:
            is_running = True
            sent = ""

            def send_line(self, text: str) -> None:
                self.sent = text

        fake = _FakeRunner()
        panel.runner = fake  # type: ignore[assignment]
        panel.set_sensitive_input(True)
        if panel.stdin_bar.echoMode() != QLineEdit.EchoMode.Password:
            problems.append("敏感输入的输入框没有切换到密码回显")
        panel.stdin_bar.setText("654321")
        panel._send_stdin()
        if fake.sent != "654321":
            problems.append("敏感输入没有送达进程 stdin")
        if "654321" in panel.view.toPlainText():
            problems.append("敏感输入被回显到了控制台正文")
        if "654321" in "\n".join(panel._raw):
            problems.append("敏感输入被记进了导出日志")
        # 终端（Windows ConPTY 等）可能把输入回显出来，后续输出里也要抹掉
        panel._on_output("服务器回显 654321")
        if "654321" in panel.view.toPlainText():
            problems.append("终端回显的敏感输入没有被抹掉")
    except Exception as exc:  # pragma: no cover - 自检路径
        problems.append(f"控制台构造失败：{exc!r}")

    # 歌词的「写入音频标签」模式靠 mutagen，而 mutagen 是懒加载的（那个功能点到了
    # 才 import）。打包时少收一个子模块，模块清单上看不出来、构建也不报错，只有
    # 用户选到那个模式才会发现 —— 所以这里真写一次再读回来，而不是 import 一下
    # 就算完。歌词本身是纯文本处理，离线验得到，不需要联网。
    try:
        from ncmgui import lyrics

        with tempfile.TemporaryDirectory(prefix="ncmgui-selftest-") as tmp:
            flac = Path(tmp) / "自检.flac"
            flac.write_bytes(lyrics.blank_flac())
            lyrics.embed(flac, "[00:01.00]自检歌词\n")
            if lyrics.read_embedded(flac) != "[00:01.00]自检歌词\n":
                problems.append("歌词写进音频标签后读不回来（mutagen 可能没打全）")
            if not lyrics.has_content("[00:01.00]自检歌词"):
                problems.append("歌词内容判定异常：有歌词的被当成了没有歌词")
            if lyrics.has_content("[00:00.00]暂无歌词"):
                problems.append("歌词内容判定异常：占位串被当成了真歌词")
    except Exception as exc:  # pragma: no cover - 自检路径
        problems.append(f"歌词标签读写失败：{exc!r}")

    if problems:
        print("自检失败：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print(f"{APP_NAME} v{VERSION} 自检通过")
    print(f"  页面：{', '.join(window.pages)}")
    print(f"  ncmctl：{window.binary or '未找到'}")
    print(f"  登录状态：{'已登录' if window.is_logged_in() else '未登录'}")
    return 0


def _demo_cover(size: int = 420) -> QPixmap:
    """截图用的封面：渐变底 + 一个抽象的月亮/海浪，不依赖任何图片文件。"""
    pixmap = QPixmap(size, size)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0.0, QColor("#18284a"))
        gradient.setColorAt(0.55, QColor("#3a2a63"))
        gradient.setColorAt(1.0, QColor("#0b0d14"))
        painter.fillRect(pixmap.rect(), gradient)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 229, 255, 46))
        painter.drawEllipse(int(size * 0.18), int(size * 0.16), int(size * 0.5), int(size * 0.5))
        painter.setBrush(QColor(190, 170, 255, 60))
        painter.drawEllipse(int(size * 0.46), int(size * 0.1), int(size * 0.34), int(size * 0.34))

        font = QFont()
        font.setPixelSize(int(size * 0.11))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#f2f6ff"))
        painter.drawText(
            pixmap.rect().adjusted(0, 0, 0, -int(size * 0.16)),
            Qt.AlignmentFlag.AlignCenter,
            "夜航",
        )
    finally:
        painter.end()
    return pixmap


def _demo_songs():
    from ncmgui.resolver import Song

    titles = (
        ("夜航", "沈以诚", "夜航", 222, "无损 44.1kHz/16bit"),
        ("海边的房间", "房东的猫", "柔软", 245, "无损 48kHz/24bit"),
        ("雨停了以后", "刘昊霖", "十城记", 198, "极高 320 kbps"),
        ("晚风心里吹", "阿梨粤", "晚风", 231, "较高 192 kbps"),
        ("星河入梦", "银河系", "漫游", 267, "标准 128 kbps"),
        ("关于你", "陈壹千", "关于你", 214, "无损 44.1kHz/16bit"),
    )
    return [
        Song(
            id=9001 + index,
            name=name,
            artists=(artist,),
            album=album,
            duration=seconds * 1000,  # Song.duration 是毫秒
            pic_url="",
            # 本地音乐页的「音乐质量」列，以及播放页右上角的实际音质
            quality=quality,
        )
        for index, (name, artist, album, seconds, quality) in enumerate(titles)
    ]


def _demo_history():
    """截图用的播放历史，时间相对当前时刻，展示本地/在线两种来源。"""
    from datetime import datetime, timedelta

    from ncmgui.library import HistoryEntry

    now = datetime.now()
    entries = []
    for index, song in enumerate(_demo_songs()):
        started = now - timedelta(hours=index * 9 + 1)
        entries.append(HistoryEntry(
            id=9000 - index,
            track_id=song.id if index % 4 else None,
            title=song.name,
            artists=song.artists,
            album=song.album,
            path="" if index % 4 else f"/music/demo/{song.name}.flac",
            source="online" if index % 4 else "local",
            quality=song.quality,
            started_at=int(started.timestamp()),
            listened_ms=song.duration - (index * 13000),
            duration_ms=song.duration,
            completed=index % 3 != 2,
        ))
    return entries


def _demo_stats():
    """截图用的 90 天统计，形状稳定、不依赖真实播放记录。"""
    from datetime import date, timedelta

    from ncmgui.library import DayEntry, RankEntry, SliceEntry, StatsData

    today = date.today()
    days = []
    total_ms = 0
    total_plays = 0
    active = 0
    for offset in range(89, -1, -1):
        day = today - timedelta(days=offset)
        plays = (offset * 5 + day.weekday()) % 9
        if offset % 11 == 0:
            plays = 0
        if day.weekday() >= 5:
            plays = min(9, plays + 2)
        listened = plays * 210_000
        days.append(DayEntry(day=day, plays=plays, listened_ms=listened))
        total_plays += plays
        total_ms += listened
        active += 1 if plays else 0

    hours = tuple(
        2 + ((hour * 7) % 13) + (14 if hour in (8, 12, 21) else 0)
        for hour in range(24)
    )

    songs = tuple(
        RankEntry(song.name, ", ".join(song.artists), 38 - index * 5,
                  35 * 60_000 - index * 150_000)
        for index, song in enumerate(_demo_songs()[:5])
    )
    albums = tuple(
        RankEntry(song.album, ", ".join(song.artists), 30 - index * 4,
                  30 * 60_000 - index * 100_000)
        for index, song in enumerate(_demo_songs()[:5])
    )
    artists = tuple(
        RankEntry(song.artists[0], "", 46 - index * 6, 50 * 60_000 - index * 200_000)
        for index, song in enumerate(_demo_songs()[:5])
    )
    qualities = (
        SliceEntry("无损", 164),
        SliceEntry("极高 320 kbps", 121),
        SliceEntry("较高 192 kbps", 48),
        SliceEntry("标准 128 kbps", 17),
    )
    return StatsData(
        days=tuple(days),
        hours=hours,
        songs=songs,
        albums=albums,
        artists=artists,
        qualities=qualities,
        total_plays=total_plays,
        total_listened_ms=total_ms,
        active_days=active,
        current_streak=5,
        longest_streak=12,
    )


def _demo_lyrics() -> list[TimedLine]:
    """截图用的歌词。当前句带逐字时间，好让幕布正好停在「以」字中间。"""
    items = [
        (0.0, "夜航"),
        (12.0, "作词：沈以诚"),
        (18.0, "作曲：沈以诚"),
        (26.0, "城市的灯一盏一盏熄灭"),
        (34.5, "风把云吹向更远的海面"),
        (43.0, "我数着舷窗外的星"),
        (52.0, "像数着没说完的从前"),
        (61.5, "如果夜色可以慢一点"),
        (70.0, "让我再看一眼"),
        (78.5, "那片被月光照亮的岸"),
        (87.0, "和你转身时的侧脸"),
        (96.0, "夜航的船不说话"),
        (105.0, "只把思念推得很远"),
        (114.0, "等天亮之前"),
        (123.0, "我会回到你身边"),
        (132.0, "——"),
        (141.0, "风把云吹向更远的海面"),
        (150.0, "我数着舷窗外的星"),
    ]

    lines: list[TimedLine] = []
    for index, (start, text) in enumerate(items):
        end = items[index + 1][0] if index + 1 < len(items) else start + 5.0
        if text == "如果夜色可以慢一点":
            # 9 个字均分 8.5 秒；66.7 秒时幕布走到第 6 个字「以」的中间
            step = (end - start) / len(text)
            words = tuple(
                Word(start + i * step, start + (i + 1) * step, char, i, i + 1)
                for i, char in enumerate(text)
            )
            lines.append(TimedLine(start, end, text, words))
        else:
            lines.append(TimedLine(start, end, text))
    return lines


def _screenshot(window: MainWindow, target: str) -> int:
    """把每个页面渲染成 PNG。用于文档配图与界面回归检查。"""
    from ncmgui.window import NAV_ITEMS

    out_dir = Path(target).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 截图只是画界面，不该顺手去拉歌单，也不该扫描真实目录
    window.defer_page_loads = True
    # 配置里记着「上次最大化」时，resize() 对最大化窗口不生效，截出来会是
    # 虚拟屏幕的尺寸。先还原再定尺寸，文档配图才可复现
    if window.isMaximized():
        window.showNormal()
    window.resize(1180, 820)
    local = window.pages.get("local")
    if local is not None:
        local.set_demo(_demo_songs(), str(default_music_dir()))
    history = window.pages.get("history")
    if history is not None:
        history.set_demo(_demo_history())
    stats = window.pages.get("stats")
    if stats is not None:
        stats.set_demo(_demo_stats())
    streaming = window.pages.get("streaming")
    if streaming is not None:
        from dataclasses import replace

        from ncmgui.streaming import StreamAlbum, StreamArtist, StreamPlaylist

        demo_songs = [
            replace(song, source="subsonic", stream_id=f"demo:{index}")
            for index, song in enumerate(_demo_songs()[:8])
        ]
        streaming.set_demo(
            "家里的 Navidrome",
            demo_songs,
            [
                StreamAlbum("a1", "夜航", ("陈婧霏",), 2024),
                StreamAlbum("a2", "平凡的一天", ("毛不易",), 2018),
                StreamAlbum("a3", "冀西南林路行", ("万能青年旅店",), 2020),
            ],
            [StreamArtist("r1", "陈婧霏"), StreamArtist("r2", "毛不易"),
             StreamArtist("r3", "万能青年旅店")],
            [
                StreamPlaylist("p1", "夜晚开车", "me", 24),
                StreamPlaylist("p2", "最近常听", "me", 18),
            ],
        )
    for index, (key, _glyph, _label, _cls) in enumerate(NAV_ITEMS):
        window.show_page(key)
        # 让布局与样式表完成一轮计算，否则抓到的可能是空白
        QApplication.processEvents()
        image = window.grab()
        path = out_dir / f"{index + 1:02d}-{key}.png"
        if not image.save(str(path)):
            print(f"保存失败：{path}")
            return 1
        print(f"已保存 {path}  ({image.width()}x{image.height()})")

    # 播放页不是导航页，单独摆一个演示状态：不联网、不起 mpv
    songs = _demo_songs()
    cover = _demo_cover()
    page = window.player_page
    page.set_demo(songs[0], cover, _demo_lyrics(), position=66.7)
    window.playback_bar.set_demo(
        songs[0], position=66.7, duration=222.0, volume=70, mode="loop"
    )
    window.open_player_page(queue=False)
    QApplication.processEvents()
    image = window.grab()
    path = out_dir / "11-player.png"
    if not image.save(str(path)):
        print(f"保存失败：{path}")
        return 1
    print(f"已保存 {path}  ({image.width()}x{image.height()})")

    page.set_demo(
        songs[0],
        cover,
        _demo_lyrics(),
        queue=songs,
        position=66.7,
    )
    window.open_player_page(queue=True)
    for _ in range(3):
        QApplication.processEvents()
    image = window.grab()
    path = out_dir / "12-player-queue.png"
    if not image.save(str(path)):
        print(f"保存失败：{path}")
        return 1
    print(f"已保存 {path}  ({image.width()}x{image.height()})")

    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)

    self_test = "--self-test" in args
    screenshot_dir: str | None = None
    if "--screenshot" in args:
        position = args.index("--screenshot")
        if position + 1 >= len(args):
            print("--screenshot 需要一个输出目录参数")
            return 2
        screenshot_dir = args[position + 1]
        del args[position:position + 2]

    if self_test:
        args = [a for a in args if a != "--self-test"]

    # 必须在建 QApplication **之前**处理：这条命令的唯一用途就是在无显示环境
    # （CI、SSH）里证明「随包的 mpv 真的能跑起来」。若先建 QApplication，
    # 没有 X11/Wayland 时会直接失败，报的还是 Qt 的错误，看不出 mpv 的死活。
    if "--mpv-version" in args:
        ensure_utf8_stdout()
        from ncmgui import player as player_mod

        binary = player_mod.find_mpv()
        if not binary:
            print("未找到 mpv")
            return 1
        print(f"mpv：{binary}")
        ok, output = player_mod.probe_mpv(binary)
        print(output)
        return 0 if ok else 1

    if self_test or screenshot_dir:
        # 这两条路径都要往标准输出打中文。Windows 上输出被重定向时 Python 用的是
        # 本地编码（cp1252），`print("自检通过")` 直接 UnicodeEncodeError ——
        # 而且崩在第一句话上，等于一个字都没打出来。
        #
        # 后果比"少打几行日志"严重得多：产物是 GUI 子系统程序（spec 里
        # console=False），未捕获的异常会弹一个模态错误框等用户点确定，进程
        # 永远不返回。CI 里没人去点，只能干等到超时，看到的却是"自检运行中，
        # 没有任何输出"，真正的 traceback 早已写进没人读的错误流里。
        ensure_utf8_stdout()

        # 没有显示环境时也能跑
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    # 让分数缩放下字体保持锐利（尤其是 Wayland 下的混合 DPI）
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(args)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName(APP_ID)
    # 要和 .desktop 的文件名、以及里面的 StartupWMClass 三处一致，
    # 否则任务栏会把窗口当成另一个程序，用不上图标
    app.setDesktopFileName(APP_ID)

    # 窗口图标。源码运行时 resources.asset 指向的 ncmgui/assets/ 里没有东西
    # （那是打包时由 spec 放进去的），QIcon 拿到空路径会构造一个空图标 ——
    # 界面照常显示，只是用系统默认图标，不影响使用
    app.setWindowIcon(QIcon(str(ICON_PATH)))

    # 先读配置再应用主题：启动时就该是用户上次选的那一档，不能先闪一下暗色
    cfg = Config()
    theme.apply_theme(app, cfg.get("ui.theme", "dark"))

    # 同一份配置、曲库和播放器只允许一个实例使用：两个实例同时写 SQLite、
    # 写配置或下载同一批文件会互相覆盖。自检/截图可以并行，不参与抢锁。
    _instance_lock = None
    if not self_test and not screenshot_dir:
        _instance_lock = QLockFile(str(config_dir() / "ncm-studio.lock"))
        # 只按「持锁进程是否还活着」判断失效；程序被挂起再久也不抢锁
        _instance_lock.setStaleLockTime(0)
        if not _instance_lock.tryLock(100):
            QMessageBox.information(
                None,
                APP_NAME,
                "NCM Studio 已经在运行。\n\n"
                "同一时间只允许一个实例，避免配置、曲库和下载互相覆盖。",
            )
            return 0

    # 自动更新是原地替换内容，文件名不会自己变。用户文件名里如果带着版本号，
    # 这里把它对齐到当前版本，并同步改掉 .desktop 的 Exec —— 否则会出现
    # 「名字写着 3.0.0、里面其实是 3.0.3」，或者菜单项指着旧文件名。
    # 自检/截图模式不碰正在运行的文件。
    if not self_test and not screenshot_dir:
        updater.sync_appimage_name()

    window = MainWindow(cfg)
    if window.start_maximized:
        window.showMaximized()
    else:
        window.show()

    if self_test:
        return _self_test(window)
    if screenshot_dir:
        return _screenshot(window, screenshot_dir)

    # 蓝牙耳机与系统媒体键：向会话总线注册 MPRIS。自检/截图不需要它
    window.mpris.start()

    # 启动后稍等片刻再检查更新：先把主界面画出来，别让网络拖慢启动；
    # 发现新版会弹窗询问是否安装
    QTimer.singleShot(2500, window.check_updates_on_start)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
