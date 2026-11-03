from __future__ import annotations

import io
import socket
import time

from dbzscc.app import runtime_service
from dbzscc.app.execution_events import ExecutionEventType
from dbzscc.app.resource_orchestrator import (
    Backend,
    DeviceMemoryKind,
    PhysicalDeviceSnapshot,
    ResourceOrchestrator,
    ResourcePolicy,
    ResourceSnapshot,
    TelemetryQuality,
)
from dbzscc.app.runtime_service import (
    CommandResult,
    RuntimeController,
    RuntimeWorkflowError,
    _parse_runtime_devices,
)


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode


class FakeRunner:
    def __init__(self, device_output: str = "") -> None:
        self.device_output = device_output
        self.run_calls: list[tuple[str, ...]] = []
        self.start_calls: list[tuple[str, ...]] = []
        self.process = FakeProcess()

    def run(self, argv, *, cwd, env, timeout) -> CommandResult:
        self.run_calls.append(tuple(argv))
        return CommandResult(0, self.device_output, "")

    def start(self, argv, *, cwd, env) -> FakeProcess:
        self.start_calls.append(tuple(argv))
        return self.process


class FakeResourceMonitor:
    def __init__(self, devices=()) -> None:
        self.devices = tuple(devices)

    def capture(self) -> ResourceSnapshot:
        now = time.monotonic()
        return ResourceSnapshot(
            captured_monotonic_s=now,
            telemetry_quality=TelemetryQuality.FRESH,
            available_physical_mib=8192,
            commit_available_mib=8192,
            logical_cpu_cores=8,
            cpu_load_ratio=0.1,
            devices=self.devices,
            source="test-fresh-snapshot",
        )


def _setup_runtime(tmp_path, monkeypatch, *, device_output="", gpu_devices=()):
    app_root = tmp_path / "app"
    for build in ("cpu-x64", "cuda-13.1-x64", "vulkan-x64"):
        runtime_dir = app_root / "runtimes" / "llama.cpp" / build
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "llama-cli.exe").write_bytes(b"fake")
        (runtime_dir / "llama-server.exe").write_bytes(b"fake")
    model_root = tmp_path / "models"
    model_root.mkdir()
    model_path = model_root / "tiny.gguf"
    model_path.write_bytes(b"not a real GGUF; sizing must fail safe")
    monkeypatch.setattr(runtime_service, "APP_ROOT", app_root)

    runner = FakeRunner(device_output)
    monitor = FakeResourceMonitor(gpu_devices)
    policy = ResourcePolicy(
        os_available_headroom_mib=0,
        min_available_physical_mib=0,
        commit_safety_margin_mib=0,
        cpu_service_reserve_threads=0,
        dedicated_gpu_headroom_mib=0,
        shared_gpu_headroom_mib=0,
        display_gpu_headroom_mib=0,
        unknown_kv_reserve_mib=512,
        unknown_backend_overhead_mib=256,
    )
    events = []
    controller = RuntimeController(
        event_sink=events.append,
        runner=runner,
        resource_monitor=monitor,
        orchestrator=ResourceOrchestrator(policy),
        health_probe=lambda _endpoint, _timeout: True,
    )
    return controller, runner, events, model_root, model_path


def test_model_enumeration_is_explicit_shallow_and_gguf_only(tmp_path) -> None:
    root = tmp_path / "selected-models"
    root.mkdir()
    direct = root / "direct.gguf"
    direct.write_bytes(b"x")
    (root / "ignored.bin").write_bytes(b"x")
    child = root / "nested"
    child.mkdir()
    (child / "nested.gguf").write_bytes(b"x")

    assert RuntimeController.enumerate_models(root) == (direct.resolve(),)


def test_device_parser_removes_parenthesized_memory_and_matches_runtime_id() -> None:
    parsed = _parse_runtime_devices(
        "Vulkan0: NVIDIA GeForce GTX 1650 (4096 MiB, 3000 MiB free)",
        Backend.VULKAN,
    )
    assert parsed == (("vulkan:0", "NVIDIA GeForce GTX 1650"),)


