"""歌曲右键菜单。

各页面能提供的动作不同（本地曲库不能「喜欢」、未登录时不能写歌单），所以菜单
不自己发请求，只根据页面传进来的回调决定显示哪些项。
"""

from __future__ import annotations

from typing import Callable, Sequence

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QMenu, QWidget

from ..resolver import Song

#: 回调签名：``(song, queue)`` / ``(song,)`` / ``(song, current_liked)``
Callback = Callable[..., None]


def song_link(song: Song) -> str:
    if getattr(song, "source", ""):
        return song.name
    return f"https://music.163.com/song?id={int(song.id)}"


def show_song_menu(
    parent: QWidget,
    song: Song,
    songs: Sequence[Song],
    *,
    play: Callback | None = None,
    play_next: Callback | None = None,
    add_queue: Callback | None = None,
    is_liked: Callable[[Song], bool] | None = None,
    toggle_like: Callback | None = None,
    add_playlist: Callback | None = None,
    download: Callback | None = None,
    comments: Callback | None = None,
    search: Callback | None = None,
    extra: Sequence[tuple[str, Callback]] = (),
    position=None,
) -> None:
    """弹出菜单。只有传了回调的动作才会出现。"""
    menu = QMenu(parent)
    queue = list(songs) or [song]

    if play is not None:
        menu.addAction("立即播放", lambda: play(song, queue))
    if play_next is not None:
        menu.addAction("下一首播放", lambda: play_next(song))
    if add_queue is not None:
        menu.addAction("加入播放队列", lambda: add_queue(song))
    if any((play, play_next, add_queue)):
        menu.addSeparator()

    if toggle_like is not None:
        liked = bool(is_liked(song)) if is_liked is not None else False
        menu.addAction("取消喜欢" if liked else "喜欢", lambda: toggle_like(song, liked))
    if add_playlist is not None:
        menu.addAction("加入歌单…", lambda: add_playlist(song))
    if download is not None:
        menu.addAction("下载…", lambda: download(song))
    if comments is not None:
        menu.addAction("查看评论…", lambda: comments(song))
    if any((toggle_like, add_playlist, download, comments)):
        menu.addSeparator()

    if search is not None:
        menu.addAction("搜索同款", lambda: search(song.display))

    if extra:
        menu.addSeparator()
        for title, callback in extra:
            menu.addAction(title, lambda cb=callback: cb(song))

    copy_menu = menu.addMenu("复制")
    copy_menu.addAction("标题", lambda: _copy(song.display))
    copy_menu.addAction("歌手", lambda: _copy(", ".join(song.artists)))
    copy_menu.addAction("歌曲 ID", lambda: _copy(str(song.id)))
    copy_menu.addAction("链接", lambda: _copy(song_link(song)))

    if position is None:
        menu.exec(parent.cursor().pos())
    else:
        menu.exec(position)


def _copy(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text or "")
