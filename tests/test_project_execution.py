from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import dbzscc.app.project_execution as execution_module
from dbzscc.app.execution_events import ExecutionEventType
from dbzscc.app.project_execution import (
    ProjectExecutionError,
    ProjectExecutionService,
    ProjectOperation,
    default_project_python,
)
from dbzscc.app.resource_orchestrator import ResourceSnapshot, TelemetryQuality
from dbzscc.app.toolchain_runtime import OneShotState
from dbzscc.app.workflow_runtime import WorkflowAction, WorkflowPlan
from dbzscc.miro.model_registry import ModelCatalogEntry


class _ResourceMonitor:
    def capture(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            captured_monotonic_s=time.monotonic(),
            telemetry_quality=TelemetryQuality.FRESH,
            available_physical_mib=32_768,
            commit_available_mib=32_768,
            logical_cpu_cores=16,
            cpu_load_ratio=0.05,
            source="test-fresh-snapshot",
        )


class _Guard:
    def __init__(self) -> None:
        self.terminated = False
        self.closed = False

    def terminate(self, _process) -> None:
        self.terminated = True

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 24680

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is None or timeout >= 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.fixture
def python_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "python-project"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nrequires-python = ">=3.10"\n',
        encoding="utf-8",
    )
    (root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_module.py").write_text("def test_value(): assert True\n", encoding="utf-8")
    environment = root / ".venv"
    scripts = environment / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    python.write_bytes(b"fixture executable")
    packages = environment / "Lib" / "site-packages"
    (packages / "pytest").mkdir(parents=True)
    (packages / "pytest" / "__init__.py").write_text("", encoding="utf-8")
    (packages / "ruff").mkdir()
    (packages / "ruff" / "__main__.py").write_text("", encoding="utf-8")
    return root, python


def _catalog_entry() -> ModelCatalogEntry:
    observation = SimpleNamespace(observation_id="observation-1", status="metadata_read")
    artifact = SimpleNamespace(
        artifact_id="artifact-1",
        current_observation_id=observation.observation_id,
        status="metadata_read",
    )
    return ModelCatalogEntry(
        artifact=artifact,
        model=None,
        observation=observation,
        selected_metadata=(),
        capability_evidence_json="{}",
        runtime_qualifications=(),
        multimodal_pairings=(),
    )


def _plan() -> WorkflowPlan:
    entry = _catalog_entry()
    return WorkflowPlan(
        actions=(WorkflowAction.RUN_TESTS, WorkflowAction.RUN_LINTER, WorkflowAction.PROPOSE_PATCH),
        rationale="Prüfe Tests und Linter vor einem gezielten Vorschlag.",
        model_name="local-model",
        model_artifact_id=entry.artifact.artifact_id,
        model_observation_id=entry.observation.observation_id,
        model_catalog_status="current_registry_record",
        model_catalog_entry=entry,
    )


def _service(*, events=None, guard=None) -> ProjectExecutionService:
    return ProjectExecutionService(
        event_sink=(events.append if events is not None else None),
        resource_monitor=_ResourceMonitor(),
        process_tree_factory=(lambda _process: guard or _Guard()),
    )


def test_default_python_uses_only_conventional_project_local_venv(python_project) -> None:
    root, python = python_project
    assert default_project_python(root) == python


def test_default_python_prefers_isolated_workflow_environment(python_project) -> None:
    root, _default_python = python_project
    python = root / ".work" / "venv314" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"isolated workflow interpreter")

    assert default_project_python(root) == python


def test_plan_builds_fixed_typed_pytest_and_ruff_invocations(python_project) -> None:
    root, python = python_project
    service = _service()
    workflow_plan = _plan()

    plans = service.prepare(
        workflow_plan,
        (WorkflowAction.RUN_TESTS, WorkflowAction.RUN_LINTER),
        project_root=root,
        python_executable=python,
        conversation_id="chat-1",
        workflow_id="workflow-1",
        run_id="persistent-workflow-run",
    )

    assert [plan.action for plan in plans] == [ProjectOperation.TESTS, ProjectOperation.LINTER]
    assert plans[0].argv == (
        str(python), "-I", "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests"
    )
    assert plans[1].argv == (
        str(python), "-I", "-B", "-m", "ruff", "check", "--no-cache", "src", "tests"
    )
    assert all(plan.invocation.working_directory == root for plan in plans)
    assert all(plan.resource_plan.admitted for plan in plans)
    assert all(
        plan.model_catalog_entry is workflow_plan.model_catalog_entry for plan in plans
    )
    assert all(plan.project_fingerprint == plans[0].project_fingerprint for plan in plans)
    assert plans[0].run_id == plans[1].run_id == "persistent-workflow-run"
    assert plans[0].invocation.invocation_id != plans[1].invocation.invocation_id


