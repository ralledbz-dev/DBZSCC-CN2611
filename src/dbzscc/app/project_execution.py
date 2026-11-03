"""Approval-gated, app-owned execution for the local Python project stack.

Only typed LangChain actions ``run_tests`` and ``run_linter`` are accepted.
Commands, targets, project root, interpreter, environment and resource demand are
fixed or explicitly selected; no shell string, PATH discovery or model command
is ever executed.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import queue
import re
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import ClassVar, TextIO

from .execution_events import ExecutionEvent, ExecutionEventType, redact_text
from .file_edit_service import validate_working_root
from .resource_orchestrator import (
    Backend,
    BackendCapability,
    BackendTarget,
    ResourceOrchestrator,
    ResourcePlan,
)
from .runtime_service import WindowsResourceMonitor
from .toolchain_runtime import (
    CandidateScope,
    InvocationOperation,
    OneShotInvocation,
    OneShotInvocationPlan,
    OneShotOutcome,
    OneShotState,
    ResourceDemand,
    RuntimeCandidateSpec,
    RuntimeFamily,
    RuntimeRegistry,
    VersionEvidence,
    plan_one_shot_invocation,
    plan_resource_demand,
)
from .workflow_runtime import WorkflowAction, WorkflowPlan


class ProjectExecutionError(RuntimeError):
    """Safe, actionable error raised before or during project execution."""


class ProjectOperation(str, Enum):
    TESTS = WorkflowAction.RUN_TESTS.value
    LINTER = WorkflowAction.RUN_LINTER.value


@dataclass(frozen=True)
class ProjectExecutionPlan:
    """A typed action bound to one project snapshot, interpreter and run ID."""

    action: ProjectOperation
    invocation: OneShotInvocationPlan
    resource_demand: ResourceDemand
    resource_plan: ResourcePlan
    conversation_id: str
    workflow_id: str
    run_id: str
    project_root: Path
    project_fingerprint: str
    python_relative_path: str

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.invocation.executable_path), *self.invocation.argv)


@dataclass(frozen=True)
class ProjectExecutionResult:
    plan: ProjectExecutionPlan
    outcome: OneShotOutcome
    output_summary: str
    dropped_output_lines: int = 0


@dataclass(frozen=True)
class _OutputItem:
    event_type: ExecutionEventType
    text: str


class _ProcessTreeGuard:
    """Windows Job Object guard that owns and terminates only this process tree."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self, process) -> None:
        self._handle = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        kernel.SetInformationJobObject.restype = ctypes.c_int
        kernel.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel.AssignProcessToJobObject.restype = ctypes.c_int
        kernel.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.TerminateJobObject.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_int
        self._kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            self._stop_unassigned_process(process)
            raise ProjectExecutionError("Windows konnte keinen Prozessbaum-Job erstellen.")
        self._handle = handle
        try:
            info = _ExtendedLimitInformation()
            info.basic_limit_information.limit_flags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(
                handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise ProjectExecutionError("Windows konnte die Prozessbaum-Regeln nicht setzen.")
            process_handle = getattr(process, "_handle", None)
            if process_handle is None or not kernel.AssignProcessToJobObject(
                handle, ctypes.c_void_p(int(process_handle))
            ):
                raise ProjectExecutionError("Der Python-Prozess ließ sich nicht sicher überwachen.")
        except (ProjectExecutionError, OSError, TypeError, ValueError):
            self.close()
            self._stop_unassigned_process(process)
            raise

    @staticmethod
    def _stop_unassigned_process(process) -> None:
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
                process.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                return

    def terminate(self, process) -> None:
        if self._handle and not self._kernel.TerminateJobObject(self._handle, 1):
            raise ProjectExecutionError("Der app-eigene Prozessbaum konnte nicht beendet werden.")
        if not self._handle:
            process.terminate()

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._kernel.CloseHandle(handle)


class _ExtendedLimitInformation(ctypes.Structure):
    pass


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "read_operation_count",
        "write_operation_count",
        "other_operation_count",
        "read_transfer_count",
        "write_transfer_count",
        "other_transfer_count",
    )]


