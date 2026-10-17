"""ncmctl GUI —— 网易云音乐命令行工具 ncmctl 的 PyQt6 图形界面。"""

import sys

APP_NAME = "ncmctl GUI"
APP_ID = "ncmctl-gui"
VERSION = "1.3.6"

#: 随程序分发的 ncmctl 版本。
#:
#: 本程序是 ncmctl 的图形前端，界面里每个功能都对应它的一个子命令，所以两者
#: 必须一起测、一起发。把版本钉死在这里，用户拿到的就是测过的那个组合，不会
#: 因为自己装了个别的版本而出现「界面有这个按钮但命令不支持」这类问题。
#:
#: 代价是上游发新版时这里得跟着改、重新发版。scripts/fetch_ncmctl.py 按这个
#: 值去下载对应产物，校验和也一并钉在那里。
NCMCTL_VERSION = "0.8.1"


def ensure_utf8_stdout() -> None:
    """让标准输出能够打印中文。

    Windows 控制台默认是 cp1252 / cp936，中文一 ``print`` 就抛
    ``UnicodeEncodeError``；重定向到文件时也可能撞上同样的编码问题。
    命令行自检在动手之前先调一次，省得每个 ``print`` 都得自己防一手。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


__all__ = [
    "APP_NAME",
    "APP_ID",
    "VERSION",
    "NCMCTL_VERSION",
    "ensure_utf8_stdout",
]
