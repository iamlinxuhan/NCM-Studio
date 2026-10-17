"""随程序分发的静态资源（图标等）。

打包后这些文件由 PyInstaller 解到 bundle 里（onefile 是临时目录，onedir 是
``_internal/``），运行时要用就得知道它在哪。

这里用 :attr:`__file__` 定位而不是 ``sys._MEIPASS``：PyInstaller 从 4.3 起保证
冻结模块的 ``__file__`` 始终是包内的绝对路径，这个模块和它旁边的 ``assets/``
相对位置在两种打包模式下都一样。``sys._MEIPASS`` 当然也能用，但它是给「整个
bundle 的根在哪」这个问题用的，问「我自己旁边的文件在哪」用 ``__file__`` 更直接。
"""

from __future__ import annotations

from pathlib import Path

#: 本模块所在目录，也就是打包后 ``ncmgui/`` 包的位置。
_PACKAGE_DIR = Path(__file__).resolve().parent


def asset(name: str) -> Path:
    """返回随包资源 ``ncmgui/assets/<name>`` 的路径。

    只拼路径、不检查存在 —— 调用方（比如 :class:`QIcon`）对缺失文件的处理
    比这里抛异常更合适，画不出图标不该让程序起不来。
    """
    return _PACKAGE_DIR / "assets" / name


#: 应用图标。spec 里的 ``datas`` 把它放在这个位置，两边必须一致。
ICON_PATH = asset("ncm-studio.png")

__all__ = ["asset", "ICON_PATH"]
