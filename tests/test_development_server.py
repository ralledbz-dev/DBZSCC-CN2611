from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from dbzscc.app.development_server import (
    DevelopmentServerController,
    DevelopmentServerError,
    DevelopmentTarget,
)
from dbzscc.app.execution_events import ExecutionEventType
from dbzscc.app.resource_orchestrator import ResourceSnapshot, TelemetryQuality
from dbzscc.app.workflow_runtime import WorkflowAction, WorkflowPlan


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


class _Process:
    pid = 8642

    def __init__(self) -> None:
        self.stdout = io.StringIO("ready\n")
        self.stderr = io.StringIO("")
        self.returncode: int | None = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise AssertionError("process should have been terminated")
        return self.returncode


class _Runner:
    def __init__(self) -> None:
        self.calls = []
        self.process = _Process()

    def start(self, argv, *, cwd, env):
        self.calls.append((tuple(argv), cwd, dict(env)))
        return self.process


class _Guard:
    def __init__(self) -> None:
        self.closed = False
        self.terminated = False

    def terminate(self, process):
        self.terminated = True
        process.terminate()

    def close(self):
        self.closed = True


@pytest.fixture
def pyside_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    (root / "src" / "demo").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "demo" / "__main__.py").write_text("import PySide6\n", encoding="utf-8")
    (root / "tests" / "test_demo.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname="fixture"\ndependencies=["PySide6>=6.7"]\n'
        '[tool.dbzscc.devserver]\nkind="pyside6-module"\nmodule="demo"\n',
        encoding="utf-8",
    )
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture interpreter")
    return root, python


def _plan(action=WorkflowAction.START_DEVELOPMENT_SERVER):
    return WorkflowPlan((action,), "Nur explizit freigegebene lokale Ausführung.", "local-model")


def _controller(events, runner=None, guard=None, health_probe=None):
    return DevelopmentServerController(
        event_sink=events.append,
        runner=runner,
        resource_monitor=_ResourceMonitor(),
        process_tree_factory=(lambda _process: guard or _Guard()),
        health_probe=health_probe,
    )


def test_prepare_composes_only_the_fixed_pyside6_module_argv(pyside_project) -> None:
    root, python = pyside_project
    controller = _controller([])
    plan = controller.prepare(
        _plan(),
        WorkflowAction.START_DEVELOPMENT_SERVER,
        project_root=root,
        python_executable=python,
        port=8765,
        conversation_id="chat-1",
        workflow_id="workflow-1",
    )

    assert plan.target is DevelopmentTarget.PYSIDE6_MODULE
    assert plan.argv == (str(python), "-B", "-m", "demo")
    assert plan.endpoint is None
    assert plan.resource_plan.admitted


def test_debug_action_uses_fixed_python_diagnostic_flags(pyside_project) -> None:
    root, python = pyside_project
    plan = _controller([]).prepare(
        _plan(WorkflowAction.DEBUG_PROJECT),
        WorkflowAction.DEBUG_PROJECT,
        project_root=root,
        python_executable=python,
        port=8765,
        conversation_id="chat-1",
        workflow_id="workflow-1",
    )

    assert plan.debug
    assert plan.argv == (
        str(python), "-B", "-X", "dev", "-X", "faulthandler", "-m", "demo"
    )


def test_manifest_rejects_free_form_commands_and_shell_fields(pyside_project) -> None:
    root, python = pyside_project
    path = root / "pyproject.toml"
    path.write_text(
        '[project]\nname="fixture"\ndependencies=["PySide6>=6.7"]\n'
        '[tool.dbzscc.devserver]\nkind="pyside6-module"\nmodule="demo"\n'
        'command="powershell -Command Write-Host unsafe"\n',
        encoding="utf-8",
    )

    with pytest.raises(DevelopmentServerError, match="ausschließlich"):
        _controller([]).prepare(
            _plan(), WorkflowAction.START_DEVELOPMENT_SERVER,
            project_root=root, python_executable=python, port=8765,
            conversation_id="chat-1", workflow_id="workflow-1",
        )


