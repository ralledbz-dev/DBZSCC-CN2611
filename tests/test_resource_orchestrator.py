from __future__ import annotations

from dataclasses import replace

import pytest

from dbzscc.app.resource_orchestrator import (
    Backend,
    BackendCapability,
    BackendTarget,
    DeviceMemoryKind,
    PhysicalDeviceSnapshot,
    PlanStatus,
    ResourceOrchestrator,
    ResourcePolicy,
    ResourceSnapshot,
    TelemetryQuality,
    WorkloadRequest,
)

NOW = 100.0
NVIDIA_ID = "pci:0000:01:00.0"
AMD_ID = "luid:amd-igpu-01"


def _nvidia(
    *,
    budget_mib: int = 4096,
    used_mib: int = 512,
    quality: TelemetryQuality = TelemetryQuality.FRESH,
) -> PhysicalDeviceSnapshot:
    return PhysicalDeviceSnapshot(
        physical_id=NVIDIA_ID,
        name="NVIDIA GTX 1650 SUPER",
        memory_kind=DeviceMemoryKind.DEDICATED,
        captured_monotonic_s=NOW,
        telemetry_quality=quality,
        dedicated_total_mib=4096,
        dedicated_budget_mib=budget_mib,
        dedicated_used_mib=used_mib,
        display_attached=False,
    )


def _amd(
    *,
    budget_mib: int = 8192,
    used_mib: int = 512,
    display_attached: bool | None = True,
    quality: TelemetryQuality = TelemetryQuality.FRESH,
) -> PhysicalDeviceSnapshot:
    return PhysicalDeviceSnapshot(
        physical_id=AMD_ID,
        name="AMD Radeon integrated graphics",
        memory_kind=DeviceMemoryKind.SHARED_SYSTEM,
        captured_monotonic_s=NOW,
        telemetry_quality=quality,
        # A reported UMA allocation is deliberately not treated as dedicated VRAM.
        dedicated_total_mib=4096,
        dedicated_budget_mib=4096,
        dedicated_used_mib=1024,
        shared_budget_mib=budget_mib,
        shared_used_mib=used_mib,
        display_attached=display_attached,
    )


def _snapshot(
    *,
    devices: tuple[PhysicalDeviceSnapshot, ...] | None = None,
    backends: tuple[BackendCapability, ...] | None = None,
    available_physical_mib: int = 24_000,
    commit_available_mib: int = 24_000,
    logical_cpu_cores: int = 12,
    cpu_load_ratio: float = 0.1,
    quality: TelemetryQuality = TelemetryQuality.FRESH,
    captured_monotonic_s: float = NOW,
) -> ResourceSnapshot:
    if devices is None:
        devices = (_nvidia(), _amd())
    if backends is None:
        backends = (
            BackendCapability(
                Backend.CUDA,
                True,
                (BackendTarget("cuda:0", NVIDIA_ID),),
            ),
            BackendCapability(
                Backend.VULKAN,
                True,
                (
                    BackendTarget("vulkan:0", AMD_ID),
                    BackendTarget("vulkan:1", NVIDIA_ID),
                ),
            ),
            BackendCapability(Backend.CPU, True, (BackendTarget("cpu", None),)),
        )
    return ResourceSnapshot(
        captured_monotonic_s=captured_monotonic_s,
        telemetry_quality=quality,
        available_physical_mib=available_physical_mib,
        commit_available_mib=commit_available_mib,
        logical_cpu_cores=logical_cpu_cores,
        cpu_load_ratio=cpu_load_ratio,
        devices=devices,
        backends=backends,
        source="deterministic-test-fixture",
    )


