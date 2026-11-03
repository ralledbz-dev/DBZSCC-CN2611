"""Bounded read-only parallel task core with injectable, replaceable workers.

The core never interprets a task as code and exposes no filesystem, database,
network, or mutation tools to workers. Worker implementations are trusted
in-process Python; ThreadPoolExecutor timeouts request cooperative cancellation
and cannot forcibly terminate a non-cooperative Python thread.
"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    wait,
)
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

_MAX_PARALLELISM = 4
_MAX_TASKS = 16
_MAX_FINDINGS = 8
_MAX_REQUEST_CHARS = 500
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")


class ReadOnlyTaskKind(str, Enum):
    ANALYZE = "analyze"
    EXTRACT_ABSTRACTIONS = "extract_abstractions"
    CHECK_CONSISTENCY = "check_consistency"


class WorkerUncertainty(str, Enum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    AMBIGUOUS_SOURCE = "ambiguous_source"
    PARTIAL_INPUT = "partial_input"
    NOT_VERIFIED = "not_verified"


class TaskStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TaskErrorCode(str, Enum):
    WORKER_FAILED = "worker_failed"
    INVALID_WORKER_RESULT = "invalid_worker_result"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ReadOnlyTask:
    """One independent bounded request; no dependencies or executable payload."""

    request: str
    scope_id: str
    kind: ReadOnlyTaskKind = ReadOnlyTaskKind.ANALYZE
    timeout_seconds: float = 10.0
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if not isinstance(self.request, str):
            raise TypeError("Task request must be text.")
        request = " ".join(self.request.split())
        if not request or len(request) > _MAX_REQUEST_CHARS:
            raise ValueError("Task request must contain 1 to 500 characters.")
        if "\n" in self.request or "\r" in self.request:
            raise ValueError("Task request must be a single line.")
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "scope_id", _canonical_uuid(self.scope_id, "scope"))
        object.__setattr__(self, "task_id", _canonical_uuid(self.task_id, "task"))
        try:
            object.__setattr__(self, "kind", ReadOnlyTaskKind(self.kind))
        except (TypeError, ValueError) as exc:
            raise ValueError("Task kind is not supported.") from exc
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0.01 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("Per-task timeout must be between 0.01 and 30 seconds.")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True)
class Finding:
    """One bounded claim with a traceable source reference and confidence."""

    topic: str
    statement: str
    source_ref: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "topic", _bounded_line(self.topic, "topic", 80))
        object.__setattr__(
            self, "statement", _bounded_line(self.statement, "statement", 300)
        )
        object.__setattr__(self, "source_ref", _canonical_uuid(self.source_ref, "source"))
        confidence = self.confidence
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise TypeError("Finding confidence must be a finite number from 0 to 1.")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("Finding confidence must be a finite number from 0 to 1.")
        object.__setattr__(self, "confidence", confidence)


@dataclass(frozen=True)
class WorkerReply:
    """Validated worker response; arbitrary exception text is never carried."""

    worker_id: str
    findings: tuple[Finding, ...]
    uncertainties: tuple[WorkerUncertainty, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or _WORKER_ID_RE.fullmatch(self.worker_id) is None:
            raise ValueError("Worker identifier must be a short controlled label.")
        if not isinstance(self.findings, tuple) or len(self.findings) > _MAX_FINDINGS:
            raise ValueError("Worker must return a tuple of at most eight findings.")
        if any(not isinstance(item, Finding) for item in self.findings):
            raise ValueError("Worker findings must use the Finding contract.")
        if not isinstance(self.uncertainties, tuple):
            raise TypeError("Worker uncertainties must be a tuple of controlled codes.")
        try:
            codes = tuple(WorkerUncertainty(code) for code in self.uncertainties)
        except (TypeError, ValueError) as exc:
            raise ValueError("Worker uncertainty code is not supported.") from exc
        object.__setattr__(self, "uncertainties", tuple(dict.fromkeys(codes)))


class ReadOnlyWorker(Protocol):
    """Injectable worker seam; no tools or mutable services are passed in."""

    def run(self, task: ReadOnlyTask, cancellation: threading.Event) -> WorkerReply:
        """Return bounded observations and honor cancellation cooperatively."""


WorkerFactory = Callable[[ReadOnlyTask], ReadOnlyWorker]


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: TaskStatus
    worker_id: str | None
    findings: tuple[Finding, ...]
    uncertainties: tuple[WorkerUncertainty, ...]
    error_code: TaskErrorCode | None
    elapsed_ms: int


@dataclass(frozen=True)
class ConflictClaim:
    task_id: str
    worker_id: str
    source_ref: str
    statement: str
    confidence: float


@dataclass(frozen=True)
class ClaimConflict:
    topic: str
    claims: tuple[ConflictClaim, ...]


@dataclass(frozen=True)
class SwarmReport:
    """Task results retain independent provenance; conflicts are not voted away."""

    results: tuple[TaskResult, ...]
    conflicts: tuple[ClaimConflict, ...]


class ReadOnlySwarm(AbstractContextManager["ReadOnlySwarm"]):
    """Run bounded independent tasks concurrently through replaceable workers."""

    def __init__(self, worker_factory: WorkerFactory, *, max_parallelism: int = 2) -> None:
        if not callable(worker_factory):
            raise TypeError("A read-only worker factory is required.")
        if type(max_parallelism) is not int or not 1 <= max_parallelism <= _MAX_PARALLELISM:
            raise ValueError("Parallelism must be between 1 and 4 workers.")
        self._worker_factory = worker_factory
        self._executor = ThreadPoolExecutor(
            max_workers=max_parallelism, thread_name_prefix="dbzscc-readonly"
        )
        self._batch_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_batch_cancel: threading.Event | None = None
        self._active_worker_cancellations: list[threading.Event] = []
        self._closed = False

    def run_batch(
        self,
        tasks: tuple[ReadOnlyTask, ...] | list[ReadOnlyTask],
        *,
        timeout_seconds: float = 30.0,
        cancellation: threading.Event | None = None,
    ) -> SwarmReport:
        """Run a bounded independent batch and return isolated task outcomes."""
        task_list = tuple(tasks)
        self._validate_batch(task_list, timeout_seconds)
        if cancellation is not None and not isinstance(cancellation, threading.Event):
            raise TypeError("Cancellation must be a threading.Event or None.")
        if not self._batch_lock.acquire(blocking=False):
            raise RuntimeError("This swarm already has an active batch.")
        try:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("This swarm has been closed.")
                batch_cancel = threading.Event()
                self._active_batch_cancel = batch_cancel
            return self._run_locked(task_list, timeout_seconds, cancellation, batch_cancel)
        finally:
            with self._state_lock:
                self._active_batch_cancel = None
                self._active_worker_cancellations.clear()
            self._batch_lock.release()

    def cancel(self) -> None:
        """Request cancellation of the active batch and all cooperative workers."""
        with self._state_lock:
            if self._active_batch_cancel is not None:
                self._active_batch_cancel.set()
            for event in self._active_worker_cancellations:
                event.set()

    def close(self) -> None:
        """Request cancellation and close the executor without blocking on workers."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            if self._active_batch_cancel is not None:
                self._active_batch_cancel.set()
            for event in self._active_worker_cancellations:
                event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _validate_batch(tasks: tuple[ReadOnlyTask, ...], timeout_seconds: float) -> None:
        if len(tasks) > _MAX_TASKS:
            raise ValueError("A batch may contain at most 16 tasks.")
        if any(not isinstance(task, ReadOnlyTask) for task in tasks):
            raise TypeError("Every batch item must be a typed ReadOnlyTask.")
        ids = [task.task_id for task in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Task identifiers must be unique within a batch.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.01 <= float(timeout_seconds) <= 60.0
        ):
            raise ValueError("Batch timeout must be between 0.01 and 60 seconds.")

    def _run_locked(
        self,
        tasks: tuple[ReadOnlyTask, ...],
        timeout_seconds: float,
        external_cancel: threading.Event | None,
        batch_cancel: threading.Event,
    ) -> SwarmReport:
        batch_started = time.monotonic()
        batch_deadline = batch_started + float(timeout_seconds)
        results: dict[str, TaskResult] = {}
        futures: dict[Future[WorkerReply], tuple[ReadOnlyTask, threading.Event, float]] = {}
        for task in tasks:
            stop_event = threading.Event()
            with self._state_lock:
                self._active_worker_cancellations.append(stop_event)
            submitted_at = time.monotonic()
            if batch_cancel.is_set() or (external_cancel is not None and external_cancel.is_set()):
                results[task.task_id] = _terminal_result(
                    task, TaskStatus.CANCELLED, TaskErrorCode.CANCELLED, submitted_at
                )
                stop_event.set()
                continue
            try:
                future = self._executor.submit(self._invoke_worker, task, stop_event)
            except RuntimeError:
                results[task.task_id] = _terminal_result(
                    task, TaskStatus.FAILED, TaskErrorCode.WORKER_FAILED, submitted_at
                )
                continue
            futures[future] = (task, stop_event, submitted_at)

        pending = set(futures)
        while pending:
            for future in tuple(pending):
                if not future.done():
                    continue
                task, stop_event, submitted_at = futures[future]
                pending.remove(future)
                results[task.task_id] = self._completed_result(
                    future, task, stop_event, submitted_at
                )

            is_cancelled = batch_cancel.is_set() or (
                external_cancel is not None and external_cancel.is_set()
            )
            if is_cancelled:
                for future in tuple(pending):
                    task, stop_event, submitted_at = futures[future]
                    stop_event.set()
                    future.cancel()
                    pending.remove(future)
                    results[task.task_id] = _terminal_result(
                        task, TaskStatus.CANCELLED, TaskErrorCode.CANCELLED, submitted_at
                    )
                break

            now = time.monotonic()
            for future in tuple(pending):
                task, stop_event, submitted_at = futures[future]
                if now >= batch_deadline or now >= submitted_at + task.timeout_seconds:
                    stop_event.set()
                    future.cancel()
                    pending.remove(future)
                    results[task.task_id] = _terminal_result(
                        task, TaskStatus.TIMED_OUT, TaskErrorCode.TIMED_OUT, submitted_at
                    )
            if pending:
                next_deadline = min(
                    batch_deadline,
                    *(futures[future][2] + futures[future][0].timeout_seconds for future in pending),
                )
                wait(pending, timeout=max(0.001, min(0.05, next_deadline - time.monotonic())),
                     return_when=FIRST_COMPLETED)

        ordered = tuple(results[task.task_id] for task in tasks)
        return SwarmReport(results=ordered, conflicts=_find_conflicts(ordered))

    def _invoke_worker(
        self, task: ReadOnlyTask, cancellation: threading.Event
    ) -> WorkerReply:
        if cancellation.is_set():
            raise CancelledError()
        worker = self._worker_factory(task)
        if not callable(getattr(worker, "run", None)):
            raise _InvalidWorkerResult
        reply = worker.run(task, cancellation)
        if not isinstance(reply, WorkerReply):
            raise _InvalidWorkerResult
        return reply

    @staticmethod
    def _completed_result(
        future: Future[WorkerReply],
        task: ReadOnlyTask,
        cancellation: threading.Event,
        submitted_at: float,
    ) -> TaskResult:
        elapsed = max(0, round((time.monotonic() - submitted_at) * 1000))
        if future.cancelled() or cancellation.is_set():
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.CANCELLED,
                worker_id=None,
                findings=(),
                uncertainties=(),
                error_code=TaskErrorCode.CANCELLED,
                elapsed_ms=elapsed,
            )
        try:
            reply = future.result()
        except _InvalidWorkerResult:
            return TaskResult(
                task.task_id, TaskStatus.FAILED, None, (), (),
                TaskErrorCode.INVALID_WORKER_RESULT, elapsed
            )
        except CancelledError:
            return TaskResult(
                task.task_id, TaskStatus.CANCELLED, None, (), (),
                TaskErrorCode.CANCELLED, elapsed
            )
        except Exception:  # noqa: BLE001 - isolate arbitrary worker exceptions at this boundary.
            return TaskResult(
                task.task_id, TaskStatus.FAILED, None, (), (),
                TaskErrorCode.WORKER_FAILED, elapsed
            )
        return TaskResult(
            task_id=task.task_id,
            status=TaskStatus.SUCCEEDED,
            worker_id=reply.worker_id,
            findings=reply.findings,
            uncertainties=reply.uncertainties,
            error_code=None,
            elapsed_ms=elapsed,
        )


