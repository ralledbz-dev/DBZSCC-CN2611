from __future__ import annotations

import array
import hashlib
import io
import json
import threading
import wave
import zipfile
from pathlib import Path
from typing import Self

import pytest

import dbzscc.app.speech_input as speech_module
from dbzscc.app.speech_input import (
    SpeechAssetManager,
    SpeechCancelled,
    SpeechSetupError,
    captured_pcm_to_wav,
)


def test_asset_status_reports_missing_without_creating_or_downloading(tmp_path: Path) -> None:
    manager = SpeechAssetManager(tmp_path)

    result = manager.status()

    assert result.state == "missing"
    assert not result.ready
    assert "Offline-STT fehlt" in result.message
    assert not manager.root.exists()


def test_runtime_status_rejects_non_object_manifest(tmp_path: Path) -> None:
    manager = SpeechAssetManager(tmp_path)
    manager.runtime_dir.mkdir(parents=True)
    manager.runtime_path.write_bytes(b"test-runtime")
    manager.runtime_manifest_path.write_text("[]", encoding="utf-8")

    status = manager.status()

    assert status.state == "error"
    assert not status.ready
    assert not status.runtime_valid


def test_capture_conversion_downmixes_and_resamples_to_whisper_wav(tmp_path: Path) -> None:
    samples = array.array("h")
    for _ in range(4_800):
        samples.extend((10_000, 10_000))
    raw = samples.tobytes()
    target = tmp_path / "bounded-temp" / "audio.wav"

    captured_pcm_to_wav(
        raw,
        sample_rate=48_000,
        channel_count=2,
        sample_format="Int16",
        output_path=target,
    )

    with wave.open(str(target), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnframes() == 1_600
        converted = array.array("h")
        converted.frombytes(wav_file.readframes(wav_file.getnframes()))
    assert sum(abs(value) for value in converted[100:-100]) / len(converted[100:-100]) > 9_000


def test_capture_conversion_handles_non_finite_float_samples(tmp_path: Path) -> None:
    samples = array.array("f", [float("nan")] * 1_000)
    target = tmp_path / "audio.wav"

    captured_pcm_to_wav(
        samples.tobytes(),
        sample_rate=16_000,
        channel_count=1,
        sample_format="Float",
        output_path=target,
    )

    with wave.open(str(target), "rb") as wav_file:
        converted = array.array("h")
        converted.frombytes(wav_file.readframes(wav_file.getnframes()))
    assert set(converted) == {0}


def test_model_and_runtime_status_require_matching_hashes(tmp_path: Path, monkeypatch) -> None:
    model = tmp_path / "models" / "stt" / "ggml-base-q5_1.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"test-model")
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    monkeypatch.setattr(speech_module, "STT_MODEL_SIZE", model.stat().st_size)
    monkeypatch.setattr(speech_module, "STT_MODEL_SHA256", model_hash)
    monkeypatch.setattr(speech_module, "STT_RUNTIME_SHA256", "r" * 64)

    manager = SpeechAssetManager(tmp_path)
    manager.runtime_dir.mkdir(parents=True)
    executable = manager.runtime_path
    executable.write_bytes(b"verified-test-runtime")
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    manager.runtime_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release": speech_module.STT_RUNTIME_VERSION,
                "archive_sha256": "r" * 64,
                "files": {"whisper-cli.exe": executable_hash},
            }
        ),
        encoding="utf-8",
    )

    assert manager.status().ready
    model.write_bytes(b"tampered")
    status = manager.status()
    assert status.state == "error"
    assert not status.ready


def test_verified_download_resumes_after_cancel_and_never_overwrites(tmp_path: Path, monkeypatch) -> None:
    payload = bytes(range(251)) * 20_000
    expected_hash = hashlib.sha256(payload).hexdigest()
    received_ranges: list[str | None] = []

    class FakeResponse:
        def __init__(self, offset: int) -> None:
            self.status = 206 if offset else 200
            self.headers = (
                {"Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}"}
                if offset
                else {}
            )
            self._body = io.BytesIO(payload[offset:])

        def getcode(self) -> int:
            return self.status

        def read(self, size: int) -> bytes:
            return self._body.read(size)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            self._body.close()

    def fake_urlopen(request, timeout: int):
        assert timeout == 25
        requested = request.headers.get("Range")
        received_ranges.append(requested)
        offset = int(requested.removeprefix("bytes=").split("-", 1)[0]) if requested else 0
        return FakeResponse(offset)

    monkeypatch.setattr(speech_module, "urlopen", fake_urlopen)
    manager = SpeechAssetManager(tmp_path)
    partial = manager.download_dir / "fixture.bin.part"
    cancel_event = threading.Event()

    def cancel_after_first_chunk(_asset: str, received: int, _total: int) -> None:
        if received:
            cancel_event.set()

    with pytest.raises(SpeechCancelled):
        manager._download_verified(
            "https://example.test/asset",
            partial,
            len(payload),
            expected_hash,
            "fixture",
            cancel_event,
            cancel_after_first_chunk,
        )
    assert 0 < partial.stat().st_size < len(payload)
    first_size = partial.stat().st_size

    cancel_event.clear()
    verified = manager._download_verified(
        "https://example.test/asset",
        partial,
        len(payload),
        expected_hash,
        "fixture",
        cancel_event,
        None,
    )
    assert verified == partial
    assert hashlib.sha256(partial.read_bytes()).hexdigest() == expected_hash
    assert received_ranges[0] is None
    assert received_ranges[1] == f"bytes={first_size}-"

    destination = manager.root / "fixture.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"preserve-existing")
    with pytest.raises(SpeechSetupError, match="destination_exists"):
        manager._rename_without_overwrite(verified, destination)
    assert destination.read_bytes() == b"preserve-existing"


def test_runtime_archive_rejects_path_traversal_and_cleans_its_staging_dir(tmp_path: Path) -> None:
    archive_path = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.txt", "do not extract")
    manager = SpeechAssetManager(tmp_path / "project")
    manager.root.mkdir(parents=True)

    with pytest.raises(SpeechSetupError, match="runtime_archive_path_invalid"):
        manager._install_runtime(archive_path, threading.Event(), None)

    assert not (manager.root.parent / "escape.txt").exists()
    assert not list(manager.root.glob(".runtime-stage-*"))
