#!/usr/bin/env python3
"""ncmctl GUI 启动入口。

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

from PyQt6.QtCore import QTimer, Qt  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ncmgui import APP_ID, APP_NAME, VERSION, ensure_utf8_stdout, theme  # noqa: E402
from ncmgui.config import Config  # noqa: E402
from ncmgui.resources import ICON_PATH  # noqa: E402
from ncmgui.window import MainWindow  # noqa: E402


def _self_test(window: MainWindow) -> int:
    """构建完成后做一遍结构性检查。"""
    from ncmgui.widgets.console import ConsolePanel, ConsoleView

    problems: list[str] = []

    expected = {
        "account",
        "liked",
        "download",
        "ncm",
        "cloud",
        "tasks",
        "proxy",
        "tools",
        "settings",
    }
    missing = expected - set(window.pages)
    if missing:
        problems.append(f"缺少页面：{sorted(missing)}")

    for key, page in window.pages.items():
        if page.content.count() == 0:
            problems.append(f"页面 {key} 没有任何内容")

    # 导航按钮数量应当和页面数量一致
    if len(window._nav_buttons) != len(window.pages):
        problems.append("侧边栏按钮与页面数量不一致")

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

    # 红心表格第 0 列的 ▶/⏸ 只由模型状态决定，和当前有没有 mpv 无关
    source = getattr(liked, "model", None)
    if source is not None:
        from ncmgui.resolver import Song

        source.set_songs([Song(id=1, name="自检", artists=("测试",))])
        source.set_playing(1)
        source.set_paused(False)
        playing_marker = source.data(source.index(0, 0), Qt.ItemDataRole.DisplayRole)
        source.set_paused(True)
        paused_marker = source.data(source.index(0, 0), Qt.ItemDataRole.DisplayRole)
        if playing_marker != theme.safe_glyph("⏸", "❚❚"):
            problems.append(f"播放中的行标记应为 ⏸，实际 {playing_marker!r}")
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
        view.append_line("开始下载", theme.CYAN)
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
        ("夜航", "沈以诚", "夜航", 222),
        ("海边的房间", "房东的猫", "柔软", 245),
        ("雨停了以后", "刘昊霖", "十城记", 198),
        ("晚风心里吹", "阿梨粤", "晚风", 231),
        ("星河入梦", "银河系", "漫游", 267),
        ("关于你", "陈壹千", "关于你", 214),
    )
    return [
        Song(
            id=9001 + index,
            name=name,
            artists=(artist,),
            album=album,
            duration=seconds * 1000,  # Song.duration 是毫秒
            pic_url="",
        )
        for index, (name, artist, album, seconds) in enumerate(titles)
    ]


def _demo_lyrics() -> list[tuple[float, str]]:
    return [
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


def _screenshot(window: MainWindow, target: str) -> int:
    """把每个页面渲染成 PNG。用于文档配图与界面回归检查。"""
    from ncmgui.window import NAV_ITEMS

    out_dir = Path(target).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 截图只是画界面，不该顺手去拉歌单
    window.defer_page_loads = True
    window.resize(1180, 820)
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
    page.set_demo(songs[0], cover, _demo_lyrics(), position=67.0)
    window.playback_bar.set_demo(
        songs[0], cover, position=67.0, duration=222.0, volume=70, mode="loop"
    )
    window.open_player_page(queue=False)
    QApplication.processEvents()
    image = window.grab()
    path = out_dir / "10-player.png"
    if not image.save(str(path)):
        print(f"保存失败：{path}")
        return 1
    print(f"已保存 {path}  ({image.width()}x{image.height()})")

    page.set_demo(
        songs[0],
        cover,
        _demo_lyrics(),
        queue=songs,
        position=67.0,
    )
    window.open_player_page(queue=True)
    for _ in range(3):
        QApplication.processEvents()
    image = window.grab()
    path = out_dir / "11-player-queue.png"
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

    theme.apply_theme(app)

    window = MainWindow(Config())
    window.show()

    if self_test:
        return _self_test(window)
    if screenshot_dir:
        return _screenshot(window, screenshot_dir)

    # 启动后稍等片刻再静默检查更新：先把主界面画出来，别让网络拖慢启动
    QTimer.singleShot(2500, window.check_updates_on_start)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
