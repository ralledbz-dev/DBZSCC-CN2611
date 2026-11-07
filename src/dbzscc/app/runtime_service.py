"""Explicit local llama.cpp runtime workflow with fail-closed resource planning.

Only this service owns processes it starts. It never searches the user's whole
machine, never uses a shell, and never terminates an unrelated process.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from http.client import HTTPConnection
from pathlib import Path
from typing import Protocol, TextIO

from dbzscc.miro.gguf_metadata import GGUFMetadataReader

from .execution_events import ExecutionEvent, ExecutionEventType, redact_text
from .resource_orchestrator import (
    Backend,
    BackendCapability,
    BackendTarget,
    DeviceMemoryKind,
    PhysicalDeviceSnapshot,
    PlanStatus,
    ResourceOrchestrator,
    ResourceSnapshot,
    TelemetryQuality,
    WorkloadRequest,
)

MIB = 1024 * 1024
APP_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_FOLDERS = {
    Backend.CPU: "cpu-x64",
    Backend.CUDA: "cuda-13.1-x64",
    Backend.VULKAN: "vulkan-x64",
}
_DEVICE_LINE = re.compile(r"\b(CUDA|Vulkan)(\d+)\s*:\s*(.+)$", re.IGNORECASE)
_GENERIC_DEVICE_LINE = re.compile(r"\bDevice\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)
_SECRET_ENV_KEY = re.compile(r"(?i)(?:API|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)")


class RuntimeWorkflowError(RuntimeError):
    """Safe, actionable error from preflight, admission or process lifecycle."""


class RuntimeBackend(str, Enum):
    CPU = Backend.CPU.value
    CUDA = Backend.CUDA.value
    VULKAN = Backend.VULKAN.value

    @property
    def planner_backend(self) -> Backend:
        return Backend(self.value)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class ManagedProcess(Protocol):
    pid: int
    stdout: TextIO | None
    stderr: TextIO | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ProcessRunner(Protocol):
    """Injectable process boundary used by the production runner and tests."""

    def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> CommandResult: ...

    def start(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> ManagedProcess: ...


class SubprocessRunner:
    """No-shell runner using a per-child environment and exact argv paths."""

    def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env),
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def start(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> ManagedProcess:
        return subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


@dataclass(frozen=True)
class RuntimeLaunchPlan:
    conversation_id: str
    workflow_id: str
    run_id: str
    backend: Backend
    model_root: Path
    model_path: Path
    runtime_directory: Path
    resource_plan: object
    target_id: str | None
    target_name: str
    context_tokens: int
    requested_threads: int
    port: int
    model_artifact_id: str | None = None
    model_observation_id: str | None = None
    model_full_file_sha256: str | None = None
    runtime_build_sha256: str | None = None
    device_physical_id: str | None = None
    runtime_parameters: tuple[tuple[str, str | int], ...] = ()

    @property
    def admitted(self) -> bool:
        return bool(getattr(self.resource_plan, "admitted", False))


@dataclass(frozen=True)
class RuntimeStartResult:
    plan: RuntimeLaunchPlan
    endpoint: str
    process_id: int
    model_ids: tuple[str, ...]


class WindowsResourceMonitor:
    """Fresh Windows RAM/commit/CPU and NVIDIA dedicated-memory telemetry."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or SubprocessRunner()

    def capture(self) -> ResourceSnapshot:
        if os.name != "nt":
            raise RuntimeWorkflowError("Die echte Ressourcenmessung ist nur unter Windows verfügbar.")
        available_physical_mib = self._available_physical_mib()
        commit_available_mib = self._commit_available_mib()
        cpu_load_ratio = self._cpu_load_ratio()
        captured = time.monotonic()
        return ResourceSnapshot(
            captured_monotonic_s=captured,
            telemetry_quality=TelemetryQuality.FRESH,
            available_physical_mib=available_physical_mib,
            commit_available_mib=commit_available_mib,
            logical_cpu_cores=os.cpu_count(),
            cpu_load_ratio=cpu_load_ratio,
            devices=self._nvidia_devices(captured),
            source="Windows GlobalMemoryStatusEx/GetPerformanceInfo/GetSystemTimes + nvidia-smi",
        )

    @staticmethod
    def _available_physical_mib() -> int:
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        value = MemoryStatusEx()
        value.dwLength = ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            raise RuntimeWorkflowError("Windows konnte den verfügbaren physischen RAM nicht messen.")
        return int(value.ullAvailPhys // MIB)

    @staticmethod
    def _commit_available_mib() -> int:
        class PerformanceInformation(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("CommitTotal", ctypes.c_size_t),
                ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t),
                ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t),
                ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t),
                ("PageSize", ctypes.c_size_t),
                ("HandleCount", ctypes.c_ulong),
                ("ProcessCount", ctypes.c_ulong),
                ("ThreadCount", ctypes.c_ulong),
            ]

        value = PerformanceInformation()
        value.cb = ctypes.sizeof(value)
        psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
        function = psapi.GetPerformanceInfo
        function.argtypes = [ctypes.POINTER(PerformanceInformation), ctypes.c_ulong]
        function.restype = ctypes.c_int
        if not function(ctypes.byref(value), value.cb) or value.PageSize <= 0:
            raise RuntimeWorkflowError("Windows konnte den verfügbaren System-Commit nicht messen.")
        return max(0, int((value.CommitLimit - value.CommitTotal) * value.PageSize // MIB))

    @staticmethod
    def _system_times() -> tuple[int, int]:
        class FileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

        idle, kernel, user = FileTime(), FileTime(), FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise RuntimeWorkflowError("Windows konnte die CPU-Auslastung nicht messen.")

        def value(item: FileTime) -> int:
            return (item.dwHighDateTime << 32) | item.dwLowDateTime

        return value(idle), value(kernel) + value(user)

    @classmethod
    def _cpu_load_ratio(cls) -> float:
        before_idle, before_total = cls._system_times()
        time.sleep(0.15)
        after_idle, after_total = cls._system_times()
        total_delta = after_total - before_total
        idle_delta = after_idle - before_idle
        if total_delta <= 0 or idle_delta < 0:
            raise RuntimeWorkflowError("Die CPU-Auslastungsmessung war ungültig.")
        return max(0.0, min(1.0, 1.0 - idle_delta / total_delta))

    def _nvidia_devices(self, captured: float) -> tuple[PhysicalDeviceSnapshot, ...]:
        executable = shutil.which("nvidia-smi")
        if not executable:
            return ()
        executable_path = Path(executable).resolve()
        env = _child_environment(executable_path.parent)
        argv = [
            str(executable_path),
            "--query-gpu=uuid,name,memory.total,memory.used",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = self.runner.run(
                argv, cwd=executable_path.parent, env=env, timeout=5.0
            )
        except (OSError, subprocess.TimeoutExpired):
            return ()
        if result.returncode != 0:
            return ()
        devices: list[PhysicalDeviceSnapshot] = []
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) < 4:
                continue
            identity, name = row[0].strip(), row[1].strip()
            total = _parse_mib_number(row[2])
            used = _parse_mib_number(row[3])
            if not identity or not name or total is None or used is None or total <= 0:
                continue
            devices.append(
                PhysicalDeviceSnapshot(
                    physical_id=f"nvidia:{identity}",
                    name=name,
                    memory_kind=DeviceMemoryKind.DEDICATED,
                    captured_monotonic_s=captured,
                    telemetry_quality=TelemetryQuality.FRESH,
                    dedicated_total_mib=total,
                    dedicated_budget_mib=total,
                    dedicated_used_mib=used,
                    display_attached=None,
                )
            )
        return tuple(devices)


class RuntimeController:
    """Preflight, plan, launch, health-check and stop one app-owned server."""

    STARTUP_TIMEOUT_SECONDS = 120.0
    STOP_TIMEOUT_SECONDS = 5.0
    MAX_OUTPUT_EVENTS = 160
    MAX_OUTPUT_CHARS = 24_000

    def __init__(
        self,
        *,
        event_sink: Callable[[ExecutionEvent], None] | None = None,
        runner: ProcessRunner | None = None,
        resource_monitor: object | None = None,
        orchestrator: ResourceOrchestrator | None = None,
        health_probe: Callable[[str, float], bool] | None = None,
        catalog_lookup: Callable[[str | os.PathLike[str]], object | None] | None = None,
        full_hash_lookup: Callable[[str, str], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._monitor = resource_monitor or WindowsResourceMonitor(self._runner)
        self._orchestrator = orchestrator or ResourceOrchestrator()
        self._event_sink = event_sink or (lambda _event: None)
        self._health_probe = health_probe or _probe_health
        self._catalog_lookup = catalog_lookup
        self._full_hash_lookup = full_hash_lookup
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._process: ManagedProcess | None = None
        self._endpoint: str | None = None
        self._healthy = False
        self._ready = False
        self._process_context: tuple[str, str, str, Path, Path] | None = None
        self._watcher: threading.Thread | None = None
        self._readers: list[threading.Thread] = []
        self._log_gate: dict[str, int | bool] = {"events": 0, "chars": 0, "truncated": False}

    @staticmethod
    def enumerate_models(model_root: str | os.PathLike[str]) -> tuple[Path, ...]:
        """List only direct regular GGUF children of one user-selected folder."""
        root = _validate_model_root(model_root)
        candidates: list[Path] = []
        try:
            entries = tuple(root.iterdir())
        except OSError as exc:
            raise RuntimeWorkflowError("Der ausgewählte Modellordner konnte nicht gelesen werden.") from exc
        for entry in entries:
            if entry.suffix.casefold() != ".gguf":
                continue
            try:
                info = entry.lstat()
                if _is_reparse_point(info.st_file_attributes if hasattr(info, "st_file_attributes") else 0):
                    continue
                if not entry.is_file():
                    continue
                candidates.append(entry.resolve(strict=True))
            except OSError:
                continue
        return tuple(sorted(candidates, key=lambda item: (item.name.casefold(), item.name)))

    def plan(
        self,
        *,
        conversation_id: str,
        model_root: str | os.PathLike[str],
        model_path: str | os.PathLike[str],
        backend: Backend | RuntimeBackend | str,
        context_tokens: int,
        cpu_threads: int,
        port: int,
        workflow_id: str | None = None,
    ) -> RuntimeLaunchPlan:
        """Run fresh `--list-devices` and telemetry preflight, then return a plan."""
        selected_backend = _coerce_backend(backend)
        workflow = workflow_id or uuid.uuid4().hex
        run = uuid.uuid4().hex
        return self._make_plan(
            conversation_id=conversation_id,
            workflow_id=workflow,
            run_id=run,
            model_root=model_root,
            model_path=model_path,
            backend=selected_backend,
            context_tokens=context_tokens,
            cpu_threads=cpu_threads,
            port=port,
        )

    def start(
        self,
        *,
        conversation_id: str,
        model_root: str | os.PathLike[str],
        model_path: str | os.PathLike[str],
        backend: Backend | RuntimeBackend | str,
        context_tokens: int,
        cpu_threads: int,
        port: int,
        workflow_id: str | None = None,
    ) -> RuntimeStartResult:
        """Freshly re-plan, explicitly start, and await a loopback health check."""
        selected_backend = _coerce_backend(backend)
        workflow = workflow_id or uuid.uuid4().hex
        run = uuid.uuid4().hex
        plan = self._make_plan(
            conversation_id=conversation_id,
            workflow_id=workflow,
            run_id=run,
            model_root=model_root,
            model_path=model_path,
            backend=selected_backend,
            context_tokens=context_tokens,
            cpu_threads=cpu_threads,
            port=port,
        )
        if not plan.admitted:
            details = "; ".join(getattr(plan.resource_plan, "fail_closed_reasons", ()))
            self._emit(
                plan,
                ExecutionEventType.ERROR,
                "Ressourcen-Vorprüfung abgelehnt; es wurde kein Prozess gestartet. "
                + (details or "Kein sicherer Backend-/Speicherplan verfügbar."),
            )
            raise RuntimeWorkflowError(
                "Ressourcen-Vorprüfung abgelehnt; kein llama-server wurde gestartet. "
                + (details or "")
            )
        self._verify_selected_model_hash(plan)
        self._ensure_port_available(plan)
        endpoint = f"http://127.0.0.1:{port}/v1"
        server = plan.runtime_directory / "llama-server.exe"
        if not server.is_file():
            raise RuntimeWorkflowError("Die ausgewählte Runtime enthält kein llama-server.exe.")
        argv = self._server_argv(plan, server)
        env = _child_environment(plan.runtime_directory)
        self._emit(
            plan,
            ExecutionEventType.TERMINAL_COMMAND,
            f"llama-server --host 127.0.0.1 --port {port} --model {plan.model_path.name} "
            f"--ctx-size {plan.context_tokens} --threads {getattr(plan.resource_plan, 'cpu_threads', plan.requested_threads)} "
            f"--n-gpu-layers {'99' if plan.backend is not Backend.CPU else '0'}",
            source="terminal",
            provenance=(
                ("command", "llama-server"),
                ("backend", plan.backend.value),
                ("model_file", plan.model_path.name),
                ("target", plan.target_id or "cpu"),
            ),
        )
        self._emit(
            plan,
            ExecutionEventType.STATUS,
            f"Starte explizit gewählte {selected_backend.value.upper()}-Runtime auf 127.0.0.1:{port}.",
        )
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeWorkflowError("Ein von der App gestarteter llama-server läuft bereits.")
            self._log_gate = {"events": 0, "chars": 0, "truncated": False}
            try:
                process = self._runner.start(argv, cwd=plan.runtime_directory, env=env)
            except OSError as exc:
                self._emit(plan, ExecutionEventType.ERROR, "llama-server konnte nicht gestartet werden.")
                raise RuntimeWorkflowError("llama-server konnte nicht gestartet werden.") from exc
            self._process = process
            self._endpoint = endpoint
            self._healthy = False
            self._ready = False
            self._process_context = (
                plan.conversation_id,
                plan.workflow_id,
                plan.run_id,
                plan.model_root,
                plan.model_path,
            )
        self._emit(
            plan,
            ExecutionEventType.PROGRESS,
            f"llama-server-Prozess gestartet · PID {process.pid} · Modell {plan.model_path.name}.",
        )
        self._start_output_readers(process, plan)
        watcher = threading.Thread(
            target=self._watch_process,
            args=(process, plan, endpoint),
            name=f"dbzscc-runtime-watch-{process.pid}",
            daemon=True,
        )
        with self._lock:
            self._watcher = watcher
        watcher.start()
        deadline = self._monotonic() + self.STARTUP_TIMEOUT_SECONDS
        while self._monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                self._finish_process(process, exit_code)
                self._emit(
                    plan,
                    ExecutionEventType.ERROR,
                    f"llama-server endete während des Starts mit Exitcode {exit_code}.",
                    exit_code=exit_code,
                )
                raise RuntimeWorkflowError(
                    f"llama-server endete während des Starts mit Exitcode {exit_code}."
                )
            if self._health_probe(endpoint, 0.6):
                with self._lock:
                    if self._process is process:
                        self._healthy = True
                        self._ready = True
                self._emit(plan, ExecutionEventType.STATUS, "Lokaler Healthcheck erfolgreich · Runtime bereit.")
                model_ids = self._list_models(endpoint, plan)
                return RuntimeStartResult(plan, endpoint, process.pid, model_ids)
            self._sleep(0.4)
        self._emit(plan, ExecutionEventType.ERROR, "Startup-Timeout: lokaler Healthcheck wurde nicht bereit.")
        self._stop_owned_process(process, plan)
        raise RuntimeWorkflowError(
            f"Startup-Timeout nach {self.STARTUP_TIMEOUT_SECONDS:.0f} Sekunden; "
            "der von der App gestartete Prozess wurde beendet."
        )

    def stop(self) -> bool:
        """Stop only the exact process object started and retained by this controller."""
        with self._lock:
            process = self._process
            context = self._process_context
        if process is None or context is None:
            return False
        conversation_id, workflow_id, run_id, model_root, model_path = context
        plan = _event_plan_context(
            conversation_id, workflow_id, run_id, model_root, model_path
        )
        return self._stop_owned_process(process, plan)

    @property
    def has_owned_process(self) -> bool:
        """Return whether this controller still owns a live process object."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def managed_endpoint(self) -> str | None:
        with self._lock:
            if self._process is None or self._process.poll() is not None or not self._healthy:
                return None
            return self._endpoint

    def is_managed_healthy(self, endpoint: str) -> bool:
        return self.managed_endpoint == endpoint.rstrip("/")

    @staticmethod
    def _server_argv(plan: RuntimeLaunchPlan, server: Path) -> list[str]:
        argv = [
            str(server),
            "--host",
            "127.0.0.1",
            "--port",
            str(plan.port),
            "--model",
            str(plan.model_path),
            "--ctx-size",
            str(plan.context_tokens),
            "--threads",
            str(getattr(plan.resource_plan, "cpu_threads", plan.requested_threads)),
            "--n-gpu-layers",
            "99" if plan.backend is not Backend.CPU else "0",
        ]
        if plan.backend is not Backend.CPU and plan.target_id:
            match = re.fullmatch(r"(?:cuda|vulkan):(\d+)", plan.target_id, re.IGNORECASE)
            if not match:
                raise RuntimeWorkflowError("Der geplante GPU-Zielbezeichner ist ungültig.")
            prefix = "CUDA" if plan.backend is Backend.CUDA else "Vulkan"
            argv.extend(("--device", f"{prefix}{match.group(1)}"))
        return argv

    def _make_plan(
        self,
        *,
        conversation_id: str,
        workflow_id: str,
        run_id: str,
        model_root: str | os.PathLike[str],
        model_path: str | os.PathLike[str],
        backend: Backend,
        context_tokens: int,
        cpu_threads: int,
        port: int,
    ) -> RuntimeLaunchPlan:
        base = _event_plan_context(
            conversation_id, workflow_id, run_id, Path(model_root), Path(model_path)
        )
        self._emit(
            base,
            ExecutionEventType.WORKFLOW_STARTED,
            "Runtime-Workflow gestartet · kein Autostart und kein Modellverzeichnis-Scan.",
        )
        self._emit(
            base,
            ExecutionEventType.COT_COMPACT_RATIONALE,
            "Prüfbare Entscheidungsspur: nur der ausgewählte Build und die ausgewählte GGUF-Datei; "
            "frische RAM-/CPU-/GPU-Messwerte; getrennte Speicherpools; Ablehnung bei unbekannten Budgets.",
        )
        self._emit(
            base,
            ExecutionEventType.TOT_PLAN_OPTION,
            "CPU: System-RAM und aktuelle CPU-Last; keine GPU-Belegung.",
        )
        self._emit(
            base,
            ExecutionEventType.TOT_PLAN_OPTION,
            "CUDA: nur ein eindeutig zugeordnetes NVIDIA-Gerät mit nvidia-smi-Dedicated-Memory.",
        )
        self._emit(
            base,
            ExecutionEventType.TOT_PLAN_OPTION,
            "Vulkan: nur ein auf NVIDIA eindeutig gematchtes Gerät; AMD-iGPU ohne belastbares Budget bleibt ausgeschlossen.",
        )
        if not conversation_id.strip():
            raise RuntimeWorkflowError("Ein Chat muss für die Laufereignisse ausgewählt sein.")
        if not 128 <= context_tokens <= 8192:
            raise RuntimeWorkflowError("Der Kontext muss zwischen 128 und 8192 Tokens liegen.")
        if not 1 <= cpu_threads <= 64:
            raise RuntimeWorkflowError("Die CPU-Threadzahl muss zwischen 1 und 64 liegen.")
        if not 1024 <= port <= 65535:
            raise RuntimeWorkflowError("Der lokale Port liegt außerhalb des gültigen Bereichs.")
        root = _validate_model_root(model_root)
        model = _validate_model_file(root, model_path)
        catalog_entry = None
        model_full_file_sha256 = None
        if self._catalog_lookup is not None:
            try:
                catalog_entry = self._catalog_lookup(model)
            except Exception as exc:
                raise RuntimeWorkflowError(
                    "Der zentrale Modellkatalog konnte nicht geprüft werden; kein Prozess wurde gestartet."
                ) from exc
            if not isinstance(catalog_entry, ModelCatalogEntry):
                raise RuntimeWorkflowError(
                    "Der zentrale Modellkatalog lieferte keinen typisierten ModelCatalogEntry."
                )
            artifact = getattr(catalog_entry, "artifact", None)
            observation = getattr(catalog_entry, "observation", None)
            current_stat = model.stat()
            current_mtime = (
                datetime.fromtimestamp(current_stat.st_mtime, timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            if (
                artifact is None
                or observation is None
                or artifact.status != "metadata_read"
                or observation.status != "metadata_read"
                or artifact.current_observation_id != observation.observation_id
                or os.path.normcase(os.path.abspath(artifact.path))
                != os.path.normcase(os.path.abspath(model))
                or artifact.file_size_bytes != current_stat.st_size
                or artifact.file_modified_at_utc != current_mtime
            ):
                raise RuntimeWorkflowError(
                    "Die ausgewählte GGUF-Datei hat keinen aktuellen, geparsten Eintrag im zentralen Modellkatalog."
                )
            artifact_id = getattr(artifact, "artifact_id", None)
            observation_id = getattr(observation, "observation_id", None)
            if not isinstance(artifact_id, str) or not isinstance(observation_id, str):
                raise RuntimeWorkflowError("Der Registry-Eintrag besitzt keine stabile Artefakt-/Beobachtungs-ID.")
            if self._full_hash_lookup is None:
                raise RuntimeWorkflowError("Der On-demand-Vollhash-Dienst des Modellkatalogs ist nicht verbunden.")
            try:
                model_full_file_sha256 = self._full_hash_lookup(artifact_id, observation_id)
            except Exception as exc:
                raise RuntimeWorkflowError(
                    "Die ausgewählte GGUF-Datei wurde beim Vollhash-Preflight geändert oder konnte nicht geprüft werden."
                ) from exc
            if not _is_sha256(model_full_file_sha256):
                raise RuntimeWorkflowError("Der zentrale Katalog lieferte keinen gültigen GGUF-Vollhash.")
        runtime_directory = (APP_ROOT / "runtimes" / "llama.cpp" / _RUNTIME_FOLDERS[backend]).resolve()
        cli = runtime_directory / "llama-cli.exe"
        if not cli.is_file():
            self._emit(base, ExecutionEventType.ERROR, "Der ausgewählte Build enthält kein llama-cli.exe.")
            raise RuntimeWorkflowError("Der ausgewählte Build enthält kein llama-cli.exe.")
        self._emit(
            base,
            ExecutionEventType.TOT_PLAN_SELECTED,
            f"Ausgewählter Zweig: {backend.value.upper()} · Datei {model.name} · Kontext {context_tokens} · Port {port}.",
            provenance=(("backend", backend.value), ("model_file", model.name)),
        )
        self._emit(base, ExecutionEventType.STATUS, "Preflight: exakt ausgewählte llama-cli.exe --list-devices.")
        env = _child_environment(runtime_directory)
        self._emit(
            base,
            ExecutionEventType.TERMINAL_COMMAND,
            "llama-cli --list-devices · Timeout 15 s",
            source="terminal",
            provenance=(("command", "llama-cli --list-devices"), ("timeout_s", "15")),
        )
        try:
            result = self._runner.run(
                [str(cli), "--list-devices"],
                cwd=runtime_directory,
                env=env,
                timeout=15.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._emit(base, ExecutionEventType.ERROR, "Geräte-Preflight fehlgeschlagen oder Zeitlimit überschritten.")
            raise RuntimeWorkflowError("Geräte-Preflight fehlgeschlagen oder Zeitlimit überschritten.") from exc
        self._emit_command_output(result.stdout, ExecutionEventType.STDOUT, base, "llama-cli --list-devices")
        self._emit_command_output(result.stderr, ExecutionEventType.STDERR, base, "llama-cli --list-devices")
        self._emit(
            base,
            ExecutionEventType.EXIT_CODE,
            f"llama-cli --list-devices endete mit Exitcode {result.returncode}.",
            exit_code=result.returncode,
            source="llama-cli --list-devices",
        )
        if result.returncode != 0:
            raise RuntimeWorkflowError(
                f"llama-cli --list-devices ist mit Exitcode {result.returncode} fehlgeschlagen."
            )
        parsed_targets = _parse_runtime_devices(result.stdout + "\n" + result.stderr, backend)
        try:
            snapshot = self._monitor.capture()
        except Exception as exc:
            self._emit(base, ExecutionEventType.ERROR, f"Ressourcenmessung fehlgeschlagen: {exc}")
            raise RuntimeWorkflowError(f"Ressourcenmessung fehlgeschlagen: {exc}") from exc
        now = self._monotonic()
        devices = tuple(replace(item, captured_monotonic_s=now) for item in snapshot.devices)
        backend_targets: list[BackendTarget] = []
        target_names: dict[str, str] = {}
        if backend is Backend.CPU:
            backend_targets.append(BackendTarget("cpu", None))
            target_names["cpu"] = "CPU"
            capability_available = True
        else:
            for target_id, name in parsed_targets:
                matched = _match_nvidia_device(name, devices)
                if matched is None:
                    continue
                backend_targets.append(BackendTarget(target_id, matched.physical_id))
                target_names[target_id] = matched.name
            capability_available = bool(backend_targets)
        capability = BackendCapability(backend, capability_available, tuple(backend_targets))
        snapshot = replace(
            snapshot,
            captured_monotonic_s=now,
            telemetry_quality=TelemetryQuality.FRESH,
            devices=devices,
            backends=(capability,),
        )
        model_size_bytes = (
            catalog_entry.artifact.file_size_bytes
            if catalog_entry is not None
            else model.stat().st_size
        )
        size_mib = max(1, math.ceil(model_size_bytes / MIB))
        kv_mib = _estimate_kv_cache_mib(model, context_tokens)
        request = WorkloadRequest(
            workload_id=f"llama-server-{run_id}",
            model_id=(
                getattr(getattr(catalog_entry, "model", None), "model_id", None)
                or model.name
            ),
            backend_preferences=(backend,),
            model_file_size_mib=size_mib,
            kv_cache_mib=kv_mib,
            context_tokens=context_tokens,
            cpu_threads=cpu_threads,
            min_cpu_threads=1,
            allow_cpu_thread_reduction=True,
            model_catalog_entry=catalog_entry,
        )
        plan = self._orchestrator.plan(request, snapshot, now_monotonic_s=now)
        target_name = target_names.get(plan.backend_target_id or "", "CPU")
        launch_plan = RuntimeLaunchPlan(
            conversation_id=conversation_id,
            workflow_id=workflow_id,
            run_id=run_id,
            backend=backend,
            model_root=root,
            model_path=model,
            runtime_directory=runtime_directory,
            resource_plan=plan,
            target_id=plan.backend_target_id,
            target_name=target_name,
            context_tokens=context_tokens,
            requested_threads=cpu_threads,
            port=port,
            model_artifact_id=getattr(
                getattr(catalog_entry, "artifact", None), "artifact_id", None
            ),
            model_observation_id=getattr(
                getattr(catalog_entry, "observation", None), "observation_id", None
            ),
            model_full_file_sha256=model_full_file_sha256,
            runtime_build_sha256=_sha256_file(runtime_directory / "llama-server.exe"),
            device_physical_id=(
                plan.physical_id
                or f"cpu:{platform.machine()}:{platform.processor() or 'unknown'}"
            ),
            runtime_parameters=tuple(
                sorted(
                    {
                        "backend": backend.value,
                        "context_tokens": context_tokens,
                        "cpu_threads": plan.cpu_threads,
                        "gpu_layers": 0 if backend is Backend.CPU else 99,
                        "port": port,
                        "target_id": plan.backend_target_id or "cpu",
                    }.items()
                )
            ),
        )
        if plan.status is PlanStatus.REJECTED:
            details = "; ".join(plan.fail_closed_reasons)
            self._emit(
                launch_plan,
                ExecutionEventType.TOT_PLAN_SELECTED,
                "Ressourcenplan abgelehnt · kein Prozessstart. " + (details or "Sicherheitsprüfung fehlgeschlagen."),
                provenance=(("backend", backend.value), ("result", "rejected")),
            )
        else:
            self._emit(
                launch_plan,
                ExecutionEventType.TOT_PLAN_SELECTED,
                f"Ressourcenplan zugelassen · Ziel {plan.backend_target_id or 'CPU'} ({target_name}) · "
                f"RAM-Reserve {plan.host_memory_mib} MiB · GPU-Budget {plan.accelerator_memory_mib} MiB · "
                f"Threads {plan.cpu_threads}.",
                provenance=(("backend", backend.value), ("result", plan.status.value)),
            )
        uncertainty = " ".join(plan.uncertainties)
        if uncertainty:
            self._emit(launch_plan, ExecutionEventType.REASONING_SUMMARY, uncertainty)
        self._emit(
            launch_plan,
            ExecutionEventType.TOOL_RESULT,
            "Frische Ressourcenplanung abgeschlossen; Plan und Unsicherheiten sind im Chat protokolliert.",
            source="resource-orchestrator",
        )
        return launch_plan

    def _verify_selected_model_hash(self, plan: RuntimeLaunchPlan) -> None:
        if plan.model_artifact_id is None:
            return
        if self._full_hash_lookup is None or plan.model_observation_id is None:
            raise RuntimeWorkflowError("Der ausgewählte Modell-Vollhash kann vor dem Start nicht erneut geprüft werden.")
        try:
            current_hash = self._full_hash_lookup(
                plan.model_artifact_id, plan.model_observation_id
            )
        except Exception as exc:
            raise RuntimeWorkflowError(
                "Das ausgewählte Modell änderte sich nach der Ressourcenprüfung; der Start wurde abgebrochen."
            ) from exc
        if current_hash != plan.model_full_file_sha256:
            raise RuntimeWorkflowError(
                "Der vollständige GGUF-Hash änderte sich nach der Ressourcenprüfung; der Start wurde abgebrochen."
            )

    def _ensure_port_available(self, plan: RuntimeLaunchPlan) -> None:
        try:
            with socket.create_connection(("127.0.0.1", plan.port), timeout=0.25):
                pass
        except OSError:
            return
        self._emit(
            plan,
            ExecutionEventType.ERROR,
            f"Port 127.0.0.1:{plan.port} ist belegt; der vorhandene Prozess bleibt unverändert.",
        )
        raise RuntimeWorkflowError(
            f"Port 127.0.0.1:{plan.port} ist belegt. Der vorhandene Prozess wurde nicht verändert."
        )

    def _start_output_readers(self, process: ManagedProcess, plan: RuntimeLaunchPlan) -> None:
        for stream, event_type in (
            (process.stdout, ExecutionEventType.STDOUT),
            (process.stderr, ExecutionEventType.STDERR),
        ):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._read_output,
                args=(stream, event_type, plan),
                name=f"dbzscc-runtime-{event_type.value}-{process.pid}",
                daemon=True,
            )
            self._readers.append(thread)
            thread.start()

    def _read_output(
        self, stream: TextIO, event_type: ExecutionEventType, plan: RuntimeLaunchPlan
    ) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                self._emit_bounded_output(plan, event_type, line.rstrip("\r\n"), "llama-server")
        except (OSError, ValueError):
            return

    def _emit_bounded_output(
        self,
        plan: RuntimeLaunchPlan,
        event_type: ExecutionEventType,
        text: str,
        source: str,
    ) -> None:
        safe = redact_text(text, scoped_paths=(plan.model_path, plan.runtime_directory), max_chars=1200)
        if not safe:
            return
        with self._lock:
            count = int(self._log_gate["events"])
            chars = int(self._log_gate["chars"])
            if count >= self.MAX_OUTPUT_EVENTS or chars + len(safe) > self.MAX_OUTPUT_CHARS:
                if not bool(self._log_gate["truncated"]):
                    self._log_gate["truncated"] = True
                    self._emit(
                        plan,
                        ExecutionEventType.TRUNCATED,
                        "Rohlog wurde bei 160 Ereignissen bzw. 24.000 Zeichen gekürzt; "
                        "weitere Zeilen werden nicht übernommen.",
                        source=source,
                    )
                return
            self._log_gate["events"] = count + 1
            self._log_gate["chars"] = chars + len(safe)
        self._emit(plan, event_type, safe, source=source)

    def _watch_process(
        self, process: ManagedProcess, plan: RuntimeLaunchPlan, endpoint: str
    ) -> None:
        while process.poll() is None:
            with self._lock:
                ready = self._process is process and self._ready
            if ready:
                healthy = self._health_probe(endpoint, 0.7)
                with self._lock:
                    changed = self._process is process and self._healthy != healthy
                    if self._process is process:
                        self._healthy = healthy
                if changed:
                    message = (
                        "Lokaler Healthcheck weiterhin erfolgreich."
                        if healthy
                        else "Lokaler Healthcheck fehlgeschlagen; Chat wird bis zur Wiederherstellung blockiert."
                    )
                    self._emit(plan, ExecutionEventType.STATUS, message)
            self._sleep(1.0)
        code = process.poll()
        self._finish_process(process, code if code is not None else -1)

    def _finish_process(self, process: ManagedProcess, exit_code: int) -> None:
        with self._lock:
            if self._process is not process:
                return
            context = self._process_context
            self._process = None
            self._endpoint = None
            self._healthy = False
            self._ready = False
            self._process_context = None
        if context:
            conversation_id, workflow_id, run_id, root, model = context
            plan = _event_plan_context(conversation_id, workflow_id, run_id, root, model)
            self._emit(
                plan,
                ExecutionEventType.EXIT_CODE,
                f"llama-server-Prozess beendet · Exitcode {exit_code}.",
                exit_code=exit_code,
                source="llama-server",
            )
            self._emit(
                plan,
                ExecutionEventType.STATUS,
                "Lokaler llama-server ist beendet.",
                source="llama-server",
            )

    def _stop_owned_process(self, process: ManagedProcess, plan: RuntimeLaunchPlan) -> bool:
        with self._lock:
            if self._process is not process:
                return False
        if process.poll() is None:
            self._emit(plan, ExecutionEventType.STATUS, "Stop angefordert · nur der App-eigene Prozess wird beendet.")
            try:
                process.terminate()
                try:
                    exit_code = process.wait(timeout=self.STOP_TIMEOUT_SECONDS)
                except (subprocess.TimeoutExpired, TimeoutError):
                    with self._lock:
                        still_owned = self._process is process
                    if not still_owned:
                        return True
                    self._emit(plan, ExecutionEventType.PROGRESS, "Prozess reagiert nicht; beende nur den App-eigenen Prozess.")
                    process.kill()
                    exit_code = process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
                self._emit(plan, ExecutionEventType.ERROR, "Der App-eigene Prozess konnte nicht sicher beendet werden.")
                raise RuntimeWorkflowError("Der App-eigene Prozess konnte nicht sicher beendet werden.") from exc
        else:
            exit_code = process.poll()
            if exit_code is None:
                exit_code = -1
        self._finish_process(process, exit_code)
        return True

    def _list_models(self, endpoint: str, plan: RuntimeLaunchPlan) -> tuple[str, ...]:
        connection = HTTPConnection("127.0.0.1", plan.port, timeout=3.0)
        try:
            connection.request("GET", "/v1/models", headers={"Accept": "application/json"})
            response = connection.getresponse()
            payload = response.read(1_000_000).decode("utf-8", errors="replace")
            if response.status != 200:
                raise RuntimeWorkflowError(f"/v1/models antwortete mit HTTP {response.status}.")
            import json

            body = json.loads(payload)
            rows = body.get("data", []) if isinstance(body, dict) else []
            models = tuple(
                row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
            )
            self._emit(
                plan,
                ExecutionEventType.TOOL_RESULT,
                f"Runtime meldet {len(models)} Modellkennung(en) über den lokalen /v1/models-Endpunkt.",
                source="llama-server HTTP health",
            )
            return models
        except Exception as exc:  # noqa: BLE001 -- server remains up if only model listing fails.
            self._emit(
                plan,
                ExecutionEventType.ERROR,
                f"Server ist gesund, aber /v1/models konnte nicht gelesen werden: {exc}",
                source="llama-server HTTP health",
            )
            return ()
        finally:
            connection.close()

    def _emit_command_output(
        self,
        text: str,
        event_type: ExecutionEventType,
        plan: RuntimeLaunchPlan,
        source: str,
    ) -> None:
        for index, line in enumerate(text.splitlines()):
            if index >= 30:
                self._emit(
                    plan,
                    ExecutionEventType.TRUNCATED,
                    f"{source}: weitere Preflight-Ausgabe wurde nach 30 Zeilen gekürzt.",
                    source=source,
                )
                return
            self._emit_bounded_output(plan, event_type, line, source)

    def _emit(
        self,
        plan: RuntimeLaunchPlan,
        event_type: ExecutionEventType,
        text: str,
        *,
        exit_code: int | None = None,
        source: str = "runtime-workflow",
        provenance: tuple[tuple[str, str], ...] = (),
    ) -> None:
        safe_text = redact_text(
            text,
            scoped_paths=(plan.model_root, plan.model_path, plan.runtime_directory),
        )
        event = ExecutionEvent(
            conversation_id=plan.conversation_id,
            workflow_id=plan.workflow_id,
            run_id=plan.run_id,
            event_type=event_type,
            text=safe_text,
            source=source,
            exit_code=exit_code,
            provenance=provenance,
        )
        try:
            self._event_sink(event)
        except Exception:  # noqa: BLE001 -- telemetry must not take down process safety.
            return


def _coerce_backend(value: Backend | RuntimeBackend | str) -> Backend:
    if isinstance(value, Backend):
        return value
    if isinstance(value, RuntimeBackend):
        return value.planner_backend
    try:
        return Backend(str(value).casefold())
    except ValueError as exc:
        raise RuntimeWorkflowError("Unbekannter llama.cpp-Backend-Build.") from exc


def _validate_model_root(value: str | os.PathLike[str]) -> Path:
    try:
        selected = Path(value).expanduser()
        info = selected.lstat()
        if selected.is_symlink() or _is_reparse_point(getattr(info, "st_file_attributes", 0)):
            raise RuntimeWorkflowError("Der Modellordner darf kein Symlink oder Reparse-Point sein.")
        root = selected.resolve(strict=True)
    except RuntimeWorkflowError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeWorkflowError("Der ausdrücklich ausgewählte Modellordner ist nicht verfügbar.") from exc
    if not root.is_dir():
        raise RuntimeWorkflowError("Der Modellpfad muss ein vorhandener, regulärer Ordner sein.")
    return root


def _validate_model_file(root: Path, value: str | os.PathLike[str]) -> Path:
    try:
        selected = Path(value).expanduser()
        info = selected.lstat()
        if selected.is_symlink() or _is_reparse_point(getattr(info, "st_file_attributes", 0)):
            raise RuntimeWorkflowError("Die GGUF-Datei darf kein Symlink oder Reparse-Point sein.")
        model = selected.resolve(strict=True)
    except RuntimeWorkflowError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeWorkflowError("Die ausgewählte Modelldatei ist nicht verfügbar.") from exc
    try:
        model.relative_to(root)
    except ValueError as exc:
        raise RuntimeWorkflowError("Die GGUF-Datei liegt außerhalb des ausgewählten Modellroots.") from exc
    if model == root or model.suffix.casefold() != ".gguf":
        raise RuntimeWorkflowError("Nur eine GGUF-Datei innerhalb des ausgewählten Modellroots ist zulässig.")
    parent = model.parent
    while parent != root:
        try:
            info = parent.lstat()
        except OSError as exc:
            raise RuntimeWorkflowError("Ein Modell-Unterordner ist nicht mehr verfügbar.") from exc
        if parent.is_symlink() or _is_reparse_point(getattr(info, "st_file_attributes", 0)):
            raise RuntimeWorkflowError("Modell-Unterordner dürfen keine Symlinks oder Reparse-Points sein.")
        parent = parent.parent
    if not model.is_file():
        raise RuntimeWorkflowError("Die ausgewählte Modelldatei ist kein reguläres, unverlinktes File.")
    return model


def _is_reparse_point(attributes: int) -> bool:
    return bool(attributes & 0x400)


def _parse_mib_number(value: str) -> int | None:
    try:
        number = float(value.strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _parse_runtime_devices(text: str, backend: Backend) -> tuple[tuple[str, str], ...]:
    prefix = "cuda" if backend is Backend.CUDA else "vulkan"
    devices: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _DEVICE_LINE.search(line.strip())
        if match:
            current_prefix = "cuda" if match.group(1).casefold() == "cuda" else "vulkan"
            if current_prefix == prefix:
                name = match.group(3).split(" (", 1)[0].strip()
                devices.append((f"{current_prefix}:{int(match.group(2))}", name))
            continue
        generic = _GENERIC_DEVICE_LINE.search(line.strip())
        if generic and backend is not Backend.CPU:
            devices.append((f"{prefix}:{int(generic.group(1))}", generic.group(2).strip()))
    return tuple(devices)


def _normal_name(value: str) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    ignored = {"nvidia", "geforce", "amd", "radeon", "graphics", "gpu", "tm", "corporation"}
    return tuple(token for token in tokens if token not in ignored)


def _match_nvidia_device(
    runtime_name: str, devices: Sequence[PhysicalDeviceSnapshot]
) -> PhysicalDeviceSnapshot | None:
    runtime_tokens = set(_normal_name(runtime_name))
    if not runtime_tokens:
        return None
    matches: list[PhysicalDeviceSnapshot] = []
    for device in devices:
        device_tokens = set(_normal_name(device.name))
        if runtime_tokens.issubset(device_tokens) or device_tokens.issubset(runtime_tokens):
            matches.append(device)
    return matches[0] if len(matches) == 1 else None


def _estimate_kv_cache_mib(model_path: Path, context_tokens: int) -> int | None:
    """Estimate an fp16 KV cache from bounded GGUF metadata with 25% headroom."""
    result = GGUFMetadataReader().read(model_path)
    document = result.metadata
    if document is None:
        return None
    architecture_field = document.field("general.architecture")
    if architecture_field is None or not isinstance(architecture_field.value, str):
        return None
    prefix = architecture_field.value

    def integer(suffix: str) -> int | None:
        field = document.field(f"{prefix}.{suffix}")
        value = field.value if field is not None else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    layers = integer("block_count")
    kv_heads = integer("attention.head_count_kv") or integer("attention.head_count")
    key_length = integer("attention.key_length")
    value_length = integer("attention.value_length")
    if key_length is None or value_length is None:
        embedding = integer("embedding_length")
        heads = integer("attention.head_count")
        if embedding is None or heads is None or embedding % heads:
            return None
        head_dimension = embedding // heads
        key_length = key_length or head_dimension
        value_length = value_length or head_dimension
    if not layers or not kv_heads or not key_length or not value_length:
        return None
    estimated_bytes = layers * context_tokens * kv_heads * (key_length + value_length) * 2
    return max(1, math.ceil(estimated_bytes * 1.25 / MIB))


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _child_environment(runtime_directory: Path) -> dict[str, str]:
    """Build a child-only DLL search path and omit secret-looking environment keys."""
    allowed_names = {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "COMSPEC",
        "PATHEXT",
    }
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in allowed_names and not _SECRET_ENV_KEY.search(name)
    }
    system_root = env.get("SYSTEMROOT", env.get("WINDIR", r"C:\Windows"))
    system32 = str(Path(system_root) / "System32")
    env["PATH"] = os.pathsep.join((str(runtime_directory), system32))
    return env


def _probe_health(endpoint: str, timeout: float) -> bool:
    try:
        parsed = endpoint.removeprefix("http://127.0.0.1:").removesuffix("/v1")
        port = int(parsed)
    except (TypeError, ValueError):
        return False
    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        response.read(4096)
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


def _event_plan_context(
    conversation_id: str,
    workflow_id: str,
    run_id: str,
    root: Path,
    model_path: Path,
) -> RuntimeLaunchPlan:
    """Minimal plan-shaped context used before telemetry planning is complete."""
    return RuntimeLaunchPlan(
        conversation_id=conversation_id,
        workflow_id=workflow_id,
        run_id=run_id,
        backend=Backend.CPU,
        model_root=root,
        model_path=model_path,
        runtime_directory=APP_ROOT,
        resource_plan=None,
        target_id=None,
        target_name="",
        context_tokens=0,
        requested_threads=0,
        port=0,
    )
from dbzscc.miro.model_registry import ModelCatalogEntry