_ExtendedLimitInformation._fields_ = [
    ("basic_limit_information", _BasicLimitInformation),
    ("io_info", _IoCounters),
    ("process_memory_limit", ctypes.c_size_t),
    ("job_memory_limit", ctypes.c_size_t),
    ("peak_process_memory_used", ctypes.c_size_t),
    ("peak_job_memory_used", ctypes.c_size_t),
]


class ProjectExecutionService:
    """Prepare fixed Python actions and supervise their app-owned execution."""

    MAX_PLAN_ACTIONS = 2
    MAX_SNAPSHOT_FILES = 20_000
    MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
    MAX_FILE_BYTES = 32 * 1024 * 1024
    MAX_OUTPUT_EVENTS = 160
    MAX_OUTPUT_CHARS = 24_000
    MAX_REPAIR_OUTPUT_CHARS = 8_000
    OUTPUT_EVENT_CHARS = 1_000
    OUTPUT_QUEUE_SIZE = 64
    TEST_MEMORY_MIB = 2_048
    LINTER_MEMORY_MIB = 512
    CPU_THREADS = 2
    TEST_TIMEOUT_S = 600.0
    LINTER_TIMEOUT_S = 180.0
    RUNTIME_ID = "project-python"
    ENVIRONMENT_PROFILE_ID = "project-python-isolated-v1"

    _COMMANDS: ClassVar[dict[ProjectOperation, tuple[str, ...]]] = {
        ProjectOperation.TESTS: (
            "-I", "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests"
        ),
        ProjectOperation.LINTER: (
            "-I", "-B", "-m", "ruff", "check", "--no-cache", "src", "tests"
        ),
    }
    _SECRET_ENV: ClassVar[re.Pattern[str]] = re.compile(
        r"(?i)(?:API|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)"
    )
    _SKIP_DIRS: ClassVar[frozenset[str]] = frozenset(
        {".git", ".pytest_cache", ".ruff_cache", "__pycache__"}
    )
    _ROOT_CONFIGS: ClassVar[tuple[str, ...]] = (
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "ruff.toml",
        ".ruff.toml",
        "conftest.py",
    )

    def __init__(
        self,
        *,
        event_sink: Callable[[ExecutionEvent], None] | None = None,
        resource_monitor: object | None = None,
        resource_orchestrator: ResourceOrchestrator | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        process_tree_factory: Callable[[object], object] | None = None,
    ) -> None:
        self._event_sink = event_sink or (lambda _event: None)
        self._resource_monitor = resource_monitor or WindowsResourceMonitor()
        self._resource_orchestrator = resource_orchestrator or ResourceOrchestrator()
        self._monotonic = monotonic
        self._process_tree_factory = process_tree_factory or _ProcessTreeGuard
        self._lock = threading.RLock()
        self._active_process = None

    @property
    def has_active_process(self) -> bool:
        with self._lock:
            return self._active_process is not None and self._active_process.poll() is None

    def prepare(
        self,
        workflow_plan: WorkflowPlan,
        actions: tuple[WorkflowAction, ...],
        *,
        project_root: str | os.PathLike[str],
        python_executable: str | os.PathLike[str],
        conversation_id: str,
        workflow_id: str,
    ) -> tuple[ProjectExecutionPlan, ...]:
        """Validate typed allowlisted actions, exact paths and fresh resource plans."""
        if not isinstance(workflow_plan, WorkflowPlan):
            raise ProjectExecutionError("Die Workflowdaten sind kein validierter LangChain-Plan.")
        if not conversation_id.strip() or not workflow_id.strip():
            raise ProjectExecutionError("Chat- und Workflow-ID müssen vorhanden sein.")
        selected = tuple(actions)
        if not selected or len(selected) > self.MAX_PLAN_ACTIONS:
            raise ProjectExecutionError("Es können nur ein oder zwei geprüfte Aktionen freigegeben werden.")
        if len(set(selected)) != len(selected):
            raise ProjectExecutionError("Doppelte Workflow-Aktionen werden abgelehnt.")
        for action in selected:
            if not isinstance(action, WorkflowAction) or action not in workflow_plan.actions:
                raise ProjectExecutionError("Die Aktion ist nicht Teil des validierten Modellplans.")
            if action.value not in {operation.value for operation in ProjectOperation}:
                raise ProjectExecutionError("Diese Modellaktion besitzt keinen ausführbaren Consumer.")

        root = _validate_project_root(project_root)
        python_path = _validate_project_python(root, python_executable)
        _require_python_project(root)
        fingerprint = _project_fingerprint(root)
        registry = RuntimeRegistry()
        discovery = registry.discover_explicit((
            RuntimeCandidateSpec(
                runtime_id=self.RUNTIME_ID,
                family=RuntimeFamily.LANGUAGE_RUNTIME,
                executable_path=python_path,
                scope=CandidateScope.PROJECT_LOCAL,
                version_evidence=VersionEvidence.DECLARED,
            ),
        ))
        if discovery.issues or len(discovery.found) != 1:
            raise ProjectExecutionError("Der ausdrücklich gewählte Python-Interpreter ist nicht verfügbar.")

        plans: list[ProjectExecutionPlan] = []
        for action in selected:
            operation = ProjectOperation(action.value)
            module = "pytest" if operation is ProjectOperation.TESTS else "ruff"
            _require_venv_module(python_path, module)
            memory = (
                self.TEST_MEMORY_MIB
                if operation is ProjectOperation.TESTS
                else self.LINTER_MEMORY_MIB
            )
            timeout = (
                self.TEST_TIMEOUT_S
                if operation is ProjectOperation.TESTS
                else self.LINTER_TIMEOUT_S
            )
            run_id = uuid.uuid4().hex
            demand = ResourceDemand(
                workload_id=f"project-{operation.value}-{run_id[:12]}",
                runtime_id=self.RUNTIME_ID,
                peak_memory_mib=memory,
                cpu_threads=self.CPU_THREADS,
                min_cpu_threads=1,
            )
            invocation = OneShotInvocation(
                invocation_id=run_id,
                runtime_id=self.RUNTIME_ID,
                operation=InvocationOperation.GENERIC,
                argv=self._COMMANDS[operation],
                project_root=root,
                working_directory=root,
                timeout_s=timeout,
                environment_profile_id=self.ENVIRONMENT_PROFILE_ID,
                resource_demand=demand,
            )
            typed_invocation = plan_one_shot_invocation(registry, invocation)
            resource_plan = self._resource_plan(demand)
            if not resource_plan.admitted:
                reasons = "; ".join(resource_plan.fail_closed_reasons or resource_plan.reasons)
                raise ProjectExecutionError(
                    "Ressourcenprüfung abgelehnt; es wurde kein Projektprozess gestartet. "
                    + (reasons or "Keine sichere CPU-Reserve verfügbar.")
                )
            plans.append(ProjectExecutionPlan(
                action=operation,
                invocation=typed_invocation,
                resource_demand=demand,
                resource_plan=resource_plan,
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                run_id=run_id,
                project_root=root,
                project_fingerprint=fingerprint,
                python_relative_path=python_path.relative_to(root).as_posix(),
            ))
        return tuple(plans)

    def execute(
        self,
        plan: ProjectExecutionPlan,
        cancel_event: threading.Event,
    ) -> ProjectExecutionResult:
        """Revalidate evidence, re-plan resources and run one fixed command."""
        if not isinstance(plan, ProjectExecutionPlan):
            raise TypeError("Only an app-prepared ProjectExecutionPlan can be executed.")
        if not isinstance(cancel_event, threading.Event):
            raise TypeError("cancel_event must be a threading.Event.")
        if plan.action.value not in {operation.value for operation in ProjectOperation}:
            raise ProjectExecutionError("Nicht erlaubte Projektaktion.")
        if tuple(plan.invocation.argv) != self._COMMANDS[plan.action]:
            raise ProjectExecutionError("Der Invocation-Plan enthält keine feste Allowlist-argv.")
        if Path(plan.invocation.executable_path) != plan.project_root / plan.python_relative_path:
            raise ProjectExecutionError("Interpreterpfad und Projektbeleg stimmen nicht überein.")
        if _project_fingerprint(plan.project_root) != plan.project_fingerprint:
            raise ProjectExecutionError("Projektdateien haben sich seit dem Plan geändert; bitte neu planen.")
        if cancel_event.is_set():
            return ProjectExecutionResult(
                plan,
                OneShotOutcome(plan.run_id, OneShotState.CANCELLED),
                "",
            )
        fresh_resource_plan = self._resource_plan(plan.resource_demand)
        if not fresh_resource_plan.admitted:
            raise ProjectExecutionError("Frische Ressourcenprüfung vor Start abgelehnt.")
        plan = replace(plan, resource_plan=fresh_resource_plan)
        with self._lock:
            if self._active_process is not None and self._active_process.poll() is None:
                raise ProjectExecutionError("Es läuft bereits ein app-eigener Projektprozess.")
            self._active_process = None

        self._emit(
            plan,
            ExecutionEventType.WORKFLOW_STARTED,
            f"Projektaktion gestartet · {plan.action.value} · app-eigener Prozess.",
        )
        self._emit(
            plan,
            ExecutionEventType.STATUS,
            "Pfad-, Quellfingerprint- und Ressourcen-Gates erneut bestanden; "
            f"{fresh_resource_plan.status.value}, Hostreserve {fresh_resource_plan.host_memory_mib} MiB, "
            f"CPU-Threads {fresh_resource_plan.cpu_threads}.",
        )
        self._emit(
            plan,
            ExecutionEventType.TERMINAL_COMMAND,
            self._command_description(plan),
            source="project-python",
        )
        try:
            process = subprocess.Popen(
                list(plan.argv),
                cwd=plan.invocation.working_directory,
                env=self._child_environment(),
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
        except (OSError, ValueError) as exc:
            self._emit(plan, ExecutionEventType.ERROR, "Der freigegebene Python-Prozess konnte nicht gestartet werden.")
            raise ProjectExecutionError("Der freigegebene Python-Prozess konnte nicht gestartet werden.") from exc

        try:
            tree_guard = self._process_tree_factory(process)
        except (ProjectExecutionError, OSError, AttributeError, TypeError, ValueError) as exc:
            self._emit(plan, ExecutionEventType.ERROR, "Prozessbaumaufsicht konnte nicht hergestellt werden.")
            raise ProjectExecutionError("Der Prozess wurde aus Sicherheitsgründen gestoppt, da die Prozessbaumaufsicht fehlte.") from exc

        with self._lock:
            self._active_process = process
        self._emit(
            plan,
            ExecutionEventType.PROGRESS,
            f"App-eigener Prozess gestartet · PID {process.pid} · Timeout {plan.invocation.timeout_s:.0f} s.",
            provenance=(("pid", str(process.pid)),),
        )
        result = self._monitor_process(plan, process, tree_guard, cancel_event)
        with self._lock:
            if self._active_process is process:
                self._active_process = None
        return result

    def _resource_plan(self, demand: ResourceDemand) -> ResourcePlan:
        try:
            snapshot = self._resource_monitor.capture()
        except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
            raise ProjectExecutionError("Windows konnte keine frische Ressourcenmessung liefern.") from exc
        cpu = BackendCapability(Backend.CPU, True, (BackendTarget("project-cpu", None),))
        capabilities = tuple(item for item in snapshot.backends if item.backend is not Backend.CPU) + (cpu,)
        snapshot = replace(snapshot, backends=capabilities)
        try:
            return plan_resource_demand(
                demand,
                snapshot,
                self._resource_orchestrator,
                now_monotonic_s=self._monotonic(),
            )
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionError("Der Projekt-Ressourcenplan konnte nicht validiert werden.") from exc

    def _monitor_process(
        self,
        plan: ProjectExecutionPlan,
        process,
        tree_guard,
        cancel_event: threading.Event,
    ) -> ProjectExecutionResult:
        output_queue: queue.Queue[_OutputItem | None] = queue.Queue(self.OUTPUT_QUEUE_SIZE)
        dropped_lines = 0
        dropped_lock = threading.Lock()
        reader_threads: list[threading.Thread] = []

        def reader(stream: TextIO | None, event_type: ExecutionEventType) -> None:
            nonlocal dropped_lines
            try:
                if stream is None:
                    return
                for line in iter(stream.readline, ""):
                    item = _OutputItem(event_type, line)
                    try:
                        output_queue.put(item, timeout=0.2)
                    except queue.Full:
                        with dropped_lock:
                            dropped_lines += 1
            finally:
                try:
                    output_queue.put(None, timeout=0.5)
                except queue.Full:
                    pass

        for stream, event_type, name in (
            (process.stdout, ExecutionEventType.STDOUT, "stdout"),
            (process.stderr, ExecutionEventType.STDERR, "stderr"),
        ):
            thread = threading.Thread(
                target=reader,
                args=(stream, event_type),
                name=f"dbzscc-project-{name}-{process.pid}",
                daemon=True,
            )
            thread.start()
            reader_threads.append(thread)

        deadline = self._monotonic() + plan.invocation.timeout_s
        event_count = 0
        event_chars = 0
        truncated = False
        done_readers = 0
        summary_parts: list[str] = []
        summary_chars = 0
        outcome_state: OneShotState | None = None
        while process.poll() is None or done_readers < len(reader_threads) or not output_queue.empty():
            if outcome_state is None:
                if cancel_event.is_set():
                    outcome_state = OneShotState.CANCELLED
                    self._emit(plan, ExecutionEventType.STATUS, "Abbruch angefordert · beende nur den app-eigenen Prozessbaum.")
                elif self._monotonic() >= deadline:
                    outcome_state = OneShotState.TIMED_OUT
                    self._emit(plan, ExecutionEventType.ERROR, "Zeitlimit erreicht · beende nur den app-eigenen Prozessbaum.")
                if outcome_state is not None:
                    try:
                        tree_guard.terminate(process)
                    except (ProjectExecutionError, OSError, AttributeError):
                        try:
                            process.terminate()
                        except (OSError, subprocess.SubprocessError):
                            pass
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                            process.wait(timeout=2.0)
                        except (OSError, subprocess.SubprocessError):
                            pass
            try:
                item = output_queue.get(timeout=0.1)
            except queue.Empty:
                item = None
            if item is None:
                if any(not thread.is_alive() for thread in reader_threads) and done_readers < len(reader_threads):
                    done_readers = sum(not thread.is_alive() for thread in reader_threads)
                continue
            safe = redact_text(
                item.text,
                scoped_paths=(plan.project_root, plan.invocation.executable_path),
                max_chars=self.OUTPUT_EVENT_CHARS,
            )
            if not safe:
                continue
            label = "stdout" if item.event_type is ExecutionEventType.STDOUT else "stderr"
            if summary_chars < self.MAX_REPAIR_OUTPUT_CHARS:
                addition = f"[{label}] {safe}\n"
                available = self.MAX_REPAIR_OUTPUT_CHARS - summary_chars
                summary_parts.append(addition[:available])
                summary_chars += min(len(addition), available)
            if event_count < self.MAX_OUTPUT_EVENTS and event_chars + len(safe) <= self.MAX_OUTPUT_CHARS:
                self._emit(plan, item.event_type, safe)
                event_count += 1
                event_chars += len(safe)
            elif not truncated:
                truncated = True
                self._emit(plan, ExecutionEventType.TRUNCATED, "Weitere stdout/stderr-Ausgaben dieses Laufs wurden gekürzt.")

        for thread in reader_threads:
            thread.join(timeout=1.0)
        tree_guard.close()
        with dropped_lock:
            dropped = dropped_lines
        if dropped and not truncated:
            self._emit(plan, ExecutionEventType.TRUNCATED, f"Ausgabe begrenzt · {dropped} weitere Zeilen verworfen.")
        exit_code = process.poll()
        if exit_code is None:
            try:
                exit_code = process.wait(timeout=1.0)
            except (OSError, subprocess.SubprocessError):
                exit_code = None
        if outcome_state is None:
            outcome_state = OneShotState.SUCCEEDED if exit_code == 0 else OneShotState.FAILED
        exit_text = (
            f"Projektaktion {plan.action.value} abgebrochen."
            if outcome_state is OneShotState.CANCELLED
            else f"Projektaktion {plan.action.value} Zeitüberschreitung."
            if outcome_state is OneShotState.TIMED_OUT
            else f"Projektaktion {plan.action.value} endete mit Exitcode {exit_code}."
        )
        self._emit(
            plan,
            ExecutionEventType.EXIT_CODE,
            exit_text,
            exit_code=exit_code,
            provenance=(("operation", plan.action.value), ("state", outcome_state.value)),
        )
        return ProjectExecutionResult(
            plan,
            OneShotOutcome(plan.run_id, outcome_state, exit_code),
            "".join(summary_parts),
            dropped,
        )

    def _emit(
        self,
        plan: ProjectExecutionPlan,
        event_type: ExecutionEventType,
        text: str,
        *,
        source: str = "project-execution",
        exit_code: int | None = None,
        provenance: tuple[tuple[str, str], ...] = (),
    ) -> None:
        base = (
            ("action", plan.action.value),
            ("python", plan.python_relative_path),
            ("workspace_sha256", plan.project_fingerprint[:16]),
            ("resource_status", plan.resource_plan.status.value),
        )
        event = ExecutionEvent(
            conversation_id=plan.conversation_id,
            workflow_id=plan.workflow_id,
            run_id=plan.run_id,
            event_type=event_type,
            text=text,
            source=source,
            exit_code=exit_code,
            provenance=base + provenance,
        )
        try:
            self._event_sink(event)
        except (OSError, RuntimeError, TypeError, ValueError):
            # Logging failure must not change an already-approved command's behavior.
            return

    @staticmethod
    def _command_description(plan: ProjectExecutionPlan) -> str:
        args = " ".join(plan.invocation.argv)
        return (
            f"Python ({plan.python_relative_path}) {args} · cwd=. · "
            f"Workspace-SHA256 {plan.project_fingerprint[:16]}"
        )

    @classmethod
    def _child_environment(cls) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not cls._SECRET_ENV.search(key)
            and key.upper() not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["QT_QPA_PLATFORM"] = "offscreen"
        return env


def default_project_python(project_root: str | os.PathLike[str]) -> Path | None:
    """Return only a conventional, existing project-local venv interpreter."""
    try:
        root = _validate_project_root(project_root)
    except (OSError, ValueError, ProjectExecutionError):
        return None
    for relative in (
        Path(".work/venv314/Scripts/python.exe"),
        Path(".venv/Scripts/python.exe"),
        Path("venv/Scripts/python.exe"),
    ):
        candidate = root / relative
        try:
            _validate_project_python(root, candidate)
        except ProjectExecutionError:
            continue
        return candidate
    return None


def _validate_project_root(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ProjectExecutionError("Die Projektwurzel muss ausdrücklich absolut und ohne '..' gewählt sein.")
    _reject_reparse_chain(candidate)
    try:
        root = validate_working_root(candidate)
    except ValueError as exc:
        raise ProjectExecutionError(str(exc)) from exc
    if root != candidate.resolve(strict=True):
        raise ProjectExecutionError("Die Projektwurzel konnte nicht eindeutig kanonisiert werden.")
    return root


def _validate_project_python(root: Path, value: str | os.PathLike[str]) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ProjectExecutionError("Der Python-Interpreter muss als exakter absoluter Projektpfad gewählt sein.")
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise ProjectExecutionError("Der Python-Interpreter muss innerhalb der gewählten Projektwurzel liegen.") from exc
    if not relative.parts or candidate.name.casefold() != "python.exe":
        raise ProjectExecutionError("Nur ein ausdrücklich gewähltes projektlokales python.exe ist zugelassen.")
    _reject_reparse_chain(candidate)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectExecutionError("Der projektlokale Python-Interpreter ist nicht verfügbar.") from exc
    if _is_reparse(metadata) or not candidate.is_file() or resolved != candidate:
        raise ProjectExecutionError("Symlink-, Junction- und Sonderdatei-Interpreter werden abgelehnt.")
    return candidate


def _require_python_project(root: Path) -> None:
    _require_regular_file(root / "pyproject.toml", root)
    _require_directory(root / "src", root)
    _require_directory(root / "tests", root)


def _require_venv_module(python_path: Path, module: str) -> None:
    if python_path.parent.name.casefold() != "scripts":
        raise ProjectExecutionError("Der Interpreter liegt nicht in einer projektlokalen Windows-venv.")
    site_packages = python_path.parent.parent / "Lib" / "site-packages"
    module_path = site_packages / module
    entrypoint = module_path / ("__main__.py" if module == "ruff" else "__init__.py")
    try:
        metadata = module_path.lstat()
        entry_metadata = entrypoint.lstat()
    except OSError as exc:
        raise ProjectExecutionError(
            f"{module} fehlt in der gewählten Projekt-venv; wähle eine vorbereitete lokale Umgebung."
        ) from exc
    if (
        _is_reparse(metadata)
        or _is_reparse(entry_metadata)
        or not module_path.is_dir()
        or not entrypoint.is_file()
    ):
        raise ProjectExecutionError(f"{module} ist keine reguläre projektlokale Paketinstallation.")


def _require_regular_file(path: Path, root: Path) -> None:
    _validate_contained_path(path, root)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProjectExecutionError(f"Erforderliche Projektdatei fehlt: {path.name}.") from exc
    if _is_reparse(metadata) or not path.is_file():
        raise ProjectExecutionError(f"Erforderliche Projektdatei ist kein regulärer Textpfad: {path.name}.")


def _require_directory(path: Path, root: Path) -> None:
    _validate_contained_path(path, root)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProjectExecutionError(f"Erforderlicher Projektordner fehlt: {path.name}.") from exc
    if _is_reparse(metadata) or not path.is_dir():
        raise ProjectExecutionError(f"Erforderlicher Projektordner ist kein regulärer Ordner: {path.name}.")


def _validate_contained_path(path: Path, root: Path) -> None:
    try:
        path.absolute().relative_to(root)
    except ValueError as exc:
        raise ProjectExecutionError("Ein erforderlicher Projektpfad liegt außerhalb der Arbeitswurzel.") from exc
    _reject_reparse_chain(path)


def _reject_reparse_chain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError:
            continue
        if _is_reparse(metadata):
            raise ProjectExecutionError("Symlink- oder Junctionpfade sind in diesem Workflow nicht zugelassen.")


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _project_fingerprint(root: Path) -> str:
    """Hash the bounded exact source/test/config surface used by these commands."""
    paths = [root / "pyproject.toml"]
    for name in ProjectExecutionService._ROOT_CONFIGS:
        path = root / name
        if path.exists():
            _require_regular_file(path, root)
            paths.append(path)
    for tree_name in ("src", "tests"):
        tree = root / tree_name
        _require_directory(tree, root)
        for directory, dirnames, filenames in os.walk(tree, followlinks=False):
            base = Path(directory)
            keep: list[str] = []
            for dirname in sorted(dirnames):
                if dirname in ProjectExecutionService._SKIP_DIRS:
                    continue
                child = base / dirname
                _validate_contained_path(child, root)
                try:
                    metadata = child.lstat()
                except OSError as exc:
                    raise ProjectExecutionError("Der Quellbaum änderte sich während der Vorprüfung.") from exc
                if _is_reparse(metadata) or not child.is_dir():
                    raise ProjectExecutionError("Symlink- oder Sonderordner im Quell-/Testbaum abgelehnt.")
                keep.append(dirname)
            dirnames[:] = keep
            for filename in sorted(filenames):
                path = base / filename
                _validate_contained_path(path, root)
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise ProjectExecutionError("Eine Quell-/Testdatei ist während der Vorprüfung verschwunden.") from exc
                if _is_reparse(metadata) or not path.is_file():
                    raise ProjectExecutionError("Symlink- oder Sonderdatei im Quell-/Testbaum abgelehnt.")
                paths.append(path)
                if len(paths) > ProjectExecutionService.MAX_SNAPSHOT_FILES:
                    raise ProjectExecutionError("Der Projektfingerprint überschreitet das Dateilimit.")

    digest = hashlib.sha256()
    total_bytes = 0
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix().casefold()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        try:
            with path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if before.st_size > ProjectExecutionService.MAX_FILE_BYTES:
                    raise ProjectExecutionError("Eine Quell-/Testdatei überschreitet das 32-MiB-Limit.")
                content = handle.read(ProjectExecutionService.MAX_FILE_BYTES + 1)
                after = os.fstat(handle.fileno())
        except OSError as exc:
            raise ProjectExecutionError("Quell-/Testdateien konnten nicht sicher gelesen werden.") from exc
        if len(content) > ProjectExecutionService.MAX_FILE_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProjectExecutionError("Eine Quell-/Testdatei änderte sich während des Fingerprints.")
        total_bytes += len(content)
        if total_bytes > ProjectExecutionService.MAX_SNAPSHOT_BYTES:
            raise ProjectExecutionError("Der Quell-/Testfingerprint überschreitet das 256-MiB-Limit.")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()