def _workload(
    workload_id: str = "job-1",
    *,
    preferences: tuple[Backend, ...] = (Backend.CUDA, Backend.VULKAN, Backend.CPU),
    weight_mib: int = 1000,
    kv_mib: int = 500,
    overhead_mib: int = 200,
    cpu_threads: int = 4,
    min_cpu_threads: int = 1,
    priority: int = 0,
) -> WorkloadRequest:
    return WorkloadRequest(
        workload_id=workload_id,
        model_id=f"model-{workload_id}",
        priority=priority,
        backend_preferences=preferences,
        estimated_weight_mib=weight_mib,
        kv_cache_mib=kv_mib,
        backend_overhead_mib=overhead_mib,
        cpu_threads=cpu_threads,
        min_cpu_threads=min_cpu_threads,
    )


def _cpu_only_snapshot(**kwargs: object) -> ResourceSnapshot:
    return _snapshot(
        devices=(),
        backends=(BackendCapability(Backend.CPU, True, (BackendTarget("cpu", None),)),),
        **kwargs,
    )


def test_vulkan_selects_nvidia_before_display_igpu_and_keeps_backend_axis() -> None:
    snapshot = _snapshot()
    request = _workload("vulkan-job", preferences=(Backend.VULKAN, Backend.CPU))

    plan = ResourceOrchestrator().plan(request, snapshot, now_monotonic_s=NOW)

    assert plan.status is PlanStatus.ADMITTED
    assert plan.backend is Backend.VULKAN
    assert plan.backend_target_id == "vulkan:1"
    assert plan.physical_id == NVIDIA_ID
    assert plan.memory_kind is DeviceMemoryKind.DEDICATED
    assert plan.accelerator_memory_mib == 1700
    assert "only the selected backend is assigned" in " ".join(plan.uncertainties)


def test_repeated_runtime_aliases_for_one_physical_adapter_are_collapsed() -> None:
    snapshot = _snapshot(
        devices=(_nvidia(),),
        backends=(
            BackendCapability(
                Backend.VULKAN,
                True,
                (
                    BackendTarget("vulkan:2", NVIDIA_ID),
                    BackendTarget("vulkan:1", NVIDIA_ID),
                ),
            ),
        ),
    )

    plan = ResourceOrchestrator().plan(
        _workload(preferences=(Backend.VULKAN,)), snapshot, now_monotonic_s=NOW
    )

    assert plan.admitted
    assert plan.backend_target_id == "vulkan:1"
    assert plan.physical_id == NVIDIA_ID


def test_cuda_and_vulkan_aliases_share_one_cumulative_physical_budget() -> None:
    snapshot = _snapshot(
        devices=(_nvidia(budget_mib=4096, used_mib=0),),
        backends=(
            BackendCapability(Backend.CUDA, True, (BackendTarget("cuda:0", NVIDIA_ID),)),
            BackendCapability(Backend.VULKAN, True, (BackendTarget("vulkan:1", NVIDIA_ID),)),
        ),
    )
    first = _workload(
        "first", preferences=(Backend.CUDA,), weight_mib=1000, kv_mib=600, overhead_mib=0
    )
    second = _workload(
        "second", preferences=(Backend.VULKAN,), weight_mib=1000, kv_mib=600, overhead_mib=0
    )

    plans = ResourceOrchestrator().plan_many((first, second), snapshot, now_monotonic_s=NOW)

    assert plans[0].admitted
    assert plans[1].status is PlanStatus.REJECTED
    assert plans[1].fail_closed_reasons == (
        "vulkan:pci:0000:01:00.0:insufficient_accelerator_memory:required_1600_mib_available_1472_mib",
    )


