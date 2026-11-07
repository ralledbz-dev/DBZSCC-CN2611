"""App-side bridge to the fixed, offline-only local RAG worker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from .config import application_data_directory

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESULTS = 5
MAX_QUERY_CHARS = 2_000
MODEL_ID = "intfloat/multilingual-e5-small"
CPU_PROVIDER = "CPUExecutionProvider"


class LocalRagWorkerError(RuntimeError):
    """A safe, user-facing error from the fixed local RAG worker."""


@dataclass(frozen=True)
class LocalRagResourceObservation:
    status: str
    cpu_time_seconds: float | None
    peak_working_set_bytes: int | None
    peak_commit_bytes: int | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "cpu_time_seconds": self.cpu_time_seconds,
            "peak_working_set_bytes": self.peak_working_set_bytes,
            "peak_commit_bytes": self.peak_commit_bytes,
            "source": self.source,
        }

    def status_text(self) -> str:
        if self.status != "measured":
            return "RAG-Worker-CPU-Zeit und RAM-Spitzen unbekannt."
        mib = 1024 * 1024
        return (
            f"RAG-Worker-Prozess: CPU-Zeit {self.cpu_time_seconds:.3f} s; "
            f"Peak-Working-Set {self.peak_working_set_bytes / mib:.1f} MiB; "
            f"Peak-Commit {self.peak_commit_bytes / mib:.1f} MiB (Windows)."
        )


_UNKNOWN_RAG_RESOURCE_OBSERVATION = LocalRagResourceObservation(
    status="unknown",
    cpu_time_seconds=None,
    peak_working_set_bytes=None,
    peak_commit_bytes=None,
    source="unavailable",
)


@dataclass(frozen=True)
class _WorkerLayout:
    root: Path
    python_relative_path: str


@dataclass(frozen=True)
class LocalRagIndexSummary:
    project_id: str
    chunk_count: int
    included_file_count: int
    included_bytes: int
    excluded_counts: tuple[tuple[str, int], ...]
    unchanged: bool
    model_id: str
    execution_provider: str
    resource_observation: LocalRagResourceObservation = _UNKNOWN_RAG_RESOURCE_OBSERVATION


@dataclass(frozen=True)
class RetrievedProjectChunk:
    relative_path: str
    format: str
    symbol: str
    line_start: int
    line_end: int
    text: str


class LocalRagWorkerClient:
    """Invoke only a fixed local RAG worker from checkout or bundle, never a shell command."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        candidate = (
            Path(database_path).expanduser()
            if database_path is not None
            else application_data_directory() / "rag-index.sqlite3"
        )
        if not candidate.is_absolute() or _is_unc_path(str(candidate)):
            raise ValueError("Die lokale RAG-Datenbank benötigt einen absoluten lokalen Pfad.")
        self._database_path = candidate
        self._last_resource_observation = _UNKNOWN_RAG_RESOURCE_OBSERVATION

    def index_workspace(self, selected_project_root: str | Path) -> LocalRagIndexSummary:
        """Index only the explicitly passed project root; no startup scan is performed."""
        root = _validate_selected_root(selected_project_root)
        project_id = _project_id(root)
        self._last_resource_observation = _UNKNOWN_RAG_RESOURCE_OBSERVATION
        layout = _resolve_worker_layout()
        payload = self._invoke(
            {
                "operation": "index",
                "project_root": str(root),
                "project_id": project_id,
                "database_path": str(self._database_path),
                "manifest_path": str(
                    self._trusted_asset(
                        layout.root, "models/rag/multilingual-e5-small/manifest.json"
                    )
                ),
            },
            timeout_seconds=600,
            layout=layout,
        )
        return _parse_index_summary(payload, project_id)

    def search_workspace(
        self, selected_project_root: str | Path, query: str
    ) -> tuple[RetrievedProjectChunk, ...]:
        """Retrieve bounded excerpts for one explicitly selected project only."""
        root = _validate_selected_root(selected_project_root)
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError("Die lokale RAG-Suche benötigt 1 bis 2000 Zeichen.")
        project_id = _project_id(root)
        self._last_resource_observation = _UNKNOWN_RAG_RESOURCE_OBSERVATION
        layout = _resolve_worker_layout()
        payload = self._invoke(
            {
                "operation": "search",
                "project_root": str(root),
                "project_id": project_id,
                "database_path": str(self._database_path),
                "manifest_path": str(
                    self._trusted_asset(
                        layout.root, "models/rag/multilingual-e5-small/manifest.json"
                    )
                ),
                "query": query,
            },
            timeout_seconds=120,
            layout=layout,
        )
        return _parse_search_results(payload, project_id)

    @property
    def last_resource_observation(self) -> LocalRagResourceObservation:
        return self._last_resource_observation

    def _invoke(
        self,
        request: dict[str, Any],
        *,
        timeout_seconds: int,
        layout: _WorkerLayout,
    ) -> dict[str, Any]:
        python = self._trusted_asset(layout.root, layout.python_relative_path)
        worker = self._trusted_asset(layout.root, "src/dbzscc/rag/worker.py")
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise LocalRagWorkerError("Der lokale RAG-Auftrag ist zu groß.")

        try:
            completed = subprocess.run(
                [str(python), "-I", str(worker)],
                input=body,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
                cwd=str(layout.root),
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalRagWorkerError(
                "Die lokale RAG-Verarbeitung überschritt ihr Zeitlimit; der Chat wurde nicht gesendet."
            ) from exc
        except OSError as exc:
            raise LocalRagWorkerError(
                "Die projektlokale CPU-RAG-Laufzeit ist nicht verfügbar."
            ) from exc

        if len(completed.stdout.encode("utf-8", errors="replace")) > 128 * 1024:
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte eine zu große Antwort.")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LocalRagWorkerError(
                "Die lokale RAG-Laufzeit lieferte keine gültige Antwort."
            ) from exc
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ein ungültiges Ergebnis.")
        if response["ok"] is False:
            message = response.get("error")
            if isinstance(message, str) and message in _SAFE_WORKER_ERRORS:
                raise LocalRagWorkerError(message)
            raise LocalRagWorkerError("Der lokale RAG-Auftrag ist fehlgeschlagen.")
        if completed.returncode != 0 or set(response) != {"ok", "result"}:
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ein ungültiges Ergebnis.")
        result = response["result"]
        if not isinstance(result, dict):
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ein ungültiges Ergebnis.")
        self._last_resource_observation = _parse_resource_observation(
            result.get("resource_observation")
        )
        return result

    def _trusted_asset(self, root: Path, relative_path: str) -> Path:
        current = root
        try:
            root_metadata = root.lstat()
            if _is_link_or_reparse_point(root_metadata) or not stat.S_ISDIR(
                root_metadata.st_mode
            ):
                raise LocalRagWorkerError(
                    "Projektlokale RAG-Dateien liegen außerhalb des Anwendungsprojekts."
                )
            trusted_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise LocalRagWorkerError("Projektlokale RAG-Dateien oder Laufzeit fehlen.") from exc
        for part in PurePath(relative_path).parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise LocalRagWorkerError(
                    "Projektlokale RAG-Dateien oder Laufzeit fehlen."
                ) from exc
            if _is_link_or_reparse_point(metadata):
                raise LocalRagWorkerError(
                    "Projektlokale RAG-Dateien dürfen keine Links oder Reparse-Points sein."
                )
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(trusted_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LocalRagWorkerError(
                "Projektlokale RAG-Dateien liegen außerhalb des Anwendungsprojekts."
            ) from exc
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise LocalRagWorkerError("Eine projektlokale RAG-Datei ist keine reguläre Datei.")
        return resolved


def _resolve_worker_layout() -> _WorkerLayout:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False):
        if not isinstance(frozen_root, (str, os.PathLike)):
            raise LocalRagWorkerError(
                "Die lokale RAG-Laufzeit ist in diesem Paket nicht konfiguriert."
            )
        return _WorkerLayout(Path(frozen_root), "runtimes/rag-worker/python.exe")

    checkout_root = Path(__file__).resolve().parents[3]
    if (checkout_root / "pyproject.toml").is_file() and (
        checkout_root / "src/dbzscc/rag/worker.py"
    ).is_file():
        return _WorkerLayout(checkout_root, ".work/venv314/Scripts/python.exe")

    if isinstance(frozen_root, (str, os.PathLike)):
        bundle_root = Path(frozen_root)
        if (bundle_root / "src/dbzscc/rag/worker.py").is_file():
            return _WorkerLayout(bundle_root, "runtimes/rag-worker/python.exe")

    install_root = Path(sys.prefix)
    if (install_root / "src/dbzscc/rag/worker.py").is_file():
        return _WorkerLayout(install_root, "runtimes/rag-worker/python.exe")
    raise LocalRagWorkerError(
        "Die lokale RAG-Laufzeit ist in diesem Paket nicht konfiguriert."
    )


