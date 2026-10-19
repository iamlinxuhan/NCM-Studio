"""把侧边栏里关系紧密的页面合并成带标签页的宿主页。

侧边栏从 16 项缩到 10 项：搜索并入发现（发现页顶部的搜索框直接把内容区
换成搜索结果），播放历史与听歌统计、下载/NCM 解码/云盘上传、账号任务/
监控代理/调试工具分别合成一个页面。原有页面类与 ``show_page(key)`` 调用
都不变，只是入口换了位置。
"""

from __future__ import annotations

from .base import TabbedPage
from .cloud import CloudPage
from .download import DownloadPage
from .history import HistoryPage
from .ncm import NcmPage
from .proxy import ProxyPage
from .stats import StatsPage
from .tasks import TasksPage
from .tools import ToolsPage


class HistoryStatsPage(TabbedPage):
    PAGE_TITLE = "历史与统计"
    PAGE_SUBTITLE = "最近听过什么，以及在哪些歌上花的时间最多。"
    TABS = (
        ("history", "播放历史", HistoryPage),
        ("stats", "听歌统计", StatsPage),
    )


class TransferPage(TabbedPage):
    PAGE_TITLE = "下载与云盘"
    PAGE_SUBTITLE = "下载任务、NCM 解码与音乐云盘上传。"
    TABS = (
        ("download", "下载", DownloadPage),
        ("ncm", "NCM 解码", NcmPage),
        ("cloud", "云盘", CloudPage),
    )


class AdvancedPage(TabbedPage):
    PAGE_TITLE = "高级工具"
    PAGE_SUBTITLE = "账号任务、代理监控与调试工具。"
    TABS = (
        ("tasks", "账号任务", TasksPage),
        ("proxy", "监控代理", ProxyPage),
        ("tools", "调试工具", ToolsPage),
    )