def test_shared_uma_backing_is_checked_twice_but_not_double_charged_to_host() -> None:
    policy = ResourcePolicy(
        os_available_headroom_mib=1000,
        min_available_physical_mib=1000,
        commit_safety_margin_mib=1000,
        cpu_service_reserve_threads=1,
        shared_gpu_headroom_mib=500,
        display_gpu_headroom_mib=500,
    )
    snapshot = _snapshot(
        devices=(_amd(budget_mib=4000, used_mib=0),),
        backends=(BackendCapability(Backend.VULKAN, True, (BackendTarget("vulkan:0", AMD_ID),)),),
        available_physical_mib=5500,
        commit_available_mib=5500,
    )
    request = _workload(
        "uma", preferences=(Backend.VULKAN,), weight_mib=1000, kv_mib=500, overhead_mib=0
    )

    plan = ResourceOrchestrator(policy).plan(request, snapshot, now_monotonic_s=NOW)

    assert plan.admitted
    assert plan.memory_kind is DeviceMemoryKind.SHARED_SYSTEM
    assert plan.accelerator_memory_mib == 1500
    assert plan.host_memory_mib == 1500
    assert plan.host_capacity_mib_before == 3500
    assert plan.accelerator_capacity_mib_before == 3000


def test_host_memory_threshold_is_admitted_at_boundary_and_rejected_one_mib_below() -> None:
    policy = ResourcePolicy(
        os_available_headroom_mib=1000,
        min_available_physical_mib=1000,
        commit_safety_margin_mib=1000,
        cpu_service_reserve_threads=1,
    )
    exact = _cpu_only_snapshot(
        available_physical_mib=3000,
        commit_available_mib=2000,
        logical_cpu_cores=4,
        cpu_load_ratio=0,
    )
    request = _workload(
        "boundary", preferences=(Backend.CPU,), weight_mib=500, kv_mib=300, overhead_mib=200
    )
    planner = ResourceOrchestrator(policy)

    admitted = planner.plan(request, exact, now_monotonic_s=NOW)
    rejected = planner.plan(
        request,
        replace(exact, available_physical_mib=2999),
        now_monotonic_s=NOW,
    )

    assert admitted.admitted
    assert admitted.host_capacity_mib_before == 1000
    assert rejected.status is PlanStatus.REJECTED
    assert rejected.fail_closed_reasons == (
        "cpu:insufficient_host_memory:required_1000_mib_available_999_mib",
    )


def test_dedicated_gpu_budget_boundary_uses_fresh_budget_and_guard() -> None:
    snapshot = _snapshot(
        devices=(_nvidia(budget_mib=4096, used_mib=1024),),
        backends=(BackendCapability(Backend.CUDA, True, (BackendTarget("cuda:0", NVIDIA_ID),)),),
    )
    exact = _workload(
        "gpu-boundary", preferences=(Backend.CUDA,), weight_mib=1000, kv_mib=700, overhead_mib=348
    )
    too_large = replace(exact, workload_id="gpu-too-large", backend_overhead_mib=349)
    planner = ResourceOrchestrator()

    admitted = planner.plan(exact, snapshot, now_monotonic_s=NOW)
    rejected = planner.plan(too_large, snapshot, now_monotonic_s=NOW)

    assert admitted.admitted
    assert admitted.accelerator_capacity_mib_before == 2048
    assert rejected.status is PlanStatus.REJECTED
    assert rejected.fail_closed_reasons == (
        f"cuda:{NVIDIA_ID}:insufficient_accelerator_memory:required_2049_mib_available_2048_mib",
    )


def test_cpu_threads_reduce_with_live_load_and_reject_below_minimum() -> None:
    snapshot = _cpu_only_snapshot(logical_cpu_cores=8, cpu_load_ratio=0.5)
    planner = ResourceOrchestrator()
    reducible = _workload("reduce", preferences=(Backend.CPU,), cpu_threads=4, min_cpu_threads=1)
    not_reducible = replace(reducible, workload_id="reject", min_cpu_threads=3)

    reduced = planner.plan(reducible, snapshot, now_monotonic_s=NOW)
    rejected = planner.plan(not_reducible, snapshot, now_monotonic_s=NOW)

    assert reduced.status is PlanStatus.REDUCED
    assert reduced.cpu_capacity_threads_before == 2
    assert reduced.cpu_threads == 2
    assert rejected.status is PlanStatus.REJECTED


