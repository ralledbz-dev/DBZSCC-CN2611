from __future__ import annotations

import os
import threading
import time
import wave
from pathlib import Path
from typing import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QAudio, QAudioFormat
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QPushButton

from dbzscc.app.speech_input import SpeechAssetsStatus, SpeechCancelled, SpeechSetupError
from dbzscc.ui.speech_input import SpeechInputWidget


class FakeAssetManager:
    def __init__(self, root: Path) -> None:
        self.temp_dir = root / "models" / "stt" / ".tmp"
        self._status = SpeechAssetsStatus("ready", "Offline-STT bereit · Test", True, True, True)

    def ensure_storage(self) -> None:
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def status(self) -> SpeechAssetsStatus:
        return self._status


class FakeDevice:
    def __init__(self) -> None:
        self.audio_format = QAudioFormat()
        self.audio_format.setSampleRate(16_000)
        self.audio_format.setChannelCount(1)
        self.audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)

    def isNull(self) -> bool:
        return False

    def isFormatSupported(self, audio_format: QAudioFormat) -> bool:
        return audio_format.sampleRate() == 16_000

    def preferredFormat(self) -> QAudioFormat:
        return self.audio_format

    def description(self) -> str:
        return "Fake microphone"


class FakeAudioIODevice(QObject):
    readyRead = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._pending = bytearray()

    def feed(self, data: bytes) -> None:
        self._pending.extend(data)
        self.readyRead.emit()

    def readAll(self) -> bytes:
        data = bytes(self._pending)
        self._pending.clear()
        return data


