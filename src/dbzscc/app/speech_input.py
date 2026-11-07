"""Pinned, project-local assets and offline transcription for speech input."""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STT_RUNTIME_VERSION = "b5454"
STT_RUNTIME_ARCHIVE = "whisper-bin-x64.zip"
STT_RUNTIME_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/"
    "b5454/whisper-bin-x64.zip"
)
STT_RUNTIME_SIZE = 8_928_640
STT_RUNTIME_SHA256 = "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb"
STT_MODEL_NAME = "ggml-base-q5_1.bin"
STT_MODEL_REVISION = "f281eb45af861ab5e5297d23694b7d46e090c02c"
STT_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
    f"{STT_MODEL_REVISION}/{STT_MODEL_NAME}?download=true"
)
STT_MODEL_SIZE = 59_707_625
STT_MODEL_SHA256 = "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898"
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_RUNTIME_UNPACKED_BYTES = 128 * 1024 * 1024

ProgressCallback = Callable[[str, int, int], None]


class SpeechSetupError(RuntimeError):
    """A safe, user-displayable STT setup or recognition failure code."""


class SpeechCancelled(SpeechSetupError):
    """The user cancelled a speech setup or recognition job."""


@dataclass(frozen=True)
class SpeechAssetsStatus:
    state: str
    message: str
    ready: bool
    model_valid: bool
    runtime_valid: bool


