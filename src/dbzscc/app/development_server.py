"""Manifest-bound local development server and PySide6 app lifecycle.

Only two typed project targets are supported: a declared PySide6 module or a
Uvicorn ASGI app. Commands are composed from fixed argv templates; no shell,
free-form command, PATH lookup, package installation, or unrelated process stop
is allowed.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import ClassVar, Protocol, TextIO

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 installations may supply tomli as project dependency.
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 without tomli.
        tomllib = None

from dbzscc.miro.model_registry import ModelCatalogEntry

from .execution_events import ExecutionEvent, ExecutionEventType, redact_text
from .project_execution import (
    ProjectExecutionError,
    _ProcessTreeGuard,
    _project_fingerprint,
    _validate_project_python,
    _validate_project_root,
)
from .resource_orchestrator import (
    Backend,
    BackendCapability,
    BackendTarget,
    ResourceOrchestrator,
    ResourcePlan,
)
from .runtime_service import WindowsResourceMonitor
from .toolchain_runtime import ResourceDemand, plan_resource_demand
from .workflow_runtime import WorkflowAction, WorkflowPlan

_MODULE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_ATTRIBUTE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_HEALTH_PATH = re.compile(r"^/[A-Za-z0-9/_-]{0,127}$")


class DevelopmentServerError(RuntimeError):
    """Safe, actionable error from manifest validation or process lifecycle."""


class DevelopmentTarget(str, Enum):
    PYSIDE6_MODULE = "pyside6-module"
    UVICORN = "uvicorn"


class ManagedProcess(Protocol):
    pid: int
    stdout: TextIO | None
    stderr: TextIO | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ProcessRunner(Protocol):
    def start(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> ManagedProcess: ...


class SubprocessRunner:
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
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )


@dataclass(frozen=True)
class DevelopmentServerPlan:
    target: DevelopmentTarget
    action: WorkflowAction
    argv: tuple[str, ...]
    project_root: Path
    python_executable: Path
    module_or_app: str
    port: int
    endpoint: str | None
    health_path: str | None
    debug: bool
    startup_timeout_seconds: float
    project_fingerprint: str
    manifest_sha256: str
    resource_plan: ResourcePlan
    resource_demand: ResourceDemand
    conversation_id: str
    workflow_id: str
    run_id: str
    model_catalog_entry: ModelCatalogEntry | None = None


@dataclass(frozen=True)
class DevelopmentServerStartResult:
    plan: DevelopmentServerPlan
    process_id: int
    endpoint: str | None


class DevelopmentServerController:
    """Prepare, revalidate, launch, health-check and stop one owned process."""

    STARTUP_TIMEOUT_SECONDS = 45.0
    STOP_TIMEOUT_SECONDS = 5.0
    STARTUP_STABILITY_SECONDS = 0.8
    MAX_OUTPUT_EVENTS = 160
    MAX_OUTPUT_CHARS = 24_000
    MAX_OUTPUT_LINE_CHARS = 1_000
    MEMORY_DEMAND_MIB: ClassVar[dict[DevelopmentTarget, int]] = {
        DevelopmentTarget.PYSIDE6_MODULE: 1536,
        DevelopmentTarget.UVICORN: 768,
    }

    def __init__(
        self,
        *,
        event_sink: Callable[[ExecutionEvent], None] | None = None,
        runner: ProcessRunner | None = None,
        resource_monitor: object | None = None,
        resource_orchestrator: ResourceOrchestrator | None = None,
        process_tree_factory: Callable[[object], object] | None = None,
        health_probe: Callable[[str, str, float], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._event_sink = event_sink or (lambda _event: None)
        self._runner = runner or SubprocessRunner()
        self._monitor = resource_monitor or WindowsResourceMonitor()
        self._orchestrator = resource_orchestrator or ResourceOrchestrator()
        self._tree_factory = process_tree_factory or _ProcessTreeGuard
        self._health_probe = health_probe or _http_health_probe
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.RLock()
        self._process: ManagedProcess | None = None
        self._tree_guard: object | None = None
        self._plan: DevelopmentServerPlan | None = None
        self._watcher: threading.Thread | None = None
        self._readers: list[threading.Thread] = []
        self._output_counts = {"events": 0, "chars": 0, "truncated": False}

    def prepare(
        self,
        workflow_plan: WorkflowPlan,
        action: WorkflowAction,
        *,
        project_root: str | os.PathLike[str],
        python_executable: str | os.PathLike[str],
        port: int,
        conversation_id: str,
        workflow_id: str,
    ) -> DevelopmentServerPlan:
        if (
            not isinstance(workflow_plan, WorkflowPlan)
            or not isinstance(action, WorkflowAction)
            or action not in workflow_plan.actions
        ):
            raise DevelopmentServerError("Die Serveraktion ist nicht Teil des validierten Workflowplans.")
        catalog_entry = workflow_plan.model_catalog_entry
        if (
            not isinstance(catalog_entry, ModelCatalogEntry)
            or catalog_entry.observation is None
            or catalog_entry.artifact.status != "metadata_read"
            or catalog_entry.observation.status != "metadata_read"
            or catalog_entry.artifact.current_observation_id
            != catalog_entry.observation.observation_id
            or workflow_plan.model_artifact_id != catalog_entry.artifact.artifact_id
            or workflow_plan.model_observation_id != catalog_entry.observation.observation_id
        ):
            raise DevelopmentServerError("Der Workflowplan besitzt keinen aktuellen zentralen MIRO-Modellbeleg.")
        if action not in {WorkflowAction.START_DEVELOPMENT_SERVER, WorkflowAction.DEBUG_PROJECT}:
            raise DevelopmentServerError("Nur die typisierten Server- und Debug-Aktionen werden unterstützt.")
        if not conversation_id.strip() or not workflow_id.strip():
            raise DevelopmentServerError("Chat- und Workflow-ID müssen vorhanden sein.")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise DevelopmentServerError("Der lokale Port muss zwischen 1024 und 65535 liegen.")
        try:
            root = _validate_project_root(project_root)
            python = _validate_project_python(root, python_executable)
            target, module_or_app, health_path, manifest_digest = _read_manifest(root)
            fingerprint = _project_fingerprint(root)
            _validate_target_source(root, target, module_or_app)
        except (ProjectExecutionError, OSError, ValueError) as exc:
            raise DevelopmentServerError(str(exc) or "Projektmanifest konnte nicht validiert werden.") from exc
        if target is DevelopmentTarget.UVICORN:
            _ensure_port_available(port)
            endpoint = f"http://127.0.0.1:{port}"
            args = ["-B"]
            if action is WorkflowAction.DEBUG_PROJECT:
                args.extend(("-X", "dev", "-X", "faulthandler"))
            args.extend(("-m", "uvicorn", module_or_app, "--host", "127.0.0.1", "--port", str(port), "--no-access-log"))
            if action is WorkflowAction.DEBUG_PROJECT:
                args.append("--reload")
        else:
            endpoint = None
            args = ["-B"]
            if action is WorkflowAction.DEBUG_PROJECT:
                args.extend(("-X", "dev", "-X", "faulthandler"))
            args.extend(("-m", module_or_app))
        argv = (str(python), *args)
        demand = ResourceDemand(
            workload_id=f"devserver-{uuid.uuid4().hex[:12]}",
            runtime_id=f"python-{target.value}",
            peak_memory_mib=self.MEMORY_DEMAND_MIB[target],
            cpu_threads=2,
            min_cpu_threads=1,
        )
        resource_plan = self._resource_plan(demand)
        if not resource_plan.admitted:
            details = "; ".join(resource_plan.fail_closed_reasons or resource_plan.reasons)
            raise DevelopmentServerError("Frische Ressourcenprüfung abgelehnt; kein Prozess gestartet. " + details)
        return DevelopmentServerPlan(
            target=target,
            action=action,
            argv=argv,
            project_root=root,
            python_executable=python,
            module_or_app=module_or_app,
            port=port,
            endpoint=endpoint,
            health_path=health_path,
            debug=action is WorkflowAction.DEBUG_PROJECT,
            startup_timeout_seconds=self.STARTUP_TIMEOUT_SECONDS,
            project_fingerprint=fingerprint,
            manifest_sha256=manifest_digest,
            resource_plan=resource_plan,
            resource_demand=demand,
            conversation_id=conversation_id,
            workflow_id=workflow_id,
            run_id=uuid.uuid4().hex,
            model_catalog_entry=catalog_entry,
        )

    def start(
        self, plan: DevelopmentServerPlan, cancel_event: threading.Event
    ) -> DevelopmentServerStartResult:
        if not isinstance(plan, DevelopmentServerPlan) or not isinstance(cancel_event, threading.Event):
            raise TypeError("A prepared DevelopmentServerPlan and threading.Event are required.")
        if plan.action not in {WorkflowAction.START_DEVELOPMENT_SERVER, WorkflowAction.DEBUG_PROJECT}:
            raise DevelopmentServerError("Nicht erlaubte Entwicklungsaktion.")
        if type(plan.port) is not int or not 1024 <= plan.port <= 65535:
            raise DevelopmentServerError("Der bestätigte Loopback-Port liegt außerhalb des erlaubten Bereichs.")
        if plan.argv[0] != str(plan.python_executable):
            raise DevelopmentServerError("Der Interpreter stimmt nicht mit dem bestätigten argv überein.")
        try:
            root = _validate_project_root(plan.project_root)
            python = _validate_project_python(root, plan.python_executable)
            target, module_or_app, health_path, manifest_digest = _read_manifest(root)
            fingerprint = _project_fingerprint(root)
            _validate_target_source(root, target, module_or_app)
        except (ProjectExecutionError, OSError, ValueError) as exc:
            raise DevelopmentServerError("Manifest-, Pfad- oder Projektprüfung vor Start fehlgeschlagen.") from exc
        if (root, python, target, module_or_app, health_path, manifest_digest, fingerprint) != (
            plan.project_root,
            plan.python_executable,
            plan.target,
            plan.module_or_app,
            plan.health_path,
            plan.manifest_sha256,
            plan.project_fingerprint,
        ):
            raise DevelopmentServerError("Projekt oder Manifest hat sich seit der Bestätigung geändert; bitte neu planen.")
        expected_endpoint = (
            f"http://127.0.0.1:{plan.port}"
            if target is DevelopmentTarget.UVICORN
            else None
        )
        if (
            plan.endpoint != expected_endpoint
            or plan.startup_timeout_seconds != self.STARTUP_TIMEOUT_SECONDS
            or plan.resource_demand.runtime_id != f"python-{target.value}"
            or plan.resource_demand.peak_memory_mib != self.MEMORY_DEMAND_MIB[target]
            or plan.resource_demand.cpu_threads != 2
            or plan.resource_demand.min_cpu_threads != 1
            or not plan.conversation_id.strip()
            or not plan.workflow_id.strip()
            or not plan.run_id.strip()
        ):
            raise DevelopmentServerError("Der bestätigte Plan enthält inkonsistente Endpoint-, Ressourcen- oder Laufdaten.")
        expected = self._argv(plan)
        if expected != plan.argv:
            raise DevelopmentServerError("Der bestätigte Befehl entspricht nicht der festen Allowlist.")
        if plan.endpoint:
            _ensure_port_available(plan.port)
        fresh = self._resource_plan(plan.resource_demand)
        if not fresh.admitted:
            raise DevelopmentServerError("Frische Ressourcenprüfung vor Start abgelehnt.")
        plan = replace(plan, resource_plan=fresh)
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise DevelopmentServerError("Ein von dieser App gestarteter Entwicklungsprozess läuft bereits.")
            self._output_counts = {"events": 0, "chars": 0, "truncated": False}
        self._emit(plan, ExecutionEventType.WORKFLOW_STARTED, "Entwicklungsprozess gestartet · app-eigener Prozessbaum.")
        self._emit(
            plan,
            ExecutionEventType.STATUS,
            f"Manifest und Ressourcen erneut geprüft · {fresh.status.value} · Hostbedarf {fresh.host_memory_mib} MiB · CPU {fresh.cpu_threads} Threads.",
        )
        self._emit(plan, ExecutionEventType.TERMINAL_COMMAND, self._command_description(plan), source="development-server")
        try:
            process = self._runner.start(plan.argv, cwd=plan.project_root, env=_child_environment(plan))
        except (OSError, ValueError) as exc:
            self._emit(plan, ExecutionEventType.ERROR, "Der freigegebene Entwicklungsprozess konnte nicht gestartet werden.")
            raise DevelopmentServerError("Der freigegebene Entwicklungsprozess konnte nicht gestartet werden.") from exc
        try:
            tree_guard = self._tree_factory(process)
        except Exception as exc:
            try:
                process.terminate()
                process.wait(timeout=self.STOP_TIMEOUT_SECONDS)
            except (OSError, subprocess.SubprocessError, AttributeError):
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except (OSError, subprocess.SubprocessError, AttributeError):
                    pass
            raise DevelopmentServerError("Prozessbaumaufsicht fehlgeschlagen; der Prozess wurde gestoppt.") from exc
        with self._lock:
            self._process = process
            self._tree_guard = tree_guard
            self._plan = plan
        self._emit(plan, ExecutionEventType.PROGRESS, f"Prozess gestartet · PID {process.pid}.", provenance=(("pid", str(process.pid)),))
        self._start_output_readers(process, plan)
        deadline = self._monotonic() + plan.startup_timeout_seconds
        ready_since: float | None = None
        while self._monotonic() < deadline:
            if cancel_event.is_set():
                self._stop_owned_process(process, plan, tree_guard)
                raise DevelopmentServerError("Start abgebrochen · app-eigener Prozessbaum beendet.")
            exit_code = process.poll()
            if exit_code is not None:
                self._finish(process, plan, tree_guard, exit_code)
                self._emit(plan, ExecutionEventType.ERROR, f"Entwicklungsprozess endete beim Start mit Exitcode {exit_code}.", exit_code=exit_code)
                raise DevelopmentServerError(f"Entwicklungsprozess endete beim Start mit Exitcode {exit_code}.")
            healthy = (
                self._health_probe(plan.endpoint, plan.health_path or "/health", 0.5)
                if plan.endpoint
                else process.poll() is None
            )
            if healthy:
                ready_since = ready_since or self._monotonic()
                if self._monotonic() - ready_since >= self.STARTUP_STABILITY_SECONDS:
                    self._emit(
                        plan,
                        ExecutionEventType.STATUS,
                        "Loopback-Healthcheck erfolgreich · Entwicklungsserver bereit."
                        if plan.endpoint
                        else "Prozess-Liveness-Healthcheck erfolgreich · PySide6-App bereit.",
                        provenance=(("endpoint", plan.endpoint or "process-liveness"),),
                    )
                    watcher = threading.Thread(
                        target=self._watch_process,
                        args=(process, plan, tree_guard),
                        name=f"dbzscc-devserver-watch-{process.pid}",
                        daemon=True,
                    )
                    with self._lock:
                        self._watcher = watcher
                    watcher.start()
                    return DevelopmentServerStartResult(plan, process.pid, plan.endpoint)
            else:
                ready_since = None
            self._sleep(0.2)
        self._emit(plan, ExecutionEventType.ERROR, "Startup-Timeout · Healthcheck wurde nicht bereit.")
        self._stop_owned_process(process, plan, tree_guard)
        raise DevelopmentServerError("Startup-Timeout; der app-eigene Prozessbaum wurde beendet.")

    def stop(self) -> bool:
        with self._lock:
            process, plan, guard = self._process, self._plan, self._tree_guard
        if process is None or plan is None or guard is None:
            return False
        self._emit(plan, ExecutionEventType.STATUS, "Stop angefordert · nur der app-eigene Prozessbaum wird beendet.")
        self._stop_owned_process(process, plan, guard)
        return True

    @property
    def has_owned_process(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def managed_endpoint(self) -> str | None:
        with self._lock:
            if self._process is None or self._process.poll() is not None or self._plan is None:
                return None
            return self._plan.endpoint

    def _stop_owned_process(self, process: ManagedProcess, plan: DevelopmentServerPlan, guard: object) -> None:
        try:
            guard.terminate(process)
        except (OSError, RuntimeError, AttributeError, TypeError, ValueError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            exit_code = process.wait(timeout=self.STOP_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
            try:
                process.kill()
                exit_code = process.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                exit_code = process.poll()
        self._finish(process, plan, guard, exit_code)

    def _watch_process(self, process: ManagedProcess, plan: DevelopmentServerPlan, guard: object) -> None:
        while process.poll() is None:
            self._sleep(0.2)
        self._finish(process, plan, guard, process.poll())

    def _finish(self, process: ManagedProcess, plan: DevelopmentServerPlan, guard: object, exit_code: int | None) -> None:
        with self._lock:
            if self._process is not process:
                return
            self._process = None
            self._tree_guard = None
            self._plan = None
        try:
            guard.close()
        except (OSError, AttributeError):
            pass
        self._emit(
            plan,
            ExecutionEventType.EXIT_CODE,
            f"Entwicklungsprozess beendet · Exitcode {exit_code}.",
            exit_code=exit_code,
            provenance=(("action", plan.action.value),),
        )

    def _start_output_readers(self, process: ManagedProcess, plan: DevelopmentServerPlan) -> None:
        for stream, event_type, label in (
            (process.stdout, ExecutionEventType.STDOUT, "stdout"),
            (process.stderr, ExecutionEventType.STDERR, "stderr"),
        ):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._read_output,
                args=(stream, event_type, label, plan),
                name=f"dbzscc-devserver-{label}-{process.pid}",
                daemon=True,
            )
            thread.start()
            self._readers.append(thread)

    def _read_output(self, stream: TextIO, event_type: ExecutionEventType, label: str, plan: DevelopmentServerPlan) -> None:
        try:
            for line in iter(stream.readline, ""):
                safe = redact_text(line, scoped_paths=(plan.project_root, plan.python_executable), max_chars=self.MAX_OUTPUT_LINE_CHARS)
                if not safe:
                    continue
                with self._lock:
                    counts = self._output_counts
                    if counts["events"] >= self.MAX_OUTPUT_EVENTS or counts["chars"] + len(safe) > self.MAX_OUTPUT_CHARS:
                        emit_line = False
                        if not counts["truncated"]:
                            counts["truncated"] = True
                            emit_truncated = True
                        else:
                            emit_truncated = False
                    else:
                        counts["events"] += 1
                        counts["chars"] += len(safe)
                        emit_truncated = False
                        emit_line = True
                if emit_truncated:
                    self._emit(plan, ExecutionEventType.TRUNCATED, "Weitere Serverausgabe wurde wegen des Limits verworfen.")
                elif emit_line:
                    self._emit(plan, event_type, safe, provenance=(("stream", label),))
        except (OSError, ValueError):
            self._emit(plan, ExecutionEventType.STATUS, f"{label} konnte nicht weiter gelesen werden.")

    def _resource_plan(self, demand: ResourceDemand) -> ResourcePlan:
        try:
            snapshot = self._monitor.capture()
            cpu = BackendCapability(Backend.CPU, True, (BackendTarget("project-cpu", None),))
            snapshot = replace(snapshot, backends=tuple(item for item in snapshot.backends if item.backend is not Backend.CPU) + (cpu,))
            return plan_resource_demand(demand, snapshot, self._orchestrator, now_monotonic_s=self._monotonic())
        except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
            raise DevelopmentServerError("Windows konnte keine gültige frische Ressourcenplanung liefern.") from exc

    @staticmethod
    def _argv(plan: DevelopmentServerPlan) -> tuple[str, ...]:
        args = ["-B"]
        if plan.debug:
            args.extend(("-X", "dev", "-X", "faulthandler"))
        if plan.target is DevelopmentTarget.PYSIDE6_MODULE:
            args.extend(("-m", plan.module_or_app))
        elif plan.target is DevelopmentTarget.UVICORN:
            args.extend(("-m", "uvicorn", plan.module_or_app, "--host", "127.0.0.1", "--port", str(plan.port), "--no-access-log"))
            if plan.debug:
                args.append("--reload")
        else:  # pragma: no cover - enum protects normal callers.
            raise DevelopmentServerError("Unbekannter Entwicklungszieltyp.")
        return (str(plan.python_executable), *args)

    def _emit(self, plan: DevelopmentServerPlan, event_type: ExecutionEventType, text: str, *, source: str = "development-server", exit_code: int | None = None, provenance: tuple[tuple[str, str], ...] = ()) -> None:
        base = (
            ("target", plan.target.value),
            ("action", plan.action.value),
            ("python", plan.python_executable.relative_to(plan.project_root).as_posix()),
            ("manifest_sha256", plan.manifest_sha256[:16]),
            ("resource_status", plan.resource_plan.status.value),
        )
        try:
            self._event_sink(ExecutionEvent(
                conversation_id=plan.conversation_id,
                workflow_id=plan.workflow_id,
                run_id=plan.run_id,
                event_type=event_type,
                text=text,
                source=source,
                exit_code=exit_code,
                provenance=base + provenance,
            ))
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    @staticmethod
    def _command_description(plan: DevelopmentServerPlan) -> str:
        safe_args = " ".join(plan.argv[1:])
        return f"python {safe_args} · cwd=ausgewählte Projektwurzel · endpoint={plan.endpoint or 'process-liveness'}"


def _read_manifest(root: Path) -> tuple[DevelopmentTarget, str, str | None, str]:
    manifest = root / "pyproject.toml"
    if tomllib is None:
        raise DevelopmentServerError("TOML-Manifestlesen benötigt unter Python 3.10 das vorhandene tomli-Paket.")
    try:
        metadata = manifest.lstat()
        if not manifest.is_file() or manifest.is_symlink() or metadata.st_size > 65_536:
            raise DevelopmentServerError("pyproject.toml fehlt, ist nicht regulär oder überschreitet 64 KiB.")
        content = manifest.read_bytes()
        data = tomllib.loads(content.decode("utf-8"))
    except DevelopmentServerError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise DevelopmentServerError("pyproject.toml konnte nicht sicher gelesen oder geparst werden.") from exc
    config = data.get("tool", {}).get("dbzscc", {}).get("devserver")
    if not isinstance(config, dict):
        raise DevelopmentServerError("Kein [tool.dbzscc.devserver]-Manifest im ausgewählten Projekt.")
    target_text = config.get("kind")
    try:
        target = DevelopmentTarget(target_text)
    except (ValueError, TypeError) as exc:
        raise DevelopmentServerError("Manifest-`kind` muss `pyside6-module` oder `uvicorn` sein.") from exc
    if target is DevelopmentTarget.PYSIDE6_MODULE:
        if set(config) != {"kind", "module"}:
            raise DevelopmentServerError("PySide6-Manifest erlaubt ausschließlich `kind` und `module`.")
        module = config.get("module")
        if not isinstance(module, str) or not _MODULE.fullmatch(module):
            raise DevelopmentServerError("Manifest-`module` muss ein Python-Modulname sein.")
        dependencies = data.get("project", {}).get("dependencies", [])
        if not any(isinstance(dep, str) and dep.split(";")[0].strip().lower().startswith("pyside6") for dep in dependencies):
            raise DevelopmentServerError("PySide6-Manifest muss PySide6 als Projektabhängigkeit deklarieren.")
        return target, module, None, hashlib.sha256(content).hexdigest()
    if not set(config).issubset({"kind", "app", "health_path"}) or "app" not in config:
        raise DevelopmentServerError("Uvicorn-Manifest erlaubt nur `kind`, `app` und optional `health_path`.")
    app = config["app"]
    module, separator, attribute = app.partition(":") if isinstance(app, str) else ("", "", "")
    if not separator or not _MODULE.fullmatch(module) or not _ATTRIBUTE.fullmatch(attribute):
        raise DevelopmentServerError("Manifest-`app` muss dem festen Importformat `paket.modul:app` entsprechen.")
    health_path = config.get("health_path", "/health")
    if not isinstance(health_path, str) or not _HEALTH_PATH.fullmatch(health_path) or ".." in health_path.split("/"):
        raise DevelopmentServerError("Manifest-`health_path` ist ungültig; erlaubt ist nur ein lokaler Pfad ohne Query.")
    return target, app, health_path, hashlib.sha256(content).hexdigest()


def _validate_target_source(root: Path, target: DevelopmentTarget, value: str) -> None:
    module = value.partition(":")[0] if target is DevelopmentTarget.UVICORN else value
    parts = module.split(".")
    candidates = (
        root / "src" / Path(*parts).with_suffix(".py"),
        root / "src" / Path(*parts) / "__main__.py",
        root / Path(*parts).with_suffix(".py"),
        root / Path(*parts) / "__main__.py",
    )
    for candidate in candidates:
        try:
            candidate.resolve(strict=True).relative_to(root)
            info = candidate.lstat()
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate.is_file() and not candidate.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400:
            return
    raise DevelopmentServerError("Manifestziel verweist nicht auf eine reguläre, projektlokale Python-Einstiegsdatei.")


def _child_environment(plan: DevelopmentServerPlan) -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    old_path = env.get("PATH", "")
    env["PATH"] = str(plan.python_executable.parent) + (os.pathsep + old_path if old_path else "")
    src = plan.project_root / "src"
    env["PYTHONPATH"] = os.pathsep.join((str(src), str(plan.project_root)))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    if plan.debug:
        env["PYTHONDEVMODE"] = "1"
        env["PYTHONFAULTHANDLER"] = "1"
    return env


def _ensure_port_available(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind(("127.0.0.1", port))
    except OSError as exc:
        raise DevelopmentServerError("Der ausgewählte Loopback-Port ist nicht verfügbar.") from exc


def _http_health_probe(endpoint: str, path: str, timeout: float) -> bool:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", int(endpoint.rsplit(":", 1)[1]), timeout=timeout)
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        healthy = 200 <= response.status < 300
        response.read(4096)
        connection.close()
        return healthy
    except (OSError, ValueError, http.client.HTTPException):
        return False
