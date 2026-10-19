"""mpv 音频能力探测与滤镜链拼装。

这里不启动播放器：只调用 ``mpv --list-properties``、``--af=help`` 这类
「打印后退出」的参数，解析出当前构建支持哪些能力。页面据此隐藏不可用的
控件，而不是让用户点一下才报错。

滤镜链保留在纯字符串层面，方便自检；真正写进 mpv 由
:class:`ncmgui.playback.PlaybackController` 负责。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
BAND_LABELS = ("31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k")

#: 均衡器预设（单位 dB，顺序与 :data:`BANDS` 一致）
PRESETS: dict[str, tuple[float, ...]] = {
    "平坦": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "流行": (-1, 0, 1, 2, 3, 2, 1, 0, -1, -1),
    "摇滚": (3, 2, 1, 0, -1, 0, 1, 2, 3, 2),
    "古典": (2, 1, 0, 0, 0, 0, -1, -1, -2, -2),
    "爵士": (2, 1, 0, 1, -1, -1, 0, 1, 2, 2),
    "低音增强": (5, 4, 3, 1, 0, 0, 0, 0, 0, 0),
    "人声": (-2, -1, 0, 1, 3, 3, 2, 0, -1, -2),
    "电子": (3, 2, 0, -1, -2, 0, 1, 2, 3, 4),
}


@dataclass(frozen=True)
class Capabilities:
    """当前 mpv 构建支持的能力。字段为 False 时界面隐藏对应控件。"""

    speed: bool = False
    pitch_correction: bool = False
    rubberband: bool = False
    ab_loop: bool = False
    audio_device: bool = False
    audio_device_list: bool = False
    lavfi: bool = False
    equalizer: bool = False
    loudnorm: bool = False
    properties: tuple[str, ...] = field(default_factory=tuple)
    filters: tuple[str, ...] = field(default_factory=tuple)

    @property
    def any_effect(self) -> bool:
        return bool(
            (self.lavfi and (self.equalizer or self.loudnorm))
            or self.rubberband
        )

    def summary(self) -> str:
        """设置页/工具窗里显示的一行能力说明。"""
        items = []
        if self.speed:
            items.append("倍速")
        if self.rubberband:
            items.append("变调")
        if self.equalizer:
            items.append("均衡器")
        if self.loudnorm:
            items.append("响度归一化")
        if self.audio_device_list:
            items.append("输出设备")
        if self.ab_loop:
            items.append("A-B 循环")
        if not items:
            return "当前 mpv 构建不支持音频工具，基础播放不受影响。"
        return "支持：" + "、".join(items)


def _run(binary: str, *args: str, timeout: float = 6.0) -> str:
    if not binary or not Path(binary).exists():
        return ""
    try:
        result = subprocess.run(
            [binary, "--no-config", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def parse_properties(text: str) -> tuple[str, ...]:
    names = []
    for line in str(text or "").splitlines():
        name = line.strip()
        if not name or name.lower() == "name":
            continue
        if " " in name or "\t" in name:
            continue
        names.append(name)
    return tuple(names)


def parse_filter_output(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """返回 ``(内建滤镜, lavfi 滤镜)``。

    ``mpv --af=help`` 先列内建滤镜，再列 ``Available libavfilter filters:``。
    """
    builtin: list[str] = []
    libav: list[str] = []
    target = builtin
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith("available libavfilter filters"):
            target = libav
            continue
        if lower.startswith("available audio filters") or lower.startswith("prefix them"):
            continue
        name = stripped.split()[0]
        if name.startswith("-") or name.endswith(":"):
            continue
        target.append(name)
    return tuple(builtin), tuple(libav)


def probe(binary: str) -> Capabilities:
    """探测一个 mpv 可执行文件；失败时返回「全不支持」而不是抛异常。"""
    if not binary:
        return Capabilities()
    properties = parse_properties(_run(binary, "--list-properties"))
    builtin, libav = parse_filter_output(_run(binary, "--af=help"))
    props = set(properties)
    builtin_set = set(builtin)
    libav_set = set(libav)
    lavfi = "lavfi" in builtin_set

    return Capabilities(
        speed="speed" in props,
        pitch_correction="audio-pitch-correction" in props,
        rubberband="rubberband" in builtin_set,
        ab_loop="ab-loop-a" in props and "ab-loop-b" in props,
        audio_device="audio-device" in props,
        audio_device_list="audio-device-list" in props,
        lavfi=lavfi,
        equalizer=lavfi and "equalizer" in libav_set,
        loudnorm=lavfi and "loudnorm" in libav_set,
        properties=properties,
        filters=builtin + libav,
    )


def parse_audio_devices(text: str) -> tuple[tuple[str, str], ...]:
    """解析 ``mpv --audio-device=help`` 的 ``'name' (说明)`` 行。"""
    devices: list[tuple[str, str]] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("'") or "' (" not in stripped:
            continue
        name, _, rest = stripped.partition("' (")
        detail = rest.rsplit(")", 1)[0].strip()
        name = name.lstrip("'").strip()
        if name and all(name != existing for existing, _ in devices):
            devices.append((name, detail))
    return tuple(devices)


def audio_devices(binary: str) -> tuple[tuple[str, str], ...]:
    return parse_audio_devices(_run(binary, "--audio-device=help"))


def normalize_gains(gains) -> tuple[float, ...]:
    values = [float(value) for value in list(gains or ())[: len(BANDS)]]
    values.extend([0.0] * (len(BANDS) - len(values)))
    return tuple(max(-12.0, min(12.0, value)) for value in values)


def build_audio_filters(
    *,
    equalizer: bool = False,
    gains=(),
    preamp: float = 0.0,
    loudness: bool = False,
    semitones: float = 0.0,
) -> str:
    """拼出 ``af`` 属性字符串。没有滤镜时返回空串（交给 mpv 清空）。"""
    chain: list[str] = []
    if equalizer:
        values = normalize_gains(gains)
        parts = []
        preamp = max(-12.0, min(12.0, float(preamp)))
        if abs(preamp) > 0.01:
            parts.append(f"volume={preamp:.1f}dB")
        for band, gain in zip(BANDS, values):
            if abs(gain) < 0.01:
                continue
            parts.append(f"equalizer=f={band}:width_type=o:width=1:g={gain:.1f}")
        if parts:
            chain.append("lavfi=[" + ",".join(parts) + "]")
    if loudness:
        chain.append("lavfi=[loudnorm=I=-16:TP=-1.5:LRA=11]")
    if abs(float(semitones)) > 0.01:
        semitones = max(-12.0, min(12.0, float(semitones)))
        ratio = 2.0 ** (semitones / 12.0)
        chain.append(f"rubberband=pitch={ratio:.6f}")
    return ",".join(chain)


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    props = parse_properties(
        "Name\n\n pid\n speed\n pitch\n audio-pitch-correction\n"
        " ab-loop-a\n ab-loop-b\n audio-device\n audio-device-list\n"
    )
    check("属性解析", props[:3], ("pid", "speed", "pitch"))
    check("属性数量", len(props), 8)

    builtin, libav = parse_filter_output(
        "Available audio filters:\n"
        "  lavfi            libavfilter bridge\n"
        "  rubberband       Pitch conversion\n"
        "\n"
        "Available libavfilter filters:\n"
        "  equalizer        two-pole EQ\n"
        "  loudnorm         EBU R128\n"
        "  rubberband       time stretch\n"
        "\n"
        "prefix them with lavfi- to select the libavfilter one.\n"
    )
    check("内建滤镜", builtin, ("lavfi", "rubberband"))
    check("lavfi 滤镜含均衡器", "equalizer" in libav, True)
    check("lavfi 滤镜含响度", "loudnorm" in libav, True)

    devices = parse_audio_devices(
        "List of detected audio devices:\n"
        "  'auto' (Autoselect device)\n"
        "  'pipewire/sink' (Speakers)\n"
    )
    check("设备数量", len(devices), 2)
    check("设备名", devices[1][0], "pipewire/sink")
    check("设备说明", devices[1][1], "Speakers")

    check("空滤镜链", build_audio_filters(), "")
    check(
        "只用均衡器",
        build_audio_filters(
            equalizer=True, gains=[3, 0, -2] + [0] * 7, preamp=0
        ),
        "lavfi=[equalizer=f=31:width_type=o:width=1:g=3.0,"
        "equalizer=f=125:width_type=o:width=1:g=-2.0]",
    )
    check(
        "响度与变调",
        build_audio_filters(loudness=True, semitones=12),
        "lavfi=[loudnorm=I=-16:TP=-1.5:LRA=11],rubberband=pitch=2.000000",
    )
    check("增益裁剪", normalize_gains([99, -99]), (12.0, -12.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    check("预设数量", len(PRESETS) >= 6, True)
    check("预设长度", all(len(v) == len(BANDS) for v in PRESETS.values()), True)

    # 有 mpv 时做一次真实探测，只报告不因环境缺失而失败
    from .player import find_mpv

    binary = find_mpv("")
    if binary:
        caps = probe(binary)
        if not caps.speed:
            failures.append("真实 mpv 探测不到 speed 属性")
        print(f"  mpv：{binary}")
        print(f"  {caps.summary()}")

    if failures:
        print(f"音频工具自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("音频工具自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