class _InvalidWorkerResult(Exception):
    pass


def _terminal_result(
    task: ReadOnlyTask,
    status: TaskStatus,
    error_code: TaskErrorCode,
    started_at: float,
) -> TaskResult:
    return TaskResult(
        task_id=task.task_id,
        status=status,
        worker_id=None,
        findings=(),
        uncertainties=(),
        error_code=error_code,
        elapsed_ms=max(0, round((time.monotonic() - started_at) * 1000)),
    )


def _find_conflicts(results: tuple[TaskResult, ...]) -> tuple[ClaimConflict, ...]:
    grouped: dict[str, list[tuple[str, str, Finding]]] = defaultdict(list)
    for result in results:
        if result.status is not TaskStatus.SUCCEEDED or result.worker_id is None:
            continue
        for finding in result.findings:
            grouped[" ".join(finding.topic.casefold().split())].append(
                (result.task_id, result.worker_id, finding)
            )
    conflicts: list[ClaimConflict] = []
    for claims in grouped.values():
        statements = {
            " ".join(finding.statement.casefold().split())
            for _, _, finding in claims
        }
        if len(statements) <= 1:
            continue
        topic = claims[0][2].topic
        conflicts.append(
            ClaimConflict(
                topic=topic,
                claims=tuple(
                    ConflictClaim(
                        task_id=task_id,
                        worker_id=worker_id,
                        source_ref=finding.source_ref,
                        statement=finding.statement,
                        confidence=finding.confidence,
                    )
                    for task_id, worker_id, finding in claims
                ),
            )
        )
    return tuple(conflicts)


def _bounded_line(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Finding {label} must be text.")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum or "\n" in value or "\r" in value:
        raise ValueError(f"Finding {label} must be a bounded single line.")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"Finding {label} cannot contain control characters.")
    return normalized


def _canonical_uuid(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label.capitalize()} identifier must be a UUID.")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{label.capitalize()} identifier must be a UUID.") from exc