class FakeAudioSource(QObject):
    stateChanged = Signal(object)

    def __init__(
        self,
        _device: object,
        _audio_format: object,
        _parent: object,
        audio_error: QAudio.Error = QAudio.Error.NoError,
    ) -> None:
        super().__init__()
        self.io_device = FakeAudioIODevice()
        self._stopped = False
        self._error = audio_error

    def start(self) -> FakeAudioIODevice:
        return self.io_device

    def stop(self) -> None:
        self._stopped = True

    def error(self) -> QAudio.Error:
        return self._error


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_for(app: QApplication, predicate: Callable[[], bool], timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def _make_widget(
    tmp_path: Path,
    *,
    recognizer: Callable[[Path, threading.Event], str],
    audio_error: QAudio.Error = QAudio.Error.NoError,
) -> tuple[SpeechInputWidget, FakeAudioSource, QPlainTextEdit, QApplication]:
    app = _application()
    composer = QPlainTextEdit()
    manager = FakeAssetManager(tmp_path)
    device = FakeDevice()
    sources: list[FakeAudioSource] = []

    def source_factory(selected_device: object, audio_format: object, parent: object) -> FakeAudioSource:
        source = FakeAudioSource(selected_device, audio_format, parent, audio_error)
        sources.append(source)
        return source

    widget = SpeechInputWidget(
        composer,
        asset_manager=manager,  # type: ignore[arg-type]
        audio_devices_provider=lambda: (device, (device,)),
        audio_source_factory=source_factory,
        recognizer=recognizer,
    )
    assert _wait_for(app, lambda: widget._active_job_kind is None)
    widget._apply_assets_status(manager.status())
    widget.start_recording()
    assert sources
    return widget, sources[0], composer, app


def test_push_to_talk_transcribes_on_worker_and_inserts_editable_preview(tmp_path: Path) -> None:
    main_thread = threading.get_ident()
    observed: dict[str, object] = {}

    def recognizer(path: Path, _cancel: threading.Event) -> str:
        observed["thread"] = threading.get_ident()
        with wave.open(str(path), "rb") as wav_file:
            observed["format"] = (
                wav_file.getnchannels(),
                wav_file.getsampwidth(),
                wav_file.getframerate(),
            )
        return "lokaler Entwurf"

    widget, source, composer, app = _make_widget(tmp_path, recognizer=recognizer)
    composer.setPlainText("Vorhandener Text")
    sent: list[str] = []
    send_button = QPushButton("Senden")
    send_button.clicked.connect(lambda: sent.append(composer.toPlainText()))
    previews: list[tuple[str, str]] = []
    widget.transcript_ready.connect(lambda run_id, text: previews.append((run_id, text)))
    try:
        source.io_device.feed(b"\x10\x27" * 1_600)
        widget.release_recording()
        assert _wait_for(app, lambda: bool(previews))
        assert composer.toPlainText() == "Vorhandener Text lokaler Entwurf"
        assert observed["thread"] != main_thread
        assert observed["format"] == (1, 2, 16_000)
        assert previews[0][0]
        assert previews[0][1] == "lokaler Entwurf"
        assert sent == []
        assert not list((tmp_path / "models" / "stt" / ".tmp").glob("audio-*.wav"))
    finally:
        widget.shutdown()
        widget.deleteLater()
        composer.deleteLater()
        send_button.deleteLater()
        app.processEvents()


def test_cancel_while_recording_discards_audio_without_recognizing(tmp_path: Path) -> None:
    recognizer_calls: list[str] = []

    def recognizer(_path: Path, _cancel: threading.Event) -> str:
        recognizer_calls.append("called")
        return "must not appear"

    widget, source, composer, app = _make_widget(tmp_path, recognizer=recognizer)
    try:
        source.io_device.feed(b"\x10\x27" * 1_600)
        widget.cancel()
        widget.release_recording()
        app.processEvents()
        assert source._stopped
        assert composer.toPlainText() == ""
        assert recognizer_calls == []
        assert "verworfen" in widget.status_label.text()
    finally:
        widget.shutdown()
        widget.deleteLater()
        composer.deleteLater()
        app.processEvents()


def test_microphone_open_error_is_visible_and_never_transcribes(tmp_path: Path) -> None:
    calls: list[str] = []

    def recognizer(_path: Path, _cancel: threading.Event) -> str:
        calls.append("called")
        return "must not appear"

    widget, source, composer, app = _make_widget(
        tmp_path,
        recognizer=recognizer,
        audio_error=QAudio.Error.OpenError,
    )
    try:
        assert not widget._recording
        assert source._stopped
        assert "Mikrofonzugriff fehlgeschlagen" in widget.status_label.text()
        assert calls == []
        assert composer.toPlainText() == ""
        assert not list((tmp_path / "models" / "stt" / ".tmp").glob("audio-*.wav"))
    finally:
        widget.shutdown()
        widget.deleteLater()
        composer.deleteLater()
        app.processEvents()


def test_failed_and_cancelled_recognition_leave_draft_unchanged_and_clean_audio(tmp_path: Path) -> None:
    def fail_recognition(_path: Path, _cancel: threading.Event) -> str:
        raise SpeechSetupError("recognition_failed")

    widget, source, composer, app = _make_widget(tmp_path, recognizer=fail_recognition)
    composer.setPlainText("draft")
    try:
        source.io_device.feed(b"\x10\x27" * 1_600)
        widget.release_recording()
        assert _wait_for(app, lambda: "fehlgeschlagen" in widget.status_label.text())
        assert composer.toPlainText() == "draft"
        assert not list((tmp_path / "models" / "stt" / ".tmp").glob("audio-*.wav"))
    finally:
        widget.shutdown()
        widget.deleteLater()
        composer.deleteLater()
        app.processEvents()

    started = threading.Event()

    def wait_for_cancel(_path: Path, cancel: threading.Event) -> str:
        started.set()
        assert cancel.wait(3)
        raise SpeechCancelled("recognition_cancelled")

    widget, source, composer, app = _make_widget(tmp_path / "cancel", recognizer=wait_for_cancel)
    composer.setPlainText("keep this draft")
    try:
        source.io_device.feed(b"\x10\x27" * 1_600)
        widget.release_recording()
        assert started.wait(2)
        widget.cancel()
        assert _wait_for(app, lambda: "abgebrochen" in widget.status_label.text())
        assert _wait_for(app, lambda: widget._active_job_kind is None)
        assert composer.toPlainText() == "keep this draft"
        assert not list((tmp_path / "cancel" / "models" / "stt" / ".tmp").glob("audio-*.wav"))
    finally:
        widget.shutdown()
        widget.deleteLater()
        composer.deleteLater()
        app.processEvents()