def test_gpu_pressure_falls_back_to_cpu_only_when_cpu_was_requested() -> None:
    snapshot = _snapshot(
        devices=(_nvidia(budget_mib=2048, used_mib=512),),
        backends=(
            BackendCapability(Backend.CUDA, True, (BackendTarget("cuda:0", NVIDIA_ID),)),
            BackendCapability(Backend.CPU, True, (BackendTarget("cpu", None),)),
        ),
    )
    plan = ResourceOrchestrator().plan(_workload("fallback"), snapshot, now_monotonic_s=NOW)

    assert plan.admitted
    assert plan.backend is Backend.CPU
    assert "cuda:" in plan.reasons[-1]


def test_stale_gpu_telemetry_can_fall_back_but_stale_host_snapshot_fails_closed() -> None:
    stale_device = _snapshot(
        devices=(_nvidia(quality=TelemetryQuality.STALE),),
        backends=(
            BackendCapability(Backend.CUDA, True, (BackendTarget("cuda:0", NVIDIA_ID),)),
            BackendCapability(Backend.CPU, True, (BackendTarget("cpu", None),)),
        ),
    )
    planner = ResourceOrchestrator()
    fallback = planner.plan(_workload("fallback"), stale_device, now_monotonic_s=NOW)
    stale_host = planner.plan(
        _workload("stale-host"),
        replace(stale_device, telemetry_quality=TelemetryQuality.STALE),
        now_monotonic_s=NOW,
    )

    assert fallback.admitted
    assert fallback.backend is Backend.CPU
    assert "device_telemetry_not_fresh" in fallback.reasons[-1]
    assert stale_host.status is PlanStatus.REJECTED
    assert "snapshot_telemetry_not_fresh" in stale_host.fail_closed_reasons


def test_expired_snapshot_is_rejected_instead_of_reusing_old_measurements() -> None:
    snapshot = _cpu_only_snapshot(captured_monotonic_s=NOW - 3)

    plan = ResourceOrchestrator().plan(
        _workload(preferences=(Backend.CPU,)), snapshot, now_monotonic_s=NOW
    )

    assert plan.status is PlanStatus.REJECTED
    assert "snapshot_expired_or_future" in plan.fail_closed_reasons


def test_unknown_footprint_is_fail_closed_and_file_size_is_not_exact_runtime_memory() -> None:
    planner = ResourceOrchestrator()
    missing = WorkloadRequest(
        workload_id="missing-size",
        model_id="unknown",
        backend_preferences=(Backend.CPU,),
    )
    file_based = WorkloadRequest(
        workload_id="file-size",
        model_id="file-size-model",
        backend_preferences=(Backend.CPU,),
        model_file_size_mib=1000,
        cpu_threads=2,
    )

    rejected = planner.plan(missing, _cpu_only_snapshot(), now_monotonic_s=NOW)
    admitted = planner.plan(file_based, _cpu_only_snapshot(), now_monotonic_s=NOW)

    assert rejected.status is PlanStatus.REJECTED
    assert "model_footprint_unknown" in rejected.fail_closed_reasons[0]
    assert admitted.admitted
    assert admitted.model_weights_mib == 1250
    assert admitted.kv_cache_mib == ResourcePolicy().unknown_kv_reserve_mib
    assert any("GGUF size is not runtime memory" in note for note in admitted.uncertainties)
    assert any("KV-cache/context footprint is unknown" in note for note in admitted.uncertainties)


def test_context_kv_estimate_and_unknown_backend_overhead_are_reported() -> None:
    request = WorkloadRequest(
        workload_id="context-estimate",
        model_id="test-model",
        backend_preferences=(Backend.CPU,),
        estimated_weight_mib=500,
        context_tokens=2048,
        kv_cache_mib_per_1k_tokens=1.5,
        cpu_threads=2,
    )

    plan = ResourceOrchestrator().plan(request, _cpu_only_snapshot(), now_monotonic_s=NOW)

    assert plan.admitted
    assert plan.kv_cache_mib == 3
    assert plan.backend_overhead_mib == ResourcePolicy().unknown_backend_overhead_mib
    assert any("context and per-1K-token rate" in note for note in plan.uncertainties)


