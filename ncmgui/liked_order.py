"""「我喜欢的音乐」顺序的本地缓存。

收藏顺序只有接口知道。为了让本地曲库的「喜欢顺序」排序不再额外发请求，
「我喜欢的音乐」页每次成功加载后把顺序写进这里；本地曲库页只读缓存，
不访问网络。

顺序只在红心列表重新加载时更新 —— 和「我喜欢的音乐」页本身的行为一致，
喜欢/取消喜欢之后也要刷新一次红心页才会变。缓存按账号 uid 区分，换号后
旧顺序自动失效。

自检::

    .venv/bin/python -m ncmgui.liked_order
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Sequence

from .config import config_dir
from .fsutil import atomic_write_text

CACHE_NAME = "liked-order.json"


def cache_path() -> Path:
    return config_dir() / CACHE_NAME


def save(order: Sequence[str], uid: int = 0) -> None:
    """写入收藏顺序（文件名主干列表）。写失败不影响红心页本身。"""
    payload = {
        "uid": int(uid or 0),
        "saved_at": int(time.time()),
        "order": [str(item) for item in order],
    }
    try:
        atomic_write_text(
            cache_path(),
            json.dumps(payload, ensure_ascii=False) + "\n",
            mode=0o600,
        )
    except OSError:
        pass


def load(uid: int = 0) -> list[str] | None:
    """读收藏顺序；缓存不存在、损坏或账号对不上时返回 ``None``。"""
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    order = raw.get("order")
    if not isinstance(order, list) or not order:
        return None
    cached_uid = int(raw.get("uid") or 0)
    # uid 未知（还没探测到账号）时先按缓存用；探测完成后会带 uid 再读一次
    if uid and cached_uid and int(uid) != cached_uid:
        return None
    return [item for item in order if isinstance(item, str)]


def clear() -> None:
    try:
        cache_path().unlink()
    except OSError:
        pass


def _selftest() -> int:
    import tempfile

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    global config_dir
    original = config_dir
    with tempfile.TemporaryDirectory() as root:
        config_dir = lambda: Path(root)  # type: ignore[assignment]
        try:
            assert load() is None
            save(["甲 - 一", "乙 - 二"], uid=7)
            assert load(7) == ["甲 - 一", "乙 - 二"]
            assert load(8) is None, "换号后必须失效"
            assert load(0) == ["甲 - 一", "乙 - 二"], "uid 未知时先按缓存用"
            clear()
            assert load(7) is None
        finally:
            config_dir = original
    print("红心顺序缓存自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
