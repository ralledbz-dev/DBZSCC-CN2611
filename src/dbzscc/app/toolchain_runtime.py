"""Explicit, side-effect-free toolchain inventory and invocation contracts.

This module does not search PATH, launch processes, install packages, or persist
results. Runtime discovery accepts only exact paths supplied by the caller.
One-shot tool plans and persistent LSP/debugger session plans have distinct
contracts. Resource demands are translated to the existing CPU-only resource
planner; admission is not process execution or an atomic reservation.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

try:
    import tomllib as _toml
except ModuleNotFoundError:
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None

from .resource_orchestrator import (
    Backend,
    ResourceOrchestrator,
    ResourcePlan,
    ResourceSnapshot,
    WorkloadRequest,
)

MAX_MANIFEST_BYTES = 1_048_576
_MAX_LSP_MESSAGE_BYTES = 16 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_VERSION = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(?:\+[0-9A-Za-z.-]+)?$")
_VERSION_CLAUSE = re.compile(
    r"^(==|!=|>=|<=|>|<)\s*([vV]?\d+(?:\.\d+)*(?:\+[0-9A-Za-z.-]+)?)$"
)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class RuntimeFamily(str, Enum):
    COMPILER = "compiler"
    BUILD_SYSTEM = "build_system"
    DEBUGGER = "debugger"
    LINTER = "linter"
    FORMATTER = "formatter"
    LANGUAGE_SERVER = "language_server"
    TEST_RUNNER = "test_runner"
    LANGUAGE_RUNTIME = "language_runtime"
    PACKAGE_MANAGER = "package_manager"
    DEVELOPMENT_SERVER = "development_server"


class ProcessModel(str, Enum):
    ONE_SHOT = "one_shot"
    PERSISTENT = "persistent"


class CandidateScope(str, Enum):
    APP_BUNDLED = "app_bundled"
    PROJECT_LOCAL = "project_local"
    EXPLICIT_SYSTEM = "explicit_system"


class VersionEvidence(str, Enum):
    DECLARED = "declared"
    OBSERVED = "observed"


class CandidatePresence(str, Enum):
    DECLARED = "declared"
    PRESENT = "present_at_explicit_path"


class PackageKind(str, Enum):
    COMPILER = "compiler"
    BUILD_SYSTEM = "build_system"
    DEBUGGER = "debugger"
    LINTER = "linter"
    FORMATTER = "formatter"
    LANGUAGE_SERVER = "language_server"
    TEST_RUNNER = "test_runner"
    FRAMEWORK = "framework"
    SDK = "sdk"
    NDK = "ndk"
    RUNTIME = "runtime"
    PACKAGE_MANAGER = "package_manager"
    LIBRARY = "library"


class RequirementSource(str, Enum):
    PROJECT_MANIFEST = "project_manifest"
    USER_DECLARED = "user_declared"
    PACKAGE_DEPENDENCY = "package_dependency"


class RuntimeIssueCode(str, Enum):
    MISSING_PATH = "missing_path"
    UNREADABLE_PATH = "unreadable_path"
    NOT_REGULAR_FILE = "not_regular_file"
    REPARSE_POINT = "reparse_point_rejected"
    DUPLICATE_RUNTIME_ID = "duplicate_runtime_id"
    INVALID_CANDIDATE = "invalid_candidate"


class ResolutionIssueCode(str, Enum):
    MISSING_CANDIDATE = "missing_candidate"
    CANDIDATE_LOCATION_NOT_OBSERVED = "candidate_location_not_observed"
    KIND_MISMATCH = "kind_mismatch"
    SCOPE_NOT_ALLOWED = "scope_not_allowed"
    VERSION_UNKNOWN = "version_unknown"
    VERSION_CONSTRAINT_UNSUPPORTED = "version_constraint_unsupported"
    VERSION_INCOMPATIBLE = "version_incompatible"
    AMBIGUOUS_CANDIDATE = "ambiguous_candidate"
    DEPENDENCY_CYCLE = "dependency_cycle"
    DEPENDENCY_CONFLICT = "dependency_conflict"


class InvocationOperation(str, Enum):
    CHECK = "check"
    FIX = "fix"
    COMPILE = "compile"
    BUILD = "build"
    TEST = "test"
    GENERIC = "generic"


class SessionProtocol(str, Enum):
    LSP_JSON_RPC_STDIO = "lsp_json_rpc_stdio"
    DEBUG_ADAPTER_STDIO = "debug_adapter_stdio"
    MANAGED_STDIO = "managed_stdio"


class OneShotState(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    NOT_STARTED = "not_started"


class SessionState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class InstallScope(str, Enum):
    APP_MANAGED = "app_managed"


@dataclass(frozen=True)
class RuntimeCandidateSpec:
    """A caller-supplied exact executable path; never a search pattern or root."""

    runtime_id: str
    family: RuntimeFamily
    executable_path: Path
    scope: CandidateScope
    version: str | None = None
    version_evidence: VersionEvidence = VersionEvidence.DECLARED
    process_model: ProcessModel | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.runtime_id, "runtime_id")
        object.__setattr__(self, "family", RuntimeFamily(self.family))
        object.__setattr__(self, "scope", CandidateScope(self.scope))
        object.__setattr__(self, "version_evidence", VersionEvidence(self.version_evidence))
        model = (
            _process_model(self.family)
            if self.process_model is None
            else ProcessModel(self.process_model)
        )
        if self.family in (RuntimeFamily.DEBUGGER, RuntimeFamily.LANGUAGE_SERVER) and model is not ProcessModel.PERSISTENT:
            raise ValueError("Debuggers and language servers must use the persistent process model.")
        object.__setattr__(self, "process_model", model)
        path = Path(self.executable_path)
        _validate_explicit_path(path, "executable_path")
        object.__setattr__(self, "executable_path", path)
        if self.version is not None:
            _validate_text(self.version, "version", 128)


@dataclass(frozen=True)
class RuntimeDescriptor:
    runtime_id: str
    family: RuntimeFamily
    process_model: ProcessModel
    executable_path: Path
    scope: CandidateScope
    version: str | None = None
    version_evidence: VersionEvidence = VersionEvidence.DECLARED


@dataclass(frozen=True)
class RuntimeDiscoveryIssue:
    runtime_id: str
    code: RuntimeIssueCode


@dataclass(frozen=True)
class RuntimeDiscoveryReport:
    found: tuple[RuntimeDescriptor, ...]
    issues: tuple[RuntimeDiscoveryIssue, ...]


class RuntimeRegistry:
    """In-memory registry populated only from exact caller-supplied paths."""

    def __init__(self) -> None:
        self._items: dict[str, RuntimeDescriptor] = {}

    def discover_explicit(
        self, candidates: Iterable[RuntimeCandidateSpec]
    ) -> RuntimeDiscoveryReport:
        found: list[RuntimeDescriptor] = []
        issues: list[RuntimeDiscoveryIssue] = []
        for candidate in candidates:
            if candidate.runtime_id in self._items:
                issues.append(
                    RuntimeDiscoveryIssue(
                        candidate.runtime_id, RuntimeIssueCode.DUPLICATE_RUNTIME_ID
                    )
                )
                continue
            issue = _inspect_exact_file(candidate.executable_path)
            if issue is not None:
                issues.append(RuntimeDiscoveryIssue(candidate.runtime_id, issue))
                continue
            descriptor = RuntimeDescriptor(
                runtime_id=candidate.runtime_id,
                family=candidate.family,
                process_model=(
                    _process_model(candidate.family)
                    if candidate.process_model is None
                    else candidate.process_model
                ),
                executable_path=candidate.executable_path,
                scope=candidate.scope,
                version=candidate.version,
                version_evidence=candidate.version_evidence,
            )
            self._items[descriptor.runtime_id] = descriptor
            found.append(descriptor)
        return RuntimeDiscoveryReport(tuple(found), tuple(issues))

    def get(self, runtime_id: str) -> RuntimeDescriptor:
        try:
            return self._items[runtime_id]
        except KeyError as exc:
            raise ValueError("Runtime is not present in the explicit registry.") from exc

    def entries(self) -> tuple[RuntimeDescriptor, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


@dataclass(frozen=True)
class PackageRequirement:
    requirement_id: str
    package_id: str
    kind: PackageKind
    version_constraint: str = ""
    allowed_scopes: tuple[CandidateScope, ...] = (
        CandidateScope.APP_BUNDLED,
        CandidateScope.PROJECT_LOCAL,
        CandidateScope.EXPLICIT_SYSTEM,
    )
    source: RequirementSource = RequirementSource.USER_DECLARED

    def __post_init__(self) -> None:
        _validate_identifier(self.requirement_id, "requirement_id")
        object.__setattr__(self, "package_id", _normalize_package_id(self.package_id))
        object.__setattr__(self, "kind", PackageKind(self.kind))
        object.__setattr__(self, "source", RequirementSource(self.source))
        _validate_text(self.version_constraint, "version_constraint", 256, allow_empty=True)
        scopes = tuple(CandidateScope(scope) for scope in self.allowed_scopes)
        if not scopes or len(set(scopes)) != len(scopes):
            raise ValueError("allowed_scopes must be non-empty and unique.")
        object.__setattr__(self, "allowed_scopes", scopes)


@dataclass(frozen=True)
class PackageCandidate:
    candidate_id: str
    package_id: str
    kind: PackageKind
    version: str | None
    scope: CandidateScope
    location: Path
    version_evidence: VersionEvidence = VersionEvidence.DECLARED
    dependencies: tuple[PackageRequirement, ...] = ()
    license_id: str | None = None
    provenance_id: str | None = None
    presence: CandidatePresence = field(default=CandidatePresence.DECLARED, init=False)

    def __post_init__(self) -> None:
        _validate_identifier(self.candidate_id, "candidate_id")
        object.__setattr__(self, "package_id", _normalize_package_id(self.package_id))
        object.__setattr__(self, "kind", PackageKind(self.kind))
        object.__setattr__(self, "scope", CandidateScope(self.scope))
        object.__setattr__(self, "version_evidence", VersionEvidence(self.version_evidence))
        path = Path(self.location)
        _validate_explicit_path(path, "location")
        object.__setattr__(self, "location", path)
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        if self.version is not None:
            _validate_text(self.version, "version", 128)
        if self.license_id is not None:
            _validate_text(self.license_id, "license_id", 128)
        if self.provenance_id is not None:
            _validate_text(self.provenance_id, "provenance_id", 256)


class PackageLocationIssueCode(str, Enum):
    MISSING_PATH = "missing_path"
    UNREADABLE_PATH = "unreadable_path"
    REPARSE_POINT = "reparse_point_rejected"
    NOT_PACKAGE_LOCATION = "not_file_or_directory"


@dataclass(frozen=True)
class PackageLocationIssue:
    candidate_id: str
    code: PackageLocationIssueCode


@dataclass(frozen=True)
class PackageLocationReport:
    present: tuple[PackageCandidate, ...]
    issues: tuple[PackageLocationIssue, ...]


def inspect_explicit_package_locations(
    candidates: Iterable[PackageCandidate],
) -> PackageLocationReport:
    """Check only supplied package paths; never walks roots or queries PATH."""
    present: list[PackageCandidate] = []
    issues: list[PackageLocationIssue] = []
    for candidate in candidates:
        try:
            info = candidate.location.lstat()
        except FileNotFoundError:
            issues.append(
                PackageLocationIssue(candidate.candidate_id, PackageLocationIssueCode.MISSING_PATH)
            )
            continue
        except OSError:
            issues.append(
                PackageLocationIssue(candidate.candidate_id, PackageLocationIssueCode.UNREADABLE_PATH)
            )
            continue
        if stat.S_ISLNK(info.st_mode) or _has_reparse_attribute(info):
            issues.append(
                PackageLocationIssue(candidate.candidate_id, PackageLocationIssueCode.REPARSE_POINT)
            )
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            issues.append(
                PackageLocationIssue(
                    candidate.candidate_id,
                    PackageLocationIssueCode.NOT_PACKAGE_LOCATION,
                )
            )
            continue
        observed = replace(candidate)
        object.__setattr__(observed, "presence", CandidatePresence.PRESENT)
        present.append(observed)
    return PackageLocationReport(tuple(present), tuple(issues))


@dataclass(frozen=True)
class ResolutionPolicy:
    scope_preference: tuple[CandidateScope, ...] = (
        CandidateScope.APP_BUNDLED,
        CandidateScope.PROJECT_LOCAL,
        CandidateScope.EXPLICIT_SYSTEM,
    )

    def __post_init__(self) -> None:
        scopes = tuple(CandidateScope(scope) for scope in self.scope_preference)
        if not scopes or len(set(scopes)) != len(scopes):
            raise ValueError("scope_preference must be non-empty and unique.")
        object.__setattr__(self, "scope_preference", scopes)


@dataclass(frozen=True)
class ResolutionIssue:
    package_id: str
    code: ResolutionIssueCode
    requirement_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionResult:
    selected: tuple[PackageCandidate, ...]
    issues: tuple[ResolutionIssue, ...]

    @property
    def satisfied(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class _PendingRequirement:
    requirement: PackageRequirement
    ancestors: tuple[str, ...] = ()


class PackageRequirementResolver:
    """Resolve normalized declarations against supplied candidate records only."""

    def __init__(self, policy: ResolutionPolicy | None = None) -> None:
        self.policy = policy or ResolutionPolicy()

    def resolve(
        self,
        requirements: Iterable[PackageRequirement],
        candidates: Iterable[PackageCandidate],
    ) -> ResolutionResult:
        available = tuple(candidates)
        pending = [_PendingRequirement(item) for item in requirements]
        selected: dict[str, PackageCandidate] = {}
        issues: list[ResolutionIssue] = []
        issue_keys: set[tuple[str, str, tuple[str, ...]]] = set()

        def add_issue(
            package_id: str,
            code: ResolutionIssueCode,
            reqs: Iterable[PackageRequirement],
        ) -> None:
            req_ids = tuple(sorted({item.requirement_id for item in reqs}))
            key = (package_id, code.value, req_ids)
            if key not in issue_keys:
                issue_keys.add(key)
                issues.append(ResolutionIssue(package_id, code, req_ids))

        while pending:
            groups: dict[str, list[_PendingRequirement]] = {}
            for item in pending:
                package_id = item.requirement.package_id
                if package_id in item.ancestors:
                    add_issue(
                        package_id,
                        ResolutionIssueCode.DEPENDENCY_CYCLE,
                        (item.requirement,),
                    )
                    continue
                groups.setdefault(package_id, []).append(item)
            pending = []

            for package_id in sorted(groups):
                items = groups[package_id]
                reqs = tuple(item.requirement for item in items)
                existing = selected.get(package_id)
                if existing is not None:
                    verdicts = [_candidate_match(existing, req) for req in reqs]
                    if any(code is not None or not matches for matches, code in verdicts):
                        add_issue(
                            package_id,
                            ResolutionIssueCode.DEPENDENCY_CONFLICT,
                            reqs,
                        )
                    continue

                same_package = [item for item in available if item.package_id == package_id]
                if not same_package:
                    add_issue(package_id, ResolutionIssueCode.MISSING_CANDIDATE, reqs)
                    continue
                observed = [
                    item for item in same_package if item.presence is CandidatePresence.PRESENT
                ]
                if not observed:
                    add_issue(
                        package_id,
                        ResolutionIssueCode.CANDIDATE_LOCATION_NOT_OBSERVED,
                        reqs,
                    )
                    continue
                same_kind = [
                    item for item in observed if all(item.kind is req.kind for req in reqs)
                ]
                if not same_kind:
                    add_issue(package_id, ResolutionIssueCode.KIND_MISMATCH, reqs)
                    continue
                allowed = set(self.policy.scope_preference)
                for req in reqs:
                    allowed.intersection_update(req.allowed_scopes)
                if not allowed:
                    add_issue(package_id, ResolutionIssueCode.SCOPE_NOT_ALLOWED, reqs)
                    continue
                scoped = [item for item in same_kind if item.scope in allowed]
                if not scoped:
                    add_issue(package_id, ResolutionIssueCode.SCOPE_NOT_ALLOWED, reqs)
                    continue

                unsupported_constraint = False
                unknown_version = False
                compatible: list[PackageCandidate] = []
                for candidate in scoped:
                    checks = [_candidate_match(candidate, req) for req in reqs]
                    if any(code is ResolutionIssueCode.VERSION_CONSTRAINT_UNSUPPORTED for _, code in checks):
                        unsupported_constraint = True
                        continue
                    if any(code is ResolutionIssueCode.VERSION_UNKNOWN for _, code in checks):
                        unknown_version = True
                        continue
                    if all(matches for matches, _ in checks):
                        compatible.append(candidate)
                if not compatible:
                    if unsupported_constraint:
                        code = ResolutionIssueCode.VERSION_CONSTRAINT_UNSUPPORTED
                    elif unknown_version:
                        code = ResolutionIssueCode.VERSION_UNKNOWN
                    else:
                        code = ResolutionIssueCode.VERSION_INCOMPATIBLE
                    add_issue(package_id, code, reqs)
                    continue

                scope_rank = {scope: index for index, scope in enumerate(self.policy.scope_preference)}
                best_scope_rank = min(scope_rank[item.scope] for item in compatible)
                best_scope = [
                    item for item in compatible if scope_rank[item.scope] == best_scope_rank
                ]
                numeric = [(item, _version_tuple(item.version)) for item in best_scope]
                known_versions = [version for _, version in numeric if version is not None]
                if known_versions:
                    best_version = max(known_versions, key=_version_sort_key)
                    finalists = [item for item, version in numeric if version == best_version]
                else:
                    finalists = best_scope
                finalists = sorted(finalists, key=lambda item: (item.candidate_id, str(item.location).casefold()))
                if len(finalists) != 1:
                    add_issue(package_id, ResolutionIssueCode.AMBIGUOUS_CANDIDATE, reqs)
                    continue

                chosen = finalists[0]
                selected[package_id] = chosen
                for item in items:
                    next_ancestors = item.ancestors + (package_id,)
                    for dependency in chosen.dependencies:
                        pending.append(_PendingRequirement(dependency, next_ancestors))

        return ResolutionResult(
            selected=tuple(selected[key] for key in sorted(selected)),
            issues=tuple(issues),
        )


@dataclass(frozen=True)
class ToolchainManifest:
    schema_version: int
    requirements: tuple[PackageRequirement, ...]

    @classmethod
    def from_json(cls, text: str) -> ToolchainManifest:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest is missing or exceeds the size limit.")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError("Manifest JSON is invalid.") from exc
        if not isinstance(data, dict) or set(data) != {"schema_version", "requirements"}:
            raise ValueError("Manifest must contain only schema_version and requirements.")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported manifest schema version.")
        raw_requirements = data["requirements"]
        if not isinstance(raw_requirements, list):
            raise TypeError("Manifest requirements must be a JSON array.")
        requirements: list[PackageRequirement] = []
        for raw in raw_requirements:
            if not isinstance(raw, dict):
                raise TypeError("Each manifest requirement must be a JSON object.")
            allowed_keys = {"id", "package_id", "kind", "version", "scopes"}
            if set(raw) - allowed_keys or not {"id", "package_id", "kind"}.issubset(raw):
                raise ValueError("Manifest requirement fields are unsupported or incomplete.")
            scope_values = raw.get("scopes", [scope.value for scope in CandidateScope])
            if not isinstance(scope_values, list) or not all(isinstance(x, str) for x in scope_values):
                raise ValueError("Manifest scopes must be a string array.")
            version = raw.get("version", "")
            if not isinstance(version, str):
                raise TypeError("Manifest version constraints must be strings.")
            requirements.append(
                PackageRequirement(
                    requirement_id=raw["id"],
                    package_id=raw["package_id"],
                    kind=PackageKind(raw["kind"]),
                    version_constraint=version,
                    allowed_scopes=tuple(CandidateScope(value) for value in scope_values),
                    source=RequirementSource.PROJECT_MANIFEST,
                )
            )
        return cls(schema_version=1, requirements=tuple(requirements))


def read_toolchain_manifest(explicit_path: Path) -> ToolchainManifest:
    """Read one explicitly selected JSON manifest; no directory walking occurs."""
    path = Path(explicit_path)
    _validate_explicit_path(path, "manifest_path")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Explicit manifest path does not exist.") from exc
    except OSError as exc:
        raise ValueError("Explicit manifest path could not be inspected.") from exc
    if stat.S_ISLNK(info.st_mode) or _has_reparse_attribute(info):
        raise ValueError("Manifest symlinks and reparse points are rejected.")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Manifest path must be a regular file.")
    if info.st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds the size limit.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Explicit manifest could not be read as UTF-8.") from exc
    return ToolchainManifest.from_json(text)


@dataclass(frozen=True)
class PackageRecipe:
    """Declarative future-install metadata; it contains no installer behavior."""

    package_id: str
    pinned_version: str
    source_url: str
    official_source_id: str
    sha256: str
    license_id: str
    dependencies: tuple[PackageRequirement, ...] = ()
    install_scope: InstallScope = InstallScope.APP_MANAGED
    requires_admin: bool = False
    mutates_global_path: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "package_id", _normalize_package_id(self.package_id))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "install_scope", InstallScope(self.install_scope))
        _validate_text(self.pinned_version, "pinned_version", 128)
        _validate_text(self.source_url, "source_url", 2048)
        _validate_text(self.official_source_id, "official_source_id", 256, allow_empty=True)
        _validate_text(self.sha256, "sha256", 128)
        _validate_text(self.license_id, "license_id", 128, allow_empty=True)


@dataclass(frozen=True)
class RecipeValidation:
    valid: bool
    issues: tuple[str, ...]


def validate_package_recipe(recipe: PackageRecipe) -> RecipeValidation:
    """Validate required metadata and deny global/system-install policies."""
    issues: list[str] = []
    if _version_tuple(recipe.pinned_version) is None:
        issues.append("version_not_pinned_numeric")
    parsed = urlparse(recipe.source_url)
    if parsed.scheme != "https" or not parsed.netloc:
        issues.append("official_https_source_required")
    if not recipe.official_source_id.strip():
        issues.append("official_source_id_required")
    if _SHA256.fullmatch(recipe.sha256) is None:
        issues.append("sha256_digest_required")
    if not recipe.license_id.strip():
        issues.append("license_id_required")
    if recipe.install_scope is not InstallScope.APP_MANAGED:
        issues.append("app_managed_install_root_required")
    if recipe.requires_admin:
        issues.append("administrator_install_forbidden")
    if recipe.mutates_global_path:
        issues.append("global_path_mutation_forbidden")
    return RecipeValidation(not issues, tuple(issues))


class PackageInstallerPort(Protocol):
    """Future-only interface; no implementation is registered in this slice."""

    def prepare_review(self, recipe: PackageRecipe, app_managed_root: Path) -> object:
        """Return a source/version/license/size and disk-preflight review."""
        ...

    def install_after_approval(
        self,
        recipe: PackageRecipe,
        review: object,
        approval: object,
        *,
        progress: object,
        cancellation: object,
    ) -> PackageCandidate:
        """Future implementations must verify, cancel, clean up, and roll back."""
        ...


@dataclass(frozen=True)
class ResourceDemand:
    workload_id: str
    runtime_id: str
    peak_memory_mib: int
    cpu_threads: int = 1
    min_cpu_threads: int = 1

    def __post_init__(self) -> None:
        _validate_identifier(self.workload_id, "workload_id")
        _validate_identifier(self.runtime_id, "runtime_id")
        for name in ("peak_memory_mib", "cpu_threads", "min_cpu_threads"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name != "peak_memory_mib" else 0):
                raise ValueError(f"{name} is outside its valid range.")
        if self.min_cpu_threads > self.cpu_threads:
            raise ValueError("min_cpu_threads cannot exceed cpu_threads.")

    def to_workload_request(self) -> WorkloadRequest:
        """Map declared host/CPU demand to the existing CPU-only planner contract."""
        return WorkloadRequest(
            workload_id=self.workload_id,
            model_id=f"tool:{self.runtime_id}",
            backend_preferences=(Backend.CPU,),
            estimated_weight_mib=self.peak_memory_mib,
            kv_cache_mib=0,
            backend_overhead_mib=0,
            cpu_threads=self.cpu_threads,
            min_cpu_threads=self.min_cpu_threads,
            allow_cpu_thread_reduction=True,
        )


def plan_resource_demand(
    demand: ResourceDemand,
    snapshot: ResourceSnapshot,
    planner: ResourceOrchestrator,
    *,
    now_monotonic_s: float | None = None,
) -> ResourcePlan:
    """Delegate admission planning only; this does not reserve or run a process."""
    return planner.plan(
        demand.to_workload_request(), snapshot, now_monotonic_s=now_monotonic_s
    )


@dataclass(frozen=True)
class OneShotInvocation:
    invocation_id: str
    runtime_id: str
    operation: InvocationOperation
    argv: tuple[str, ...]
    project_root: Path
    working_directory: Path
    timeout_s: float
    environment_profile_id: str
    resource_demand: ResourceDemand
    write_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.invocation_id, "invocation_id")
        _validate_identifier(self.runtime_id, "runtime_id")
        object.__setattr__(self, "operation", InvocationOperation(self.operation))
        if isinstance(self.argv, str):
            raise TypeError("argv must be an immutable sequence, not a command string.")
        argv = tuple(self.argv)
        if not argv or not all(isinstance(item, str) and "\x00" not in item for item in argv):
            raise ValueError("argv must contain at least one NUL-free string argument.")
        object.__setattr__(self, "argv", argv)
        root = Path(self.project_root)
        cwd = Path(self.working_directory)
        _validate_explicit_path(root, "project_root")
        _validate_explicit_path(cwd, "working_directory")
        if not _is_lexically_within(cwd, root):
            raise ValueError("working_directory must stay within project_root.")
        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "working_directory", cwd)
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive.")
        _validate_identifier(self.environment_profile_id, "environment_profile_id")
        if self.resource_demand.runtime_id != self.runtime_id:
            raise ValueError("Resource demand runtime_id must match the invocation.")
        scope = tuple(self.write_scope)
        for item in scope:
            _validate_relative_scope(item)
        if self.operation is InvocationOperation.FIX and not scope:
            raise ValueError("Fix invocations require a non-empty explicit write_scope.")
        if self.operation is not InvocationOperation.FIX and scope:
            raise ValueError("write_scope is reserved for explicit fix invocations.")
        object.__setattr__(self, "write_scope", scope)


@dataclass(frozen=True)
class OneShotInvocationPlan:
    invocation_id: str
    runtime_id: str
    executable_path: Path
    operation: InvocationOperation
    argv: tuple[str, ...]
    project_root: Path
    working_directory: Path
    timeout_s: float
    environment_profile_id: str
    resource_demand: ResourceDemand


@dataclass(frozen=True)
class PersistentSessionRequest:
    session_id: str
    runtime_id: str
    project_root: Path
    working_directory: Path
    argv: tuple[str, ...]
    environment_profile_id: str
    initialization_timeout_s: float
    resource_demand: ResourceDemand
    max_message_bytes: int = 4 * 1024 * 1024
    idle_timeout_s: float | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.session_id, "session_id")
        _validate_identifier(self.runtime_id, "runtime_id")
        root = Path(self.project_root)
        cwd = Path(self.working_directory)
        _validate_explicit_path(root, "project_root")
        _validate_explicit_path(cwd, "working_directory")
        if not _is_lexically_within(cwd, root):
            raise ValueError("working_directory must stay within project_root.")
        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "working_directory", cwd)
        if isinstance(self.argv, str):
            raise TypeError("argv must be an immutable sequence, not a command string.")
        argv = tuple(self.argv)
        if not all(isinstance(item, str) and "\x00" not in item for item in argv):
            raise ValueError("argv entries must be NUL-free strings.")
        object.__setattr__(self, "argv", argv)
        _validate_identifier(self.environment_profile_id, "environment_profile_id")
        if not math.isfinite(self.initialization_timeout_s) or self.initialization_timeout_s <= 0:
            raise ValueError("initialization_timeout_s must be finite and positive.")
        if type(self.max_message_bytes) is not int or not 1 <= self.max_message_bytes <= _MAX_LSP_MESSAGE_BYTES:
            raise ValueError("max_message_bytes is outside the supported bound.")
        if self.idle_timeout_s is not None and (
            not math.isfinite(self.idle_timeout_s) or self.idle_timeout_s <= 0
        ):
            raise ValueError("idle_timeout_s must be finite and positive when set.")
        if self.resource_demand.runtime_id != self.runtime_id:
            raise ValueError("Resource demand runtime_id must match the session.")


@dataclass(frozen=True)
class PersistentSessionPlan:
    session_id: str
    runtime_id: str
    executable_path: Path
    protocol: SessionProtocol
    argv: tuple[str, ...]
    project_root: Path
    working_directory: Path
    environment_profile_id: str
    initialization_timeout_s: float
    resource_demand: ResourceDemand
    max_message_bytes: int
    idle_timeout_s: float | None


@dataclass(frozen=True)
class OneShotOutcome:
    invocation_id: str
    state: OneShotState
    exit_code: int | None = None


@dataclass(frozen=True)
class PersistentSessionHandle:
    session_id: str
    runtime_id: str
    state: SessionState


class OneShotExecutionPort(Protocol):
    """Future execution seam; this codebase has no concrete process runner."""

    def execute(self, plan: OneShotInvocationPlan) -> OneShotOutcome:
        ...


class PersistentSessionPort(Protocol):
    """Future process-supervision seam for LSP/debugger sessions."""

    def start(self, plan: PersistentSessionPlan) -> PersistentSessionHandle:
        ...


class TypedRuntimeInvoker:
    """Route typed plans to injected ports; never calls subprocess itself."""

    def __init__(
        self,
        registry: RuntimeRegistry,
        one_shot_port: OneShotExecutionPort,
        session_port: PersistentSessionPort,
    ) -> None:
        self._registry = registry
        self._one_shot_port = one_shot_port
        self._session_port = session_port

    def invoke(
        self, request: OneShotInvocation | PersistentSessionRequest
    ) -> OneShotOutcome | PersistentSessionHandle:
        if isinstance(request, OneShotInvocation):
            plan = plan_one_shot_invocation(self._registry, request)
            result = self._one_shot_port.execute(plan)
            if result.invocation_id != request.invocation_id:
                raise ValueError("One-shot port returned a mismatched invocation_id.")
            return result
        if isinstance(request, PersistentSessionRequest):
            plan = plan_persistent_session(self._registry, request)
            result = self._session_port.start(plan)
            if result.session_id != request.session_id or result.runtime_id != request.runtime_id:
                raise ValueError("Session port returned mismatched identity.")
            return result
        raise TypeError("Only typed one-shot invocations or persistent session requests are accepted.")


def plan_one_shot_invocation(
    registry: RuntimeRegistry, request: OneShotInvocation
) -> OneShotInvocationPlan:
    runtime = registry.get(request.runtime_id)
    if runtime.process_model is not ProcessModel.ONE_SHOT:
        raise ValueError("Persistent runtimes cannot be invoked as one-shot tools.")
    allowed_families = {
        InvocationOperation.CHECK: {RuntimeFamily.LINTER, RuntimeFamily.FORMATTER},
        InvocationOperation.FIX: {RuntimeFamily.LINTER, RuntimeFamily.FORMATTER},
        InvocationOperation.COMPILE: {RuntimeFamily.COMPILER},
        InvocationOperation.BUILD: {RuntimeFamily.BUILD_SYSTEM},
        InvocationOperation.TEST: {RuntimeFamily.TEST_RUNNER},
        InvocationOperation.GENERIC: {
            RuntimeFamily.COMPILER,
            RuntimeFamily.BUILD_SYSTEM,
            RuntimeFamily.LINTER,
            RuntimeFamily.FORMATTER,
            RuntimeFamily.TEST_RUNNER,
            RuntimeFamily.LANGUAGE_RUNTIME,
            RuntimeFamily.PACKAGE_MANAGER,
        },
    }
    if runtime.family not in allowed_families[request.operation]:
        raise ValueError("Invocation operation does not match the registered runtime family.")
    return OneShotInvocationPlan(
        invocation_id=request.invocation_id,
        runtime_id=request.runtime_id,
        executable_path=runtime.executable_path,
        operation=request.operation,
        argv=request.argv,
        project_root=request.project_root,
        working_directory=request.working_directory,
        timeout_s=request.timeout_s,
        environment_profile_id=request.environment_profile_id,
        resource_demand=request.resource_demand,
    )


def plan_persistent_session(
    registry: RuntimeRegistry, request: PersistentSessionRequest
) -> PersistentSessionPlan:
    runtime = registry.get(request.runtime_id)
    if runtime.process_model is not ProcessModel.PERSISTENT:
        raise ValueError("One-shot runtimes cannot be started as persistent sessions.")
    protocol_by_family = {
        RuntimeFamily.LANGUAGE_SERVER: SessionProtocol.LSP_JSON_RPC_STDIO,
        RuntimeFamily.DEBUGGER: SessionProtocol.DEBUG_ADAPTER_STDIO,
    }
    if runtime.family not in {
        RuntimeFamily.LANGUAGE_SERVER,
        RuntimeFamily.DEBUGGER,
        RuntimeFamily.DEVELOPMENT_SERVER,
        RuntimeFamily.LANGUAGE_RUNTIME,
    }:
        raise ValueError("Registered runtime family has no persistent protocol.")
    protocol = protocol_by_family.get(runtime.family, SessionProtocol.MANAGED_STDIO)
    return PersistentSessionPlan(
        session_id=request.session_id,
        runtime_id=request.runtime_id,
        executable_path=runtime.executable_path,
        protocol=protocol,
        argv=request.argv,
        project_root=request.project_root,
        working_directory=request.working_directory,
        environment_profile_id=request.environment_profile_id,
        initialization_timeout_s=request.initialization_timeout_s,
        resource_demand=request.resource_demand,
        max_message_bytes=request.max_message_bytes,
        idle_timeout_s=request.idle_timeout_s,
    )


def _process_model(family: RuntimeFamily) -> ProcessModel:
    if family in (
        RuntimeFamily.DEBUGGER,
        RuntimeFamily.LANGUAGE_SERVER,
        RuntimeFamily.DEVELOPMENT_SERVER,
    ):
        return ProcessModel.PERSISTENT
    return ProcessModel.ONE_SHOT


def _candidate_match(
    candidate: PackageCandidate, requirement: PackageRequirement
) -> tuple[bool, ResolutionIssueCode | None]:
    constraint = requirement.version_constraint.strip()
    if not constraint or constraint == "*":
        return True, None
    if candidate.version is None:
        return False, ResolutionIssueCode.VERSION_UNKNOWN
    candidate_version = _version_tuple(candidate.version)
    if candidate_version is None:
        return False, ResolutionIssueCode.VERSION_UNKNOWN
    clauses = [item.strip() for item in constraint.split(",")]
    if not clauses or any(not item for item in clauses):
        return False, ResolutionIssueCode.VERSION_CONSTRAINT_UNSUPPORTED
    for clause in clauses:
        match = _VERSION_CLAUSE.fullmatch(clause)
        if match is None:
            return False, ResolutionIssueCode.VERSION_CONSTRAINT_UNSUPPORTED
        operator, raw_required = match.groups()
        required = _version_tuple(raw_required)
        if required is None:
            return False, ResolutionIssueCode.VERSION_CONSTRAINT_UNSUPPORTED
        comparison = _compare_versions(candidate_version, required)
        valid = {
            "==": comparison == 0,
            "!=": comparison != 0,
            ">=": comparison >= 0,
            "<=": comparison <= 0,
            ">": comparison > 0,
            "<": comparison < 0,
        }[operator]
        if not valid:
            return False, ResolutionIssueCode.VERSION_INCOMPATIBLE
    return True, None


def _version_tuple(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    match = _VERSION.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    left_padded = left + (0,) * (width - len(left))
    right_padded = right + (0,) * (width - len(right))
    return (left_padded > right_padded) - (left_padded < right_padded)


def _version_sort_key(value: tuple[int, ...]) -> tuple[int, ...]:
    return value + (0,) * max(0, 8 - len(value))


def _inspect_exact_file(path: Path) -> RuntimeIssueCode | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return RuntimeIssueCode.MISSING_PATH
    except OSError:
        return RuntimeIssueCode.UNREADABLE_PATH
    if stat.S_ISLNK(info.st_mode) or _has_reparse_attribute(info):
        return RuntimeIssueCode.REPARSE_POINT
    if not stat.S_ISREG(info.st_mode):
        return RuntimeIssueCode.NOT_REGULAR_FILE
    return None


def _has_reparse_attribute(info: os.stat_result) -> bool:
    # FILE_ATTRIBUTE_REPARSE_POINT (0x400) covers Windows symlinks/junctions.
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _validate_explicit_path(path: Path, field_name: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute, explicitly supplied path.")
    if ".." in path.parts:
        raise ValueError(f"{field_name} must not contain parent traversal.")


def _is_lexically_within(child: Path, parent: Path) -> bool:
    try:
        child.absolute().relative_to(parent.absolute())
    except ValueError:
        return False
    return True


def _validate_relative_scope(value: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("write_scope entries must be non-empty relative paths.")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("write_scope entries must not escape the project root.")


def _normalize_package_id(value: str) -> str:
    _validate_text(value, "package_id", 128)
    if _PACKAGE_ID.fullmatch(value) is None:
        raise ValueError("package_id contains unsupported characters.")
    return value.casefold()


def _validate_identifier(value: str, field_name: str) -> None:
    _validate_text(value, field_name, 128)
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} contains unsupported characters.")


def _validate_text(
    value: str, field_name: str, max_length: int, *, allow_empty: bool = False
) -> None:
    if not isinstance(value, str) or len(value) > max_length or "\x00" in value:
        raise ValueError(f"{field_name} must be bounded NUL-free text.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty.")


@dataclass(frozen=True)
class StackComponentSpec:
    """Supported/planned component declaration, never an installation claim."""

    component_id: str
    package_id: str
    kind: PackageKind
    role: str
    runtime_family: RuntimeFamily | None
    process_model: ProcessModel | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.component_id, "component_id")
        object.__setattr__(self, "package_id", _normalize_package_id(self.package_id))
        object.__setattr__(self, "kind", PackageKind(self.kind))
        _validate_text(self.role, "role", 128)
        if self.runtime_family is None:
            if self.process_model is not None:
                raise ValueError("A process model requires a runtime family.")
            return
        family = RuntimeFamily(self.runtime_family)
        object.__setattr__(self, "runtime_family", family)
        model = _process_model(family) if self.process_model is None else ProcessModel(self.process_model)
        if family in (RuntimeFamily.DEBUGGER, RuntimeFamily.LANGUAGE_SERVER) and model is not ProcessModel.PERSISTENT:
            raise ValueError("Debuggers and language servers must be persistent components.")
        object.__setattr__(self, "process_model", model)


@dataclass(frozen=True)
class StackProfile:
    stack_id: str
    display_name: str
    components: tuple[StackComponentSpec, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.stack_id, "stack_id")
        _validate_text(self.display_name, "display_name", 128)
        components = tuple(self.components)
        ids = [item.component_id for item in components]
        if len(ids) != len(set(ids)):
            raise ValueError("Stack component_id values must be unique within a profile.")
        object.__setattr__(self, "components", components)


def _stack_component(
    component_id: str,
    package_id: str,
    kind: PackageKind,
    role: str,
    family: RuntimeFamily | None = None,
    process_model: ProcessModel | None = None,
) -> StackComponentSpec:
    return StackComponentSpec(
        component_id, package_id, kind, role, family, process_model
    )


STACK_CATALOG: tuple[StackProfile, ...] = (
    StackProfile(
        "python",
        "Python",
        (
            _stack_component("python", "python", PackageKind.RUNTIME, "interpreter", RuntimeFamily.LANGUAGE_RUNTIME),
            _stack_component("venv", "python-venv", PackageKind.RUNTIME, "virtual environment"),
            _stack_component("pip", "pip", PackageKind.PACKAGE_MANAGER, "package manager", RuntimeFamily.PACKAGE_MANAGER),
            _stack_component("pytest", "pytest", PackageKind.TEST_RUNNER, "tests", RuntimeFamily.TEST_RUNNER),
            _stack_component("ruff-check", "ruff", PackageKind.LINTER, "lint", RuntimeFamily.LINTER),
            _stack_component("ruff-format", "ruff", PackageKind.FORMATTER, "format", RuntimeFamily.FORMATTER),
            _stack_component("pyright", "pyright", PackageKind.LANGUAGE_SERVER, "LSP", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("python-lsp", "python-lsp-server", PackageKind.LANGUAGE_SERVER, "LSP alternative", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("debugpy", "debugpy", PackageKind.DEBUGGER, "debug adapter", RuntimeFamily.DEBUGGER),
            _stack_component("pyside6", "pyside6", PackageKind.FRAMEWORK, "Qt framework"),
        ),
    ),
    StackProfile(
        "java-kotlin",
        "Java / Kotlin",
        (
            _stack_component("jdk", "jdk", PackageKind.SDK, "JDK/JRE and javac", RuntimeFamily.LANGUAGE_RUNTIME),
            _stack_component("kotlin-compiler", "kotlinc", PackageKind.COMPILER, "Kotlin compiler", RuntimeFamily.COMPILER),
            _stack_component("gradle", "gradle", PackageKind.BUILD_SYSTEM, "build", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("maven", "maven", PackageKind.BUILD_SYSTEM, "build alternative", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("junit", "junit", PackageKind.TEST_RUNNER, "tests", RuntimeFamily.TEST_RUNNER),
            _stack_component("detekt", "detekt", PackageKind.LINTER, "Kotlin lint", RuntimeFamily.LINTER),
            _stack_component("ktlint", "ktlint", PackageKind.FORMATTER, "Kotlin format", RuntimeFamily.FORMATTER),
            _stack_component("jdtls", "jdtls", PackageKind.LANGUAGE_SERVER, "Java LSP", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("kotlin-lsp", "kotlin-language-server", PackageKind.LANGUAGE_SERVER, "Kotlin LSP", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("jdb", "jdb", PackageKind.DEBUGGER, "Java debugger", RuntimeFamily.DEBUGGER),
        ),
    ),
    StackProfile(
        "rust",
        "Rust",
        (
            _stack_component("rustup", "rustup", PackageKind.PACKAGE_MANAGER, "toolchain manager", RuntimeFamily.PACKAGE_MANAGER),
            _stack_component("rustc", "rustc", PackageKind.COMPILER, "compiler", RuntimeFamily.COMPILER),
            _stack_component("cargo-build", "cargo", PackageKind.BUILD_SYSTEM, "build", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("cargo-test", "cargo", PackageKind.TEST_RUNNER, "tests", RuntimeFamily.TEST_RUNNER),
            _stack_component("rustfmt", "rustfmt", PackageKind.FORMATTER, "format", RuntimeFamily.FORMATTER),
            _stack_component("clippy", "clippy", PackageKind.LINTER, "lint", RuntimeFamily.LINTER),
            _stack_component("rust-analyzer", "rust-analyzer", PackageKind.LANGUAGE_SERVER, "LSP", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("rust-gdb", "rust-gdb", PackageKind.DEBUGGER, "debug adapter", RuntimeFamily.DEBUGGER),
        ),
    ),
    StackProfile(
        "javascript-typescript-electron",
        "JavaScript / TypeScript / Vite / Electron",
        (
            _stack_component("node", "nodejs", PackageKind.RUNTIME, "Node.js runtime", RuntimeFamily.LANGUAGE_RUNTIME),
            _stack_component("npm", "npm", PackageKind.PACKAGE_MANAGER, "package manager", RuntimeFamily.PACKAGE_MANAGER),
            _stack_component("pnpm", "pnpm", PackageKind.PACKAGE_MANAGER, "package manager alternative", RuntimeFamily.PACKAGE_MANAGER),
            _stack_component("yarn", "yarn", PackageKind.PACKAGE_MANAGER, "package manager alternative", RuntimeFamily.PACKAGE_MANAGER),
            _stack_component("typescript", "typescript", PackageKind.COMPILER, "TypeScript compiler", RuntimeFamily.COMPILER),
            _stack_component("tsserver", "typescript", PackageKind.LANGUAGE_SERVER, "TypeScript LSP", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("vite-build", "vite", PackageKind.BUILD_SYSTEM, "production build", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("vite-dev", "vite", PackageKind.BUILD_SYSTEM, "development server", RuntimeFamily.DEVELOPMENT_SERVER),
            _stack_component("vitest", "vitest", PackageKind.TEST_RUNNER, "tests", RuntimeFamily.TEST_RUNNER),
            _stack_component("eslint", "eslint", PackageKind.LINTER, "lint", RuntimeFamily.LINTER),
            _stack_component("prettier", "prettier", PackageKind.FORMATTER, "format", RuntimeFamily.FORMATTER),
            _stack_component("electron-runtime", "electron", PackageKind.RUNTIME, "desktop runtime", RuntimeFamily.LANGUAGE_RUNTIME),
            _stack_component("electron-builder", "electron-builder", PackageKind.BUILD_SYSTEM, "packaging tool", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("electron-forge", "electron-forge", PackageKind.BUILD_SYSTEM, "packaging alternative", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("node-debugger", "node-inspector", PackageKind.DEBUGGER, "Node debug adapter", RuntimeFamily.DEBUGGER),
        ),
    ),
    StackProfile(
        "android",
        "Android / SDK / NDK",
        (
            _stack_component("jdk", "jdk", PackageKind.SDK, "required JDK/JRE", RuntimeFamily.LANGUAGE_RUNTIME),
            _stack_component("android-sdk", "android-sdk", PackageKind.SDK, "Android SDK platforms and tools"),
            _stack_component("android-ndk", "android-ndk", PackageKind.NDK, "native compiler/toolchain", RuntimeFamily.COMPILER),
            _stack_component("gradle", "gradle", PackageKind.BUILD_SYSTEM, "build", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("adb", "android-platform-tools", PackageKind.SDK, "device bridge"),
            _stack_component("kotlin-compiler", "kotlinc", PackageKind.COMPILER, "Kotlin compiler", RuntimeFamily.COMPILER),
            _stack_component("android-tests", "android-test-runner", PackageKind.TEST_RUNNER, "tests", RuntimeFamily.TEST_RUNNER),
            _stack_component("android-debugger", "lldb-android", PackageKind.DEBUGGER, "native debugger", RuntimeFamily.DEBUGGER),
        ),
    ),
    StackProfile(
        "cpp",
        "C / C++",
        (
            _stack_component("clang", "llvm-clang", PackageKind.COMPILER, "compiler", RuntimeFamily.COMPILER),
            _stack_component("cmake", "cmake", PackageKind.BUILD_SYSTEM, "build generator", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("ninja", "ninja", PackageKind.BUILD_SYSTEM, "build executor", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("ctest", "ctest", PackageKind.TEST_RUNNER, "tests", RuntimeFamily.TEST_RUNNER),
            _stack_component("clang-tidy", "clang-tidy", PackageKind.LINTER, "lint", RuntimeFamily.LINTER),
            _stack_component("clang-format", "clang-format", PackageKind.FORMATTER, "format", RuntimeFamily.FORMATTER),
            _stack_component("clangd", "clangd", PackageKind.LANGUAGE_SERVER, "LSP", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("lldb", "lldb", PackageKind.DEBUGGER, "debugger", RuntimeFamily.DEBUGGER),
            _stack_component("qt6", "qt6", PackageKind.FRAMEWORK, "optional GUI framework"),
        ),
    ),
    StackProfile(
        "dotnet",
        ".NET",
        (
            _stack_component("dotnet-sdk", "dotnet-sdk", PackageKind.SDK, "SDK/compiler/runtime", RuntimeFamily.LANGUAGE_RUNTIME),
            _stack_component("dotnet-runtime", "dotnet-runtime", PackageKind.RUNTIME, "runtime", RuntimeFamily.LANGUAGE_RUNTIME),
            _stack_component("msbuild", "msbuild", PackageKind.BUILD_SYSTEM, "build", RuntimeFamily.BUILD_SYSTEM),
            _stack_component("roslyn", "roslyn-csc", PackageKind.COMPILER, "C# compiler", RuntimeFamily.COMPILER),
            _stack_component("vstest", "vstest", PackageKind.TEST_RUNNER, "tests", RuntimeFamily.TEST_RUNNER),
            _stack_component("dotnet-analyzers", "dotnet-analyzers", PackageKind.LINTER, "analysis", RuntimeFamily.LINTER),
            _stack_component("csharp-lsp", "csharp-language-server", PackageKind.LANGUAGE_SERVER, "LSP", RuntimeFamily.LANGUAGE_SERVER),
            _stack_component("vsdbg", "vsdbg", PackageKind.DEBUGGER, "debug adapter", RuntimeFamily.DEBUGGER),
            _stack_component("aspnetcore", "aspnetcore-framework", PackageKind.FRAMEWORK, "optional web framework"),
        ),
    ),
)


def list_stack_profiles() -> tuple[StackProfile, ...]:
    """Return supported/planned stack declarations; this is not installed inventory."""
    return STACK_CATALOG


def get_stack_profile(stack_id: str) -> StackProfile:
    normalized = stack_id.casefold()
    for profile in STACK_CATALOG:
        if profile.stack_id.casefold() == normalized:
            return profile
    raise ValueError("Stack profile is not in the supported/planned catalog.")


_DEPENDENCY_SPEC = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[([A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*)\])?\s*(.*?)\s*$"
)


def read_pyproject_requirements(
    explicit_path: Path,
    *,
    include_optional_groups: Iterable[str] = (),
) -> tuple[PackageRequirement, ...]:
    """Read PEP-621/build-system requirements from one explicit pyproject.toml.

    Supported dependency declarations are direct PEP-508 names and the numeric
    version-constraint subset handled by :class:`PackageRequirementResolver`.
    Environment markers and extras fail closed rather than being silently lost.
    Python 3.10 needs an already-available ``tomli`` module; this function never
    downloads or installs a parser.
    """
    path = Path(explicit_path)
    _validate_explicit_path(path, "pyproject_path")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Explicit pyproject.toml path does not exist.") from exc
    except OSError as exc:
        raise ValueError("Explicit pyproject.toml path could not be inspected.") from exc
    if stat.S_ISLNK(info.st_mode) or _has_reparse_attribute(info):
        raise ValueError("pyproject.toml symlinks and reparse points are rejected.")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("pyproject.toml path must be a regular file.")
    if info.st_size > MAX_MANIFEST_BYTES:
        raise ValueError("pyproject.toml exceeds the size limit.")
    if _toml is None:
        raise RuntimeError("No TOML parser is already available; no install was attempted.")
    try:
        text = path.read_text(encoding="utf-8")
        document = _toml.loads(text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Explicit pyproject.toml could not be read or parsed.") from exc
    if not isinstance(document, Mapping):
        raise TypeError("pyproject.toml root must be a table.")

    requirements: list[PackageRequirement] = []
    build_system = document.get("build-system", {})
    if not isinstance(build_system, Mapping):
        raise TypeError("pyproject.toml build-system must be a table.")
    build_requires = build_system.get("requires", [])
    requirements.extend(
        _parse_dependency_list(
            build_requires,
            requirement_prefix="pyproject:build-system",
            default_kind=PackageKind.BUILD_SYSTEM,
        )
    )

    project = document.get("project")
    if not isinstance(project, Mapping):
        raise TypeError("PEP-621 pyproject.toml must contain a project table.")
    python_constraint = project.get("requires-python")
    if python_constraint is not None:
        if not isinstance(python_constraint, str):
            raise TypeError("project.requires-python must be a string.")
        requirements.append(
            PackageRequirement(
                requirement_id="pyproject:project.requires-python",
                package_id="python",
                kind=PackageKind.RUNTIME,
                version_constraint=python_constraint,
                source=RequirementSource.PROJECT_MANIFEST,
            )
        )
    requirements.extend(
        _parse_dependency_list(
            project.get("dependencies", []),
            requirement_prefix="pyproject:project.dependencies",
            default_kind=PackageKind.LIBRARY,
        )
    )

    selected_groups = tuple(include_optional_groups)
    if len(selected_groups) != len(set(selected_groups)):
        raise ValueError("Optional dependency groups must be unique.")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, Mapping):
        raise TypeError("project.optional-dependencies must be a table.")
    for group in selected_groups:
        _validate_identifier(group, "optional_dependency_group")
        if group not in optional:
            raise ValueError("Requested optional dependency group is not declared.")
        requirements.extend(
            _parse_dependency_list(
                optional[group],
                requirement_prefix=f"pyproject:project.optional-dependencies.{group}",
                default_kind=PackageKind.LIBRARY,
            )
        )
    return tuple(requirements)


def _parse_dependency_list(
    values: object,
    *,
    requirement_prefix: str,
    default_kind: PackageKind,
) -> tuple[PackageRequirement, ...]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("Manifest dependency values must be arrays of strings.")
    parsed: list[PackageRequirement] = []
    for index, value in enumerate(values):
        if ";" in value:
            raise ValueError("Conditional dependency markers are not supported yet.")
        match = _DEPENDENCY_SPEC.fullmatch(value)
        if match is None:
            raise ValueError("Manifest dependency is outside the supported PEP-508 subset.")
        name, extras, constraint = match.groups()
        if extras:
            raise ValueError("PEP-508 extras are not supported by this resolver slice.")
        package_id = _normalize_package_id(name)
        kind = _project_dependency_kind(package_id, default_kind)
        parsed.append(
            PackageRequirement(
                requirement_id=f"{requirement_prefix}.{index}.{package_id}",
                package_id=package_id,
                kind=kind,
                version_constraint=constraint.strip(),
                source=RequirementSource.PROJECT_MANIFEST,
            )
        )
    return tuple(parsed)


def _project_dependency_kind(package_id: str, default_kind: PackageKind) -> PackageKind:
    if package_id in {"pyside6", "pyqt6", "qt6", "aspnetcore-framework"}:
        return PackageKind.FRAMEWORK
    if package_id == "pytest":
        return PackageKind.TEST_RUNNER
    if package_id == "ruff":
        return PackageKind.LINTER
    return default_kind