def test_action_must_be_present_in_validated_model_plan(pyside_project) -> None:
    root, python = pyside_project

    with pytest.raises(DevelopmentServerError, match="nicht Teil"):
        _controller([]).prepare(
            _plan(WorkflowAction.RUN_TESTS), WorkflowAction.START_DEVELOPMENT_SERVER,
            project_root=root, python_executable=python, port=8765,
            conversation_id="chat-1", workflow_id="workflow-1",
        )


def test_manifest_change_after_confirmation_blocks_process_start(pyside_project) -> None:
    root, python = pyside_project
    runner = _Runner()
    controller = _controller([], runner=runner)
    plan = controller.prepare(
        _plan(), WorkflowAction.START_DEVELOPMENT_SERVER,
        project_root=root, python_executable=python, port=8765,
        conversation_id="chat-1", workflow_id="workflow-1",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname="changed"\ndependencies=["PySide6>=6.7"]\n'
        '[tool.dbzscc.devserver]\nkind="pyside6-module"\nmodule="demo"\n',
        encoding="utf-8",
    )

    with pytest.raises(DevelopmentServerError, match="geändert"):
        controller.start(plan, __import__("threading").Event())
    assert runner.calls == []


def test_uvicorn_manifest_binds_loopback_only_and_checks_declared_health_path(tmp_path) -> None:
    root = tmp_path / "asgi-project"
    (root / "src" / "service").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "service" / "main.py").write_text("app = object()\n", encoding="utf-8")
    (root / "tests" / "test_main.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname="fixture"\n'
        '[tool.dbzscc.devserver]\nkind="uvicorn"\napp="service.main:app"\nhealth_path="/health"\n',
        encoding="utf-8",
    )
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture interpreter")

    plan = _controller([]).prepare(
        _plan(), WorkflowAction.START_DEVELOPMENT_SERVER,
        project_root=root, python_executable=python, port=8765,
        conversation_id="chat-1", workflow_id="workflow-1",
    )

    assert plan.target is DevelopmentTarget.UVICORN
    assert plan.endpoint == "http://127.0.0.1:8765"
    assert plan.argv == (
        str(python), "-B", "-m", "uvicorn", "service.main:app",
        "--host", "127.0.0.1", "--port", "8765", "--no-access-log",
    )


def test_managed_lifecycle_streams_events_and_stops_only_owned_process(pyside_project, monkeypatch) -> None:
    import threading

    root, python = pyside_project
    events = []
    runner = _Runner()
    guard = _Guard()
    controller = _controller(events, runner=runner, guard=guard)
    monkeypatch.setenv("DBZSCC_LLM_API_KEY", "must-not-enter-devserver")
    plan = controller.prepare(
        _plan(), WorkflowAction.START_DEVELOPMENT_SERVER,
        project_root=root, python_executable=python, port=8765,
        conversation_id="chat-1", workflow_id="workflow-1",
    )

    result = controller.start(plan, threading.Event())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(event.event_type is ExecutionEventType.STDOUT for event in events):
        time.sleep(0.01)
    assert result.process_id == runner.process.pid
    assert controller.has_owned_process
    assert runner.calls[0][0] == plan.argv
    assert "DBZSCC_LLM_API_KEY" not in runner.calls[0][2]
    assert any(event.event_type is ExecutionEventType.STATUS and "Healthcheck erfolgreich" in event.text for event in events)
    assert any(event.event_type is ExecutionEventType.STDOUT and "ready" in event.text for event in events)

    assert controller.stop()
    assert guard.terminated and guard.closed
    assert runner.process.returncode == 0
    assert not controller.has_owned_process
    assert any(event.event_type is ExecutionEventType.EXIT_CODE and event.exit_code == 0 for event in events)
