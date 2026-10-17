#!/usr/bin/env python3
"""生成应用图标（PNG + ICO）。

    python3 scripts/make_icon.py

产物提交进仓库（``packaging/``），构建时直接用，不在 CI 里现场生成 ——
免得打包机上的 Pillow 版本或字体差异让每次构建出来的图标都不一样。

图形是**纯几何绘制**的，不用字体：跨机器渲染完全一致，也不受系统装没装
某个字体影响。配色取自 :mod:`ncmgui.theme` 的主强调渐变（青 → 紫），
底色透明，免得在浅色主题的桌面上出现一个突兀的方块。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "packaging"

#: 最终尺寸。AppImage 与 freedesktop 图标规范都推荐不低于 256。
SIZE = 256

#: 超采样倍数。先画大图再缩，边缘才不会有锯齿。
SS = 4

#: 与 ncmgui/theme.py 的主强调色保持一致：CYAN → PURPLE 的对角渐变。
CYAN = (0x00, 0xE5, 0xFF)
PURPLE = (0x7C, 0x5C, 0xFF)

#: 音符用窗口底色画在渐变方块上 —— 深色压在亮色上，小尺寸下轮廓最清楚。
NOTE = (0x07, 0x0A, 0x11)

#: 圆角半径占边长的比例。
RADIUS = 0.22


def _diagonal_gradient(size: int) -> Image.Image:
    """左上到右下的对角渐变。

    先在很小的图上逐像素算，再放大 —— 直接对 1024×1024 逐像素跑 Python 循环
    要几百万次，慢且没必要，渐变本身是光滑的。
    """
    small = 16
    img = Image.new("RGB", (small, small))
    pixels = img.load()
    span = 2 * (small - 1)
    for x in range(small):
        for y in range(small):
            t = (x + y) / span
            pixels[x, y] = tuple(
                round(c + (p - c) * t) for c, p in zip(CYAN, PURPLE)
            )
    return img.resize((size, size), Image.BICUBIC)


def _draw_note(draw: ImageDraw.ImageDraw, size: int) -> None:
    """两个带符杠的八分音符（♫ 的几何版）。

    坐标全部按边长归一化，这样改 SIZE 不用重算。两个音符错开高度、顶端用一条
    斜杠连起来，比单个音符在 16px 下更容易辨认。
    """
    def px(value: float) -> float:
        return value * size

    head_rx, head_ry = 0.105, 0.082
    # (中心 x, 中心 y)：第二个音符高一些，形成常见的音符对
    heads = ((0.34, 0.72), (0.68, 0.63))
    # 符干贴着符头右缘，从符头中心往上
    stems = ((0.405, 0.30, 0.72), (0.745, 0.21, 0.63))
    stem_w = 0.040

    for cx, cy in heads:
        draw.ellipse(
            [
                px(cx - head_rx),
                px(cy - head_ry),
                px(cx + head_rx),
                px(cy + head_ry),
            ],
            fill=NOTE,
        )

    for left, top, bottom in stems:
        draw.rectangle(
            [px(left), px(top), px(left + stem_w), px(bottom)], fill=NOTE
        )

    # 符杠：连接两根符干顶端的一条斜带
    beam_thickness = 0.085
    draw.polygon(
        [
            (px(0.405), px(0.30)),
            (px(0.745 + stem_w), px(0.21)),
            (px(0.745 + stem_w), px(0.21 + beam_thickness)),
            (px(0.405), px(0.30 + beam_thickness)),
        ],
        fill=NOTE,
    )


def build(size: int = SIZE) -> Image.Image:
    big = size * SS

    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    # 渐变方块：用一张遮罩把渐变裁成圆角矩形
    gradient = _diagonal_gradient(big).convert("RGBA")
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, big - 1, big - 1], radius=int(big * RADIUS), fill=255
    )
    canvas.paste(gradient, (0, 0), mask)

    _draw_note(ImageDraw.Draw(canvas), big)

    return canvas.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    icon = build()

    png = OUT_DIR / "ncm-studio.png"
    icon.save(png, "PNG")
    print(f"  {png.relative_to(ROOT)}  {png.stat().st_size} 字节")

    # Windows 用 .ico。多尺寸一起塞进去，任务栏、资源管理器、Alt-Tab 各取所需
    ico = OUT_DIR / "ncm-studio.ico"
    icon.save(
        ico,
        "ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"  {ico.relative_to(ROOT)}  {ico.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
