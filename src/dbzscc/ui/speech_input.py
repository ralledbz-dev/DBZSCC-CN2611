"""Push-to-talk capture and transcript preview for the local chat composer."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSource, QMediaDevices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QWidget

from dbzscc.app.speech_input import (
    SpeechAssetManager,
    SpeechAssetsStatus,
    SpeechCancelled,
    SpeechSetupError,
    captured_pcm_to_wav,
)

MAX_RECORDING_SECONDS = 30
MAX_RECORDING_BYTES = 12 * 1024 * 1024


class SpeechTaskSignals(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)
    cancelled = Signal(str)
    progress = Signal(str, str, int, int)


class SpeechTask(QRunnable):
    """Run one correlated speech job away from the GUI thread."""

    def __init__(
        self,
        run_id: str,
        job_kind: str,
        function: Callable[[Callable[[str, int, int], None], threading.Event], object],
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.run_id = run_id
        self.job_kind = job_kind
        self.function = function
        self.cancel_event = cancel_event
        self.signals = SpeechTaskSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        progress = lambda asset, received, total: self.signals.progress.emit(
            self.run_id, asset, received, total
        )
        try:
            result = self.function(progress, self.cancel_event)
        except SpeechCancelled:
            self.signals.cancelled.emit(self.run_id)
        except SpeechSetupError as exc:
            code = str(exc) if str(exc) else "operation_failed"
            self.signals.failed.emit(self.run_id, code)
        except Exception:  # noqa: BLE001 -- never expose local paths or raw exceptions to the UI.
            self.signals.failed.emit(self.run_id, "operation_failed")
        else:
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.run_id)
            else:
                self.signals.completed.emit(self.run_id, result)


class SpeechInputWidget(QWidget):
    """A hold-to-record control that inserts only an editable draft, never sends it."""

    transcript_ready = Signal(str, str)

    def __init__(
        self,
        composer: QPlainTextEdit,
        *,
        asset_manager: SpeechAssetManager | None = None,
        audio_devices_provider: Callable[[], tuple[object, tuple[object, ...]]] | None = None,
        audio_source_factory: Callable[[object, object, QWidget], object] | None = None,
        recognizer: Callable[[Path, threading.Event], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.composer = composer
        self.asset_manager = asset_manager or SpeechAssetManager()
        self.audio_devices_provider = audio_devices_provider or self._qt_audio_devices
        self.audio_source_factory = audio_source_factory or self._qt_audio_source
        self.recognizer = recognizer or self.asset_manager.transcribe
        self._tasks: set[SpeechTask] = set()
        self._active_run_id: str | None = None
        self._active_job_kind: str | None = None
        self._cancel_event: threading.Event | None = None
        self._assets_status: SpeechAssetsStatus | None = None
        self._source = None
        self._audio_device = None
        self._audio_format = None
        self._audio_io = None
        self._audio_io_connected = False
        self._audio_chunks: bytearray | None = None
        self._recording_started_at = 0.0
        self._recording = False
        self._closing = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)
        self.hold_button = QPushButton("Mikrofon halten")
        self.hold_button.setToolTip(
            "Gedrückt halten zum lokalen Aufnehmen; beim Loslassen wird ein bearbeitbarer Entwurf eingefügt."
        )
        self.hold_button.pressed.connect(self.start_recording)
        self.hold_button.released.connect(self.release_recording)
        layout.addWidget(self.hold_button)
        self.cancel_button = QPushButton("Abbrechen")
        self.cancel_button.setToolTip("Aufnahme oder Erkennung verwerfen und temporäres Audio löschen.")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        layout.addWidget(self.cancel_button)
        self.prepare_button = QPushButton("Offline-STT laden / prüfen")
        self.prepare_button.setToolTip(
            "Lädt nur die fest gepinnte Runtime und das Modell von den offiziellen Upstream-Quellen in den Projektordner."
        )
        self.prepare_button.clicked.connect(self.prepare_assets)
        layout.addWidget(self.prepare_button)
        self.status_label = QLabel("Offline-STT wird lokal geprüft …")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Status der Offline-Spracheingabe")
        layout.addWidget(self.status_label, 1)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._update_recording_status)
        self._limit_timer = QTimer(self)
        self._limit_timer.setSingleShot(True)
        self._limit_timer.timeout.connect(self._recording_limit_reached)
        app = QGuiApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._application_state_changed)
            app.aboutToQuit.connect(self.shutdown)
        self._check_assets()

    @staticmethod
    def _qt_audio_devices() -> tuple[object, tuple[object, ...]]:
        return QMediaDevices.defaultAudioInput(), tuple(QMediaDevices.audioInputs())

    @staticmethod
    def _qt_audio_source(device: object, audio_format: object, parent: QWidget) -> object:
        return QAudioSource(device, audio_format, parent)

    def _check_assets(self) -> None:
        if self._closing or self._active_job_kind is not None:
            return
        self.hold_button.setEnabled(False)
        self.prepare_button.setEnabled(False)
        self.status_label.setText("Offline-STT: Prüfe Runtime und SHA-256 im Hintergrund …")
        run_id = uuid.uuid4().hex
        self._start_task(run_id, "status", lambda _progress, _cancel: self.asset_manager.status())

    def prepare_assets(self) -> None:
        if self._closing or self._active_job_kind is not None:
            return
        run_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        self._active_run_id = run_id
        self._cancel_event = cancel_event
        self._active_job_kind = "prepare"
        self.hold_button.setEnabled(False)
        self.prepare_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_label.setText(
            "Offline-STT wird ausschließlich im Projektordner geladen und geprüft …"
        )
        task = SpeechTask(
            run_id,
            "prepare",
            lambda progress, event: self.asset_manager.prepare(event, progress),
            cancel_event,
        )
        self._connect_task(task)
        QThreadPool.globalInstance().start(task)

    def start_recording(self) -> None:
        if self._closing or self._recording or self._active_job_kind is not None:
            return
        if self._assets_status is None or not self._assets_status.ready:
            self.status_label.setText(
                "Offline-STT ist nicht bereit. Zuerst „Offline-STT laden / prüfen“ auswählen."
            )
            return
        try:
            device, devices = self.audio_devices_provider()
            if device is None or (hasattr(device, "isNull") and device.isNull()):
                if not devices:
                    self.status_label.setText(
                        "Kein Mikrofon verfügbar. Prüfe Windows-Eingabegerät und Mikrofonzugriff."
                    )
                    return
                device = devices[0]
            audio_format = self._choose_capture_format(device)
            source = self.audio_source_factory(device, audio_format, self)
            source.stateChanged.connect(self._source_state_changed)
            self._source = source
            self._audio_device = device
            self._audio_format = audio_format
            self._audio_chunks = bytearray()
            self._recording_started_at = time.monotonic()
            self._recording = True
            self.hold_button.setText("Loslassen zum Erkennen")
            self.hold_button.setEnabled(True)
            self.hold_button.setStyleSheet("background: #b3261e; color: white; font-weight: 700;")
            self.cancel_button.setEnabled(True)
            name = self._safe_device_name(device)
            self.status_label.setText(f"● Aufnahme läuft · {name} · nur solange die Taste gehalten wird")
            self.status_label.setStyleSheet("color: #c62828; font-weight: 700;")
            self._elapsed_timer.start()
            self._limit_timer.start(MAX_RECORDING_SECONDS * 1000)
            audio_io = source.start()
            self._audio_io = audio_io
            if audio_io is None or source.error() != QAudio.Error.NoError:
                self._discard_recording("Mikrofonzugriff fehlgeschlagen. Prüfe Windows-Mikrofonberechtigungen und Eingabegerät.")
                return
            audio_io.readyRead.connect(self._read_audio)
            self._audio_io_connected = True
        except (RuntimeError, TypeError, ValueError):
            self._discard_recording(
                "Mikrofon oder Audioformat nicht verfügbar. Prüfe Windows-Eingabegerät und Mikrofonzugriff."
            )

    def release_recording(self) -> None:
        if not self._recording:
            return
        self._finish_recording()

    def cancel(self) -> None:
        if self._recording:
            self._stop_source()
            self._audio_chunks = None
            self._recording = False
            self._reset_capture_controls()
            self.status_label.setText("Aufnahme abgebrochen · Audio verworfen")
            return
        if self._active_job_kind is not None and self._cancel_event is not None:
            self._cancel_event.set()
            self.cancel_button.setEnabled(False)
            if self._active_job_kind == "prepare":
                self.status_label.setText("Abbruch läuft · Teil-Downloads bleiben für Fortsetzung erhalten …")
            else:
                self.status_label.setText("Erkennung wird abgebrochen · keine Vorschau wird eingefügt …")

    def shutdown(self) -> None:
        self._closing = True
        if self._recording:
            self._stop_source()
            self._audio_chunks = None
            self._recording = False
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._elapsed_timer.stop()
        self._limit_timer.stop()

    def _choose_capture_format(self, device: object) -> QAudioFormat:
        requested = QAudioFormat()
        requested.setSampleRate(16_000)
        requested.setChannelCount(1)
        requested.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if device.isFormatSupported(requested):
            return requested
        preferred = device.preferredFormat()
        sample_name = getattr(preferred.sampleFormat(), "name", "Unknown")
        if sample_name not in {"UInt8", "Int16", "Int32", "Float"}:
            raise ValueError("unsupported_capture_format")
        return preferred

    @staticmethod
    def _safe_device_name(device: object) -> str:
        try:
            name = str(device.description()).strip()
        except (AttributeError, RuntimeError):
            name = "Standardmikrofon"
        if not name:
            return "Standardmikrofon"
        return name[:80]

    @Slot()
    def _read_audio(self) -> None:
        if not self._recording or self._audio_io is None or self._audio_chunks is None:
            return
        try:
            chunk = bytes(self._audio_io.readAll())
        except (RuntimeError, TypeError):
            self._discard_recording("Audioeingabefehler · Aufnahme verworfen")
            return
        if not chunk:
            return
        remaining = MAX_RECORDING_BYTES - len(self._audio_chunks)
        if remaining > 0:
            self._audio_chunks.extend(chunk[:remaining])
        if len(chunk) > remaining or len(self._audio_chunks) >= MAX_RECORDING_BYTES:
            self._finish_recording(limit_reached=True)

    @Slot()
    def _finish_recording(self, *, limit_reached: bool = False) -> None:
        if not self._recording:
            return
        raw = bytes(self._audio_chunks or b"")
        audio_format = self._audio_format
        self._stop_source()
        self._audio_chunks = None
        self._recording = False
        self._reset_capture_controls()
        if not raw or audio_format is None:
            self.status_label.setText("Keine Audiodaten empfangen · Mikrofonstatus prüfen")
            return
        run_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        self._active_run_id = run_id
        self._cancel_event = cancel_event
        self._active_job_kind = "recognition"
        self.hold_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_label.setText(
            "Zeitlimit erreicht · lokale Erkennung läuft …"
            if limit_reached
            else "Aufnahme beendet · lokale Erkennung läuft …"
        )
        self._elapsed_timer.stop()
        self._limit_timer.stop()
        sample_rate = int(audio_format.sampleRate())
        channel_count = int(audio_format.channelCount())
        sample_name = getattr(audio_format.sampleFormat(), "name", "Unknown")

        def recognize(_progress: Callable[[str, int, int], None], event: threading.Event) -> str:
            audio_path: Path | None = None
            try:
                self.asset_manager.ensure_storage()
                audio_path = self.asset_manager.temp_dir / f"audio-{run_id}.wav"
                if event.is_set():
                    raise SpeechCancelled("recognition_cancelled")
                captured_pcm_to_wav(
                    raw,
                    sample_rate=sample_rate,
                    channel_count=channel_count,
                    sample_format=sample_name,
                    output_path=audio_path,
                )
                if event.is_set():
                    raise SpeechCancelled("recognition_cancelled")
                return self.recognizer(audio_path, event)
            finally:
                if audio_path is not None:
                    try:
                        audio_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        task = SpeechTask(run_id, "recognition", recognize, cancel_event)
        self._connect_task(task)
        QThreadPool.globalInstance().start(task)

    def _recording_limit_reached(self) -> None:
        if self._recording:
            self._finish_recording(limit_reached=True)

    def _stop_source(self) -> None:
        self._elapsed_timer.stop()
        self._limit_timer.stop()
        audio_io = self._audio_io
        source = self._source
        self._audio_io = None
        audio_io_connected = self._audio_io_connected
        self._audio_io_connected = False
        self._source = None
        self._audio_device = None
        self._audio_format = None
        if audio_io is not None and audio_io_connected:
            try:
                audio_io.readyRead.disconnect(self._read_audio)
            except (RuntimeError, TypeError):
                pass
        if source is not None:
            try:
                source.stop()
            except (RuntimeError, TypeError):
                pass
            source.deleteLater()

    def _discard_recording(self, message: str) -> None:
        self._stop_source()
        self._audio_chunks = None
        self._recording = False
        self._reset_capture_controls()
        self.status_label.setText(message)

    def _reset_capture_controls(self) -> None:
        self.hold_button.setText("Mikrofon halten")
        self.hold_button.setStyleSheet("")
        self.hold_button.setEnabled(bool(self._assets_status and self._assets_status.ready))
        self.cancel_button.setEnabled(self._active_job_kind is not None)
        self.status_label.setStyleSheet("")

    def _update_recording_status(self) -> None:
        if not self._recording:
            return
        elapsed = max(0, int(time.monotonic() - self._recording_started_at))
        self.status_label.setText(
            f"● Aufnahme läuft · {elapsed:02d} s / {MAX_RECORDING_SECONDS} s · beim Loslassen stoppen"
        )

    @Slot(object)
    def _source_state_changed(self, _state: object) -> None:
        if not self._recording or self._source is None:
            return
        try:
            audio_error = self._source.error()
        except RuntimeError:
            audio_error = QAudio.Error.OpenError
        if audio_error != QAudio.Error.NoError:
            self._discard_recording(
                "Mikrofonfehler oder Zugriff verweigert · Windows-Eingabegerät und Mikrofonberechtigungen prüfen"
            )

    @Slot(object)
    def _application_state_changed(self, state: object) -> None:
        if state != Qt.ApplicationState.ApplicationActive and (
            self._recording or self._active_job_kind == "recognition"
        ):
            self.cancel()

    def _start_task(
        self,
        run_id: str,
        job_kind: str,
        function: Callable[[Callable[[str, int, int], None], threading.Event], object],
    ) -> None:
        cancel_event = threading.Event()
        self._active_run_id = run_id
        self._active_job_kind = job_kind
        self._cancel_event = cancel_event
        task = SpeechTask(run_id, job_kind, function, cancel_event)
        self._connect_task(task)
        QThreadPool.globalInstance().start(task)

    def _connect_task(self, task: SpeechTask) -> None:
        self._tasks.add(task)
        task.signals.completed.connect(self._task_completed)
        task.signals.failed.connect(self._task_failed)
        task.signals.cancelled.connect(self._task_cancelled)
        task.signals.progress.connect(self._task_progress)

    @Slot(str, object)
    def _task_completed(self, run_id: str, result: object) -> None:
        self._discard_task(run_id)
        if self._closing or run_id != self._active_run_id:
            return
        job_kind = self._active_job_kind
        self._active_job_kind = None
        self._cancel_event = None
        self._active_run_id = None
        self.cancel_button.setEnabled(False)
        if job_kind == "status" or job_kind == "prepare":
            if isinstance(result, SpeechAssetsStatus):
                self._apply_assets_status(result)
            else:
                self._apply_assets_status(self.asset_manager.status())
            return
        text = " ".join(str(result).split()).strip()
        if not text:
            self.status_label.setText("Kein verständlicher Text erkannt · Composer unverändert")
            return
        self._insert_preview(text)
        self.status_label.setText("Vorschau eingefügt · bitte prüfen und selbst auf „Senden“ klicken")
        self.transcript_ready.emit(run_id, text)

    @Slot(str, str)
    def _task_failed(self, run_id: str, code: str) -> None:
        self._discard_task(run_id)
        if self._closing or run_id != self._active_run_id:
            return
        job_kind = self._active_job_kind
        self._active_job_kind = None
        self._cancel_event = None
        self._active_run_id = None
        self.cancel_button.setEnabled(False)
        if job_kind in {"status", "prepare"}:
            messages = {
                "runtime_unverified": "Runtime-Prüfung fehlgeschlagen · vorhanden, aber unverifiziert; wird nicht überschrieben.",
                "runtime_exists_unverified": "Runtime-Dateien vorhanden, aber nicht verifiziert · manuell prüfen oder quarantänisieren; kein automatischer Überschreibvorgang.",
                "model_unverified": "Modell-Hash stimmt nicht · die vorhandene Datei bleibt unangetastet.",
                "model_exists_unverified": "Modell-Datei vorhanden, aber nicht verifiziert · manuell prüfen oder quarantänisieren; kein Überschreiben.",
                "download_hash_mismatch": "Download-Prüfsumme stimmt nicht · Datei quarantänisiert; erneut laden.",
                "download_unavailable": "Upstream-Download nicht erreichbar · Netzwerk prüfen; später erneut versuchen.",
                "cancelled": "Abgebrochen · Teil-Downloads bleiben für Fortsetzung erhalten.",
            }
            self.status_label.setText(messages.get(code, "Offline-STT konnte nicht eingerichtet werden · Status und Netzwerk prüfen."))
            if job_kind == "status":
                self.prepare_button.setEnabled(True)
            self.hold_button.setEnabled(False)
            return
        messages = {
            "recognition_cancelled": "Erkennung abgebrochen · keine Vorschau eingefügt.",
            "recognition_timeout": "Lokale Erkennung dauerte zu lange · Audio wurde gelöscht.",
            "recognition_empty": "Kein verständlicher Text erkannt · Composer unverändert.",
            "assets_not_ready": "Offline-STT ist nicht mehr verfügbar · Modellstatus prüfen.",
            "audio_too_short": "Aufnahme zu kurz · beim Sprechen etwas länger halten.",
            "audio_format_unsupported": "Mikrofonformat nicht unterstützt · anderes Eingabegerät auswählen.",
            "recognition_failed": "Lokale Erkennung fehlgeschlagen · temporäres Audio wurde gelöscht.",
        }
        self.status_label.setText(messages.get(code, "Lokale Erkennung fehlgeschlagen · Audio wurde gelöscht; keine Vorschau eingefügt."))
        self.hold_button.setEnabled(bool(self._assets_status and self._assets_status.ready))

    @Slot(str)
    def _task_cancelled(self, run_id: str) -> None:
        self._discard_task(run_id)
        if self._closing or run_id != self._active_run_id:
            return
        job_kind = self._active_job_kind
        self._active_job_kind = None
        self._cancel_event = None
        self._active_run_id = None
        self.cancel_button.setEnabled(False)
        self.status_label.setText(
            "Abgebrochen · Teil-Downloads bleiben erhalten."
            if job_kind == "prepare"
            else "Erkennung abgebrochen · keine Vorschau eingefügt."
        )
        self.hold_button.setEnabled(bool(self._assets_status and self._assets_status.ready))
        self.prepare_button.setEnabled(True)

    @Slot(str, str, int, int)
    def _task_progress(self, run_id: str, asset: str, received: int, total: int) -> None:
        if self._closing or run_id != self._active_run_id:
            return
        percent = 0 if total <= 0 else min(100, int(received * 100 / total))
        if asset == "runtime":
            label = f"whisper.cpp-Runtime wird geladen · {percent}%"
        elif asset == "model":
            label = f"Mehrsprachiges Offline-Modell wird geladen · {percent}%"
        elif asset == "runtime_extract":
            label = "Verifiziere Runtime-Archiv und installiere projektlokal …"
        elif asset == "ready":
            label = "Offline-STT bereit · Hashes geprüft"
        else:
            label = "Offline-STT wird eingerichtet …"
        self.status_label.setText(label)

    def _apply_assets_status(self, status: SpeechAssetsStatus) -> None:
        self._assets_status = status
        self.status_label.setText(status.message)
        self.hold_button.setEnabled(status.ready and not self._closing)
        self.prepare_button.setEnabled(not self._closing)
        self.cancel_button.setEnabled(False)

    def _insert_preview(self, text: str) -> None:
        cursor = self.composer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        current = self.composer.toPlainText()
        if current and not current[-1].isspace():
            cursor.insertText(" ")
        cursor.insertText(text)
        self.composer.setTextCursor(cursor)
        self.composer.setFocus(Qt.FocusReason.OtherFocusReason)

    def _discard_task(self, run_id: str) -> None:
        for task in tuple(self._tasks):
            if task.run_id == run_id:
                self._tasks.discard(task)