def _validate_selected_root(selected_project_root: str | Path) -> Path:
    if not isinstance(selected_project_root, (str, Path)) or not str(selected_project_root):
        raise ValueError("Wählen Sie zuerst ausdrücklich einen Projektordner.")
    candidate = Path(selected_project_root).expanduser()
    if not candidate.is_absolute() or _is_unc_path(str(candidate)):
        raise ValueError("Wählen Sie einen absoluten lokalen Projektordner.")
    try:
        metadata = candidate.lstat()
        if _is_link_or_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Der ausgewählte Projektordner muss ein lokales reales Verzeichnis sein.")
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Der ausgewählte Projektordner ist nicht verfügbar.") from exc


def _project_id(root: Path) -> str:
    normalized = os.path.normcase(str(root.resolve(strict=True)))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"workspace-{digest}"


def _parse_index_summary(value: dict[str, Any], project_id: str) -> LocalRagIndexSummary:
    expected = {
        "operation",
        "project_id",
        "chunk_count",
        "included_file_count",
        "included_bytes",
        "excluded_counts",
        "unchanged",
        "created_at",
        "model_id",
        "execution_provider",
        "resource_observation",
    }
    if set(value) != expected or value.get("operation") != "index":
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Indexdaten.")
    if value.get("project_id") != project_id:
        raise LocalRagWorkerError("Die Indexantwort gehört nicht zum ausgewählten Projekt.")
    counts = (value.get("chunk_count"), value.get("included_file_count"), value.get("included_bytes"))
    if any(type(count) is not int or count < 0 for count in counts):
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Indexzähler.")
    excluded = value.get("excluded_counts")
    if not isinstance(excluded, dict) or any(
        not isinstance(reason, str)
        or len(reason) > 64
        or type(count) is not int
        or count < 0
        for reason, count in excluded.items()
    ):
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Scan-Zähler.")
    if type(value.get("unchanged")) is not bool:
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte einen ungültigen Indexstatus.")
    _validate_model_binding(value)
    return LocalRagIndexSummary(
        project_id=project_id,
        chunk_count=counts[0],
        included_file_count=counts[1],
        included_bytes=counts[2],
        excluded_counts=tuple(sorted(excluded.items())),
        unchanged=value["unchanged"],
        model_id=MODEL_ID,
        execution_provider=CPU_PROVIDER,
        resource_observation=_parse_resource_observation(value["resource_observation"]),
    )


