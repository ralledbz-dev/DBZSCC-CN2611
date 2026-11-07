"""Static one-request JSON worker for the local CPU RAG package.

This module is launched only by the application through a fixed local worker
interpreter. It accepts two fixed operations and never executes request-supplied
commands, imports, or code.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 16 * 1024
MAX_QUERY_CHARS = 2_000
MAX_RESULTS = 5


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        _write_response({"ok": False, "error": "Der lokale RAG-Auftrag ist zu groß."})
        return 2
    try:
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        result = _handle_request(request)
        result["resource_observation"] = _process_resource_observation()
    except _InvalidRequest:
        _write_response({"ok": False, "error": "Der lokale RAG-Auftrag ist ungültig."})
        return 2
    except _NoIndex:
        _write_response(
            {"ok": False, "error": "Für dieses Projekt ist noch kein RAG-Index aktiv."}
        )
        return 3
    except _ModelUnavailable:
        _write_response(
            {"ok": False, "error": "Lokales E5-Modell oder CPU-Laufzeit ist nicht verfügbar."}
        )
        return 4
    except _WorkspaceRejected:
        _write_response(
            {"ok": False, "error": "Das ausgewählte Projekt konnte nicht sicher vollständig gelesen werden."}
        )
        return 5
    except Exception:  # noqa: BLE001 -- the worker boundary emits only a fixed safe error.
        _write_response({"ok": False, "error": "Der lokale RAG-Auftrag ist fehlgeschlagen."})
        return 1
    _write_response({"ok": True, "result": result})
    return 0


def _handle_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise _InvalidRequest
    operation = request.get("operation")
    if operation == "index":
        expected = {"operation", "project_root", "project_id", "database_path", "manifest_path"}
    elif operation == "search":
        expected = {
            "operation",
            "project_root",
            "project_id",
            "database_path",
            "manifest_path",
            "query",
        }
    else:
        raise _InvalidRequest
    if set(request) != expected:
        raise _InvalidRequest

    root = _selected_root(request["project_root"])
    project_id = request["project_id"]
    if not isinstance(project_id, str) or project_id != _project_id(root):
        raise _InvalidRequest
    database = _absolute_local_file_path(request["database_path"])
    manifest = _absolute_local_file_path(request["manifest_path"])

    _add_source_root_to_sys_path()
    from dbzscc.rag import E5OnnxEmbedder, LocalRagIndex, NoActiveIndexError
    from dbzscc.rag.embeddings import EmbeddingSetupError
    from dbzscc.rag.workspace import WorkspaceScanError

    try:
        embedder = E5OnnxEmbedder(manifest, intra_op_num_threads=2)
    except (EmbeddingSetupError, ImportError) as exc:
        raise _ModelUnavailable from exc

    try:
        with LocalRagIndex(database, embedder) as index:
            if operation == "index":
                built = index.rebuild_workspace(root, project_id)
                return {
                    "operation": "index",
                    "project_id": built.project_id,
                    "chunk_count": built.chunk_count,
                    "included_file_count": built.included_file_count,
                    "included_bytes": built.included_bytes,
                    "excluded_counts": dict(built.excluded_counts or ()),
                    "unchanged": built.unchanged,
                    "created_at": built.created_at,
                    "model_id": embedder.model_id,
                    "execution_provider": embedder.execution_provider,
                }

            query = request["query"]
            if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
                raise _InvalidRequest
            hits = index.search(project_id=project_id, query=query, limit=MAX_RESULTS)
            return {
                "operation": "search",
                "project_id": project_id,
                "model_id": embedder.model_id,
                "execution_provider": embedder.execution_provider,
                "results": [
                    {
                        "relative_path": hit.relative_path,
                        "format": hit.format,
                        "symbol": hit.symbol,
                        "line_start": hit.line_start,
                        "line_end": hit.line_end,
                        "text": hit.text,
                    }
                    for hit in hits
                ],
            }
    except NoActiveIndexError as exc:
        raise _NoIndex from exc
    except WorkspaceScanError as exc:
        raise _WorkspaceRejected from exc


def _selected_root(value: Any) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise _InvalidRequest
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or _is_unc_path(str(candidate)):
        raise _InvalidRequest
    try:
        metadata = candidate.lstat()
        if _is_link_or_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise _InvalidRequest
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _InvalidRequest from exc
    return resolved


def _process_resource_observation() -> dict[str, Any]:
    """Return lifetime metrics for this worker process, or explicit unknowns."""
    if os.name != "nt":
        return _unknown_resource_observation()
    try:
        return _windows_process_resource_observation()
    except Exception:  # noqa: BLE001 -- resource telemetry must never fail an otherwise valid RAG job.
        return _unknown_resource_observation()


def _windows_process_resource_observation() -> dict[str, Any]:
    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = ctypes.c_void_p
    process = get_current_process()

    creation, exit_time, kernel_time, user_time = FileTime(), FileTime(), FileTime(), FileTime()
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    get_process_times.restype = ctypes.c_int
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_process_memory = psapi.GetProcessMemoryInfo
    get_process_memory.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    get_process_memory.restype = ctypes.c_int
    if not get_process_times(
        process,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ) or not get_process_memory(process, ctypes.byref(counters), counters.cb):
        return _unknown_resource_observation()

    def filetime_value(value: FileTime) -> int:
        return (value.high << 32) | value.low

    working_set = int(counters.peak_working_set_size)
    peak_commit = int(counters.peak_pagefile_usage)
    if working_set <= 0 or peak_commit <= 0:
        return _unknown_resource_observation()
    cpu_ticks = filetime_value(kernel_time) + filetime_value(user_time)
    return {
        "status": "measured",
        "cpu_time_seconds": round(cpu_ticks / 10_000_000, 6),
        "peak_working_set_bytes": working_set,
        "peak_commit_bytes": peak_commit,
        "source": "windows_process_counters",
    }


def _unknown_resource_observation() -> dict[str, Any]:
    return {
        "status": "unknown",
        "cpu_time_seconds": None,
        "peak_working_set_bytes": None,
        "peak_commit_bytes": None,
        "source": "unavailable",
    }


def _absolute_local_file_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise _InvalidRequest
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or _is_unc_path(str(candidate)):
        raise _InvalidRequest
    return candidate


def _project_id(root: Path) -> str:
    normalized = os.path.normcase(str(root.resolve(strict=True)))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"workspace-{digest}"


def _add_source_root_to_sys_path() -> None:
    source_root = Path(__file__).resolve().parents[2]
    if not source_root.is_dir():
        raise _InvalidRequest
    sys.path.insert(0, str(source_root))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidRequest
        result[key] = value
    return result


def _is_unc_path(value: str) -> bool:
    return value.startswith(("\\\\", "//"))


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


def _write_response(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class _InvalidRequest(Exception):
    pass


class _NoIndex(Exception):
    pass


class _ModelUnavailable(Exception):
    pass


class _WorkspaceRejected(Exception):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
