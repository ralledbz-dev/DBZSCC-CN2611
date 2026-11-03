from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbzscc.app.resource_orchestrator import (
    Backend,
    BackendCapability,
    BackendTarget,
    PlanStatus,
    ResourceOrchestrator,
    ResourcePolicy,
    ResourceSnapshot,
    TelemetryQuality,
)
from dbzscc.app.toolchain_runtime import (
    STACK_CATALOG,
    CandidatePresence,
    CandidateScope,
    InvocationOperation,
    OneShotInvocation,
    OneShotOutcome,
    OneShotState,
    PackageCandidate,
    PackageKind,
    PackageLocationIssueCode,
    PackageRecipe,
    PackageRequirement,
    PackageRequirementResolver,
    PersistentSessionHandle,
    PersistentSessionRequest,
    ProcessModel,
    RequirementSource,
    ResolutionIssueCode,
    ResourceDemand,
    RuntimeCandidateSpec,
    RuntimeFamily,
    RuntimeIssueCode,
    RuntimeRegistry,
    SessionProtocol,
    SessionState,
    ToolchainManifest,
    TypedRuntimeInvoker,
    VersionEvidence,
    get_stack_profile,
    inspect_explicit_package_locations,
    list_stack_profiles,
    plan_one_shot_invocation,
    plan_persistent_session,
    plan_resource_demand,
    read_pyproject_requirements,
    read_toolchain_manifest,
    validate_package_recipe,
)