def _parse_search_results(
    value: dict[str, Any], project_id: str
) -> tuple[RetrievedProjectChunk, ...]:
    expected = {
        "operation",
        "project_id",
        "model_id",
        "execution_provider",
        "results",
        "resource_observation",
    }
    if set(value) != expected or value.get("operation") != "search":
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Suchdaten.")
    if value.get("project_id") != project_id:
        raise LocalRagWorkerError("Die Suchantwort gehört nicht zum ausgewählten Projekt.")
    _validate_model_binding(value)
    _parse_resource_observation(value["resource_observation"])
    results = value.get("results")
    if not isinstance(results, list) or len(results) > MAX_RESULTS:
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte zu viele Suchtreffer.")
    parsed: list[RetrievedProjectChunk] = []
    for item in results:
        if not isinstance(item, dict) or set(item) != {
            "relative_path",
            "format",
            "symbol",
            "line_start",
            "line_end",
            "text",
        }:
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte einen ungültigen Treffer.")
        relative_path = item["relative_path"]
        parts = relative_path.replace("\\", "/").split("/") if isinstance(relative_path, str) else []
        if (
            not parts
            or len(relative_path) > 4_096
            or relative_path.startswith(("/", "\\"))
            or ":" in parts[0]
            or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
            or any(part.casefold() == "codesources" for part in parts)
        ):
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte einen unsicheren Quellpfad.")
        text = item["text"]
        format_name = item["format"]
        symbol = item["symbol"]
        start = item["line_start"]
        end = item["line_end"]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 3_000
            or not isinstance(format_name, str)
            or not format_name
            or len(format_name) > 32
            or not isinstance(symbol, str)
            or not symbol
            or len(symbol) > 256
            or type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
        ):
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Trefferinhalte.")
        parsed.append(
            RetrievedProjectChunk(
                relative_path=relative_path.replace("\\", "/"),
                format=format_name,
                symbol=symbol,
                line_start=start,
                line_end=end,
                text=text,
            )
        )
    return tuple(parsed)


