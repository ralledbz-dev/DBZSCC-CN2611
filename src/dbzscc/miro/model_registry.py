"""Local, transactional registry for discovered MIRO model artifacts.

Model identities are derived only from selected GGUF metadata; local artifact
IDs remain path-specific. The registry stores the GGUF metadata-prefix hash,
not a digest of the full (potentially very large) model file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import unicodedata
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .gguf_metadata import (
    GGUF_METADATA_PARSER_VERSION,
    GGUFMetadataDocument,
    GGUFMetadataReader,
)
from .local_inventory import (
    LocalArtifact,
    LocalInventoryScanner,
    RootInput,
    ScanIssue,
)
from .resumable_inventory import ResumableLocalInventoryScanner

_SCHEMA_VERSION = 5
_IDENTITY_SCHEMA = "miro-model-identity-v1"
_SELECTED_METADATA_KEYS = (
    "general.architecture",
    "general.name",
    "general.basename",
    "general.organization",
    "general.file_type",
    "general.quantization_version",
    "general.parameter_count",
    "general.size_label",
    "clip.projector_type",
    "clip.model_architecture",
    "clip.projector_architecture",
    "clip.has_vision_encoder",
    "audio.projector_type",
    "audio.model_architecture",
    "audio.projector_architecture",
    "audio.has_projector",
)

_MIGRATION_1 = (
    """CREATE TABLE models (
        model_id TEXT PRIMARY KEY,
        identity_schema TEXT NOT NULL,
        display_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        architecture TEXT NOT NULL,
        normalized_architecture TEXT NOT NULL,
        organization TEXT,
        normalized_organization TEXT,
        identity_status TEXT NOT NULL CHECK (identity_status IN ('resolved', 'partial')),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )""",
    """CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        source_root TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        current_observation_id TEXT
    )""",
    """CREATE TABLE artifact_observations (
        observation_id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        model_id TEXT REFERENCES models(model_id) ON DELETE RESTRICT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
        file_modified_at_utc TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'metadata_read', 'metadata_parse_failed',
            'artifact_changed_during_scan', 'runtime_candidate'
        )),
        identity_status TEXT NOT NULL CHECK (identity_status IN (
            'resolved', 'partial', 'unresolved', 'not_applicable'
        )),
        metadata_prefix_sha256 TEXT,
        parsed_file_size_bytes INTEGER,
        parsed_file_modified_at_utc TEXT,
        metadata_end_offset INTEGER,
        gguf_version INTEGER,
        tensor_count INTEGER,
        metadata_count INTEGER,
        selected_metadata_json TEXT NOT NULL,
        identity_evidence_json TEXT NOT NULL,
        identity_uncertainty_json TEXT NOT NULL,
        parse_issue_code TEXT,
        parse_issue_offset INTEGER,
        parse_issue_detail TEXT
    )""",
    "CREATE INDEX artifacts_path_idx ON artifacts(path)",
    "CREATE INDEX observations_model_idx ON artifact_observations(model_id)",
)

_MIGRATION_2 = (
    "ALTER TABLE artifact_observations ADD COLUMN parser_version TEXT NOT NULL DEFAULT 'gguf-prefix-v1'",
    "ALTER TABLE artifact_observations ADD COLUMN capability_evidence_json TEXT NOT NULL DEFAULT '{}'",
    """CREATE TABLE discovery_runs (
        run_id TEXT PRIMARY KEY,
        roots_fingerprint TEXT NOT NULL,
        roots_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        state TEXT NOT NULL CHECK (state IN ('partial', 'completed')),
        continuation_token TEXT,
        directories_visited INTEGER NOT NULL DEFAULT 0,
        entries_seen INTEGER NOT NULL DEFAULT 0,
        regular_files_seen INTEGER NOT NULL DEFAULT 0,
        artifacts_seen INTEGER NOT NULL DEFAULT 0,
        parse_failures INTEGER NOT NULL DEFAULT 0,
        issues_json TEXT NOT NULL DEFAULT '[]'
    )""",
    "CREATE INDEX discovery_runs_roots_idx ON discovery_runs(roots_fingerprint, state, updated_at)",
    """CREATE TABLE discovery_run_artifacts (
        run_id TEXT NOT NULL REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        PRIMARY KEY (run_id, artifact_id)
    )""",
    """CREATE TABLE runtime_qualifications (
        qualification_id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        model_id TEXT,
        artifact_fingerprint TEXT NOT NULL,
        backend TEXT NOT NULL CHECK (backend IN ('cpu', 'cuda', 'vulkan')),
        runtime_id TEXT NOT NULL,
        runtime_version TEXT NOT NULL,
        settings_profile_hash TEXT NOT NULL,
        settings_profile_json TEXT NOT NULL,
        resource_profile_json TEXT NOT NULL,
        modality TEXT NOT NULL CHECK (modality IN ('text', 'image', 'audio')),
        status TEXT NOT NULL CHECK (status IN (
            'qualified', 'runtime_started', 'failed', 'timeout', 'not_tested'
        )),
        observed_at TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence_json TEXT NOT NULL
    )""",
    "CREATE INDEX runtime_qualifications_current_idx ON runtime_qualifications(artifact_id, artifact_fingerprint, backend, runtime_id, runtime_version, settings_profile_hash, observed_at)",
    """CREATE TABLE multimodal_pairings (
        pair_id TEXT PRIMARY KEY,
        modality TEXT NOT NULL CHECK (modality IN ('image', 'audio')),
        model_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        projector_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        model_metadata_sha256 TEXT NOT NULL,
        projector_metadata_sha256 TEXT NOT NULL,
        status TEXT NOT NULL,
        compatibility_status TEXT NOT NULL,
        runtime_test_status TEXT NOT NULL CHECK (runtime_test_status IN (
            'not_tested', 'qualified', 'failed', 'timeout'
        )),
        runtime_id TEXT,
        runtime_version TEXT,
        observed_at TEXT NOT NULL,
        evidence_json TEXT NOT NULL
    )""",
    "CREATE INDEX multimodal_pairings_current_idx ON multimodal_pairings(model_artifact_id, projector_artifact_id, modality, observed_at)",
)

_MIGRATION_3 = (
    """CREATE TABLE artifact_observations_v3 (
        observation_id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        model_id TEXT REFERENCES models(model_id) ON DELETE RESTRICT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
        file_modified_at_utc TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'metadata_read', 'metadata_parse_failed',
            'artifact_changed_during_scan', 'runtime_candidate',
            'non_gguf_format_candidate'
        )),
        identity_status TEXT NOT NULL CHECK (identity_status IN (
            'resolved', 'partial', 'unresolved', 'not_applicable'
        )),
        metadata_prefix_sha256 TEXT,
        parsed_file_size_bytes INTEGER,
        parsed_file_modified_at_utc TEXT,
        metadata_end_offset INTEGER,
        gguf_version INTEGER,
        tensor_count INTEGER,
        metadata_count INTEGER,
        selected_metadata_json TEXT NOT NULL,
        identity_evidence_json TEXT NOT NULL,
        identity_uncertainty_json TEXT NOT NULL,
        parse_issue_code TEXT,
        parse_issue_offset INTEGER,
        parse_issue_detail TEXT,
        parser_version TEXT NOT NULL,
        capability_evidence_json TEXT NOT NULL
    )""",
    """INSERT INTO artifact_observations_v3 (
        observation_id, artifact_id, model_id, first_seen_at, last_seen_at,
        file_size_bytes, file_modified_at_utc, status, identity_status,
        metadata_prefix_sha256, parsed_file_size_bytes,
        parsed_file_modified_at_utc, metadata_end_offset, gguf_version,
        tensor_count, metadata_count, selected_metadata_json,
        identity_evidence_json, identity_uncertainty_json, parse_issue_code,
        parse_issue_offset, parse_issue_detail, parser_version,
        capability_evidence_json
    ) SELECT observation_id, artifact_id, model_id, first_seen_at, last_seen_at,
        file_size_bytes, file_modified_at_utc, status, identity_status,
        metadata_prefix_sha256, parsed_file_size_bytes,
        parsed_file_modified_at_utc, metadata_end_offset, gguf_version,
        tensor_count, metadata_count, selected_metadata_json,
        identity_evidence_json, identity_uncertainty_json, parse_issue_code,
        parse_issue_offset, parse_issue_detail, parser_version,
        capability_evidence_json FROM artifact_observations""",
    "DROP TABLE artifact_observations",
    "ALTER TABLE artifact_observations_v3 RENAME TO artifact_observations",
    "CREATE INDEX observations_model_idx ON artifact_observations(model_id)",
    """CREATE TABLE runtime_qualifications_v3 (
        qualification_id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        model_id TEXT,
        artifact_fingerprint TEXT NOT NULL,
        backend TEXT NOT NULL CHECK (backend IN ('cpu', 'cuda', 'vulkan')),
        runtime_id TEXT NOT NULL,
        runtime_version TEXT NOT NULL,
        settings_profile_hash TEXT NOT NULL,
        settings_profile_json TEXT NOT NULL,
        resource_profile_json TEXT NOT NULL,
        modality TEXT NOT NULL CHECK (modality IN ('text', 'image', 'audio')),
        status TEXT NOT NULL CHECK (status IN (
            'qualified', 'runtime_started', 'runtime_tested', 'failed',
            'timeout', 'not_tested'
        )),
        observed_at TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence_json TEXT NOT NULL
    )""",
    """INSERT INTO runtime_qualifications_v3 (
        qualification_id, artifact_id, model_id, artifact_fingerprint,
        backend, runtime_id, runtime_version, settings_profile_hash,
        settings_profile_json, resource_profile_json, modality, status,
        observed_at, source, evidence_json
    ) SELECT qualification_id, artifact_id, model_id, artifact_fingerprint,
        backend, runtime_id, runtime_version, settings_profile_hash,
        settings_profile_json, resource_profile_json, modality, status,
        observed_at, source, evidence_json FROM runtime_qualifications""",
    "DROP TABLE runtime_qualifications",
    "ALTER TABLE runtime_qualifications_v3 RENAME TO runtime_qualifications",
    "CREATE INDEX runtime_qualifications_current_idx ON runtime_qualifications(artifact_id, artifact_fingerprint, backend, runtime_id, runtime_version, settings_profile_hash, observed_at)",
)

_MIGRATION_4 = (
    "ALTER TABLE discovery_runs ADD COLUMN reparse_points_skipped INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE discovery_runs ADD COLUMN access_denied_directories INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE discovery_runs ADD COLUMN non_gguf_artifacts_seen INTEGER NOT NULL DEFAULT 0",
)

_MIGRATION_5 = (
    """CREATE TABLE artifact_full_hashes (
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
        observation_id TEXT NOT NULL REFERENCES artifact_observations(observation_id) ON DELETE RESTRICT,
        file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
        file_modified_at_utc TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        verified_at TEXT NOT NULL,
        PRIMARY KEY (artifact_id, observation_id)
    )""",
    "ALTER TABLE runtime_qualifications ADD COLUMN model_full_file_sha256 TEXT",
    "ALTER TABLE runtime_qualifications ADD COLUMN projector_artifact_id TEXT",
    "ALTER TABLE runtime_qualifications ADD COLUMN projector_full_file_sha256 TEXT",
    "ALTER TABLE runtime_qualifications ADD COLUMN device_physical_id TEXT",
    "ALTER TABLE runtime_qualifications ADD COLUMN runtime_parameters_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE multimodal_pairings ADD COLUMN model_full_file_sha256 TEXT",
    "ALTER TABLE multimodal_pairings ADD COLUMN projector_full_file_sha256 TEXT",
)


class RegistryError(RuntimeError):
    """Base error for a local MIRO model registry operation."""


class RegistryStorageError(RegistryError):
    """A database operation failed; its active transaction was rolled back."""


class RegistrySchemaError(RegistryError):
    """The database schema is newer than this code can safely read."""


@dataclass(frozen=True)
class NormalizedModelIdentity:
    """A stable metadata-derived identity and its explicit confidence signals."""

    model_id: str | None
    display_name: str | None
    normalized_name: str | None
    architecture: str | None
    normalized_architecture: str | None
    organization: str | None
    normalized_organization: str | None
    status: str
    evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]
    selected_metadata: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    identity_schema: str
    display_name: str
    normalized_name: str
    architecture: str
    normalized_architecture: str
    organization: str | None
    normalized_organization: str | None
    identity_status: str
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    path: str
    source_root: str
    first_seen_at: str
    last_seen_at: str
    current_observation_id: str | None
    status: str | None
    identity_status: str | None
    model_id: str | None
    metadata_prefix_sha256: str | None
    parse_issue_code: str | None
    file_size_bytes: int | None = None
    file_modified_at_utc: str | None = None
    parser_version: str | None = None
    capability_evidence_json: str = "{}"


@dataclass(frozen=True)
class ArtifactObservationRecord:
    observation_id: str
    artifact_id: str
    model_id: str | None
    first_seen_at: str
    last_seen_at: str
    file_size_bytes: int
    file_modified_at_utc: str
    status: str
    identity_status: str
    metadata_prefix_sha256: str | None
    parsed_file_size_bytes: int | None
    parsed_file_modified_at_utc: str | None
    metadata_end_offset: int | None
    gguf_version: int | None
    tensor_count: int | None
    metadata_count: int | None
    selected_metadata_json: str
    identity_evidence_json: str
    identity_uncertainty_json: str
    parse_issue_code: str | None
    parse_issue_offset: int | None
    parse_issue_detail: str | None
    parser_version: str
    capability_evidence_json: str


@dataclass(frozen=True)
class RegistrySyncResult:
    """Summary of one inventory/metadata pass and the SQLite upsert."""

    run_id: str
    scanned_at: str
    artifact_count: int
    parse_failure_count: int
    unresolved_identity_count: int
    scanner_issues: tuple[ScanIssue, ...]
    complete: bool = True
    continuation_token: str | None = None
    directories_visited: int = 0
    entries_seen: int = 0
    regular_files_seen: int = 0
    reparse_points_skipped: int = 0
    access_denied_directories: int = 0
    non_gguf_artifact_count: int = 0


@dataclass(frozen=True)
class DiscoveryRunRecord:
    run_id: str
    roots_fingerprint: str
    roots_json: str
    started_at: str
    updated_at: str
    completed_at: str | None
    state: str
    continuation_token: str | None
    directories_visited: int
    entries_seen: int
    regular_files_seen: int
    artifacts_seen: int
    parse_failures: int
    reparse_points_skipped: int
    access_denied_directories: int
    non_gguf_artifacts_seen: int
    issues_json: str


@dataclass(frozen=True)
class CatalogQuery:
    root: str | None = None
    run_id: str | None = None
    modality: str | None = None
    backend: str | None = None
    runtime_id: str | None = None
    runtime_version: str | None = None
    settings_profile_hash: str | None = None
    require_qualified: bool = False
    include_runtime_artifacts: bool = False


@dataclass(frozen=True)
class RuntimeQualificationRecord:
    qualification_id: str
    artifact_id: str
    model_id: str | None
    artifact_fingerprint: str
    backend: str
    runtime_id: str
    runtime_version: str
    settings_profile_hash: str
    settings_profile_json: str
    resource_profile_json: str
    modality: str
    status: str
    observed_at: str
    source: str
    evidence_json: str
    model_full_file_sha256: str | None = None
    projector_artifact_id: str | None = None
    projector_full_file_sha256: str | None = None
    device_physical_id: str | None = None
    runtime_parameters_json: str = "{}"


@dataclass(frozen=True)
class MultimodalPairRecord:
    pair_id: str
    modality: str
    model_artifact_id: str
    projector_artifact_id: str
    model_metadata_sha256: str
    projector_metadata_sha256: str
    status: str
    compatibility_status: str
    runtime_test_status: str
    runtime_id: str | None
    runtime_version: str | None
    observed_at: str
    evidence_json: str
    model_full_file_sha256: str | None = None
    projector_full_file_sha256: str | None = None


@dataclass(frozen=True)
class ModelCatalogEntry:
    artifact: ArtifactRecord
    model: ModelRecord | None
    observation: ArtifactObservationRecord | None
    selected_metadata: tuple[tuple[str, Any], ...]
    capability_evidence_json: str
    runtime_qualifications: tuple[RuntimeQualificationRecord, ...]
    multimodal_pairings: tuple[MultimodalPairRecord, ...]
    full_file_sha256: str | None = None

    @property
    def image_role(self) -> str:
        return _capability_role(self.capability_evidence_json, "image")

    @property
    def audio_role(self) -> str:
        return _capability_role(self.capability_evidence_json, "audio")


@dataclass(frozen=True)
class _ObservationInput:
    artifact: LocalArtifact
    model_id: str | None
    status: str
    identity_status: str
    metadata_prefix_sha256: str | None
    parsed_file_size_bytes: int | None
    parsed_file_modified_at_utc: str | None
    metadata_end_offset: int | None
    gguf_version: int | None
    tensor_count: int | None
    metadata_count: int | None
    selected_metadata_json: str
    identity_evidence_json: str
    identity_uncertainty_json: str
    parse_issue_code: str | None
    parse_issue_offset: int | None
    parse_issue_detail: str | None
    parser_version: str
    capability_evidence_json: str
    identity: NormalizedModelIdentity | None


def normalize_gguf_identity(
    document: GGUFMetadataDocument,
) -> NormalizedModelIdentity:
    """Normalize a small, explicit subset of GGUF metadata.

    ``general.architecture`` and ``general.name`` form the preferred identity.
    ``general.basename`` is an explicitly weaker name fallback. No filename,
    path, tensor payload, or external model catalog is used as identity evidence.
    """
    fields = {item.key: item for item in document.fields}
    selected: dict[str, Any] = {}
    uncertainty: list[str] = []

    for key in _SELECTED_METADATA_KEYS:
        item = fields.get(key)
        if item is None:
            continue
        value = item.value
        valid_string = item.type_name == "string" and isinstance(value, str)
        valid_integer = item.type_name in {
            "uint8",
            "uint16",
            "uint32",
            "uint64",
            "int8",
            "int16",
            "int32",
            "int64",
        } and type(value) is int
        valid_float = (
            item.type_name in {"float32", "float64"}
            and type(value) is float
            and math.isfinite(value)
        )
        valid_boolean = item.type_name == "bool" and type(value) is bool
        if valid_string or valid_integer or valid_float or valid_boolean:
            selected[key] = value

    architecture = _metadata_text(fields.get("general.architecture"))
    if architecture is None:
        uncertainty.append(
            "invalid_general_architecture"
            if "general.architecture" in fields
            else "missing_general_architecture"
        )

    name = _metadata_text(fields.get("general.name"))
    name_key = "general.name"
    if name is None:
        basename = _metadata_text(fields.get("general.basename"))
        if basename is not None:
            name = basename
            name_key = "general.basename"
            uncertainty.append("name_from_general_basename")
        elif "general.name" in fields or "general.basename" in fields:
            uncertainty.append("invalid_general_name_and_basename")
        else:
            uncertainty.append("missing_general_name_and_basename")

    organization = _metadata_text(fields.get("general.organization"))
    if "general.organization" in fields and organization is None:
        uncertainty.append("invalid_general_organization_ignored")

    normalized_architecture = _normalize_identity_component(architecture)
    normalized_name = _normalize_identity_component(name)
    normalized_organization = _normalize_identity_component(organization)
    evidence: list[str] = []
    if architecture is not None:
        evidence.append("general.architecture")
    if name is not None:
        evidence.append(name_key)
    if organization is not None:
        evidence.append("general.organization")

    model_id = None
    if normalized_architecture is not None and normalized_name is not None:
        canonical_identity = {
            "schema": _IDENTITY_SCHEMA,
            "architecture": normalized_architecture,
            "name": normalized_name,
            "organization": normalized_organization,
        }
        identity_bytes = _canonical_json(canonical_identity).encode("utf-8")
        digest = hashlib.sha256(b"dbzscc-miro-model-identity\0" + identity_bytes).hexdigest()
        model_id = f"miro-model-v1-{digest}"
        status = "resolved" if name_key == "general.name" else "partial"
    else:
        status = "unresolved"

    return NormalizedModelIdentity(
        model_id=model_id,
        display_name=name,
        normalized_name=normalized_name,
        architecture=architecture,
        normalized_architecture=normalized_architecture,
        organization=organization,
        normalized_organization=normalized_organization,
        status=status,
        evidence=tuple(evidence),
        uncertainty=tuple(uncertainty),
        selected_metadata=tuple(sorted(selected.items())),
    )


class LocalModelRegistry:
    """Persist local MIRO discovery using a small, migrations-aware SQLite DB.

    Pass explicit roots to ``synchronize``. Discovery and GGUF reads use the
    existing read-only services; only registry state is written. Database writes
    for a whole pass are atomic, and a storage failure is raised to the caller.
    """

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        scanner: LocalInventoryScanner | None = None,
        reader: GGUFMetadataReader | None = None,
    ) -> None:
        raw_path = os.fspath(database_path)
        if not isinstance(raw_path, str) or not raw_path.strip() or raw_path == ":memory:":
            raise ValueError("database_path must be a non-empty persistent file path")
        self.database_path = Path(raw_path).expanduser().absolute()
        self._scanner = scanner or ResumableLocalInventoryScanner()
        self._reader = reader or GGUFMetadataReader()
        self._initialize_schema()

    def synchronize(
        self,
        model_roots: Iterable[RootInput] = (),
        runtime_roots: Iterable[RootInput] = (),
        *,
        observed_at: datetime | None = None,
        resume_token: str | None = None,
        run_id: str | None = None,
    ) -> RegistrySyncResult:
        """Discover explicit roots, read GGUF prefixes and transactionally upsert.

        A root scan is performed before the SQLite transaction so filesystem
        I/O never holds a database write lock. Parse failures become observations;
        SQLite failures roll back the entire persistence pass and raise
        ``RegistryStorageError``.
        """
        model_root_values = _root_values(model_roots)
        runtime_root_values = _root_values(runtime_roots)
        if resume_token is None:
            inventory = self._scanner.scan(
                model_roots=model_root_values,
                runtime_roots=runtime_root_values,
            )
        else:
            inventory = self._scanner.scan(
                model_roots=model_root_values,
                runtime_roots=runtime_root_values,
                resume_token=resume_token,
            )
        timestamp = _timestamp_text(observed_at or inventory.scanned_at)
        prepared = tuple(self._prepare(artifact) for artifact in inventory.artifacts)
        selected_run_id = run_id or uuid.uuid4().hex

        try:
            with self._connection() as connection, connection:
                for observation in prepared:
                    self._upsert_observation(connection, observation, timestamp)
                persisted_run = self._persist_discovery_run(
                    connection,
                    run_id=selected_run_id,
                    model_roots=model_root_values,
                    runtime_roots=runtime_root_values,
                    inventory=inventory,
                    prepared=prepared,
                    timestamp=timestamp,
                )
        except sqlite3.Error as exc:
            raise RegistryStorageError(f"SQLite registry transaction failed: {exc}") from exc

        return RegistrySyncResult(
            run_id=selected_run_id,
            scanned_at=timestamp,
            artifact_count=persisted_run.artifacts_seen,
            parse_failure_count=persisted_run.parse_failures,
            unresolved_identity_count=sum(
                item.status == "metadata_read" and item.identity_status == "unresolved"
                for item in prepared
            ),
            scanner_issues=tuple(
                ScanIssue(**item)
                for item in json.loads(persisted_run.issues_json)
                if not (inventory.complete and item.get("code") == "scan_cancelled")
            ),
            complete=inventory.complete,
            continuation_token=inventory.continuation_token,
            directories_visited=persisted_run.directories_visited,
            entries_seen=persisted_run.entries_seen,
            regular_files_seen=persisted_run.regular_files_seen,
            reparse_points_skipped=persisted_run.reparse_points_skipped,
            access_denied_directories=persisted_run.access_denied_directories,
            non_gguf_artifact_count=persisted_run.non_gguf_artifacts_seen,
        )

    def get_resumable_scan(
        self,
        model_roots: Iterable[RootInput],
        runtime_roots: Iterable[RootInput] = (),
    ) -> DiscoveryRunRecord | None:
        """Return the newest incomplete scan only for the same configured roots."""
        roots_json = _roots_json(_root_values(model_roots), _root_values(runtime_roots))
        fingerprint = hashlib.sha256(roots_json.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            row = connection.execute(
                """SELECT run_id, roots_fingerprint, roots_json, started_at,
                          updated_at, completed_at, state, continuation_token,
                          directories_visited, entries_seen, regular_files_seen,
                          artifacts_seen, parse_failures, reparse_points_skipped,
                          access_denied_directories, non_gguf_artifacts_seen, issues_json
                   FROM discovery_runs
                   WHERE roots_fingerprint = ? AND state = 'partial'
                     AND continuation_token IS NOT NULL
                   ORDER BY updated_at DESC, run_id DESC LIMIT 1""",
                (fingerprint,),
            ).fetchone()
        return DiscoveryRunRecord(**dict(row)) if row is not None else None

    def get_discovery_run(self, run_id: str) -> DiscoveryRunRecord | None:
        """Read one persisted scan summary, including cumulative traversal metrics."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM discovery_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return DiscoveryRunRecord(**dict(row)) if row is not None else None

    def _persist_discovery_run(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        model_roots: tuple[RootInput, ...],
        runtime_roots: tuple[RootInput, ...],
        inventory: Any,
        prepared: tuple[_ObservationInput, ...],
        timestamp: str,
    ) -> DiscoveryRunRecord:
        roots_json = _roots_json(model_roots, runtime_roots)
        roots_fingerprint = hashlib.sha256(roots_json.encode("utf-8")).hexdigest()
        previous = connection.execute(
            "SELECT * FROM discovery_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if previous is not None and previous["roots_fingerprint"] != roots_fingerprint:
            raise RegistryError("A discovery run cannot be resumed with different roots.")
        started_at = previous["started_at"] if previous is not None else timestamp
        prior = previous or {}
        issues = json.loads(prior["issues_json"]) if previous is not None else []
        issue_keys = {
            (item.get("code"), item.get("root"), item.get("path"), item.get("detail"))
            for item in issues
            if isinstance(item, dict)
        }
        for issue in inventory.issues:
            record = {
                "code": issue.code,
                "root": issue.root,
                "path": issue.path,
                "detail": issue.detail,
            }
            key = (record["code"], record["root"], record["path"], record["detail"])
            if key not in issue_keys:
                issues.append(record)
                issue_keys.add(key)
        new_parse_failures = sum(
            item.status == "metadata_parse_failed" for item in prepared
        )
        parse_failures = (prior["parse_failures"] if previous is not None else 0) + new_parse_failures
        scan_has_issues = any(
            isinstance(item, dict) and item.get("code") != "scan_cancelled"
            for item in issues
        ) or parse_failures > 0
        state = "completed" if inventory.complete and not scan_has_issues else "partial"
        connection.execute(
            """INSERT INTO discovery_runs (
                    run_id, roots_fingerprint, roots_json, started_at, updated_at,
                    completed_at, state, continuation_token, directories_visited,
                    entries_seen, regular_files_seen, artifacts_seen, parse_failures,
                    reparse_points_skipped, access_denied_directories,
                    non_gguf_artifacts_seen, issues_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    roots_fingerprint = excluded.roots_fingerprint,
                    roots_json = excluded.roots_json,
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at,
                    state = excluded.state,
                    continuation_token = excluded.continuation_token,
                    directories_visited = excluded.directories_visited,
                    entries_seen = excluded.entries_seen,
                    regular_files_seen = excluded.regular_files_seen,
                    parse_failures = excluded.parse_failures,
                    reparse_points_skipped = excluded.reparse_points_skipped,
                    access_denied_directories = excluded.access_denied_directories,
                    non_gguf_artifacts_seen = excluded.non_gguf_artifacts_seen,
                    issues_json = excluded.issues_json""",
            (
                run_id,
                roots_fingerprint,
                roots_json,
                started_at,
                timestamp,
                timestamp if state == "completed" else None,
                state,
                inventory.continuation_token,
                (previous["directories_visited"] if previous is not None else 0)
                + inventory.directories_visited,
                (previous["entries_seen"] if previous is not None else 0)
                + inventory.entries_seen,
                (previous["regular_files_seen"] if previous is not None else 0)
                + inventory.regular_files_seen,
                parse_failures,
                (previous["reparse_points_skipped"] if previous is not None else 0)
                + inventory.reparse_points_skipped,
                (previous["access_denied_directories"] if previous is not None else 0)
                + inventory.access_denied_directories,
                (previous["non_gguf_artifacts_seen"] if previous is not None else 0)
                + sum(
                    item.artifact.kind == "non_gguf_model_candidate"
                    for item in prepared
                ),
                _canonical_json(issues),
            ),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO discovery_run_artifacts(run_id, artifact_id) VALUES (?, ?)",
            ((run_id, item.artifact.artifact_id) for item in prepared),
        )
        count_row = connection.execute(
            "SELECT COUNT(*) AS count FROM discovery_run_artifacts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        artifact_count = int(count_row["count"])
        non_gguf_row = connection.execute(
            """SELECT COUNT(*) AS count
               FROM discovery_run_artifacts AS run_artifact
               JOIN artifacts AS artifact ON artifact.artifact_id = run_artifact.artifact_id
               WHERE run_artifact.run_id = ?
                 AND artifact.kind = 'non_gguf_model_candidate'""",
            (run_id,),
        ).fetchone()
        non_gguf_count = int(non_gguf_row["count"])
        connection.execute(
            """UPDATE discovery_runs
               SET artifacts_seen = ?, non_gguf_artifacts_seen = ?
               WHERE run_id = ?""",
            (artifact_count, non_gguf_count, run_id),
        )
        run_row = connection.execute(
            "SELECT * FROM discovery_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return DiscoveryRunRecord(**dict(run_row))

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        """Return one path-specific artifact and its current observation."""
        query = """SELECT a.artifact_id, a.kind, a.path, a.source_root,
                    a.first_seen_at, a.last_seen_at, a.current_observation_id,
                    o.status, o.identity_status, o.model_id,
                    o.metadata_prefix_sha256, o.parse_issue_code,
                    o.file_size_bytes, o.file_modified_at_utc, o.parser_version,
                    COALESCE(o.capability_evidence_json, '{}') AS capability_evidence_json
                FROM artifacts AS a
                LEFT JOIN artifact_observations AS o
                  ON o.observation_id = a.current_observation_id
                WHERE a.artifact_id = ?"""
        with self._connection() as connection:
            row = connection.execute(query, (artifact_id,)).fetchone()
        return ArtifactRecord(**dict(row)) if row is not None else None

    def ensure_full_file_sha256(
        self, artifact_id: str, *, expected_observation_id: str | None = None
    ) -> str:
        """Stream and persist a full GGUF digest only when an artifact is selected."""
        with self._connection() as connection:
            row = connection.execute(
                """SELECT a.path, a.current_observation_id, o.status,
                          o.file_size_bytes, o.file_modified_at_utc,
                          o.metadata_prefix_sha256
                   FROM artifacts AS a
                   JOIN artifact_observations AS o
                     ON o.observation_id = a.current_observation_id
                   WHERE a.artifact_id = ? AND a.kind = 'gguf_candidate'""",
                (artifact_id,),
            ).fetchone()
        if row is None or row["status"] != "metadata_read":
            raise RegistryError("Full hashing requires a current, parsed GGUF artifact.")
        observation_id = row["current_observation_id"]
        if expected_observation_id is not None and observation_id != expected_observation_id:
            raise RegistryError("The GGUF observation changed before full hashing.")

        path = Path(row["path"])
        try:
            before = path.lstat()
            if path.is_symlink() or not path.is_file() or not stat.S_ISREG(before.st_mode):
                raise RegistryError("Full hashing requires a regular, non-linked GGUF file.")
            if (
                before.st_size != row["file_size_bytes"]
                or _file_modified_text(before.st_mtime) != row["file_modified_at_utc"]
            ):
                raise RegistryError("The GGUF file changed after the inventory scan.")
        except OSError as exc:
            raise RegistryError("The selected GGUF file is no longer readable.") from exc

        parsed = self._reader.read(path)
        if (
            not parsed.ok
            or parsed.metadata is None
            or parsed.metadata.metadata_prefix_sha256 != row["metadata_prefix_sha256"]
        ):
            raise RegistryError("The GGUF metadata prefix changed after the inventory scan.")

        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            after = path.lstat()
        except OSError as exc:
            raise RegistryError("The selected GGUF file could not be fully hashed.") from exc
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or getattr(after, "st_ctime_ns", None) != getattr(before, "st_ctime_ns", None)
        ):
            raise RegistryError("The GGUF file changed while its full hash was being computed.")
        full_hash = digest.hexdigest()
        verified_at = _timestamp_text(datetime.now(timezone.utc))
        try:
            with self._connection() as connection, connection:
                current = connection.execute(
                    """SELECT a.current_observation_id, o.file_size_bytes,
                              o.file_modified_at_utc
                       FROM artifacts AS a JOIN artifact_observations AS o
                         ON o.observation_id = a.current_observation_id
                       WHERE a.artifact_id = ?""",
                    (artifact_id,),
                ).fetchone()
                if (
                    current is None
                    or current["current_observation_id"] != observation_id
                    or current["file_size_bytes"] != after.st_size
                    or current["file_modified_at_utc"] != _file_modified_text(after.st_mtime)
                ):
                    raise RegistryError("The GGUF observation changed before hash persistence.")
                previous = connection.execute(
                    """SELECT sha256 FROM artifact_full_hashes
                       WHERE artifact_id = ? AND observation_id = ?""",
                    (artifact_id, observation_id),
                ).fetchone()
                if previous is not None and previous["sha256"] != full_hash:
                    raise RegistryError("The GGUF bytes changed since the stored full hash.")
                connection.execute(
                    """INSERT OR IGNORE INTO artifact_full_hashes (
                           artifact_id, observation_id, file_size_bytes,
                           file_modified_at_utc, sha256, verified_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        artifact_id,
                        observation_id,
                        after.st_size,
                        _file_modified_text(after.st_mtime),
                        full_hash,
                        verified_at,
                    ),
                )
        except sqlite3.Error as exc:
            raise RegistryStorageError(f"SQLite full-hash transaction failed: {exc}") from exc
        return full_hash

    def ensure_multimodal_pair_full_hashes(self, pair_id: str) -> tuple[str, str]:
        """Hash both files for one explicitly selected, current pairing candidate."""
        with self._connection() as connection:
            pair = connection.execute(
                """SELECT modality, model_artifact_id, projector_artifact_id,
                          model_metadata_sha256, projector_metadata_sha256
                   FROM multimodal_pairings WHERE pair_id = ?""",
                (pair_id,),
            ).fetchone()
        if pair is None or pair["modality"] not in {"image", "audio"}:
            raise RegistryError("The selected multimodal pair is unavailable.")
        model_hash = self.ensure_full_file_sha256(pair["model_artifact_id"])
        projector_hash = self.ensure_full_file_sha256(pair["projector_artifact_id"])
        observed_at = _timestamp_text(datetime.now(timezone.utc))
        try:
            with self._connection() as connection, connection:
                current = connection.execute(
                    """SELECT p.model_metadata_sha256, p.projector_metadata_sha256,
                              m.metadata_prefix_sha256 AS current_model_metadata,
                              x.metadata_prefix_sha256 AS current_projector_metadata
                       FROM multimodal_pairings AS p
                       JOIN artifacts AS ma ON ma.artifact_id = p.model_artifact_id
                       JOIN artifact_observations AS m
                         ON m.observation_id = ma.current_observation_id
                       JOIN artifacts AS pa ON pa.artifact_id = p.projector_artifact_id
                       JOIN artifact_observations AS x
                         ON x.observation_id = pa.current_observation_id
                       WHERE p.pair_id = ? AND p.model_artifact_id = ?
                         AND p.projector_artifact_id = ?""",
                    (pair_id, pair["model_artifact_id"], pair["projector_artifact_id"]),
                ).fetchone()
                if (
                    current is None
                    or current["model_metadata_sha256"] != current["current_model_metadata"]
                    or current["projector_metadata_sha256"] != current["current_projector_metadata"]
                ):
                    raise RegistryError("The selected pair changed since its current metadata observation.")
                connection.execute(
                    """UPDATE multimodal_pairings
                       SET model_full_file_sha256 = ?, projector_full_file_sha256 = ?,
                           observed_at = ?
                       WHERE pair_id = ?""",
                    (model_hash, projector_hash, observed_at, pair_id),
                )
        except sqlite3.Error as exc:
            raise RegistryStorageError(f"SQLite pair-hash transaction failed: {exc}") from exc
        return model_hash, projector_hash

    def list_artifacts(self) -> tuple[ArtifactRecord, ...]:
        """Return artifacts ordered by kind and absolute path."""
        query = """SELECT a.artifact_id, a.kind, a.path, a.source_root,
                    a.first_seen_at, a.last_seen_at, a.current_observation_id,
                    o.status, o.identity_status, o.model_id,
                    o.metadata_prefix_sha256, o.parse_issue_code,
                    o.file_size_bytes, o.file_modified_at_utc, o.parser_version,
                    COALESCE(o.capability_evidence_json, '{}') AS capability_evidence_json
                FROM artifacts AS a
                LEFT JOIN artifact_observations AS o
                  ON o.observation_id = a.current_observation_id
                ORDER BY a.kind, a.path COLLATE NOCASE, a.path"""
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(ArtifactRecord(**dict(row)) for row in rows)

    def list_models(self) -> tuple[ModelRecord, ...]:
        """Return metadata-derived model identities, not local file variants."""
        query = """SELECT model_id, identity_schema, display_name, normalized_name,
                    architecture, normalized_architecture, organization,
                    normalized_organization, identity_status, first_seen_at,
                    last_seen_at
                FROM models
                ORDER BY normalized_architecture, normalized_name, model_id"""
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(ModelRecord(**dict(row)) for row in rows)

    def list_observations(
        self, artifact_id: str | None = None
    ) -> tuple[ArtifactObservationRecord, ...]:
        """Return provenance records, optionally scoped to one artifact ID."""
        columns = """observation_id, artifact_id, model_id, first_seen_at,
                    last_seen_at, file_size_bytes, file_modified_at_utc, status,
                    identity_status, metadata_prefix_sha256,
                    parsed_file_size_bytes, parsed_file_modified_at_utc,
                    metadata_end_offset, gguf_version, tensor_count, metadata_count,
                    selected_metadata_json, identity_evidence_json,
                    identity_uncertainty_json, parse_issue_code,
                    parse_issue_offset, parse_issue_detail, parser_version,
                    capability_evidence_json"""
        if artifact_id is None:
            query = f"SELECT {columns} FROM artifact_observations ORDER BY artifact_id, first_seen_at, observation_id"
            parameters: tuple[str, ...] = ()
        else:
            query = f"SELECT {columns} FROM artifact_observations WHERE artifact_id = ? ORDER BY first_seen_at, observation_id"
            parameters = (artifact_id,)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(ArtifactObservationRecord(**dict(row)) for row in rows)

    def query_catalog(
        self, query: CatalogQuery | None = None
    ) -> tuple[ModelCatalogEntry, ...]:
        """Return one typed view of identities, snapshots and current evidence.

        Runtime evidence is included only when it matches the current artifact
        fingerprint; older file hashes remain in SQLite for audit but are not
        surfaced as current qualifications.
        """
        query = query or CatalogQuery()
        if query.modality not in {None, "text", "image", "audio"}:
            raise ValueError("Unsupported catalog modality.")
        if query.backend not in {None, "cpu", "cuda", "vulkan"}:
            raise ValueError("Unsupported runtime backend.")
        run_artifact_ids: set[str] | None = None
        if query.run_id is not None:
            with self._connection() as connection:
                run_row = connection.execute(
                    "SELECT run_id FROM discovery_runs WHERE run_id = ?",
                    (query.run_id,),
                ).fetchone()
                if run_row is None:
                    return ()
                run_artifact_ids = {
                    row["artifact_id"]
                    for row in connection.execute(
                        "SELECT artifact_id FROM discovery_run_artifacts WHERE run_id = ?",
                        (query.run_id,),
                    ).fetchall()
                }
        artifacts = self.list_artifacts()
        observations = self.list_observations()
        observations_by_id = {item.observation_id: item for item in observations}
        current_observations = {
            item.artifact_id: observations_by_id.get(item.current_observation_id or "")
            for item in artifacts
        }
        models = {item.model_id: item for item in self.list_models()}
        with self._connection() as connection:
            full_hash_rows = connection.execute(
                """SELECT h.artifact_id, h.observation_id, h.sha256
                   FROM artifact_full_hashes AS h
                   JOIN artifacts AS a ON a.artifact_id = h.artifact_id
                   JOIN artifact_observations AS o
                     ON o.observation_id = a.current_observation_id
                   WHERE h.observation_id = a.current_observation_id
                     AND h.file_size_bytes = o.file_size_bytes
                     AND h.file_modified_at_utc = o.file_modified_at_utc"""
            ).fetchall()
            qualification_rows = connection.execute(
                """SELECT qualification_id, artifact_id, model_id,
                          artifact_fingerprint, backend, runtime_id, runtime_version,
                          settings_profile_hash, settings_profile_json,
                          resource_profile_json, modality, status, observed_at,
                          source, evidence_json, model_full_file_sha256,
                          projector_artifact_id, projector_full_file_sha256,
                          device_physical_id, runtime_parameters_json
                   FROM runtime_qualifications
                   ORDER BY observed_at DESC, qualification_id DESC"""
            ).fetchall()
            pair_rows = connection.execute(
                """SELECT pair_id, modality, model_artifact_id,
                          projector_artifact_id, model_metadata_sha256,
                          projector_metadata_sha256, status, compatibility_status,
                          runtime_test_status, runtime_id, runtime_version,
                          observed_at, evidence_json, model_full_file_sha256,
                          projector_full_file_sha256
                   FROM multimodal_pairings
                   ORDER BY observed_at DESC, pair_id DESC"""
            ).fetchall()
        full_hashes_by_artifact = {
            row["artifact_id"]: row["sha256"] for row in full_hash_rows
        }
        qualifications_by_artifact: dict[str, list[RuntimeQualificationRecord]] = {}
        latest: dict[tuple[str, ...], RuntimeQualificationRecord] = {}
        for row in qualification_rows:
            item = RuntimeQualificationRecord(**dict(row))
            key = (
                item.artifact_id,
                item.artifact_fingerprint,
                item.backend,
                item.runtime_id,
                item.runtime_version,
                item.settings_profile_hash,
                item.modality,
                item.model_full_file_sha256 or "",
                item.projector_artifact_id or "",
                item.projector_full_file_sha256 or "",
                item.device_physical_id or "",
                item.runtime_parameters_json,
            )
            latest.setdefault(key, item)
        for item in latest.values():
            qualifications_by_artifact.setdefault(item.artifact_id, []).append(item)
        pairings = tuple(MultimodalPairRecord(**dict(row)) for row in pair_rows)
        pairings_by_model: dict[str, list[MultimodalPairRecord]] = {}
        for item in pairings:
            model_observation = current_observations.get(item.model_artifact_id)
            projector_observation = current_observations.get(item.projector_artifact_id)
            if (
                model_observation is None
                or projector_observation is None
                or model_observation.metadata_prefix_sha256 != item.model_metadata_sha256
                or projector_observation.metadata_prefix_sha256
                != item.projector_metadata_sha256
                or (
                    item.runtime_test_status == "qualified"
                    and (
                        not item.model_full_file_sha256
                        or not item.projector_full_file_sha256
                        or full_hashes_by_artifact.get(item.model_artifact_id)
                        != item.model_full_file_sha256
                        or full_hashes_by_artifact.get(item.projector_artifact_id)
                        != item.projector_full_file_sha256
                    )
                )
            ):
                continue
            pairings_by_model.setdefault(item.model_artifact_id, []).append(item)

        results: list[ModelCatalogEntry] = []
        for artifact in artifacts:
            if (
                artifact.kind == "llama_executable_candidate"
                and not query.include_runtime_artifacts
            ):
                continue
            if query.root is not None and not _path_is_within(artifact.path, query.root):
                continue
            if run_artifact_ids is not None and artifact.artifact_id not in run_artifact_ids:
                continue
            observation = current_observations.get(artifact.artifact_id)
            if observation is None and artifact.kind == "gguf_candidate":
                continue
            fingerprint = _artifact_fingerprint(observation) if observation else None
            qualifications = tuple(
                item
                for item in qualifications_by_artifact.get(artifact.artifact_id, ())
                if item.artifact_fingerprint == fingerprint
                and (
                    item.status != "qualified"
                    or (
                        item.model_full_file_sha256 is not None
                        and full_hashes_by_artifact.get(artifact.artifact_id)
                        == item.model_full_file_sha256
                        and item.device_physical_id is not None
                    )
                )
                and (query.backend is None or item.backend == query.backend)
                and (query.runtime_id is None or item.runtime_id == query.runtime_id)
                and (query.runtime_version is None or item.runtime_version == query.runtime_version)
                and (
                    query.settings_profile_hash is None
                    or item.settings_profile_hash == query.settings_profile_hash
                )
                and (query.modality is None or item.modality == query.modality)
            )
            if query.require_qualified and not any(
                item.status == "qualified" for item in qualifications
            ):
                continue
            selected_metadata = _json_pairs(
                observation.selected_metadata_json if observation else "{}"
            )
            capability_json = (
                observation.capability_evidence_json if observation else "{}"
            )
            current_pairs = tuple(pairings_by_model.get(artifact.artifact_id, ()))
            if query.modality == "image" and _capability_role(capability_json, "image") == "not_indicated" and not any(
                item.modality == "image" for item in current_pairs
            ):
                continue
            if query.modality == "audio" and _capability_role(capability_json, "audio") == "not_indicated" and not any(
                item.modality == "audio" for item in current_pairs
            ):
                continue
            results.append(
                ModelCatalogEntry(
                    artifact=artifact,
                    model=models.get(artifact.model_id or ""),
                    observation=observation,
                    selected_metadata=selected_metadata,
                    capability_evidence_json=capability_json,
                    runtime_qualifications=qualifications,
                    multimodal_pairings=current_pairs,
                    full_file_sha256=full_hashes_by_artifact.get(artifact.artifact_id),
                )
            )
        return tuple(results)

    def record_runtime_qualification(
        self,
        artifact_id: str,
        *,
        backend: str,
        runtime_id: str,
        runtime_version: str,
        settings_profile: dict[str, Any],
        resource_profile: dict[str, Any],
        modality: str = "text",
        status: str,
        source: str,
        evidence: dict[str, Any],
        model_full_file_sha256: str | None = None,
        projector_artifact_id: str | None = None,
        projector_full_file_sha256: str | None = None,
        device_physical_id: str | None = None,
        runtime_parameters: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> RuntimeQualificationRecord:
        if backend not in {"cpu", "cuda", "vulkan"}:
            raise ValueError("Unsupported runtime backend.")
        if modality not in {"text", "image", "audio"}:
            raise ValueError("Unsupported modality.")
        if status not in {
            "runtime_started",
            "runtime_tested",
            "failed",
            "timeout",
            "not_tested",
        }:
            raise ValueError("Unsupported runtime qualification status.")
        if not runtime_id.strip() or not runtime_version.strip() or not source.strip():
            raise ValueError("Runtime identity and evidence source are required.")
        if status == "runtime_tested":
            if not model_full_file_sha256 or not device_physical_id or not device_physical_id.strip():
                raise RegistryError(
                    "A runtime test requires the current full GGUF hash and physical device identity."
                )
            build_hash = runtime_version.removeprefix("sha256:")
            if len(build_hash) != 64 or any(char not in "0123456789abcdef" for char in build_hash):
                raise RegistryError("A runtime test requires the exact SHA-256 runtime build identity.")
            if modality in {"image", "audio"} and (
                not projector_artifact_id or not projector_full_file_sha256
            ):
                raise RegistryError("Multimodal qualification requires the selected projector and its full hash.")
            if modality == "text" and (
                projector_artifact_id is not None or projector_full_file_sha256 is not None
            ):
                raise RegistryError("Text-only qualification cannot carry a projector binding.")
        settings_json = _canonical_json(settings_profile)
        resource_json = _canonical_json(resource_profile)
        runtime_parameters_json = _canonical_json(runtime_parameters or {})
        evidence_json = _canonical_json(evidence)
        settings_hash = hashlib.sha256(settings_json.encode("utf-8")).hexdigest()
        timestamp = _timestamp_text(observed_at or datetime.now(timezone.utc))
        with self._connection() as connection, connection:
            row = connection.execute(
                """SELECT o.observation_id, o.model_id, o.file_size_bytes, o.file_modified_at_utc,
                          o.metadata_prefix_sha256, o.parser_version, o.status
                   FROM artifacts AS a
                   JOIN artifact_observations AS o
                     ON o.observation_id = a.current_observation_id
                   WHERE a.artifact_id = ?""",
                (artifact_id,),
            ).fetchone()
            if row is None or row["status"] != "metadata_read":
                raise RegistryError("Qualification requires a current, successfully parsed GGUF observation.")
            if status == "runtime_tested":
                hash_row = connection.execute(
                    """SELECT sha256 FROM artifact_full_hashes
                       WHERE artifact_id = ? AND observation_id = ?""",
                    (artifact_id, row["observation_id"]),
                ).fetchone()
                if hash_row is None or hash_row["sha256"] != model_full_file_sha256:
                    raise RegistryError("The runtime test is not bound to the current full GGUF hash.")
                if modality in {"image", "audio"}:
                    pair_row = connection.execute(
                        """SELECT pair_id FROM multimodal_pairings
                           WHERE modality = ? AND model_artifact_id = ?
                             AND projector_artifact_id = ?
                             AND model_full_file_sha256 = ?
                             AND projector_full_file_sha256 = ?
                             AND runtime_test_status = 'not_tested'
                           ORDER BY observed_at DESC LIMIT 1""",
                        (
                            modality,
                            artifact_id,
                            projector_artifact_id,
                            model_full_file_sha256,
                            projector_full_file_sha256,
                        ),
                    ).fetchone()
                    if pair_row is None:
                        raise RegistryError("The multimodal runtime test is not bound to a hashed current pair.")
                    projector_hash_row = connection.execute(
                        """SELECT h.sha256
                           FROM artifacts AS a
                           JOIN artifact_observations AS o
                             ON o.observation_id = a.current_observation_id
                           JOIN artifact_full_hashes AS h
                             ON h.artifact_id = a.artifact_id
                            AND h.observation_id = o.observation_id
                           WHERE a.artifact_id = ?""",
                        (projector_artifact_id,),
                    ).fetchone()
                    if (
                        projector_hash_row is None
                        or projector_hash_row["sha256"] != projector_full_file_sha256
                    ):
                        raise RegistryError("The projector test hash is not current.")
            fingerprint = _snapshot_fingerprint(
                row["file_size_bytes"],
                row["file_modified_at_utc"],
                row["metadata_prefix_sha256"],
                row["parser_version"],
            )
            qualification_id = hashlib.sha256(
                _canonical_json(
                    {
                        "artifact_id": artifact_id,
                        "fingerprint": fingerprint,
                        "backend": backend,
                        "runtime_id": runtime_id,
                        "runtime_version": runtime_version,
                        "settings_hash": settings_hash,
                        "modality": modality,
                        "model_full_file_sha256": model_full_file_sha256,
                        "projector_artifact_id": projector_artifact_id,
                        "projector_full_file_sha256": projector_full_file_sha256,
                        "device_physical_id": device_physical_id,
                        "runtime_parameters_json": runtime_parameters_json,
                        "status": status,
                        "observed_at": timestamp,
                        "source": source,
                        "evidence": evidence,
                    }
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """INSERT INTO runtime_qualifications (
                    qualification_id, artifact_id, model_id, artifact_fingerprint,
                    backend, runtime_id, runtime_version, settings_profile_hash,
                    settings_profile_json, resource_profile_json, modality, status,
                    observed_at, source, evidence_json, model_full_file_sha256,
                    projector_artifact_id, projector_full_file_sha256,
                    device_physical_id, runtime_parameters_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    qualification_id,
                    artifact_id,
                    row["model_id"],
                    fingerprint,
                    backend,
                    runtime_id,
                    runtime_version,
                    settings_hash,
                    settings_json,
                    resource_json,
                    modality,
                    status,
                    timestamp,
                    source,
                    evidence_json,
                    model_full_file_sha256,
                    projector_artifact_id,
                    projector_full_file_sha256,
                    device_physical_id,
                    runtime_parameters_json,
                ),
            )
            if status == "runtime_tested":
                tested_rows = connection.execute(
                    """SELECT source, evidence_json FROM runtime_qualifications
                       WHERE artifact_id = ? AND artifact_fingerprint = ?
                         AND backend = ? AND runtime_id = ? AND runtime_version = ?
                         AND settings_profile_hash = ? AND modality = ?
                         AND model_full_file_sha256 IS ?
                         AND projector_artifact_id IS ?
                         AND projector_full_file_sha256 IS ?
                         AND device_physical_id IS ?
                         AND runtime_parameters_json = ?
                         AND status = 'runtime_tested'""",
                    (
                        artifact_id,
                        fingerprint,
                        backend,
                        runtime_id,
                        runtime_version,
                        settings_hash,
                        modality,
                        model_full_file_sha256,
                        projector_artifact_id,
                        projector_full_file_sha256,
                        device_physical_id,
                        runtime_parameters_json,
                    ),
                ).fetchall()
                successful_answers: dict[str, dict[str, str]] = {}
                for tested_row in tested_rows:
                    if tested_row["source"] != "managed-chat-complete-response":
                        continue
                    try:
                        tested_evidence = json.loads(tested_row["evidence_json"])
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(tested_evidence, dict):
                        continue
                    run_id = tested_evidence.get("run_id")
                    message_id = tested_evidence.get("response_message_id")
                    answer_sha256 = tested_evidence.get("response_sha256")
                    if (
                        isinstance(run_id, str)
                        and run_id
                        and isinstance(message_id, str)
                        and message_id
                        and isinstance(answer_sha256, str)
                        and len(answer_sha256) == 64
                        and all(char in "0123456789abcdef" for char in answer_sha256)
                        and tested_evidence.get("response_complete") is True
                        and tested_evidence.get("response_persisted") is True
                        and tested_evidence.get("model_observation_id") == row["observation_id"]
                        and tested_evidence.get("runtime_build_sha256")
                        == runtime_version.removeprefix("sha256:")
                    ):
                        successful_answers.setdefault(
                            run_id,
                            {"response_message_id": message_id, "response_sha256": answer_sha256},
                        )
                if (
                    len(successful_answers) >= 2
                    and len({item["response_message_id"] for item in successful_answers.values()})
                    >= 2
                ):
                    qualification_time = max(
                        datetime.now(timezone.utc),
                        (observed_at or datetime.now(timezone.utc)) + timedelta(microseconds=1),
                    )
                    qualified_at = _timestamp_text(qualification_time)
                    qualification_evidence = {
                        "qualification_basis": "two_independent_complete_chat_runs",
                        "required_independent_runs": 2,
                        "successful_answers": {
                            run_id: successful_answers[run_id]
                            for run_id in sorted(successful_answers)
                        },
                        "runtime_build": runtime_version,
                        "settings_profile_hash": settings_hash,
                        "model_full_file_sha256": model_full_file_sha256,
                        "projector_artifact_id": projector_artifact_id,
                        "projector_full_file_sha256": projector_full_file_sha256,
                        "device_physical_id": device_physical_id,
                        "runtime_parameters": json.loads(runtime_parameters_json),
                    }
                    qualified_source = "derived-two-independent-successful-runs"
                    qualified_id = hashlib.sha256(
                        _canonical_json(
                            {
                                "artifact_id": artifact_id,
                                "fingerprint": fingerprint,
                                "backend": backend,
                                "runtime_id": runtime_id,
                                "runtime_version": runtime_version,
                                "settings_hash": settings_hash,
                                "modality": modality,
                                "model_full_file_sha256": model_full_file_sha256,
                                "projector_artifact_id": projector_artifact_id,
                                "projector_full_file_sha256": projector_full_file_sha256,
                                "device_physical_id": device_physical_id,
                                "runtime_parameters_json": runtime_parameters_json,
                                "status": "qualified",
                                "observed_at": qualified_at,
                                "source": qualified_source,
                                "evidence": qualification_evidence,
                            }
                        ).encode("utf-8")
                    ).hexdigest()
                    connection.execute(
                        """INSERT INTO runtime_qualifications (
                            qualification_id, artifact_id, model_id, artifact_fingerprint,
                            backend, runtime_id, runtime_version, settings_profile_hash,
                            settings_profile_json, resource_profile_json, modality, status,
                            observed_at, source, evidence_json, model_full_file_sha256,
                            projector_artifact_id, projector_full_file_sha256,
                            device_physical_id, runtime_parameters_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'qualified', ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            qualified_id,
                            artifact_id,
                            row["model_id"],
                            fingerprint,
                            backend,
                            runtime_id,
                            runtime_version,
                            settings_hash,
                            settings_json,
                            resource_json,
                            modality,
                            qualified_at,
                            qualified_source,
                            _canonical_json(qualification_evidence),
                            model_full_file_sha256,
                            projector_artifact_id,
                            projector_full_file_sha256,
                            device_physical_id,
                            runtime_parameters_json,
                        ),
                    )
                    if modality in {"image", "audio"} and projector_artifact_id is not None:
                        connection.execute(
                            """UPDATE multimodal_pairings
                               SET runtime_test_status = 'qualified', runtime_id = ?,
                                   runtime_version = ?, observed_at = ?, evidence_json = ?
                               WHERE modality = ? AND model_artifact_id = ?
                                 AND projector_artifact_id = ?
                                 AND model_full_file_sha256 = ?
                                 AND projector_full_file_sha256 = ?""",
                            (
                                runtime_id,
                                runtime_version,
                                qualified_at,
                                _canonical_json(qualification_evidence),
                                modality,
                                artifact_id,
                                projector_artifact_id,
                                model_full_file_sha256,
                                projector_full_file_sha256,
                            ),
                        )
        return RuntimeQualificationRecord(
            qualification_id=qualification_id,
            artifact_id=artifact_id,
            model_id=row["model_id"],
            artifact_fingerprint=fingerprint,
            backend=backend,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            settings_profile_hash=settings_hash,
            settings_profile_json=settings_json,
            resource_profile_json=resource_json,
            modality=modality,
            status=status,
            observed_at=timestamp,
            source=source,
            evidence_json=evidence_json,
            model_full_file_sha256=model_full_file_sha256,
            projector_artifact_id=projector_artifact_id,
            projector_full_file_sha256=projector_full_file_sha256,
            device_physical_id=device_physical_id,
            runtime_parameters_json=runtime_parameters_json,
        )

    def record_multimodal_pairings(self, pairings: Iterable[Any]) -> int:
        """Persist metadata-only pair candidates against both current hashes."""
        items = tuple(pairings)
        timestamp = _timestamp_text(datetime.now(timezone.utc))
        stored = 0
        try:
            with self._connection() as connection, connection:
                for pair in items:
                    for artifact_id, expected_hash in (
                        (pair.model_artifact_id, pair.model_metadata_sha256),
                        (pair.projector_artifact_id, pair.projector_metadata_sha256),
                    ):
                        row = connection.execute(
                            """SELECT o.metadata_prefix_sha256
                               FROM artifacts AS a JOIN artifact_observations AS o
                                 ON o.observation_id = a.current_observation_id
                               WHERE a.artifact_id = ?""",
                            (artifact_id,),
                        ).fetchone()
                        if row is None or row["metadata_prefix_sha256"] != expected_hash:
                            raise RegistryError("Pair candidate does not match both current metadata hashes.")
                    evidence = {
                        "evidence": list(pair.evidence),
                        "runtime_verified": bool(pair.runtime_verified),
                    }
                    connection.execute(
                        """INSERT INTO multimodal_pairings (
                            pair_id, modality, model_artifact_id, projector_artifact_id,
                            model_metadata_sha256, projector_metadata_sha256, status,
                            compatibility_status, runtime_test_status, runtime_id,
                            runtime_version, observed_at, evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                        ON CONFLICT(pair_id) DO UPDATE SET
                            status = excluded.status,
                            compatibility_status = excluded.compatibility_status,
                            observed_at = excluded.observed_at,
                            evidence_json = excluded.evidence_json""",
                        (
                            pair.pair_id,
                            pair.modality,
                            pair.model_artifact_id,
                            pair.projector_artifact_id,
                            pair.model_metadata_sha256,
                            pair.projector_metadata_sha256,
                            pair.status,
                            pair.compatibility_status,
                            "qualified" if pair.runtime_verified else "not_tested",
                            timestamp,
                            _canonical_json(evidence),
                        ),
                    )
                    stored += 1
        except sqlite3.Error as exc:
            raise RegistryStorageError(f"SQLite pairing transaction failed: {exc}") from exc
        return stored

    def _prepare(self, artifact: LocalArtifact) -> _ObservationInput:
        if artifact.kind != "gguf_candidate":
            is_runtime = artifact.kind == "llama_executable_candidate"
            return _ObservationInput(
                artifact=artifact,
                model_id=None,
                status="runtime_candidate" if is_runtime else "non_gguf_format_candidate",
                identity_status="not_applicable",
                metadata_prefix_sha256=None,
                parsed_file_size_bytes=None,
                parsed_file_modified_at_utc=None,
                metadata_end_offset=None,
                gguf_version=None,
                tensor_count=None,
                metadata_count=None,
                selected_metadata_json="{}",
                identity_evidence_json="[]",
                identity_uncertainty_json="[]",
                parse_issue_code=None,
                parse_issue_offset=None,
                parse_issue_detail=None,
                parser_version="not_applicable",
                capability_evidence_json=_canonical_json(
                    {
                        "status": "runtime_candidate" if is_runtime else "unsupported_format_not_startable",
                        "artifact_kind": artifact.kind,
                    }
                ),
                identity=None,
            )

        result = self._reader.read(artifact.path)
        if not result.ok or result.metadata is None:
            issue = result.issue
            return _ObservationInput(
                artifact=artifact,
                model_id=None,
                status="metadata_parse_failed",
                identity_status="unresolved",
                metadata_prefix_sha256=None,
                parsed_file_size_bytes=None,
                parsed_file_modified_at_utc=None,
                metadata_end_offset=None,
                gguf_version=None,
                tensor_count=None,
                metadata_count=None,
                selected_metadata_json="{}",
                identity_evidence_json="[]",
                identity_uncertainty_json=_canonical_json(["metadata_parse_failed"]),
                parse_issue_code=issue.code if issue else "unknown_parse_failure",
                parse_issue_offset=issue.offset if issue else None,
                parse_issue_detail=issue.detail if issue else "GGUF reader returned no metadata.",
                parser_version=GGUF_METADATA_PARSER_VERSION,
                capability_evidence_json=_canonical_json(
                    {
                        "parser_version": GGUF_METADATA_PARSER_VERSION,
                        "status": "parse_failed",
                        "issue_code": issue.code if issue else "unknown_parse_failure",
                    }
                ),
                identity=None,
            )

        metadata = result.metadata
        identity = normalize_gguf_identity(metadata)
        inventory_mtime = _timestamp_text(artifact.modified_at)
        parsed_mtime = _timestamp_text(metadata.modified_at)
        snapshot_mismatch = (
            artifact.size_bytes != metadata.file_size_bytes
            or inventory_mtime != parsed_mtime
        )
        if snapshot_mismatch:
            capability_evidence_json = _canonical_json(
                {"parser_version": GGUF_METADATA_PARSER_VERSION, "status": "stale_snapshot"}
            )
        else:
            from .capability_evidence import extract_gguf_capability_evidence

            capability_evidence_json = _serialize_capability_profile(
                extract_gguf_capability_evidence(metadata)
            )
        return _ObservationInput(
            artifact=artifact,
            model_id=None if snapshot_mismatch else identity.model_id,
            status="artifact_changed_during_scan" if snapshot_mismatch else "metadata_read",
            identity_status="unresolved" if snapshot_mismatch else identity.status,
            metadata_prefix_sha256=metadata.metadata_prefix_sha256,
            parsed_file_size_bytes=metadata.file_size_bytes,
            parsed_file_modified_at_utc=parsed_mtime,
            metadata_end_offset=metadata.metadata_end_offset,
            gguf_version=metadata.version,
            tensor_count=metadata.tensor_count,
            metadata_count=metadata.metadata_count,
            selected_metadata_json=_canonical_json(dict(identity.selected_metadata)),
            identity_evidence_json=_canonical_json(list(identity.evidence) if not snapshot_mismatch else []),
            identity_uncertainty_json=_canonical_json(
                list(identity.uncertainty)
                if not snapshot_mismatch
                else ["artifact_changed_during_scan"]
            ),
            parse_issue_code="artifact_changed_during_scan" if snapshot_mismatch else None,
            parse_issue_offset=None,
            parse_issue_detail=(
                "Inventory file size or modification time differs from the successfully parsed file; identity was not attached."
                if snapshot_mismatch
                else None
            ),
            parser_version=GGUF_METADATA_PARSER_VERSION,
            capability_evidence_json=capability_evidence_json,
            identity=None if snapshot_mismatch else identity,
        )

    def _upsert_observation(
        self,
        connection: sqlite3.Connection,
        item: _ObservationInput,
        observed_at: str,
    ) -> None:
        artifact = item.artifact
        identity = item.identity
        if item.model_id is not None and identity is not None:
            connection.execute(
                """INSERT INTO models (
                    model_id, identity_schema, display_name, normalized_name,
                    architecture, normalized_architecture, organization,
                    normalized_organization, identity_status, first_seen_at,
                    last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id) DO UPDATE SET
                    identity_status = CASE
                        WHEN excluded.identity_status = 'resolved' THEN 'resolved'
                        ELSE models.identity_status
                    END,
                    last_seen_at = MAX(models.last_seen_at, excluded.last_seen_at)""",
                (
                    item.model_id,
                    _IDENTITY_SCHEMA,
                    identity.display_name,
                    identity.normalized_name,
                    identity.architecture,
                    identity.normalized_architecture,
                    identity.organization,
                    identity.normalized_organization,
                    identity.status,
                    observed_at,
                    observed_at,
                ),
            )

        observation_id = _observation_id(item)
        connection.execute(
            """INSERT INTO artifacts (
                artifact_id, kind, path, source_root, first_seen_at, last_seen_at,
                current_observation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                kind = excluded.kind,
                path = excluded.path,
                source_root = excluded.source_root,
                last_seen_at = MAX(artifacts.last_seen_at, excluded.last_seen_at),
                current_observation_id = excluded.current_observation_id""",
            (
                artifact.artifact_id,
                artifact.kind,
                artifact.path,
                artifact.source_root,
                observed_at,
                observed_at,
                observation_id,
            ),
        )
        connection.execute(
            """INSERT INTO artifact_observations (
                observation_id, artifact_id, model_id, first_seen_at, last_seen_at,
                file_size_bytes, file_modified_at_utc, status, identity_status,
                metadata_prefix_sha256, parsed_file_size_bytes,
                parsed_file_modified_at_utc, metadata_end_offset, gguf_version,
                tensor_count, metadata_count, selected_metadata_json,
                identity_evidence_json, identity_uncertainty_json, parse_issue_code,
                parse_issue_offset, parse_issue_detail, parser_version,
                capability_evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(observation_id) DO UPDATE SET
                last_seen_at = MAX(artifact_observations.last_seen_at, excluded.last_seen_at)""",
            (
                observation_id,
                artifact.artifact_id,
                item.model_id,
                observed_at,
                observed_at,
                artifact.size_bytes,
                _timestamp_text(artifact.modified_at),
                item.status,
                item.identity_status,
                item.metadata_prefix_sha256,
                item.parsed_file_size_bytes,
                item.parsed_file_modified_at_utc,
                item.metadata_end_offset,
                item.gguf_version,
                item.tensor_count,
                item.metadata_count,
                item.selected_metadata_json,
                item.identity_evidence_json,
                item.identity_uncertainty_json,
                item.parse_issue_code,
                item.parse_issue_offset,
                item.parse_issue_detail,
                item.parser_version,
                item.capability_evidence_json,
            ),
        )

    def _initialize_schema(self) -> None:
        try:
            with self._connection() as connection, connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )"""
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                version = int(row["version"])
                if version > _SCHEMA_VERSION:
                    raise RegistrySchemaError(
                        f"Registry schema version {version} is newer than supported {_SCHEMA_VERSION}."
                    )
                if version < 1:
                    for statement in _MIGRATION_1:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (1, _timestamp_text(datetime.now(timezone.utc))),
                    )
                    version = 1
                if version < 2:
                    for statement in _MIGRATION_2:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (2, _timestamp_text(datetime.now(timezone.utc))),
                    )
                    version = 2
                if version < 3:
                    for statement in _MIGRATION_3:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (3, _timestamp_text(datetime.now(timezone.utc))),
                    )
                    version = 3
                if version < 4:
                    for statement in _MIGRATION_4:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (4, _timestamp_text(datetime.now(timezone.utc))),
                    )
                    version = 4
                if version < 5:
                    for statement in _MIGRATION_5:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (5, _timestamp_text(datetime.now(timezone.utc))),
                    )
        except sqlite3.Error as exc:
            raise RegistryStorageError(f"Could not initialize SQLite registry: {exc}") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.database_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()


def _metadata_text(field: Any) -> str | None:
    if field is None or field.type_name != "string" or not isinstance(field.value, str):
        return None
    cleaned = unicodedata.normalize("NFKC", field.value)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _normalize_identity_component(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split()).casefold()
    return normalized or None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _root_values(roots: Iterable[RootInput] | RootInput | None) -> tuple[RootInput, ...]:
    if roots is None:
        return ()
    if isinstance(roots, (str, os.PathLike)):
        return (roots,)
    try:
        return tuple(roots)
    except TypeError:
        return (roots,)


def _roots_json(
    model_roots: Iterable[RootInput], runtime_roots: Iterable[RootInput]
) -> str:
    def normalize(value: RootInput) -> str:
        try:
            return os.path.abspath(os.fspath(Path(value).expanduser()))
        except (OSError, RuntimeError, TypeError, ValueError):
            return f"<invalid:{type(value).__name__}:{value!s}>"

    return _canonical_json(
        {
            "model_roots": [normalize(item) for item in model_roots],
            "runtime_roots": [normalize(item) for item in runtime_roots],
        }
    )


def _json_pairs(value: str) -> tuple[tuple[str, Any], ...]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    return tuple(sorted(parsed.items()))


def _capability_role(value: str, modality: str) -> str:
    try:
        parsed = json.loads(value)
        entry = parsed.get(modality, {})
        role = entry.get("role") if isinstance(entry, dict) else None
    except (AttributeError, TypeError, json.JSONDecodeError):
        role = None
    return role if isinstance(role, str) else "not_indicated"


def _snapshot_fingerprint(
    file_size_bytes: int,
    file_modified_at_utc: str,
    metadata_prefix_sha256: str | None,
    parser_version: str,
) -> str:
    material = _canonical_json(
        {
            "file_size_bytes": file_size_bytes,
            "file_modified_at_utc": file_modified_at_utc,
            "metadata_prefix_sha256": metadata_prefix_sha256,
            "parser_version": parser_version,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _artifact_fingerprint(observation: ArtifactObservationRecord | None) -> str | None:
    if observation is None or observation.status != "metadata_read":
        return None
    return _snapshot_fingerprint(
        observation.file_size_bytes,
        observation.file_modified_at_utc,
        observation.metadata_prefix_sha256,
        observation.parser_version,
    )


def _path_is_within(path: str, root: str) -> bool:
    try:
        candidate = os.path.normcase(os.path.abspath(path))
        boundary = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath((candidate, boundary)) == boundary
    except (OSError, ValueError):
        return False


def _serialize_capability_profile(profile: Any) -> str:
    return _canonical_json(
        {
            "parser_version": GGUF_METADATA_PARSER_VERSION,
            "metadata_evidence": [asdict(item) for item in profile.metadata_evidence],
            "issues": [asdict(item) for item in profile.issues],
            "image": {
                "status": profile.image_status,
                "role": profile.image_role,
                "evidence_keys": list(profile.image_evidence_keys),
            },
            "audio": {
                "status": profile.audio_status,
                "role": profile.audio_role,
                "evidence_keys": list(profile.audio_evidence_keys),
            },
            "text_only_status": profile.text_only_status,
            "runtime_test_status": profile.runtime_test_status,
            "runtime_tested_capabilities": list(profile.runtime_tested_capabilities),
            "multimodal_status": profile.multimodal_status,
            "multimodal_verification_requirements": list(
                profile.multimodal_verification_requirements
            ),
        }
    )


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _file_modified_text(timestamp: float) -> str:
    return _timestamp_text(datetime.fromtimestamp(timestamp, timezone.utc))


def _observation_id(item: _ObservationInput) -> str:
    artifact = item.artifact
    identity = {
        "artifact_id": artifact.artifact_id,
        "file_size_bytes": artifact.size_bytes,
        "file_modified_at_utc": _timestamp_text(artifact.modified_at),
        "status": item.status,
        "identity_status": item.identity_status,
        "model_id": item.model_id,
        "metadata_prefix_sha256": item.metadata_prefix_sha256,
        "parser_version": item.parser_version,
        "parse_issue_code": item.parse_issue_code,
        "parsed_file_size_bytes": item.parsed_file_size_bytes,
        "parsed_file_modified_at_utc": item.parsed_file_modified_at_utc,
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"miro-observation-v1-{digest}"
