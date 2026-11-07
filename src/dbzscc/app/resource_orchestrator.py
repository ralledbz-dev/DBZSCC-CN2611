"""Deterministic, Qt-free resource admission and backend/device planning.

This module plans one backend target per workload. It never starts, stops, or
monitors a process; callers must provide fresh telemetry and stable physical
adapter identities, then enforce the returned reservation atomically.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dbzscc.miro.model_registry import ModelCatalogEntry


class Backend(str, Enum):
    """A runtime/build axis, deliberately separate from physical devices."""

    CPU = "cpu"
    CUDA = "cuda"
    VULKAN = "vulkan"


class DeviceMemoryKind(str, Enum):
    """The pool constrained by a physical adapter placement."""

    DEDICATED = "dedicated_vram"
    SHARED_SYSTEM = "shared_system_memory"


class TelemetryQuality(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class PlanStatus(str, Enum):
    ADMITTED = "admitted"
    REDUCED = "admitted_reduced"
    REJECTED = "rejected"


@dataclass(frozen=True)
class BackendTarget:
    """A runtime-visible target mapped to one stable physical adapter ID.

    CPU targets have ``physical_id=None``. GPU runtime IDs such as ``cuda:0``
    or ``vulkan:1`` are not stable hardware identities and must map to the
    same ``physical_id`` when they refer to the same adapter.
    """

    target_id: str
    physical_id: str | None


@dataclass(frozen=True)
class BackendCapability:
    """Availability of one build/runtime and its runtime-visible targets."""

    backend: Backend
    available: bool
    targets: tuple[BackendTarget, ...] = ()


@dataclass(frozen=True)
class PhysicalDeviceSnapshot:
    """Fresh, adapter-specific telemetry; shared and dedicated pools stay apart."""

    physical_id: str
    name: str
    memory_kind: DeviceMemoryKind
    captured_monotonic_s: float
    telemetry_quality: TelemetryQuality
    dedicated_total_mib: int | None = None
    dedicated_budget_mib: int | None = None
    dedicated_used_mib: int | None = None
    shared_budget_mib: int | None = None
    shared_used_mib: int | None = None
    display_attached: bool | None = None


@dataclass(frozen=True)
class ResourceReservation:
    """An active or pending peak claim, keyed by physical adapter, not backend.

    Available-memory telemetry already reflects observed use. Only the part of
    a reservation not yet visible in telemetry is deducted. ``None`` observed
    values mean unknown and therefore deduct the full claim conservatively.
    For an integrated/shared adapter, ``host_memory_mib`` already includes the
    shared backing; the shared claim is an additional pool constraint, not a
    second host-RAM charge.
    """

    workload_id: str
    backend: Backend
    physical_id: str | None = None
    host_memory_mib: int = 0
    cpu_threads: int = 0
    device_memory_mib: int = 0
    shared_memory_mib: int = 0
    observed_host_memory_mib: int | None = None
    observed_cpu_threads: int | None = None
    observed_device_memory_mib: int | None = None
    observed_shared_memory_mib: int | None = None


@dataclass(frozen=True)
class ResourceSnapshot:
    """One current OS/CPU/device sample plus already-running reservations.

    ``available_physical_mib`` and ``commit_available_mib`` are dynamic
    headroom readings, not installed-memory totals. Device budget/use pairs
    are likewise live readings. ``captured_monotonic_s`` must use the same
    monotonic clock as the planner's ``now_monotonic_s`` argument.
    """

    captured_monotonic_s: float
    telemetry_quality: TelemetryQuality
    available_physical_mib: int | None
    commit_available_mib: int | None
    logical_cpu_cores: int | None
    cpu_load_ratio: float | None
    devices: tuple[PhysicalDeviceSnapshot, ...] = ()
    backends: tuple[BackendCapability, ...] = ()
    reservations: tuple[ResourceReservation, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class WorkloadRequest:
    """Resource inputs for one model workload; no model-specific policy is implied."""

    workload_id: str
    model_id: str
    priority: int = 0
    backend_preferences: tuple[Backend, ...] = (Backend.CUDA, Backend.VULKAN, Backend.CPU)
    model_file_size_mib: int | None = None
    estimated_weight_mib: int | None = None
    kv_cache_mib: int | None = None
    context_tokens: int | None = None
    kv_cache_mib_per_1k_tokens: float | None = None
    backend_overhead_mib: int | None = None
    host_memory_mib: int | None = None
    cpu_threads: int = 4
    min_cpu_threads: int = 1
    allow_cpu_thread_reduction: bool = True
    model_catalog_entry: ModelCatalogEntry | None = None

    def __post_init__(self) -> None:
        if not self.workload_id.strip() or not self.model_id.strip():
            raise ValueError("workload_id and model_id must be non-empty")
        if not self.backend_preferences:
            raise ValueError("backend_preferences must not be empty")
        if self.cpu_threads < 1 or self.min_cpu_threads < 1:
            raise ValueError("CPU thread counts must be positive")
        if self.min_cpu_threads > self.cpu_threads:
            raise ValueError("min_cpu_threads cannot exceed cpu_threads")
        if self.model_catalog_entry is not None:
            from dbzscc.miro.model_registry import ModelCatalogEntry

            if not isinstance(self.model_catalog_entry, ModelCatalogEntry):
                raise ValueError("model_catalog_entry must be a current parsed registry record")
            artifact = self.model_catalog_entry.artifact
            observation = self.model_catalog_entry.observation
            if (
                observation is None
                or artifact.status != "metadata_read"
                or observation.status != "metadata_read"
                or artifact.current_observation_id != observation.observation_id
            ):
                raise ValueError("model_catalog_entry must be a current parsed registry record")
        for name in (
            "model_file_size_mib",
            "estimated_weight_mib",
            "kv_cache_mib",
            "context_tokens",
            "backend_overhead_mib",
            "host_memory_mib",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.kv_cache_mib_per_1k_tokens is not None:
            rate = self.kv_cache_mib_per_1k_tokens
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("kv_cache_mib_per_1k_tokens must be finite and non-negative")


@dataclass(frozen=True)
class ResourcePolicy:
    """Configurable conservative thresholds; these are policy, not telemetry."""

    max_snapshot_age_s: float = 2.0
    os_available_headroom_mib: int = 6144
    min_available_physical_mib: int = 4096
    commit_safety_margin_mib: int = 4096
    cpu_service_reserve_threads: int = 2
    dedicated_gpu_headroom_mib: int = 1024
    shared_gpu_headroom_mib: int = 2048
    display_gpu_headroom_mib: int = 1024
    unknown_kv_reserve_mib: int = 1024
    unknown_backend_overhead_mib: int = 512
    file_size_weight_multiplier: float = 1.25

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_snapshot_age_s) or self.max_snapshot_age_s < 0:
            raise ValueError("max_snapshot_age_s must be finite and non-negative")
        for name in (
            "os_available_headroom_mib",
            "min_available_physical_mib",
            "commit_safety_margin_mib",
            "cpu_service_reserve_threads",
            "dedicated_gpu_headroom_mib",
            "shared_gpu_headroom_mib",
            "display_gpu_headroom_mib",
            "unknown_kv_reserve_mib",
            "unknown_backend_overhead_mib",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if (
            not math.isfinite(self.file_size_weight_multiplier)
            or self.file_size_weight_multiplier < 1.0
        ):
            raise ValueError("file_size_weight_multiplier must be finite and at least 1")


@dataclass(frozen=True)
class ResourcePlan:
    """Typed, auditable placement and budgets for a single workload."""

    workload_id: str
    model_id: str
    status: PlanStatus
    backend: Backend | None = None
    backend_target_id: str | None = None
    physical_id: str | None = None
    memory_kind: DeviceMemoryKind | None = None
    accelerator_memory_mib: int = 0
    host_memory_mib: int = 0
    cpu_threads: int = 0
    model_weights_mib: int | None = None
    kv_cache_mib: int | None = None
    backend_overhead_mib: int | None = None
    host_capacity_mib_before: int | None = None
    accelerator_capacity_mib_before: int | None = None
    cpu_capacity_threads_before: int | None = None
    reasons: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    fail_closed_reasons: tuple[str, ...] = ()

    @property
    def admitted(self) -> bool:
        return self.status in (PlanStatus.ADMITTED, PlanStatus.REDUCED)


@dataclass(frozen=True)
class _ModelEstimate:
    weights_mib: int
    kv_cache_mib: int
    backend_overhead_mib: int
    total_mib: int
    uncertainties: tuple[str, ...]


class ResourceOrchestrator:
    """Pure deterministic planner; process lifecycle is intentionally external."""

    def __init__(self, policy: ResourcePolicy | None = None) -> None:
        self.policy = policy or ResourcePolicy()

    def plan(
        self,
        workload: WorkloadRequest,
        snapshot: ResourceSnapshot,
        *,
        now_monotonic_s: float | None = None,
    ) -> ResourcePlan:
        """Plan one workload. Pass ``now_monotonic_s`` for reproducible decisions."""
        return self.plan_many((workload,), snapshot, now_monotonic_s=now_monotonic_s)[0]

    def plan_many(
        self,
        workloads: Iterable[WorkloadRequest],
        snapshot: ResourceSnapshot,
        *,
        now_monotonic_s: float | None = None,
    ) -> tuple[ResourcePlan, ...]:
        """Plan a batch by descending priority, reserving accepted claims cumulatively.

        The returned tuple preserves caller order. Equal priorities are ordered by
        workload ID, making contention outcomes deterministic and auditable.
        """
        requests = tuple(workloads)
        if not requests:
            return ()
        now = time.monotonic() if now_monotonic_s is None else now_monotonic_s
        snapshot_errors = self._snapshot_errors(snapshot, now)
        indexed = list(enumerate(requests))
        ids: dict[str, list[int]] = defaultdict(list)
        for index, request in indexed:
            ids[request.workload_id].append(index)
        duplicate_indexes = {
            index for indexes in ids.values() if len(indexes) > 1 for index in indexes
        }

        plans: list[ResourcePlan | None] = [None] * len(requests)
        added_reservations: list[ResourceReservation] = []
        ordered = sorted(
            (pair for pair in indexed if pair[0] not in duplicate_indexes),
            key=lambda pair: (
                -pair[1].priority,
                pair[1].workload_id.casefold(),
                pair[1].workload_id,
                pair[0],
            ),
        )
        for index, request in indexed:
            if index in duplicate_indexes:
                plans[index] = self._rejected(
                    request,
                    ("duplicate_workload_id: batch workload IDs must be unique",),
                )
            elif snapshot_errors:
                plans[index] = self._rejected(request, snapshot_errors)

        if snapshot_errors:
            return tuple(plan for plan in plans if plan is not None)

        for index, request in ordered:
            estimate, estimate_errors = self._estimate(request)
            if estimate_errors:
                plans[index] = self._rejected(request, estimate_errors)
                continue
            assert estimate is not None
            plan = self._plan_one(
                request,
                estimate,
                snapshot,
                tuple(snapshot.reservations) + tuple(added_reservations),
                now,
            )
            plans[index] = plan
            if plan.admitted:
                added_reservations.append(self._reservation_from_plan(plan))

        return tuple(plan for plan in plans if plan is not None)

    def _plan_one(
        self,
        request: WorkloadRequest,
        estimate: _ModelEstimate,
        snapshot: ResourceSnapshot,
        reservations: tuple[ResourceReservation, ...],
        now: float,
    ) -> ResourcePlan:
        host_capacity = self._host_capacity(snapshot, reservations)
        cpu_capacity = self._cpu_capacity(snapshot, reservations)
        if host_capacity is None:
            return self._rejected(request, ("host_memory_telemetry_unavailable",), estimate)
        if cpu_capacity is None:
            return self._rejected(request, ("cpu_telemetry_unavailable",), estimate)

        failures: list[str] = []
        capabilities: dict[Backend, list[BackendCapability]] = defaultdict(list)
        for capability in snapshot.backends:
            capabilities[capability.backend].append(capability)

        for backend in request.backend_preferences:
            backend_caps = capabilities.get(backend, [])
            if not backend_caps or not any(cap.available for cap in backend_caps):
                failures.append(f"{backend.value}: backend_build_unavailable")
                continue
            targets = [target for cap in backend_caps if cap.available for target in cap.targets]
            if backend is Backend.CPU:
                targets = [target for target in targets if target.physical_id is None]
                if not targets:
                    failures.append("cpu: no_explicit_cpu_target")
                    continue
                target = min(targets, key=lambda item: (item.target_id.casefold(), item.target_id))
                plan, candidate_failure = self._try_candidate(
                    request,
                    estimate,
                    snapshot,
                    reservations,
                    backend,
                    target,
                    None,
                    host_capacity,
                    cpu_capacity,
                    None,
                )
                if plan is not None:
                    return self._with_fallback_reasons(plan, failures)
                failures.append(f"cpu:{candidate_failure}")
                continue

            # Collapse repeated API/runtime aliases to one physical adapter before
            # evaluating capacity. The selected runtime target remains explicit.
            unique_targets: dict[str, list[BackendTarget]] = defaultdict(list)
            for target in targets:
                if target.physical_id:
                    unique_targets[target.physical_id].append(target)
                else:
                    failures.append(f"{backend.value}: target_missing_physical_id")
            if not unique_targets:
                failures.append(f"{backend.value}: no_physical_targets")
                continue
            candidates: list[tuple[PhysicalDeviceSnapshot, BackendTarget]] = []
            for physical_id, aliases in unique_targets.items():
                device, device_error = self._device_for(snapshot, physical_id)
                if device_error:
                    failures.append(f"{backend.value}:{physical_id}:{device_error}")
                    continue
                assert device is not None
                target = min(aliases, key=lambda item: (item.target_id.casefold(), item.target_id))
                candidates.append((device, target))
            candidates.sort(
                key=lambda pair: (
                    0 if pair[0].memory_kind is DeviceMemoryKind.DEDICATED else 1,
                    pair[0].physical_id.casefold(),
                    pair[1].target_id.casefold(),
                    pair[1].target_id,
                )
            )
            for device, target in candidates:
                capacity, device_uncertainties, device_error = self._device_capacity(
                    device, reservations, now
                )
                if device_error:
                    failures.append(f"{backend.value}:{device.physical_id}:{device_error}")
                    continue
                assert capacity is not None
                plan, candidate_failure = self._try_candidate(
                    request,
                    estimate,
                    snapshot,
                    reservations,
                    backend,
                    target,
                    device,
                    host_capacity,
                    cpu_capacity,
                    capacity,
                    extra_uncertainties=device_uncertainties,
                )
                if plan is not None:
                    return self._with_fallback_reasons(plan, failures)
                failures.append(f"{backend.value}:{device.physical_id}:{candidate_failure}")

        fail_reasons = tuple(dict.fromkeys(failures)) or ("no_backend_target_available",)
        return self._rejected(request, fail_reasons, estimate)

    def _try_candidate(
        self,
        request: WorkloadRequest,
        estimate: _ModelEstimate,
        snapshot: ResourceSnapshot,
        reservations: tuple[ResourceReservation, ...],
        backend: Backend,
        target: BackendTarget,
        device: PhysicalDeviceSnapshot | None,
        host_capacity: int,
        cpu_capacity: int,
        accelerator_capacity: int | None,
        *,
        extra_uncertainties: tuple[str, ...] = (),
    ) -> tuple[ResourcePlan | None, str | None]:
        if device is None:
            host_required = max(estimate.total_mib, request.host_memory_mib or 0)
            accelerator_required = 0
            memory_kind = None
        elif device.memory_kind is DeviceMemoryKind.SHARED_SYSTEM:
            accelerator_required = estimate.total_mib
            # UMA/shared allocation is the same host backing, tested against both
            # budgets but never added twice to host RAM.
            host_required = max(accelerator_required, request.host_memory_mib or 0)
            memory_kind = DeviceMemoryKind.SHARED_SYSTEM
        else:
            accelerator_required = estimate.total_mib
            # Without a measured per-model host footprint, reserve the full
            # conservative model estimate in host RAM as well as dedicated VRAM.
            host_required = request.host_memory_mib or estimate.total_mib
            memory_kind = DeviceMemoryKind.DEDICATED

        if host_required > host_capacity:
            return None, (
                f"insufficient_host_memory:required_{host_required}_mib_"
                f"available_{host_capacity}_mib"
            )
        if accelerator_capacity is not None and accelerator_required > accelerator_capacity:
            return None, (
                f"insufficient_accelerator_memory:required_{accelerator_required}_mib_"
                f"available_{accelerator_capacity}_mib"
            )
        assigned_threads = min(request.cpu_threads, cpu_capacity)
        if assigned_threads < request.min_cpu_threads or (
            assigned_threads < request.cpu_threads and not request.allow_cpu_thread_reduction
        ):
            return None, (
                f"insufficient_cpu_threads:requested_{request.cpu_threads}_"
                f"available_{cpu_capacity}_minimum_{request.min_cpu_threads}"
            )

        reduced = assigned_threads < request.cpu_threads
        reasons = [f"Selected {backend.value} target {target.target_id}."]
        if device is not None:
            reasons.append(
                f"Physical adapter {device.physical_id} is budgeted once across backend aliases."
            )
        if reduced:
            reasons.append(
                f"CPU threads reduced from {request.cpu_threads} to {assigned_threads} "
                "to respect current load and service reserve."
            )
        uncertainties = list(estimate.uncertainties)
        uncertainties.extend(extra_uncertainties)
        uncertainties.append(
            "One backend target is planned; no cross-backend split or same-process CUDA+Vulkan support is assumed."
        )
        if (
            device is not None
            and device.memory_kind is DeviceMemoryKind.DEDICATED
            and request.host_memory_mib is None
        ):
            uncertainties.append(
                "Dedicated-GPU host residency is unknown; the full conservative model estimate is reserved in host RAM."
            )
        if failures := self._other_backend_alias_note(snapshot, device):
            uncertainties.append(failures)
        return ResourcePlan(
            workload_id=request.workload_id,
            model_id=request.model_id,
            status=PlanStatus.REDUCED if reduced else PlanStatus.ADMITTED,
            backend=backend,
            backend_target_id=target.target_id,
            physical_id=device.physical_id if device is not None else None,
            memory_kind=memory_kind,
            accelerator_memory_mib=accelerator_required,
            host_memory_mib=host_required,
            cpu_threads=assigned_threads,
            model_weights_mib=estimate.weights_mib,
            kv_cache_mib=estimate.kv_cache_mib,
            backend_overhead_mib=estimate.backend_overhead_mib,
            host_capacity_mib_before=host_capacity,
            accelerator_capacity_mib_before=accelerator_capacity,
            cpu_capacity_threads_before=cpu_capacity,
            reasons=tuple(reasons),
            uncertainties=tuple(dict.fromkeys(uncertainties)),
        ), None

    def _host_capacity(
        self,
        snapshot: ResourceSnapshot,
        reservations: tuple[ResourceReservation, ...],
    ) -> int | None:
        available = snapshot.available_physical_mib
        commit_available = snapshot.commit_available_mib
        if available is None or commit_available is None or available < 0 or commit_available < 0:
            return None
        physical_capacity = (
            available
            - self.policy.os_available_headroom_mib
            - self.policy.min_available_physical_mib
        )
        commit_capacity = commit_available - self.policy.commit_safety_margin_mib
        unobserved = sum(
            self._unobserved(item.host_memory_mib, item.observed_host_memory_mib)
            for item in reservations
        )
        return max(0, min(physical_capacity, commit_capacity) - unobserved)

    def _cpu_capacity(
        self,
        snapshot: ResourceSnapshot,
        reservations: tuple[ResourceReservation, ...],
    ) -> int | None:
        cores = snapshot.logical_cpu_cores
        load = snapshot.cpu_load_ratio
        if (
            cores is None
            or cores <= 0
            or load is None
            or not math.isfinite(load)
            or not 0 <= load <= 1
        ):
            return None
        load_adjusted = math.floor(cores * (1.0 - load))
        unobserved_threads = sum(
            self._unobserved(item.cpu_threads, item.observed_cpu_threads) for item in reservations
        )
        return max(
            0,
            load_adjusted - self.policy.cpu_service_reserve_threads - unobserved_threads,
        )

    def _device_capacity(
        self,
        device: PhysicalDeviceSnapshot,
        reservations: tuple[ResourceReservation, ...],
        now: float,
    ) -> tuple[int | None, tuple[str, ...], str | None]:
        if device.telemetry_quality is not TelemetryQuality.FRESH:
            return None, (), "device_telemetry_not_fresh"
        age = now - device.captured_monotonic_s
        if not math.isfinite(age) or age < 0 or age > self.policy.max_snapshot_age_s:
            return None, (), "device_snapshot_expired_or_future"
        relevant = tuple(item for item in reservations if item.physical_id == device.physical_id)
        if device.memory_kind is DeviceMemoryKind.DEDICATED:
            values = (
                device.dedicated_total_mib,
                device.dedicated_budget_mib,
                device.dedicated_used_mib,
            )
            if any(value is None or value < 0 for value in values):
                return None, (), "dedicated_memory_budget_unknown"
            total, budget, used = values
            free = min(total, budget) - used  # type: ignore[arg-type]
            claims = sum(
                self._unobserved(item.device_memory_mib, item.observed_device_memory_mib)
                for item in relevant
            )
            capacity = max(0, free - self.policy.dedicated_gpu_headroom_mib - claims)
            return capacity, (), None

        if device.shared_budget_mib is None or device.shared_used_mib is None:
            return None, (), "shared_memory_budget_unknown"
        if device.shared_budget_mib < 0 or device.shared_used_mib < 0:
            return None, (), "shared_memory_budget_invalid"
        claims = sum(
            self._unobserved(item.shared_memory_mib, item.observed_shared_memory_mib)
            for item in relevant
        )
        headroom = self.policy.shared_gpu_headroom_mib
        uncertainties: list[str] = []
        if device.display_attached is not False:
            headroom += self.policy.display_gpu_headroom_mib
            if device.display_attached is None:
                uncertainties.append(
                    "Display-to-adapter role is unknown; the additional display headroom is retained."
                )
            else:
                uncertainties.append(
                    "Display-driving shared-memory adapter is subject to extra dynamic headroom."
                )
        free = device.shared_budget_mib - device.shared_used_mib
        capacity = max(0, free - headroom - claims)
        return capacity, tuple(uncertainties), None

    def _snapshot_errors(self, snapshot: ResourceSnapshot, now: float) -> tuple[str, ...]:
        errors: list[str] = []
        if snapshot.telemetry_quality is not TelemetryQuality.FRESH:
            errors.append("snapshot_telemetry_not_fresh")
        age = now - snapshot.captured_monotonic_s
        if not math.isfinite(age) or age < 0 or age > self.policy.max_snapshot_age_s:
            errors.append("snapshot_expired_or_future")
        if snapshot.available_physical_mib is None or snapshot.commit_available_mib is None:
            errors.append("host_memory_telemetry_unavailable")
        elif snapshot.available_physical_mib < 0 or snapshot.commit_available_mib < 0:
            errors.append("host_memory_telemetry_invalid")
        if (
            snapshot.logical_cpu_cores is None
            or snapshot.logical_cpu_cores <= 0
            or snapshot.cpu_load_ratio is None
            or not math.isfinite(snapshot.cpu_load_ratio)
            or not 0 <= snapshot.cpu_load_ratio <= 1
        ):
            errors.append("cpu_telemetry_unavailable_or_invalid")
        return tuple(errors)

    def _device_for(
        self, snapshot: ResourceSnapshot, physical_id: str
    ) -> tuple[PhysicalDeviceSnapshot | None, str | None]:
        matches = [device for device in snapshot.devices if device.physical_id == physical_id]
        if not matches:
            return None, "physical_adapter_not_in_snapshot"
        first = matches[0]
        if any(device != first for device in matches[1:]):
            return None, "conflicting_duplicate_physical_snapshots"
        return first, None

    def _estimate(self, request: WorkloadRequest) -> tuple[_ModelEstimate | None, tuple[str, ...]]:
        uncertainties: list[str] = []
        if request.estimated_weight_mib is not None:
            weights = request.estimated_weight_mib
            uncertainties.append(
                "Caller-provided weight footprint is an estimate, not a measured runtime peak."
            )
        elif request.model_file_size_mib is not None and request.model_file_size_mib > 0:
            weights = math.ceil(
                request.model_file_size_mib * self.policy.file_size_weight_multiplier
            )
            uncertainties.append(
                "Weight memory is conservatively derived from file size; GGUF size is not runtime memory."
            )
        else:
            return None, (
                "model_footprint_unknown: provide estimated_weight_mib or model_file_size_mib",
            )

        if request.kv_cache_mib is not None:
            kv_cache = request.kv_cache_mib
            uncertainties.append("KV-cache estimate is caller-supplied and not runtime-measured.")
        elif request.context_tokens is not None and request.kv_cache_mib_per_1k_tokens is not None:
            kv_cache = math.ceil(request.context_tokens * request.kv_cache_mib_per_1k_tokens / 1024)
            uncertainties.append(
                "KV-cache is estimated from caller-supplied context and per-1K-token rate."
            )
        else:
            kv_cache = self.policy.unknown_kv_reserve_mib
            uncertainties.append(
                "KV-cache/context footprint is unknown; a configurable conservative reserve is used."
            )

        if request.backend_overhead_mib is None:
            backend_overhead = self.policy.unknown_backend_overhead_mib
            uncertainties.append(
                "Backend/allocator/workspace overhead is unknown; a configurable reserve is used."
            )
        else:
            backend_overhead = request.backend_overhead_mib
            uncertainties.append("Backend overhead is caller-supplied and not runtime-measured.")
        return (
            _ModelEstimate(
                weights_mib=weights,
                kv_cache_mib=kv_cache,
                backend_overhead_mib=backend_overhead,
                total_mib=weights + kv_cache + backend_overhead,
                uncertainties=tuple(uncertainties),
            ),
            (),
        )

    @staticmethod
    def _unobserved(claim: int, observed: int | None) -> int:
        return max(0, claim - (0 if observed is None else observed))

    @staticmethod
    def _reservation_from_plan(plan: ResourcePlan) -> ResourceReservation:
        return ResourceReservation(
            workload_id=plan.workload_id,
            backend=plan.backend or Backend.CPU,
            physical_id=plan.physical_id,
            host_memory_mib=plan.host_memory_mib,
            cpu_threads=plan.cpu_threads,
            device_memory_mib=(
                plan.accelerator_memory_mib if plan.memory_kind is DeviceMemoryKind.DEDICATED else 0
            ),
            shared_memory_mib=(
                plan.accelerator_memory_mib
                if plan.memory_kind is DeviceMemoryKind.SHARED_SYSTEM
                else 0
            ),
            observed_host_memory_mib=0,
            observed_cpu_threads=0,
            observed_device_memory_mib=0,
            observed_shared_memory_mib=0,
        )

    @staticmethod
    def _other_backend_alias_note(
        snapshot: ResourceSnapshot,
        device: PhysicalDeviceSnapshot | None,
    ) -> str | None:
        if device is None:
            return None
        aliases = {
            capability.backend
            for capability in snapshot.backends
            if capability.available
            and capability.backend in (Backend.CUDA, Backend.VULKAN)
            and any(target.physical_id == device.physical_id for target in capability.targets)
        }
        if Backend.CUDA in aliases and Backend.VULKAN in aliases:
            return (
                "The same physical adapter is visible through CUDA and Vulkan aliases; "
                "only the selected backend is assigned, with a shared physical budget."
            )
        return None

    @staticmethod
    def _with_fallback_reasons(plan: ResourcePlan, failures: list[str]) -> ResourcePlan:
        if not failures:
            return plan
        return ResourcePlan(
            **{
                **plan.__dict__,
                "reasons": plan.reasons
                + ("Earlier preferred candidate(s) were skipped: " + "; ".join(failures),),
            }
        )

    @staticmethod
    def _rejected(
        request: WorkloadRequest,
        reasons: tuple[str, ...],
        estimate: _ModelEstimate | None = None,
    ) -> ResourcePlan:
        return ResourcePlan(
            workload_id=request.workload_id,
            model_id=request.model_id,
            status=PlanStatus.REJECTED,
            model_weights_mib=estimate.weights_mib if estimate else None,
            kv_cache_mib=estimate.kv_cache_mib if estimate else None,
            backend_overhead_mib=estimate.backend_overhead_mib if estimate else None,
            uncertainties=estimate.uncertainties if estimate else (),
            fail_closed_reasons=tuple(dict.fromkeys(reasons)),
        )