class SpeechAssetManager:
    """Manage only the app-owned ``models/stt`` directory under a project root."""

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[3]).resolve()
        self.root = self.project_root / "models" / "stt"
        self.model_path = self.root / STT_MODEL_NAME
        self.runtime_dir = self.root / "runtime"
        self.runtime_path = self.runtime_dir / "whisper-cli.exe"
        self.download_dir = self.root / ".downloads"
        self.temp_dir = self.root / ".tmp"
        self.runtime_manifest_path = self.runtime_dir / "install_manifest.json"

    def status(self) -> SpeechAssetsStatus:
        if not self._storage_paths_safe():
            return SpeechAssetsStatus(
                "error",
                "STT-Speicherpfad verweist außerhalb des Projektordners; es wird nichts geschrieben.",
                False,
                False,
                False,
            )
        model_state = self._model_state()
        runtime_state = self._runtime_state()
        if model_state == "invalid" or runtime_state == "invalid":
            return SpeechAssetsStatus(
                "error",
                "Eine vorhandene STT-Datei ist ungültig oder nicht verifiziert; sie wird nicht überschrieben. Prüfe die lokale STT-Installation.",
                False,
                model_state == "valid",
                runtime_state == "valid",
            )
        if model_state != "valid" and runtime_state != "valid":
            return SpeechAssetsStatus(
                "missing",
                "Offline-STT fehlt. „Offline-STT laden / prüfen“ lädt Runtime und deutsches Basismodell lokal herunter.",
                False,
                False,
                False,
            )
        if model_state != "valid":
            return SpeechAssetsStatus(
                "missing",
                "Offline-Modell fehlt. „Offline-STT laden / prüfen“ lädt das mehrsprachige Modell lokal herunter.",
                False,
                False,
                True,
            )
        if runtime_state != "valid":
            return SpeechAssetsStatus(
                "missing",
                "Lokale whisper.cpp-CPU-Runtime fehlt. „Offline-STT laden / prüfen“ richtet sie projektlokal ein.",
                False,
                True,
                False,
            )
        return SpeechAssetsStatus(
            "ready",
            "Offline-STT bereit · whisper.cpp CPU · Deutsch",
            True,
            True,
            True,
        )

    def prepare(
        self,
        cancel_event: threading.Event,
        progress: ProgressCallback | None = None,
    ) -> SpeechAssetsStatus:
        """Install missing pinned assets; existing files are verified, never replaced."""
        self.ensure_storage()
        self.root.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._check_cancel(cancel_event)

        runtime_state = self._runtime_state()
        if runtime_state == "invalid":
            raise SpeechSetupError("runtime_unverified")
        if runtime_state != "valid":
            if self.runtime_dir.exists():
                raise SpeechSetupError("runtime_exists_unverified")
            archive_part = self.download_dir / f"{STT_RUNTIME_ARCHIVE}.part"
            verified_archive = self._download_verified(
                STT_RUNTIME_URL,
                archive_part,
                STT_RUNTIME_SIZE,
                STT_RUNTIME_SHA256,
                "runtime",
                cancel_event,
                progress,
            )
            self._install_runtime(verified_archive, cancel_event, progress)
            try:
                verified_archive.unlink()
            except OSError:
                pass

        self._check_cancel(cancel_event)
        model_state = self._model_state()
        if model_state == "invalid":
            raise SpeechSetupError("model_unverified")
        if model_state != "valid":
            if self.model_path.exists():
                raise SpeechSetupError("model_exists_unverified")
            model_part = self.download_dir / f"{STT_MODEL_NAME}.part"
            verified_model = self._download_verified(
                STT_MODEL_URL,
                model_part,
                STT_MODEL_SIZE,
                STT_MODEL_SHA256,
                "model",
                cancel_event,
                progress,
            )
            self._check_cancel(cancel_event)
            self._rename_without_overwrite(verified_model, self.model_path)

        final_status = self.status()
        if not final_status.ready:
            raise SpeechSetupError("installed_assets_not_ready")
        self._notify(progress, "ready", 1, 1)
        return final_status

    def transcribe(
        self,
        wav_path: Path,
        cancel_event: threading.Event,
    ) -> str:
        """Transcribe an app-created WAV with the verified local CPU executable only."""
        if not self._storage_paths_safe():
            raise SpeechSetupError("storage_path_not_local")
        if self.status().ready is not True:
            raise SpeechSetupError("assets_not_ready")
        try:
            wav_resolved = wav_path.resolve(strict=True)
            temp_resolved = self.temp_dir.resolve(strict=True)
            if not wav_resolved.is_relative_to(temp_resolved) or wav_resolved.suffix.lower() != ".wav":
                raise SpeechSetupError("audio_path_invalid")
        except OSError as exc:
            raise SpeechSetupError("audio_unavailable") from exc
        if wav_resolved.stat().st_size > 16 * 1024 * 1024:
            raise SpeechSetupError("audio_too_large")
        self._check_cancel(cancel_event)

        output_base = self.temp_dir / f"transcript-{uuid.uuid4().hex}"
        command = [
            str(self.runtime_path),
            "-m",
            str(self.model_path),
            "-f",
            str(wav_resolved),
            "-l",
            "de",
            "-ng",
            "-t",
            str(max(1, min(4, os.cpu_count() or 1))),
            "-nt",
            "-otxt",
            "-of",
            str(output_base),
        ]
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.runtime_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + 120
            while process.poll() is None:
                if cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise SpeechCancelled("recognition_cancelled")
                if time.monotonic() >= deadline:
                    process.kill()
                    raise SpeechSetupError("recognition_timeout")
                time.sleep(0.05)
            stdout, _stderr = process.communicate(timeout=2)
            self._check_cancel(cancel_event)
            text_path = output_base.with_suffix(".txt")
            if process.returncode != 0:
                raise SpeechSetupError("recognition_failed")
            if text_path.is_file():
                text = text_path.read_text(encoding="utf-8", errors="replace")
            else:
                text = _transcript_from_stdout(stdout)
            text = " ".join(text.split()).strip()
            if not text:
                raise SpeechSetupError("recognition_empty")
            if len(text) > 20_000:
                text = text[:20_000].rstrip()
            return text
        except SpeechSetupError:
            raise
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise SpeechSetupError("recognition_failed") from exc
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.SubprocessError:
                    pass
            for suffix in (".txt", ".json", ".srt", ".vtt", ".tsv"):
                try:
                    output_base.with_suffix(suffix).unlink(missing_ok=True)
                except OSError:
                    pass

    def _model_state(self) -> str:
        if self._path_is_link(self.model_path):
            return "invalid"
        if not self.model_path.exists():
            return "missing"
        if not self.model_path.is_file():
            return "invalid"
        try:
            if self.model_path.stat().st_size != STT_MODEL_SIZE:
                return "invalid"
            return "valid" if _sha256_file(self.model_path) == STT_MODEL_SHA256 else "invalid"
        except OSError:
            return "invalid"

    def _runtime_state(self) -> str:
        if self._path_is_link(self.runtime_dir) or self._path_is_link(self.runtime_path):
            return "invalid"
        if not self.runtime_dir.exists():
            return "missing"
        if not self.runtime_path.is_file() or self._path_is_link(self.runtime_manifest_path):
            return "invalid"
        try:
            manifest = json.loads(self.runtime_manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return "invalid"
        if not isinstance(manifest, dict):
            return "invalid"
        if (
            manifest.get("schema_version") != 1
            or manifest.get("release") != STT_RUNTIME_VERSION
            or manifest.get("archive_sha256") != STT_RUNTIME_SHA256
            or not isinstance(manifest.get("files"), dict)
            or "whisper-cli.exe" not in manifest["files"]
        ):
            return "invalid"
        try:
            for relative, digest in manifest["files"].items():
                candidate_path = self.runtime_dir / relative
                if self._path_is_link(candidate_path):
                    return "invalid"
                candidate = candidate_path.resolve(strict=True)
                if not candidate.is_relative_to(self.runtime_dir.resolve(strict=True)):
                    return "invalid"
                if not candidate.is_file() or _sha256_file(candidate) != digest:
                    return "invalid"
            return "valid"
        except (OSError, ValueError, TypeError):
            return "invalid"

    def _download_verified(
        self,
        url: str,
        part_path: Path,
        expected_size: int,
        expected_sha256: str,
        asset_name: str,
        cancel_event: threading.Event,
        progress: ProgressCallback | None,
    ) -> Path:
        if not self._storage_paths_safe():
            raise SpeechSetupError("storage_path_not_local")
        self._check_cancel(cancel_event)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        if part_path.is_symlink() or (part_path.exists() and not part_path.is_file()):
            raise SpeechSetupError("download_partial_invalid")
        received = part_path.stat().st_size if part_path.exists() else 0
        if received > expected_size:
            self._quarantine_partial(part_path)
            received = 0
        if received == expected_size:
            if _sha256_file(part_path) == expected_sha256:
                self._notify(progress, asset_name, received, expected_size)
                return part_path
            self._quarantine_partial(part_path)
            received = 0

        headers = {"User-Agent": "DBZSCC2611-offline-speech/1"}
        if received:
            headers["Range"] = f"bytes={received}-"
        request = Request(url, headers=headers)
        try:
            response = urlopen(request, timeout=25)
        except (HTTPError, URLError, OSError) as exc:
            raise SpeechSetupError("download_unavailable") from exc
        with response:
            status = getattr(response, "status", response.getcode())
            if received and status == 206:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {received}-"):
                    raise SpeechSetupError("download_range_invalid")
                mode = "ab"
            elif status == 200:
                received = 0
                mode = "wb"
            else:
                raise SpeechSetupError("download_http_error")
            try:
                with part_path.open(mode) as stream:
                    while True:
                        self._check_cancel(cancel_event)
                        chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        if received + len(chunk) > expected_size:
                            raise SpeechSetupError("download_size_invalid")
                        stream.write(chunk)
                        received += len(chunk)
                        self._notify(progress, asset_name, received, expected_size)
            except SpeechCancelled:
                raise
            except OSError as exc:
                raise SpeechSetupError("download_write_failed") from exc

        self._check_cancel(cancel_event)
        if received != expected_size or part_path.stat().st_size != expected_size:
            raise SpeechSetupError("download_incomplete")
        if _sha256_file(part_path) != expected_sha256:
            self._quarantine_partial(part_path)
            raise SpeechSetupError("download_hash_mismatch")
        return part_path

    def _install_runtime(
        self,
        archive_path: Path,
        cancel_event: threading.Event,
        progress: ProgressCallback | None,
    ) -> None:
        if self.runtime_dir.exists():
            raise SpeechSetupError("runtime_exists_unverified")
        stage = self.root / f".runtime-stage-{uuid.uuid4().hex}"
        stage.mkdir(parents=False, exist_ok=False)
        try:
            total_unpacked = 0
            seen: set[str] = set()
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    self._check_cancel(cancel_event)
                    posix_name = PurePosixPath(info.filename)
                    windows_name = PureWindowsPath(info.filename)
                    if (
                        posix_name.is_absolute()
                        or windows_name.is_absolute()
                        or windows_name.drive
                        or ".." in posix_name.parts
                        or ".." in windows_name.parts
                        or not posix_name.parts
                    ):
                        raise SpeechSetupError("runtime_archive_path_invalid")
                    relative = Path(*posix_name.parts)
                    key = relative.as_posix().casefold()
                    if key in seen:
                        raise SpeechSetupError("runtime_archive_duplicate_path")
                    seen.add(key)
                    if info.is_dir():
                        (stage / relative).mkdir(parents=True, exist_ok=True)
                        continue
                    total_unpacked += info.file_size
                    if total_unpacked > MAX_RUNTIME_UNPACKED_BYTES or info.file_size > MAX_RUNTIME_UNPACKED_BYTES:
                        raise SpeechSetupError("runtime_archive_too_large")
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if mode and (mode & 0o170000) == 0o120000:
                        raise SpeechSetupError("runtime_archive_link_invalid")
                    destination = stage / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("xb") as target:
                        shutil.copyfileobj(source, target, length=DOWNLOAD_CHUNK_BYTES)
                    self._notify(progress, "runtime_extract", total_unpacked, MAX_RUNTIME_UNPACKED_BYTES)
            cli = stage / "whisper-cli.exe"
            if not cli.is_file():
                candidates = list(stage.rglob("whisper-cli.exe"))
                if len(candidates) != 1:
                    raise SpeechSetupError("runtime_executable_missing")
                nested = candidates[0]
                for child in list(nested.parent.iterdir()):
                    child.rename(stage / child.name)
                shutil.rmtree(nested.parent, ignore_errors=True)
            cli = stage / "whisper-cli.exe"
            if not cli.is_file():
                raise SpeechSetupError("runtime_executable_missing")
            hashes: dict[str, str] = {}
            for candidate in sorted(stage.iterdir()):
                if candidate.is_file() and candidate.suffix.lower() in {".exe", ".dll"}:
                    hashes[candidate.name] = _sha256_file(candidate)
            if "whisper-cli.exe" not in hashes:
                raise SpeechSetupError("runtime_executable_missing")
            manifest = {
                "schema_version": 1,
                "release": STT_RUNTIME_VERSION,
                "archive": STT_RUNTIME_ARCHIVE,
                "archive_url": STT_RUNTIME_URL,
                "archive_size_bytes": STT_RUNTIME_SIZE,
                "archive_sha256": STT_RUNTIME_SHA256,
                "files": hashes,
            }
            manifest_path = stage / "install_manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._check_cancel(cancel_event)
            self._rename_without_overwrite(stage, self.runtime_dir)
        except SpeechCancelled:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(stage, ignore_errors=True)
            raise SpeechSetupError("runtime_install_failed") from exc
        except SpeechSetupError:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    @staticmethod
    def _rename_without_overwrite(source: Path, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise SpeechSetupError("destination_exists")
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise SpeechSetupError("destination_exists") from exc
        except OSError as exc:
            if destination.exists() or destination.is_symlink():
                raise SpeechSetupError("destination_exists") from exc
            raise SpeechSetupError("install_rename_failed") from exc

    @staticmethod
    def _quarantine_partial(path: Path) -> None:
        if not path.exists():
            return
        quarantine = path.with_name(f"{path.name}.invalid-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(path, quarantine)
        except OSError as exc:
            raise SpeechSetupError("download_partial_unusable") from exc

    @staticmethod
    def _check_cancel(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise SpeechCancelled("cancelled")

    def ensure_storage(self) -> None:
        """Create only private STT subdirectories after rejecting reparse points."""
        if not self._storage_paths_safe():
            raise SpeechSetupError("storage_path_not_local")
        self.root.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def _storage_paths_safe(self) -> bool:
        return not any(
            self._path_is_link(path)
            for path in (
                self.project_root / "models",
                self.root,
                self.download_dir,
                self.temp_dir,
            )
        )

    @staticmethod
    def _path_is_link(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False

    @staticmethod
    def _notify(
        callback: ProgressCallback | None,
        asset: str,
        received: int,
        total: int,
    ) -> None:
        if callback is not None:
            callback(asset, received, total)


def captured_pcm_to_wav(
    raw: bytes,
    *,
    sample_rate: int,
    channel_count: int,
    sample_format: str,
    output_path: Path,
) -> None:
    """Convert Qt PCM to mono 16-kHz signed 16-bit WAV using a band-limited resampler."""
    if not raw or sample_rate < 8_000 or sample_rate > 192_000:
        raise SpeechSetupError("audio_format_invalid")
    if not 1 <= channel_count <= 8:
        raise SpeechSetupError("audio_format_invalid")
    mono = _decode_mono_samples(raw, channel_count, sample_format)
    if len(mono) < max(1, sample_rate // 20):
        raise SpeechSetupError("audio_too_short")
    target = _resample(mono, sample_rate, 16_000)
    pcm = bytearray(len(target) * 2)
    for index, value in enumerate(target):
        clipped = max(-32768, min(32767, round(value)))
        pcm[index * 2 : index * 2 + 2] = clipped.to_bytes(2, "little", signed=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(pcm)


def _decode_mono_samples(raw: bytes, channel_count: int, sample_format: str) -> list[float]:
    kind = sample_format.casefold()
    if "int16" in kind:
        values = array.array("h")
        values.frombytes(raw[: len(raw) - len(raw) % 2])
        if sys.byteorder != "little":
            values.byteswap()
        scale = 1.0
    elif "int32" in kind:
        values = array.array("i")
        values.frombytes(raw[: len(raw) - len(raw) % 4])
        if values.itemsize != 4:
            raise SpeechSetupError("audio_format_unsupported")
        if sys.byteorder != "little":
            values.byteswap()
        scale = 1.0 / 65536.0
    elif "float" in kind:
        values = array.array("f")
        values.frombytes(raw[: len(raw) - len(raw) % 4])
        if values.itemsize != 4:
            raise SpeechSetupError("audio_format_unsupported")
        if sys.byteorder != "little":
            values.byteswap()
        scale = 32767.0
    elif "uint8" in kind:
        values = raw
        scale = 257.0
    else:
        raise SpeechSetupError("audio_format_unsupported")
    frame_count = len(values) // channel_count
    if frame_count == 0:
        raise SpeechSetupError("audio_too_short")
    mono: list[float] = []
    for frame in range(frame_count):
        offset = frame * channel_count
        if "uint8" in kind:
            value = sum(values[offset + channel] - 128 for channel in range(channel_count)) / channel_count
        else:
            value = sum(float(values[offset + channel]) for channel in range(channel_count)) / channel_count
        if not math.isfinite(value):
            value = 0.0
        mono.append(value * scale)
    return mono


def _resample(samples: list[float], source_rate: int, target_rate: int) -> list[int]:
    if source_rate == target_rate:
        return [max(-32768, min(32767, round(sample))) for sample in samples]
    count = max(1, round(len(samples) * target_rate / source_rate))
    cutoff = min(1.0, target_rate / source_rate)
    radius = 8
    output: list[int] = []
    for target_index in range(count):
        position = target_index * source_rate / target_rate
        center = int(position)
        weighted = 0.0
        weight_sum = 0.0
        for source_index in range(max(0, center - radius + 1), min(len(samples), center + radius + 1)):
            distance = source_index - position
            if abs(distance) >= radius:
                continue
            argument = cutoff * distance
            sinc = 1.0 if argument == 0.0 else math.sin(math.pi * argument) / (math.pi * argument)
            window = 0.5 + 0.5 * math.cos(math.pi * distance / radius)
            weight = cutoff * sinc * window
            weighted += samples[source_index] * weight
            weight_sum += weight
        value = weighted / weight_sum if weight_sum else samples[min(center, len(samples) - 1)]
        output.append(max(-32768, min(32767, round(value))))
    return output


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _transcript_from_stdout(output: str) -> str:
    segments: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "-->" in stripped and "]" in stripped:
            stripped = stripped.rsplit("]", 1)[-1].strip()
        if stripped and not stripped.startswith(("whisper_", "ggml_", "main:", "system_info:")):
            segments.append(stripped)
    return " ".join(segments)