def test_priority_order_controls_cumulative_admission_but_results_keep_input_order() -> None:
    snapshot = _snapshot(
        devices=(_nvidia(budget_mib=4096, used_mib=0),),
        backends=(BackendCapability(Backend.CUDA, True, (BackendTarget("cuda:0", NVIDIA_ID),)),),
    )
    low = _workload(
        "low", preferences=(Backend.CUDA,), weight_mib=1000, kv_mib=700, overhead_mib=0, priority=1
    )
    high = _workload(
        "high",
        preferences=(Backend.CUDA,),
        weight_mib=1000,
        kv_mib=700,
        overhead_mib=0,
        priority=10,
    )

    plans = ResourceOrchestrator().plan_many((low, high), snapshot, now_monotonic_s=NOW)

    assert plans[0].workload_id == "low"
    assert plans[0].status is PlanStatus.REJECTED
    assert plans[1].workload_id == "high"
    assert plans[1].admitted


def test_duplicate_physical_device_records_with_conflicting_budgets_fail_closed() -> None:
    snapshot = _snapshot(
        devices=(
            _nvidia(budget_mib=4096, used_mib=512),
            _nvidia(budget_mib=2048, used_mib=512),
        ),
        backends=(BackendCapability(Backend.CUDA, True, (BackendTarget("cuda:0", NVIDIA_ID),)),),
    )

    plan = ResourceOrchestrator().plan(
        _workload(preferences=(Backend.CUDA,)), snapshot, now_monotonic_s=NOW
    )

    assert plan.status is PlanStatus.REJECTED
    assert "conflicting_duplicate_physical_snapshots" in plan.fail_closed_reasons[0]


def test_duplicate_workload_ids_in_one_batch_are_rejected() -> None:
    requests = (_workload("duplicate"), _workload("duplicate"))

    plans = ResourceOrchestrator().plan_many(requests, _snapshot(), now_monotonic_s=NOW)

    assert all(plan.status is PlanStatus.REJECTED for plan in plans)
    assert all("duplicate_workload_id" in plan.fail_closed_reasons[0] for plan in plans)


def test_invalid_snapshot_cpu_or_memory_data_never_silently_allocates() -> None:
    planner = ResourceOrchestrator()
    bad_ram = _cpu_only_snapshot(available_physical_mib=None)
    bad_cpu = _cpu_only_snapshot(cpu_load_ratio=1.2)

    ram_plan = planner.plan(_workload(preferences=(Backend.CPU,)), bad_ram, now_monotonic_s=NOW)
    cpu_plan = planner.plan(_workload(preferences=(Backend.CPU,)), bad_cpu, now_monotonic_s=NOW)

    assert ram_plan.status is PlanStatus.REJECTED
    assert "host_memory_telemetry_unavailable" in ram_plan.fail_closed_reasons
    assert cpu_plan.status is PlanStatus.REJECTED
    assert "cpu_telemetry_unavailable_or_invalid" in cpu_plan.fail_closed_reasons


@pytest.mark.parametrize("backend", [Backend.CUDA, Backend.VULKAN])
def test_gpu_plan_never_claims_shared_ram_as_dedicated_vram(backend: Backend) -> None:
    snapshot = _snapshot(
        devices=(_amd(),),
        backends=(
            BackendCapability(backend, True, (BackendTarget(f"{backend.value}:0", AMD_ID),)),
        ),
    )

    plan = ResourceOrchestrator().plan(
        _workload(preferences=(backend,)), snapshot, now_monotonic_s=NOW
    )

    assert plan.admitted
    assert plan.memory_kind is DeviceMemoryKind.SHARED_SYSTEM
    assert plan.accelerator_memory_mib == plan.host_memory_mib
    assert plan.memory_kind is not DeviceMemoryKind.DEDICATED