def test_pipeline_targets_follow_validated_pyproject_manifest(python_project) -> None:
    root, python = python_project
    (root / "tests" / "unit").mkdir()
    (root / "tests" / "unit" / "test_smoke.py").write_text(
        "def test_smoke(): assert True\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests/unit"]\n'
        '[tool.dbzscc.execution]\npytest_paths = ["tests/unit"]\n'
        'ruff_paths = ["src/module.py"]\n',
        encoding="utf-8",
    )

    plans = _service().prepare(
        _plan(),
        (WorkflowAction.RUN_TESTS, WorkflowAction.RUN_LINTER),
        project_root=root,
        python_executable=python,
        conversation_id="chat-manifest",
        workflow_id="workflow-manifest",
        run_id="manifest-run",
    )

    assert plans[0].invocation.argv[-1] == "tests/unit"
    assert plans[1].invocation.argv[-1] == "src/module.py"
    assert plans[0].run_id == plans[1].run_id == "manifest-run"


@pytest.mark.parametrize(
    ("execution_table", "message"),
    [
        ('pytest_paths = ["../outside"]\n', "sichere relative Pfade"),
        ('command = ["python", "-c", "arbitrary"]\n', "Nicht unterstützte"),
        ('ruff_paths = [".venv/Scripts"]\n', "sichere relative Pfade"),
    ],
)
def test_pipeline_manifest_rejects_unsafe_targets_and_commands(
    python_project, execution_table: str, message: str
) -> None:
    root, python = python_project
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\n[tool.dbzscc.execution]\n' + execution_table,
        encoding="utf-8",
    )

    with pytest.raises(ProjectExecutionError, match=message):
        _service().prepare(
            _plan(),
            (WorkflowAction.RUN_TESTS,),
            project_root=root,
            python_executable=python,
            conversation_id="chat-manifest",
            workflow_id="workflow-manifest",
        )


def test_prepare_rejects_actions_not_in_plan_and_unsupported_server_action(python_project) -> None:
    root, python = python_project
    service = _service()
    with pytest.raises(ProjectExecutionError, match="nicht Teil"):
        service.prepare(
            WorkflowPlan((WorkflowAction.RUN_TESTS,), "Tests.", "local-model"),
            (WorkflowAction.RUN_LINTER,),
            project_root=root,
            python_executable=python,
            conversation_id="chat-1",
            workflow_id="workflow-1",
        )
    unsupported_plan = WorkflowPlan(
        (WorkflowAction.START_DEVELOPMENT_SERVER,), "Server.", "local-model"
    )
    with pytest.raises(ProjectExecutionError, match="keinen ausführbaren Consumer"):
        service.prepare(
            unsupported_plan,
            (WorkflowAction.START_DEVELOPMENT_SERVER,),
            project_root=root,
            python_executable=python,
            conversation_id="chat-1",
            workflow_id="workflow-1",
        )


def test_prepare_rejects_remote_or_non_python_paths_and_missing_modules(python_project) -> None:
    root, python = python_project
    service = _service()
    with pytest.raises(ProjectExecutionError, match="innerhalb"):
        service.prepare(
            _plan(),
            (WorkflowAction.RUN_TESTS,),
            project_root=root,
            python_executable=Path(os.environ.get("WINDIR", "C:/Windows")) / "python.exe",
            conversation_id="chat-1",
            workflow_id="workflow-1",
        )
    with pytest.raises(ProjectExecutionError, match="ruff fehlt"):
        (python.parent.parent / "Lib" / "site-packages" / "ruff" / "__main__.py").unlink()
        service.prepare(
            _plan(),
            (WorkflowAction.RUN_LINTER,),
            project_root=root,
            python_executable=python,
            conversation_id="chat-1",
            workflow_id="workflow-1",
        )


