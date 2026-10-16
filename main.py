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

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication, QIcon  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ncmgui import APP_ID, APP_NAME, VERSION, ensure_utf8_stdout, theme  # noqa: E402
from ncmgui.config import Config  # noqa: E402
from ncmgui.resources import ICON_PATH  # noqa: E402
from ncmgui.window import MainWindow  # noqa: E402


def _self_test(window: MainWindow) -> int:
    """构建完成后做一遍结构性检查。"""
    from ncmgui.widgets.console import ConsolePanel, ConsoleView

    problems: list[str] = []

    expected = {"account", "download", "ncm", "cloud", "tasks", "proxy", "tools", "settings"}
    missing = expected - set(window.pages)
    if missing:
        problems.append(f"缺少页面：{sorted(missing)}")

    for key, page in window.pages.items():
        if page.content.count() == 0:
            problems.append(f"页面 {key} 没有任何内容")

    # 导航按钮数量应当和页面数量一致
    if len(window._nav_buttons) != len(window.pages):
        problems.append("侧边栏按钮与页面数量不一致")

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


def _screenshot(window: MainWindow, target: str) -> int:
    """把每个页面渲染成 PNG。用于文档配图与界面回归检查。"""
    from ncmgui.window import NAV_ITEMS

    out_dir = Path(target).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

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

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
