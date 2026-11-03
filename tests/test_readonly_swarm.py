from __future__ import annotations

import threading
import uuid

import pytest

from dbzscc.app.readonly_swarm import (
    Finding,
    ReadOnlySwarm,
    ReadOnlyTask,
    TaskErrorCode,
    TaskStatus,
    WorkerReply,
    WorkerUncertainty,
)

SCOPE_ID = str(uuid.uuid4())
SOURCE_A = str(uuid.uuid4())
SOURCE_B = str(uuid.uuid4())


def _task(request: str, *, timeout: float = 1.0, task_id: str | None = None) -> ReadOnlyTask:
    arguments = {"request": request, "scope_id": SCOPE_ID, "timeout_seconds": timeout}
    if task_id is not None:
        arguments["task_id"] = task_id
    return ReadOnlyTask(**arguments)


class ImmediateWorker:
    def __init__(self, worker_id: str, statement: str, source_ref: str) -> None:
        self.worker_id = worker_id
        self.statement = statement
        self.source_ref = source_ref

    def run(self, task: ReadOnlyTask, cancellation: threading.Event) -> WorkerReply:
        assert not cancellation.is_set()
        return WorkerReply(
            worker_id=self.worker_id,
            findings=(Finding("runtime", self.statement, self.source_ref, 0.8),),
        )


def test_parallel_read_only_tasks_preserve_sources_and_report_conflicts() -> None:
    barrier = threading.Barrier(2)

    class BarrierWorker:
        def __init__(self, worker_id: str, statement: str, source_ref: str) -> None:
            self.worker_id = worker_id
            self.statement = statement
            self.source_ref = source_ref

        def run(self, task: ReadOnlyTask, cancellation: threading.Event) -> WorkerReply:
            barrier.wait(timeout=1)
            return WorkerReply(
                self.worker_id,
                (Finding("runtime", self.statement, self.source_ref, 0.8),),
            )

    def factory(task: ReadOnlyTask) -> BarrierWorker:
        if task.request == "worker A":
            return BarrierWorker("agent-a", "Runtime is available.", SOURCE_A)
        return BarrierWorker("agent-b", "Runtime is unavailable.", SOURCE_B)

    with ReadOnlySwarm(factory, max_parallelism=2) as swarm:
        report = swarm.run_batch([_task("worker A"), _task("worker B")])

    assert [item.status for item in report.results] == [TaskStatus.SUCCEEDED] * 2
    assert {item.worker_id for item in report.results} == {"agent-a", "agent-b"}
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.topic == "runtime"
    assert {claim.source_ref for claim in conflict.claims} == {SOURCE_A, SOURCE_B}
    assert {claim.statement for claim in conflict.claims} == {
        "Runtime is available.",
        "Runtime is unavailable.",
    }


def test_worker_failure_is_isolated_and_raw_exception_text_is_not_returned() -> None:
    def factory(task: ReadOnlyTask):
        if task.request == "fail":
            class FailingWorker:
                def run(self, task: ReadOnlyTask, cancellation: threading.Event) -> WorkerReply:
                    raise RuntimeError("secret-bearing raw provider traceback")

            return FailingWorker()
        return ImmediateWorker("agent-ok", "Evidence retained.", SOURCE_A)

    with ReadOnlySwarm(factory, max_parallelism=2) as swarm:
        report = swarm.run_batch([_task("fail"), _task("succeed")])

    failed, succeeded = report.results
    assert failed.status is TaskStatus.FAILED
    assert failed.error_code is TaskErrorCode.WORKER_FAILED
    assert failed.findings == ()
    assert succeeded.status is TaskStatus.SUCCEEDED
    assert succeeded.findings[0].source_ref == SOURCE_A
    assert "secret-bearing" not in repr(report)


def test_timeout_sets_cooperative_cancellation_and_returns_bounded_result() -> None:
    started = threading.Event()
    finished = threading.Event()
    observed_cancel = threading.Event()

    class WaitingWorker:
        def run(self, task: ReadOnlyTask, cancellation: threading.Event) -> WorkerReply:
            started.set()
            cancellation.wait(timeout=2)
            if cancellation.is_set():
                observed_cancel.set()
            finished.set()
            return WorkerReply("agent-wait", ())

    with ReadOnlySwarm(lambda task: WaitingWorker()) as swarm:
        report = swarm.run_batch([_task("wait", timeout=0.05)], timeout_seconds=1)

    assert started.wait(timeout=0.5)
    assert report.results[0].status is TaskStatus.TIMED_OUT
    assert report.results[0].error_code is TaskErrorCode.TIMED_OUT
    assert finished.wait(timeout=0.5)
    assert observed_cancel.is_set()


def test_external_cancellation_marks_pending_worker_cancelled() -> None:
    started = threading.Event()
    cancellation = threading.Event()

    class WaitingWorker:
        def run(self, task: ReadOnlyTask, stop_event: threading.Event) -> WorkerReply:
            started.set()
            stop_event.wait(timeout=2)
            return WorkerReply("agent-wait", ())

    def request_cancel() -> None:
        assert started.wait(timeout=0.5)
        cancellation.set()

    cancel_thread = threading.Thread(target=request_cancel)
    cancel_thread.start()
    with ReadOnlySwarm(lambda task: WaitingWorker()) as swarm:
        report = swarm.run_batch(
            [_task("cancel me")], timeout_seconds=1, cancellation=cancellation
        )
    cancel_thread.join(timeout=0.5)

    assert report.results[0].status is TaskStatus.CANCELLED
    assert report.results[0].error_code is TaskErrorCode.CANCELLED


def test_uncertainty_is_preserved_and_conflict_is_not_invented_for_same_claim() -> None:
    def factory(task: ReadOnlyTask) -> ImmediateWorker:
        return ImmediateWorker("agent-same", "One cautious answer.", SOURCE_A)

    with ReadOnlySwarm(factory, max_parallelism=2) as swarm:
        report = swarm.run_batch(
            [
                _task("one"),
                _task("two"),
            ]
        )

    assert report.conflicts == ()
    assert all(result.status is TaskStatus.SUCCEEDED for result in report.results)

    class UncertainWorker:
        def run(self, task: ReadOnlyTask, cancellation: threading.Event) -> WorkerReply:
            return WorkerReply(
                "agent-uncertain",
                (),
                (WorkerUncertainty.INSUFFICIENT_EVIDENCE,),
            )

    with ReadOnlySwarm(lambda task: UncertainWorker()) as swarm:
        uncertain = swarm.run_batch([_task("insufficient")])
    assert uncertain.results[0].uncertainties == (WorkerUncertainty.INSUFFICIENT_EVIDENCE,)


def test_duplicate_tasks_and_unbounded_parallelism_are_rejected() -> None:
    duplicate_id = str(uuid.uuid4())
    with (
        ReadOnlySwarm(lambda task: ImmediateWorker("agent", "ok", SOURCE_A)) as swarm,
        pytest.raises(ValueError, match="unique"),
    ):
        swarm.run_batch(
            [_task("first", task_id=duplicate_id), _task("second", task_id=duplicate_id)]
        )
    with pytest.raises(ValueError, match="between 1 and 4"):
        ReadOnlySwarm(lambda task: ImmediateWorker("agent", "ok", SOURCE_A), max_parallelism=5)