def test_execute_streams_redacted_output_and_records_exit_evidence(python_project, monkeypatch) -> None:
    root, python = python_project
    events = []
    guard = _Guard()
    service = _service(events=events, guard=guard)
    plan = service.prepare(
        _plan(),
        (WorkflowAction.RUN_TESTS,),
        project_root=root,
        python_executable=python,
        conversation_id="chat-1",
        workflow_id="workflow-1",
    )[0]
    secret = "local-test-secret-value"
    monkeypatch.setenv("TEST_API_KEY", secret)
    process = _FakeProcess(stdout=f"tests/test_module.py: passed {secret}\n", stderr="warning\n")
    observed = {}

    def fake_popen(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        return process

    monkeypatch.setattr(execution_module.subprocess, "Popen", fake_popen)
    result = service.execute(plan, threading.Event())

    assert result.outcome.state.value == "succeeded"
    assert result.outcome.exit_code == 0
    assert observed["argv"] == list(plan.argv)
    assert observed["shell"] is False
    assert observed["cwd"] == root
    assert secret not in result.output_summary
    assert "[redacted]" in result.output_summary
    assert any(event.event_type is ExecutionEventType.STDOUT for event in events)
    assert any(event.event_type is ExecutionEventType.STDERR for event in events)
    assert all(event.run_id == plan.run_id for event in events)
    assert all(event.step_id == "run_tests" for event in events)
    assert all(event.provenance[2] == ("project_fingerprint", plan.project_fingerprint) for event in events)
    exit_event = next(event for event in events if event.event_type is ExecutionEventType.EXIT_CODE)
    assert exit_event.run_id == plan.run_id
    assert exit_event.workflow_id == plan.workflow_id
    assert exit_event.exit_code == 0
    assert exit_event.state == "succeeded"
    assert exit_event.provenance[0] == ("action", "run_tests")
    assert guard.closed


def test_execute_rejects_stale_project_fingerprint_before_start(python_project, monkeypatch) -> None:
    root, python = python_project
    service = _service()
    plan = service.prepare(
        _plan(),
        (WorkflowAction.RUN_TESTS,),
        project_root=root,
        python_executable=python,
        conversation_id="chat-1",
        workflow_id="workflow-1",
    )[0]
    (root / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(
        execution_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("stale project must not start a process"),
    )

    with pytest.raises(ProjectExecutionError, match="geändert"):
        service.execute(plan, threading.Event())


def test_execute_cancellation_before_start_does_not_spawn_process(python_project, monkeypatch) -> None:
    root, python = python_project
    service = _service()
    plan = service.prepare(
        _plan(),
        (WorkflowAction.RUN_TESTS,),
        project_root=root,
        python_executable=python,
        conversation_id="chat-1",
        workflow_id="workflow-1",
    )[0]
    cancellation = threading.Event()
    cancellation.set()
    monkeypatch.setattr(
        execution_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("cancelled operation must not spawn a process"),
    )

    result = service.execute(plan, cancellation)

    assert result.outcome.state is OneShotState.CANCELLED
    assert result.outcome.exit_code is None


def test_execute_rejects_manipulated_argv_before_start(python_project, monkeypatch) -> None:
    root, python = python_project
    service = _service()
    plan = service.prepare(
        _plan(),
        (WorkflowAction.RUN_TESTS,),
        project_root=root,
        python_executable=python,
        conversation_id="chat-1",
        workflow_id="workflow-1",
    )[0]
    forged = replace(plan, invocation=replace(plan.invocation, argv=("whoami",)))
    monkeypatch.setattr(
        execution_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("manipulated argv must not start"),
    )

    with pytest.raises(ProjectExecutionError, match="Allowlist-argv"):
        service.execute(forged, threading.Event())


def test_resource_rejection_fails_closed_before_invocation(python_project) -> None:
    root, python = python_project

    class NoResources:
        def capture(self) -> ResourceSnapshot:
            return ResourceSnapshot(
                captured_monotonic_s=time.monotonic(),
                telemetry_quality=TelemetryQuality.UNKNOWN,
                available_physical_mib=None,
                commit_available_mib=None,
                logical_cpu_cores=None,
                cpu_load_ratio=None,
                source="unknown-test-snapshot",
            )

    service = ProjectExecutionService(resource_monitor=NoResources())
    with pytest.raises(ProjectExecutionError, match="Ressourcenprüfung abgelehnt"):
        service.prepare(
            _plan(),
            (WorkflowAction.RUN_TESTS,),
            project_root=root,
            python_executable=python,
            conversation_id="chat-1",
            workflow_id="workflow-1",
        )
