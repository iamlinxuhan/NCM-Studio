"""播放工具窗：倍速、变调、均衡器、A-B 循环、定时关闭、输出设备与音频信息。

所有改动立即作用到正在播放的 mpv；不支持的项按能力探测结果禁用，而不是
等用户点了再报错。窗口关掉后速度/均衡器等设置仍然保留。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import dsp, theme
from ..player import format_clock
from ..widgets.card import accent_button, ghost_button


class PlaybackToolsDialog(QDialog):
    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent or window)
        self.window = window
        self.playback = window.playback
        self.caps = self.playback.capabilities()
        self._updating = False
        self._device_rows: list[tuple[str, str]] = []

        self.setWindowTitle("播放工具")
        self.resize(620, 560)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("播放工具")
        title.setObjectName("CardTitle")
        subtitle = QLabel(self.caps.summary())
        subtitle.setObjectName("Hint")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_effects(), "音效")
        self.tabs.addTab(self._build_loop(), "循环与定时")
        self.tabs.addTab(self._build_device(), "设备与信息")
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        reset = ghost_button("恢复默认", compact=True)
        reset.clicked.connect(self._reset)
        close = accent_button("关闭", glow=False)
        close.clicked.connect(self.accept)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._eq_timer = QTimer(self)
        self._eq_timer.setSingleShot(True)
        self._eq_timer.setInterval(80)
        self._eq_timer.timeout.connect(self._apply_equalizer)

        self.playback.speedChanged.connect(self._sync_speed)
        self.playback.pitchChanged.connect(self._sync_pitch)
        self.playback.equalizerChanged.connect(self._sync_equalizer)
        self.playback.abLoopChanged.connect(self._sync_ab)
        self.playback.audioInfoChanged.connect(self._refresh_info)
        self.playback.audioDevicesChanged.connect(self._sync_devices)
        self.playback.sleepTimerChanged.connect(self._sync_sleep)
        self._sync_speed(self.playback.speed)
        self._sync_pitch(self.playback.pitch)
        self._sync_equalizer()
        self._sync_ab(*self.playback.ab_loop)
        self._sync_devices()
        self._sync_sleep(self.playback.sleep_remaining, self.playback.sleep_after_track)
        self._refresh_info()

    # ---------------------------------------------------------------- 音效

    def _build_effects(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(14)

        speed_row = QHBoxLayout()
        speed_label = QLabel("播放速度")
        speed_label.setMinimumWidth(76)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 200)
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setPageStep(10)
        self.speed_slider.setEnabled(self.caps.speed)
        self.speed_value = QLabel("")
        self.speed_value.setMinimumWidth(56)
        self.speed_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.speed_slider.valueChanged.connect(self._on_speed)
        speed_row.addWidget(speed_label)
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_value)
        layout.addLayout(speed_row)

        pitch_row = QHBoxLayout()
        pitch_label = QLabel("变调")
        pitch_label.setMinimumWidth(76)
        self.pitch_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_slider.setRange(-12, 12)
        self.pitch_slider.setEnabled(self.caps.rubberband)
        self.pitch_value = QLabel("")
        self.pitch_value.setMinimumWidth(56)
        self.pitch_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.pitch_slider.valueChanged.connect(self._on_pitch)
        pitch_row.addWidget(pitch_label)
        pitch_row.addWidget(self.pitch_slider, 1)
        pitch_row.addWidget(self.pitch_value)
        layout.addLayout(pitch_row)
        if not self.caps.rubberband:
            hint = QLabel("当前 mpv 缺少 rubberband 滤镜，无法变调。")
            hint.setObjectName("Hint")
            layout.addWidget(hint)

        self.eq_check = QCheckBox("启用均衡器")
        self.eq_check.toggled.connect(self._on_eq_toggled)
        layout.addWidget(self.eq_check)

        eq_bar = QHBoxLayout()
        preset_label = QLabel("预设")
        self.preset_combo = QComboBox()
        for name in dsp.PRESETS:
            self.preset_combo.addItem(name)
        self.preset_combo.currentTextChanged.connect(self._on_preset)
        eq_bar.addWidget(preset_label)
        eq_bar.addWidget(self.preset_combo, 1)
        preamp_label = QLabel("前置增益")
        self.preamp_value = QLabel("")
        self.preamp_value.setMinimumWidth(50)
        self.preamp_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.preamp_slider = QSlider(Qt.Orientation.Horizontal)
        self.preamp_slider.setRange(-12, 12)
        self.preamp_slider.setFixedWidth(130)
        self.preamp_slider.valueChanged.connect(self._on_preamp)
        eq_bar.addWidget(preamp_label)
        eq_bar.addWidget(self.preamp_slider)
        eq_bar.addWidget(self.preamp_value)
        layout.addLayout(eq_bar)

        bands = QHBoxLayout()
        bands.setSpacing(10)
        self.eq_sliders: list[QSlider] = []
        for label in dsp.BAND_LABELS:
            column = QVBoxLayout()
            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)
            slider.setMinimumHeight(132)
            slider.setToolTip(f"{label} Hz")
            slider.valueChanged.connect(self._on_band)
            band_label = QLabel(label)
            band_label.setObjectName("Hint")
            band_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(slider, 1, Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(band_label)
            self.eq_sliders.append(slider)
            bands.addLayout(column)
        layout.addLayout(bands)

        self.eq_note = QLabel("")
        self.eq_note.setObjectName("Hint")
        self.eq_note.setWordWrap(True)
        if not self.caps.equalizer:
            self.eq_note.setText("当前 mpv 不支持 lavfi 均衡器，相关控件已禁用。")
        layout.addWidget(self.eq_note)

        self.loudness_check = QCheckBox("响度归一化（loudnorm，约 -16 LUFS）")
        self.loudness_check.setEnabled(self.caps.loudnorm)
        self.loudness_check.toggled.connect(self._on_loudness)
        layout.addWidget(self.loudness_check)
        layout.addStretch(1)

        if not self.caps.equalizer:
            self.eq_check.setEnabled(False)
            self.preset_combo.setEnabled(False)
            self.preamp_slider.setEnabled(False)
            for slider in self.eq_sliders:
                slider.setEnabled(False)
        return page

    def _on_speed(self, raw: int) -> None:
        if self._updating:
            return
        self.playback.set_speed(raw / 100.0)

    def _on_pitch(self, value: int) -> None:
        if self._updating:
            return
        self.playback.set_pitch(float(value))

    def _sync_speed(self, value: float) -> None:
        self._updating = True
        self.speed_slider.setValue(int(round(float(value) * 100)))
        self.speed_value.setText(f"{value:.2f}×")
        self._updating = False

    def _sync_pitch(self, value: float) -> None:
        self._updating = True
        self.pitch_slider.setValue(int(round(float(value))))
        self.pitch_value.setText(f"{value:+.1f} 半音" if abs(value) > 0.01 else "原调")
        self._updating = False

    def _on_eq_toggled(self, enabled: bool) -> None:
        if self._updating:
            return
        self._sync_eq_enabled()
        self.playback.set_equalizer(enabled=enabled, gains=self._gains())

    def _sync_eq_enabled(self) -> None:
        enabled = self.eq_check.isChecked() and self.caps.equalizer
        for slider in self.eq_sliders:
            slider.setEnabled(enabled)
        self.preset_combo.setEnabled(enabled)
        self.preamp_slider.setEnabled(enabled)

    def _on_preset(self, name: str) -> None:
        if self._updating:
            return
        values = dsp.PRESETS.get(name)
        if values is None:
            return
        self._updating = True
        for slider, value in zip(self.eq_sliders, values):
            slider.setValue(int(round(value)))
        self._updating = False
        self.preset_combo.setCurrentText(name)
        self._eq_timer.start()

    def _on_band(self, _value: int) -> None:
        if self._updating:
            return
        self._eq_timer.start()

    def _on_preamp(self, value: int) -> None:
        if self._updating:
            return
        self.preamp_value.setText(f"{value:+d} dB")
        self._eq_timer.start()

    def _gains(self) -> tuple[float, ...]:
        return tuple(float(slider.value()) for slider in self.eq_sliders)

    def _apply_equalizer(self) -> None:
        self.playback.set_equalizer(
            enabled=self.eq_check.isChecked(),
            gains=self._gains(),
            preamp=float(self.preamp_slider.value()),
        )

    def _sync_equalizer(self) -> None:
        self._updating = True
        enabled = self.playback.equalizer_enabled
        self.eq_check.setChecked(enabled)
        gains = self.playback.equalizer_gains
        for slider, value in zip(self.eq_sliders, gains):
            slider.setValue(int(round(value)))
        self.preamp_slider.setValue(int(round(self.playback.equalizer_preamp)))
        self.preamp_value.setText(f"{int(round(self.playback.equalizer_preamp)):+d} dB")
        self.loudness_check.setChecked(self.playback.loudness)
        self._sync_eq_enabled()
        self._updating = False

    def _on_loudness(self, enabled: bool) -> None:
        if self._updating:
            return
        self.playback.set_loudness(enabled)

    # ---------------------------------------------------------------- 循环

    def _build_loop(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(14)

        ab_title = QLabel("A-B 循环")
        ab_title.setObjectName("CardTitle")
        layout.addWidget(ab_title)
        ab_hint = QLabel("把 A、B 设在当前播放位置；切歌或清除后失效。")
        ab_hint.setObjectName("Hint")
        ab_hint.setWordWrap(True)
        layout.addWidget(ab_hint)

        self.ab_label = QLabel("")
        self.ab_label.setObjectName("Hint")
        ab_row = QHBoxLayout()
        self.ab_a_btn = ghost_button("设 A 点", compact=True)
        self.ab_b_btn = ghost_button("设 B 点", compact=True)
        self.ab_clear_btn = ghost_button("清除", compact=True)
        self.ab_a_btn.setEnabled(self.caps.ab_loop)
        self.ab_b_btn.setEnabled(self.caps.ab_loop)
        self.ab_clear_btn.setEnabled(self.caps.ab_loop)
        self.ab_a_btn.clicked.connect(self._set_a)
        self.ab_b_btn.clicked.connect(self._set_b)
        self.ab_clear_btn.clicked.connect(self.playback.clear_ab_loop)
        ab_row.addWidget(self.ab_a_btn)
        ab_row.addWidget(self.ab_b_btn)
        ab_row.addWidget(self.ab_clear_btn)
        ab_row.addStretch(1)
        layout.addLayout(ab_row)
        layout.addWidget(self.ab_label)
        if not self.caps.ab_loop:
            note = QLabel("当前 mpv 不支持 A-B 循环。")
            note.setObjectName("Hint")
            layout.addWidget(note)

        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        layout.addWidget(line)

        timer_title = QLabel("定时关闭")
        timer_title.setObjectName("CardTitle")
        layout.addWidget(timer_title)
        timer_hint = QLabel("到点后暂停播放；也可以等当前歌曲播完再停。")
        timer_hint.setObjectName("Hint")
        layout.addWidget(timer_hint)

        timer_row = QHBoxLayout()
        self.sleep_spin = QSpinBox()
        self.sleep_spin.setRange(1, 180)
        self.sleep_spin.setValue(30)
        self.sleep_spin.setSuffix(" 分钟")
        self.sleep_start_btn = accent_button("开始倒计时", glow=False)
        self.sleep_start_btn.setMinimumHeight(32)
        self.sleep_start_btn.clicked.connect(
            lambda: self.playback.start_sleep(self.sleep_spin.value())
        )
        self.sleep_track_btn = ghost_button("播完当前曲", compact=True)
        self.sleep_track_btn.clicked.connect(self.playback.sleep_after_current_track)
        self.sleep_cancel_btn = ghost_button("取消", compact=True)
        self.sleep_cancel_btn.clicked.connect(self.playback.cancel_sleep)
        timer_row.addWidget(self.sleep_spin)
        timer_row.addWidget(self.sleep_start_btn)
        timer_row.addWidget(self.sleep_track_btn)
        timer_row.addWidget(self.sleep_cancel_btn)
        timer_row.addStretch(1)
        layout.addLayout(timer_row)

        self.sleep_label = QLabel("")
        self.sleep_label.setObjectName("Hint")
        layout.addWidget(self.sleep_label)
        layout.addStretch(1)
        return page

    def _set_a(self) -> None:
        self.playback.set_ab_a(self.playback.position)

    def _set_b(self) -> None:
        a, _b = self.playback.ab_loop
        position = self.playback.position
        if a is not None and position <= a:
            self.ab_label.setText("B 点必须晚于 A 点，请往后播放一点再设。")
            return
        self.playback.set_ab_b(position)

    def _sync_ab(self, a, b) -> None:
        parts = []
        parts.append(f"A：{format_clock(a)}" if a is not None else "A：未设置")
        parts.append(f"B：{format_clock(b)}" if b is not None else "B：未设置")
        self.ab_label.setText("　".join(parts))

    def _sync_sleep(self, remaining: int, after_track: bool) -> None:
        if after_track:
            self.sleep_label.setText("将在当前歌曲播完后停止。")
        elif remaining > 0:
            minutes, seconds = divmod(max(0, int(remaining)), 60)
            self.sleep_label.setText(f"剩余 {minutes:02d}:{seconds:02d} 后暂停。")
        else:
            self.sleep_label.setText("未启用定时关闭。")

    # ---------------------------------------------------------------- 设备

    def _build_device(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(12)

        device_row = QHBoxLayout()
        label = QLabel("输出设备")
        label.setMinimumWidth(76)
        self.device_combo = QComboBox()
        self.device_combo.setEnabled(self.caps.audio_device)
        self.device_combo.currentIndexChanged.connect(self._on_device)
        refresh = ghost_button("刷新", compact=True)
        refresh.clicked.connect(self._reload_devices)
        device_row.addWidget(label)
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(refresh)
        layout.addLayout(device_row)
        self.device_note = QLabel("切换设备时会恢复该设备上次的音量。")
        self.device_note.setObjectName("Hint")
        self.device_note.setWordWrap(True)
        layout.addWidget(self.device_note)

        info_title = QLabel("音频信息")
        info_title.setObjectName("CardTitle")
        layout.addWidget(info_title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(7)
        self._info_labels: dict[str, QLabel] = {}
        rows = (
            ("codec", "编码"),
            ("samplerate", "采样率"),
            ("format", "采样格式"),
            ("channels", "声道"),
            ("bitrate", "码率"),
            ("ao", "音频输出"),
            ("device", "设备"),
            ("speed", "播放速度"),
            ("pitch", "变调"),
            ("eq", "均衡器"),
        )
        for index, (key, caption) in enumerate(rows):
            name = QLabel(caption)
            name.setObjectName("Hint")
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            grid.addWidget(name, index // 2, (index % 2) * 2)
            grid.addWidget(value, index // 2, (index % 2) * 2 + 1)
            self._info_labels[key] = value
        layout.addLayout(grid)

        self.cap_label = QLabel(self.caps.summary())
        self.cap_label.setObjectName("Hint")
        self.cap_label.setWordWrap(True)
        layout.addWidget(self.cap_label)
        layout.addStretch(1)
        return page

    def _reload_devices(self) -> None:
        binary = self.playback.mpv_binary
        rows = list(dsp.audio_devices(binary)) if binary else []
        if not rows:
            rows = list(self.playback.audio_devices)
        self._device_rows = rows
        self._fill_devices()
        if not rows:
            self.device_note.setText(
                "没能枚举到设备；开始播放后 mpv 会自己上报可用设备。"
            )

    def _sync_devices(self) -> None:
        rows = list(self.playback.audio_devices)
        if rows and rows != self._device_rows:
            self._device_rows = rows
            self._fill_devices()

    def _fill_devices(self) -> None:
        current = self.playback.audio_device
        self._updating = True
        self.device_combo.clear()
        self.device_combo.addItem("自动选择", "")
        for name, description in self._device_rows:
            self.device_combo.addItem(description or name, name)
        index = self.device_combo.findData(current)
        self.device_combo.setCurrentIndex(index if index >= 0 else 0)
        self._updating = False

    def _on_device(self, _index: int) -> None:
        if self._updating:
            return
        self.playback.set_audio_device(str(self.device_combo.currentData() or ""))

    def _refresh_info(self) -> None:
        info = self.playback.audio_info
        samplerate = info.get("samplerate")
        try:
            samplerate_text = f"{float(samplerate) / 1000:.1f} kHz"
        except (TypeError, ValueError):
            samplerate_text = "—"
        bitrate = info.get("bitrate")
        try:
            bitrate_text = f"{float(bitrate) / 1000:.0f} kbps"
        except (TypeError, ValueError):
            bitrate_text = "—"
        channels = info.get("channels")
        layout_name = info.get("channel_layout") or ""
        channels_text = "—"
        if channels:
            try:
                channels_text = f"{int(channels)} 声道"
            except (TypeError, ValueError):
                channels_text = str(channels)
            if layout_name:
                channels_text += f"（{layout_name}）"
        device_name = str(info.get("device") or self.playback.audio_device or "")
        device_text = "自动"
        for name, description in self._device_rows:
            if name == device_name:
                device_text = description or name
                break
        if device_name and device_text == "自动":
            device_text = device_name

        values = {
            "codec": str(info.get("codec") or "—"),
            "samplerate": samplerate_text,
            "format": str(info.get("format") or "—"),
            "channels": channels_text,
            "bitrate": bitrate_text,
            "ao": str(info.get("ao") or "—"),
            "device": device_text,
            "speed": f"{self.playback.speed:.2f}×",
            "pitch": (
                f"{self.playback.pitch:+.1f} 半音"
                if abs(self.playback.pitch) > 0.01 else "原调"
            ),
            "eq": "开启" if self.playback.equalizer_enabled else "关闭",
        }
        for key, label in self._info_labels.items():
            label.setText(values.get(key, "—"))

    # ---------------------------------------------------------------- 其它

    def _reset(self) -> None:
        self.playback.set_speed(1.0)
        self.playback.set_pitch(0.0)
        self.playback.set_loudness(False)
        self.playback.set_equalizer(enabled=False, gains=(0,) * 10, preamp=0)
        self._updating = True
        self.preset_combo.setCurrentText("平坦")
        self._updating = False