def test_cpu_plan_uses_fresh_preflight_but_does_not_start_a_process(tmp_path, monkeypatch) -> None:
    controller, runner, events, model_root, model_path = _setup_runtime(
        tmp_path, monkeypatch, device_output="CPU backend available"
    )

    plan = controller.plan(
        conversation_id="chat-1",
        model_root=model_root,
        model_path=model_path,
        backend=Backend.CPU,
        context_tokens=2048,
        cpu_threads=4,
        port=8080,
    )

    assert plan.admitted
    assert plan.resource_plan.status.value in {"admitted", "admitted_reduced"}
    assert runner.run_calls[0][1:] == ("--list-devices",)
    assert runner.start_calls == []
    assert any(event.event_type is ExecutionEventType.TOT_PLAN_SELECTED for event in events)
    assert any(event.event_type is ExecutionEventType.TOOL_RESULT for event in events)


def test_vulkan_target_requires_unique_nvidia_memory_match(tmp_path, monkeypatch) -> None:
    now = time.monotonic()
    gpu = PhysicalDeviceSnapshot(
        physical_id="nvidia:GPU-test",
        name="NVIDIA GeForce GTX 1650",
        memory_kind=DeviceMemoryKind.DEDICATED,
        captured_monotonic_s=now,
        telemetry_quality=TelemetryQuality.FRESH,
        dedicated_total_mib=8192,
        dedicated_budget_mib=8192,
        dedicated_used_mib=512,
    )
    controller, runner, _events, model_root, model_path = _setup_runtime(
        tmp_path,
        monkeypatch,
        device_output="Vulkan0: NVIDIA GeForce GTX 1650 (8192 MiB)",
        gpu_devices=(gpu,),
    )

    plan = controller.plan(
        conversation_id="chat-2",
        model_root=model_root,
        model_path=model_path,
        backend=Backend.VULKAN,
        context_tokens=2048,
        cpu_threads=4,
        port=8081,
    )

    assert plan.admitted
    assert plan.target_id == "vulkan:0"
    argv = RuntimeController._server_argv(plan, plan.runtime_directory / "llama-server.exe")
    assert argv[argv.index("--device") + 1] == "Vulkan0"
    assert runner.start_calls == []


def test_unmatched_vulkan_device_is_fail_closed_and_never_started(tmp_path, monkeypatch) -> None:
    controller, runner, _events, model_root, model_path = _setup_runtime(
        tmp_path,
        monkeypatch,
        device_output="Vulkan0: AMD Radeon Graphics (4096 MiB)",
        gpu_devices=(),
    )

    try:
        controller.start(
            conversation_id="chat-3",
            model_root=model_root,
            model_path=model_path,
            backend=Backend.VULKAN,
            context_tokens=2048,
            cpu_threads=4,
            port=8082,
        )
    except RuntimeWorkflowError as exc:
        assert "abgelehnt" in str(exc)
    else:
        raise AssertionError("unmatched Vulkan target must be rejected")

    assert runner.start_calls == []


def test_server_start_and_stop_are_limited_to_controller_owned_process(tmp_path, monkeypatch) -> None:
    controller, runner, events, model_root, model_path = _setup_runtime(
        tmp_path, monkeypatch, device_output="CPU backend available"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    result = controller.start(
        conversation_id="chat-4",
        model_root=model_root,
        model_path=model_path,
        backend=Backend.CPU,
        context_tokens=2048,
        cpu_threads=4,
        port=port,
    )

    assert result.endpoint == f"http://127.0.0.1:{port}/v1"
    assert result.process_id == 4242
    assert controller.has_owned_process
    assert controller.managed_endpoint == result.endpoint
    assert "--host" in runner.start_calls[0]
    assert runner.start_calls[0][runner.start_calls[0].index("--host") + 1] == "127.0.0.1"

    other_controller = RuntimeController()
    assert other_controller.stop() is False
    assert controller.stop() is True
    assert runner.process.terminated
    assert not runner.process.killed
    assert not controller.has_owned_process
    assert any(event.event_type is ExecutionEventType.EXIT_CODE for event in events)