def _validate_model_binding(value: dict[str, Any]) -> None:
    if value.get("model_id") != MODEL_ID or value.get("execution_provider") != CPU_PROVIDER:
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit hat keine CPU-E5-Bindung bestätigt.")


def _parse_resource_observation(value: Any) -> LocalRagResourceObservation:
    expected = {
        "status",
        "cpu_time_seconds",
        "peak_working_set_bytes",
        "peak_commit_bytes",
        "source",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Ressourcendaten.")
    if value["status"] == "unknown":
        if (
            value["cpu_time_seconds"] is not None
            or value["peak_working_set_bytes"] is not None
            or value["peak_commit_bytes"] is not None
            or value["source"] != "unavailable"
        ):
            raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Ressourcendaten.")
        return _UNKNOWN_RAG_RESOURCE_OBSERVATION
    cpu_time = value["cpu_time_seconds"]
    working_set = value["peak_working_set_bytes"]
    commit = value["peak_commit_bytes"]
    if (
        value["status"] != "measured"
        or value["source"] != "windows_process_counters"
        or isinstance(cpu_time, bool)
        or not isinstance(cpu_time, (int, float))
        or not math.isfinite(cpu_time)
        or cpu_time < 0
        or type(working_set) is not int
        or working_set <= 0
        or type(commit) is not int
        or commit <= 0
    ):
        raise LocalRagWorkerError("Die lokale RAG-Laufzeit lieferte ungültige Ressourcendaten.")
    return LocalRagResourceObservation(
        status="measured",
        cpu_time_seconds=float(cpu_time),
        peak_working_set_bytes=working_set,
        peak_commit_bytes=commit,
        source="windows_process_counters",
    )


def _is_unc_path(value: str) -> bool:
    return value.startswith(("\\\\", "//"))


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


_SAFE_WORKER_ERRORS = frozenset(
    {
        "Der lokale RAG-Auftrag ist zu groß.",
        "Der lokale RAG-Auftrag ist ungültig.",
        "Für dieses Projekt ist noch kein RAG-Index aktiv.",
        "Lokales E5-Modell oder CPU-Laufzeit ist nicht verfügbar.",
        "Das ausgewählte Projekt konnte nicht sicher vollständig gelesen werden.",
        "Der lokale RAG-Auftrag ist fehlgeschlagen.",
    }
)