def _candidate_path(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture", encoding="utf-8")
    return path


def _candidate(
    root: Path,
    candidate_id: str,
    package_id: str,
    version: str | None,
    *,
    kind: PackageKind = PackageKind.RUNTIME,
    scope: CandidateScope = CandidateScope.PROJECT_LOCAL,
    dependencies: tuple[PackageRequirement, ...] = (),
) -> PackageCandidate:
    candidate = PackageCandidate(
        candidate_id=candidate_id,
        package_id=package_id,
        kind=kind,
        version=version,
        scope=scope,
        location=_candidate_path(root, candidate_id),
        version_evidence=VersionEvidence.OBSERVED,
        dependencies=dependencies,
        license_id="MIT",
        provenance_id="explicit-test-fixture",
    )
    report = inspect_explicit_package_locations((candidate,))
    assert not report.issues
    return report.present[0]


def _requirement(
    requirement_id: str,
    package_id: str,
    version: str = "",
    *,
    kind: PackageKind = PackageKind.RUNTIME,
    source: RequirementSource = RequirementSource.USER_DECLARED,
) -> PackageRequirement:
    return PackageRequirement(
        requirement_id=requirement_id,
        package_id=package_id,
        kind=kind,
        version_constraint=version,
        source=source,
    )


def _resource_snapshot() -> ResourceSnapshot:
    return ResourceSnapshot(
        captured_monotonic_s=100.0,
        telemetry_quality=TelemetryQuality.FRESH,
        available_physical_mib=12_000,
        commit_available_mib=12_000,
        logical_cpu_cores=8,
        cpu_load_ratio=0.1,
        backends=(BackendCapability(Backend.CPU, True, (BackendTarget("cpu", None),)),),
        source="unit-test",
    )


def _runtime_registry(tmp_path: Path) -> RuntimeRegistry:
    registry = RuntimeRegistry()
    specs = (
        RuntimeCandidateSpec(
            "ruff-project",
            RuntimeFamily.LINTER,
            _candidate_path(tmp_path, "venv/Scripts/ruff.exe"),
            CandidateScope.PROJECT_LOCAL,
            version="0.14.0",
            version_evidence=VersionEvidence.OBSERVED,
        ),
        RuntimeCandidateSpec(
            "pyright-lsp",
            RuntimeFamily.LANGUAGE_SERVER,
            _candidate_path(tmp_path, "tools/pyright-langserver.exe"),
            CandidateScope.PROJECT_LOCAL,
            version="1.1.0",
            version_evidence=VersionEvidence.DECLARED,
        ),
        RuntimeCandidateSpec(
            "debugger",
            RuntimeFamily.DEBUGGER,
            _candidate_path(tmp_path, "tools/debug-adapter.exe"),
            CandidateScope.APP_BUNDLED,
        ),
    )
    report = registry.discover_explicit(specs)
    assert len(report.found) == 3
    assert not report.issues
    return registry


def test_discovery_uses_only_exact_paths_and_separates_persistent_runtimes(
    tmp_path: Path,
) -> None:
    registry = RuntimeRegistry()
    executable = _candidate_path(tmp_path, "tools/clang-format.exe")
    missing = tmp_path / "not-installed.exe"
    report = registry.discover_explicit(
        (
            RuntimeCandidateSpec(
                "clang-format",
                RuntimeFamily.FORMATTER,
                executable,
                CandidateScope.PROJECT_LOCAL,
            ),
            RuntimeCandidateSpec(
                "missing-lsp",
                RuntimeFamily.LANGUAGE_SERVER,
                missing,
                CandidateScope.EXPLICIT_SYSTEM,
            ),
        )
    )

    assert [item.runtime_id for item in report.found] == ["clang-format"]
    assert report.found[0].process_model is ProcessModel.ONE_SHOT
    assert len(report.issues) == 1
    assert report.issues[0].runtime_id == "missing-lsp"
    assert report.issues[0].code is RuntimeIssueCode.MISSING_PATH


def test_discovery_rejects_directories_duplicate_ids_and_relative_paths(
    tmp_path: Path,
) -> None:
    registry = RuntimeRegistry()
    directory = tmp_path / "tool-dir"
    directory.mkdir()
    binary = _candidate_path(tmp_path, "tool.exe")
    report = registry.discover_explicit(
        (
            RuntimeCandidateSpec(
                "directory", RuntimeFamily.COMPILER, directory, CandidateScope.PROJECT_LOCAL
            ),
            RuntimeCandidateSpec(
                "compiler", RuntimeFamily.COMPILER, binary, CandidateScope.PROJECT_LOCAL
            ),
            RuntimeCandidateSpec(
                "compiler", RuntimeFamily.COMPILER, binary, CandidateScope.PROJECT_LOCAL
            ),
        )
    )

    assert report.found[0].runtime_id == "compiler"
    assert report.issues[0].code is RuntimeIssueCode.NOT_REGULAR_FILE
    assert report.issues[1].code is RuntimeIssueCode.DUPLICATE_RUNTIME_ID
    with pytest.raises(ValueError, match="absolute"):
        RuntimeCandidateSpec(
            "relative", RuntimeFamily.COMPILER, Path("compiler.exe"), CandidateScope.PROJECT_LOCAL
        )


def test_manifest_reads_typed_project_requirements_and_rejects_unknown_schemas(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "requirements": [
            {
                "id": "python-runtime",
                "package_id": "Python",
                "kind": "runtime",
                "version": ">=3.10,<4.0",
                "scopes": ["project_local", "app_bundled"],
            },
            {
                "id": "android-ndk",
                "package_id": "android-ndk",
                "kind": "ndk",
                "version": "==27.2.12479018",
            },
        ],
    }
    manifest_path = tmp_path / "dbzscc.toolchain.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = read_toolchain_manifest(manifest_path)

    assert manifest.schema_version == 1
    assert manifest.requirements[0].package_id == "python"
    assert manifest.requirements[0].version_constraint == ">=3.10,<4.0"
    assert manifest.requirements[0].source is RequirementSource.PROJECT_MANIFEST
    assert manifest.requirements[1].kind is PackageKind.NDK
    with pytest.raises(ValueError, match="schema version"):
        ToolchainManifest.from_json('{"schema_version": 2, "requirements": []}')
    with pytest.raises(ValueError, match="incomplete"):
        ToolchainManifest.from_json(
            '{"schema_version": 1, "requirements": [{"id": "bad"}]}'
        )


def test_resolver_selects_declared_scope_and_recursively_resolves_dependencies(
    tmp_path: Path,
) -> None:
    jdk_requirement = _requirement(
        "android-ndk-needs-jdk",
        "openjdk",
        ">=17,<22",
        kind=PackageKind.FRAMEWORK,
        source=RequirementSource.PACKAGE_DEPENDENCY,
    )
    requirements = (
        _requirement("python", "python", ">=3.10,<4.0"),
        _requirement("ndk", "android-ndk", "==27.2.12479018", kind=PackageKind.NDK),
    )
    candidates = (
        _candidate(
            tmp_path,
            "python-app",
            "python",
            "3.12.2",
            scope=CandidateScope.APP_BUNDLED,
        ),
        _candidate(
            tmp_path,
            "python-project",
            "python",
            "3.15.2",
            scope=CandidateScope.PROJECT_LOCAL,
        ),
        _candidate(
            tmp_path,
            "ndk-project",
            "android-ndk",
            "27.2.12479018",
            kind=PackageKind.NDK,
            dependencies=(jdk_requirement,),
        ),
        _candidate(
            tmp_path,
            "jdk-project",
            "openjdk",
            "21.0.2+13",
            kind=PackageKind.FRAMEWORK,
        ),
    )

    result = PackageRequirementResolver().resolve(requirements, candidates)

    assert result.satisfied
    assert {item.package_id: item.candidate_id for item in result.selected} == {
        "android-ndk": "ndk-project",
        "openjdk": "jdk-project",
        "python": "python-app",
    }


def test_resolver_reports_missing_dependency_and_incompatible_version(
    tmp_path: Path,
) -> None:
    dependency = _requirement(
        "missing-cmake",
        "cmake",
        ">=3.27",
        kind=PackageKind.BUILD_SYSTEM,
        source=RequirementSource.PACKAGE_DEPENDENCY,
    )
    sdk = _candidate(
        tmp_path,
        "sdk",
        "android-sdk",
        "35.0",
        kind=PackageKind.SDK,
        dependencies=(dependency,),
    )
    python = _candidate(tmp_path, "python-old", "python", "3.9.18")
    result = PackageRequirementResolver().resolve(
        (
            _requirement("sdk", "android-sdk", ">=35", kind=PackageKind.SDK),
            _requirement("python", "python", ">=3.10"),
        ),
        (sdk, python),
    )

    assert not result.satisfied
    assert {issue.package_id: issue.code for issue in result.issues} == {
        "cmake": ResolutionIssueCode.MISSING_CANDIDATE,
        "python": ResolutionIssueCode.VERSION_INCOMPATIBLE,
    }


def test_resolver_requires_an_observed_explicit_package_location(tmp_path: Path) -> None:
    missing = PackageCandidate(
        candidate_id="uninstalled-python",
        package_id="python",
        kind=PackageKind.RUNTIME,
        version="3.13.0",
        scope=CandidateScope.PROJECT_LOCAL,
        location=tmp_path / "missing-python.exe",
    )
    result = PackageRequirementResolver().resolve(
        (_requirement("python", "python", ">=3.10"),), (missing,)
    )
    inspected = inspect_explicit_package_locations((missing,))

    assert result.issues[0].code is ResolutionIssueCode.CANDIDATE_LOCATION_NOT_OBSERVED
    assert inspected.issues[0].code is PackageLocationIssueCode.MISSING_PATH
    assert missing.presence is CandidatePresence.DECLARED

    with pytest.raises(TypeError):
        PackageCandidate(
            candidate_id="forged-present-python",
            package_id="python",
            kind=PackageKind.RUNTIME,
            version="3.13.0",
            scope=CandidateScope.PROJECT_LOCAL,
            location=missing.location,
            presence=CandidatePresence.PRESENT,
        )


def test_resolver_detects_version_constraint_conflicts_cycles_and_ambiguity(
    tmp_path: Path,
) -> None:
    incompatible = PackageRequirementResolver().resolve(
        (
            _requirement("low", "python", ">=3.12"),
            _requirement("high", "python", "<3.10"),
        ),
        (
            _candidate(tmp_path, "python-39", "python", "3.9.20"),
            _candidate(tmp_path, "python-313", "python", "3.13.0"),
        ),
    )
    assert incompatible.issues[0].code is ResolutionIssueCode.VERSION_INCOMPATIBLE

    a = _requirement("a-needs-b", "package-b", "", source=RequirementSource.PACKAGE_DEPENDENCY)
    b = _requirement("b-needs-a", "package-a", "", source=RequirementSource.PACKAGE_DEPENDENCY)
    cycle = PackageRequirementResolver().resolve(
        (_requirement("root-a", "package-a"),),
        (
            _candidate(tmp_path, "a", "package-a", "1.0", dependencies=(a,)),
            _candidate(tmp_path, "b", "package-b", "1.0", dependencies=(b,)),
        ),
    )
    assert any(issue.code is ResolutionIssueCode.DEPENDENCY_CYCLE for issue in cycle.issues)

    ambiguous = PackageRequirementResolver().resolve(
        (_requirement("ruff", "ruff", "==0.14.0", kind=PackageKind.LINTER),),
        (
            _candidate(
                tmp_path,
                "ruff-a",
                "ruff",
                "0.14.0",
                kind=PackageKind.LINTER,
                scope=CandidateScope.PROJECT_LOCAL,
            ),
            _candidate(
                tmp_path,
                "ruff-b",
                "ruff",
                "0.14.0",
                kind=PackageKind.LINTER,
                scope=CandidateScope.PROJECT_LOCAL,
            ),
        ),
    )
    assert ambiguous.issues[0].code is ResolutionIssueCode.AMBIGUOUS_CANDIDATE


def test_resolver_rejects_unimplemented_constraint_syntax_and_unknown_versions(
    tmp_path: Path,
) -> None:
    unsupported = PackageRequirementResolver().resolve(
        (_requirement("cargo", "cargo", "^1.70", kind=PackageKind.COMPILER),),
        (_candidate(tmp_path, "cargo", "cargo", "1.80.0", kind=PackageKind.COMPILER),),
    )
    unknown = PackageRequirementResolver().resolve(
        (_requirement("sdk", "android-sdk", ">=35", kind=PackageKind.SDK),),
        (_candidate(tmp_path, "sdk", "android-sdk", None, kind=PackageKind.SDK),),
    )
    assert unsupported.issues[0].code is ResolutionIssueCode.VERSION_CONSTRAINT_UNSUPPORTED
    assert unknown.issues[0].code is ResolutionIssueCode.VERSION_UNKNOWN


def test_package_recipe_gate_requires_pin_provenance_hash_license_and_private_root() -> None:
    valid = PackageRecipe(
        package_id="android-ndk",
        pinned_version="27.2.12479018",
        source_url="https://developer.android.com/ndk/downloads",
        official_source_id="android-developer-site",
        sha256="a" * 64,
        license_id="Android-SDK-License",
    )
    assert validate_package_recipe(valid).valid

    invalid = PackageRecipe(
        package_id="android-ndk",
        pinned_version="latest",
        source_url="http://example.invalid/archive.zip",
        official_source_id="",
        sha256="not-a-digest",
        license_id="",
        requires_admin=True,
        mutates_global_path=True,
    )
    issues = validate_package_recipe(invalid).issues
    assert "version_not_pinned_numeric" in issues
    assert "official_https_source_required" in issues
    assert "sha256_digest_required" in issues
    assert "license_id_required" in issues
    assert "administrator_install_forbidden" in issues
    assert "global_path_mutation_forbidden" in issues


def test_resource_demand_is_planned_through_existing_orchestrator_without_process_work(
    tmp_path: Path,
) -> None:
    demand = ResourceDemand(
        workload_id="lint-run-1",
        runtime_id="ruff-project",
        peak_memory_mib=512,
        cpu_threads=2,
        min_cpu_threads=1,
    )
    planner = ResourceOrchestrator(
        ResourcePolicy(
            os_available_headroom_mib=0,
            min_available_physical_mib=0,
            commit_safety_margin_mib=0,
            cpu_service_reserve_threads=1,
        )
    )

    plan = plan_resource_demand(
        demand, _resource_snapshot(), planner, now_monotonic_s=100.0
    )
    workload = demand.to_workload_request()

    assert plan.status is PlanStatus.ADMITTED
    assert plan.backend is Backend.CPU
    assert plan.host_memory_mib == 512
    assert plan.cpu_threads == 2
    assert workload.model_id == "tool:ruff-project"
    assert workload.backend_preferences == (Backend.CPU,)


def test_typed_invoker_routes_one_shot_and_persistent_requests_to_separate_ports(
    tmp_path: Path,
) -> None:
    registry = _runtime_registry(tmp_path)
    calls: list[object] = []

    class OneShotPort:
        def execute(self, plan):
            calls.append(("one-shot", plan))
            return OneShotOutcome(plan.invocation_id, OneShotState.SUCCEEDED, 0)

    class SessionPort:
        def start(self, plan):
            calls.append(("session", plan))
            return PersistentSessionHandle(
                plan.session_id, plan.runtime_id, SessionState.STARTING
            )

    invoker = TypedRuntimeInvoker(registry, OneShotPort(), SessionPort())
    project_root = tmp_path.absolute()
    demand = ResourceDemand("ruff-job", "ruff-project", 256)
    one_shot = OneShotInvocation(
        invocation_id="run-001",
        runtime_id="ruff-project",
        operation=InvocationOperation.CHECK,
        argv=("check", "src"),
        project_root=project_root,
        working_directory=project_root,
        timeout_s=30,
        environment_profile_id="project-default",
        resource_demand=demand,
    )
    result = invoker.invoke(one_shot)

    assert result == OneShotOutcome("run-001", OneShotState.SUCCEEDED, 0)
    assert calls[0][0] == "one-shot"
    assert calls[0][1].argv == ("check", "src")
    assert calls[0][1].executable_path.name == "ruff.exe"

    session_request = PersistentSessionRequest(
        session_id="lsp-session-1",
        runtime_id="pyright-lsp",
        project_root=project_root,
        working_directory=project_root,
        argv=("--stdio",),
        environment_profile_id="project-default",
        initialization_timeout_s=10,
        resource_demand=ResourceDemand("lsp-job", "pyright-lsp", 512),
    )
    session_result = invoker.invoke(session_request)
    assert session_result == PersistentSessionHandle(
        "lsp-session-1", "pyright-lsp", SessionState.STARTING
    )
    assert calls[1][0] == "session"
    assert calls[1][1].protocol is SessionProtocol.LSP_JSON_RPC_STDIO


def test_invocation_plans_reject_lifecycle_mismatch_shell_strings_and_path_escape(
    tmp_path: Path,
) -> None:
    registry = _runtime_registry(tmp_path)
    root = tmp_path.absolute()
    demand = ResourceDemand("lint-run", "ruff-project", 128)

    with pytest.raises(ValueError, match="Persistent runtimes"):
        plan_one_shot_invocation(
            registry,
            OneShotInvocation(
                "bad-run",
                "pyright-lsp",
                InvocationOperation.GENERIC,
                ("--stdio",),
                root,
                root,
                10,
                "default",
                ResourceDemand("bad-job", "pyright-lsp", 128),
            ),
        )
    with pytest.raises(TypeError, match="command string"):
        OneShotInvocation(
            "shell-run",
            "ruff-project",
            InvocationOperation.CHECK,
            "ruff check .",
            root,
            root,
            10,
            "default",
            demand,
        )
    with pytest.raises(ValueError, match="within project_root"):
        OneShotInvocation(
            "escape-run",
            "ruff-project",
            InvocationOperation.CHECK,
            ("check",),
            root,
            root.parent,
            10,
            "default",
            demand,
        )
    with pytest.raises(ValueError, match="write_scope"):
        OneShotInvocation(
            "fix-run",
            "ruff-project",
            InvocationOperation.FIX,
            ("check", "--fix"),
            root,
            root,
            10,
            "default",
            demand,
        )


def test_debugger_is_persistent_and_uses_its_separate_protocol(tmp_path: Path) -> None:
    registry = _runtime_registry(tmp_path)
    root = tmp_path.absolute()
    request = PersistentSessionRequest(
        session_id="debug-session",
        runtime_id="debugger",
        project_root=root,
        working_directory=root,
        argv=("--interpreter=mi" ,),
        environment_profile_id="debug-profile",
        initialization_timeout_s=15,
        resource_demand=ResourceDemand("debug-work", "debugger", 1024),
    )

    plan = plan_persistent_session(registry, request)

    assert registry.get("debugger").process_model is ProcessModel.PERSISTENT
    assert plan.protocol is SessionProtocol.DEBUG_ADAPTER_STDIO


def test_session_message_and_timeout_limits_are_validated(tmp_path: Path) -> None:
    root = tmp_path.absolute()
    with pytest.raises(ValueError, match="max_message_bytes"):
        PersistentSessionRequest(
            "oversize",
            "pyright-lsp",
            root,
            root,
            (),
            "default",
            5,
            ResourceDemand("oversize-work", "pyright-lsp", 100),
            max_message_bytes=32 * 1024 * 1024,
        )


def test_resource_demand_rejects_invalid_memory_and_cpu_values() -> None:
    with pytest.raises(ValueError, match="peak_memory_mib"):
        ResourceDemand("invalid", "compiler", -1)
    with pytest.raises(ValueError, match="min_cpu_threads"):
        ResourceDemand("invalid", "compiler", 100, cpu_threads=1, min_cpu_threads=2)


def test_catalog_covers_requested_stacks_without_claiming_local_installation() -> None:
    profiles = {profile.stack_id: profile for profile in list_stack_profiles()}
    assert set(profiles) == {
        "python",
        "java-kotlin",
        "rust",
        "javascript-typescript-electron",
        "android",
        "cpp",
        "dotnet",
    }
    assert RuntimeRegistry().entries() == ()

    def packages(stack_id: str) -> set[str]:
        return {item.package_id for item in get_stack_profile(stack_id).components}

    assert {"python", "python-venv", "pytest", "ruff", "pyright", "debugpy"} <= packages("python")
    assert {"jdk", "kotlinc", "gradle", "maven", "detekt", "ktlint", "jdtls"} <= packages("java-kotlin")
    assert {"rustc", "cargo", "rustfmt", "clippy", "rust-analyzer"} <= packages("rust")
    assert {"nodejs", "npm", "pnpm", "yarn", "typescript", "vite", "electron", "vitest", "eslint", "prettier"} <= packages("javascript-typescript-electron")
    assert {"android-sdk", "android-ndk", "jdk", "gradle"} <= packages("android")
    assert {"llvm-clang", "cmake", "ninja", "clangd", "lldb"} <= packages("cpp")
    assert {"dotnet-sdk", "dotnet-runtime", "msbuild", "vstest", "csharp-language-server"} <= packages("dotnet")

    js_profile = get_stack_profile("javascript-typescript-electron")
    vite_build = next(item for item in js_profile.components if item.component_id == "vite-build")
    vite_dev = next(item for item in js_profile.components if item.component_id == "vite-dev")
    assert vite_build.process_model is ProcessModel.ONE_SHOT
    assert vite_dev.process_model is ProcessModel.PERSISTENT
    assert all(not hasattr(item, "installed") for profile in STACK_CATALOG for item in profile.components)


def test_generic_persistent_stack_process_uses_supervised_stdio(tmp_path: Path) -> None:
    registry = RuntimeRegistry()
    executable = _candidate_path(tmp_path, "node_modules/vite/bin/vite.js")
    report = registry.discover_explicit(
        (
            RuntimeCandidateSpec(
                "vite-dev-server",
                RuntimeFamily.DEVELOPMENT_SERVER,
                executable,
                CandidateScope.PROJECT_LOCAL,
            ),
        )
    )
    assert not report.issues
    root = tmp_path.absolute()
    request = PersistentSessionRequest(
        "vite-session",
        "vite-dev-server",
        root,
        root,
        ("--host",),
        "dev-profile",
        10,
        ResourceDemand("vite-dev-work", "vite-dev-server", 256),
    )

    plan = plan_persistent_session(registry, request)

    assert plan.protocol is SessionProtocol.MANAGED_STDIO
    assert registry.get("vite-dev-server").process_model is ProcessModel.PERSISTENT



def test_pyproject_adapter_reads_build_runtime_framework_and_selected_dev_requirements(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '''\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "example-app"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = ["PySide6>=6.7", "requests>=2.32"]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.4"]
''',
        encoding="utf-8",
    )

    core = read_pyproject_requirements(pyproject)
    with_dev = read_pyproject_requirements(pyproject, include_optional_groups=("dev",))
    by_package = {item.package_id: item for item in with_dev}

    assert by_package["python"].version_constraint == ">=3.10"
    assert by_package["setuptools"].kind is PackageKind.BUILD_SYSTEM
    assert by_package["pyside6"].kind is PackageKind.FRAMEWORK
    assert by_package["requests"].kind is PackageKind.LIBRARY
    assert by_package["pytest"].kind is PackageKind.TEST_RUNNER
    assert by_package["ruff"].kind is PackageKind.LINTER
    assert by_package["ruff"].version_constraint == ">=0.4"
    assert "pytest" not in {item.package_id for item in core}
    assert all(item.source is RequirementSource.PROJECT_MANIFEST for item in with_dev)

    pyproject.write_text(
        '''\
[project]
name = "example-app"
version = "1.0.0"
dependencies = ["demo[feature]>=1"]
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="extras"):
        read_pyproject_requirements(pyproject)


def test_actual_checkout_pyproject_requirements_are_read_from_its_exact_path() -> None:
    project_root = Path(__file__).resolve().parents[1]
    requirements = read_pyproject_requirements(
        project_root / "pyproject.toml", include_optional_groups=("dev",)
    )
    by_package = {item.package_id: item for item in requirements}

    assert by_package["python"].version_constraint == ">=3.10"
    assert by_package["pyside6"].version_constraint == ">=6.7"
    assert by_package["pytest"].version_constraint == ">=8"
    assert by_package["ruff"].version_constraint == ">=0.4"
    assert by_package["setuptools"].version_constraint == ">=68"
