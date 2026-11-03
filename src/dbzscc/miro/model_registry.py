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
import unicodedata
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .gguf_metadata import GGUFMetadataDocument, GGUFMetadataReader
from .local_inventory import (
    LocalArtifact,
    LocalInventoryScanner,
    RootInput,
    ScanIssue,
)

_SCHEMA_VERSION = 1
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


@dataclass(frozen=True)
class RegistrySyncResult:
    """Summary of one inventory/metadata pass and the SQLite upsert."""

    run_id: str
    scanned_at: str
    artifact_count: int
    parse_failure_count: int
    unresolved_identity_count: int
    scanner_issues: tuple[ScanIssue, ...]


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
        self._scanner = scanner or LocalInventoryScanner()
        self._reader = reader or GGUFMetadataReader()
        self._initialize_schema()

    def synchronize(
        self,
        model_roots: Iterable[RootInput] = (),
        runtime_roots: Iterable[RootInput] = (),
        *,
        observed_at: datetime | None = None,
    ) -> RegistrySyncResult:
        """Discover explicit roots, read GGUF prefixes and transactionally upsert.

        A root scan is performed before the SQLite transaction so filesystem
        I/O never holds a database write lock. Parse failures become observations;
        SQLite failures roll back the entire persistence pass and raise
        ``RegistryStorageError``.
        """
        inventory = self._scanner.scan(model_roots=model_roots, runtime_roots=runtime_roots)
        timestamp = _timestamp_text(observed_at or inventory.scanned_at)
        prepared = tuple(self._prepare(artifact) for artifact in inventory.artifacts)
        run_id = uuid.uuid4().hex

        try:
            with self._connection() as connection, connection:
                for observation in prepared:
                    self._upsert_observation(connection, observation, timestamp)
        except sqlite3.Error as exc:
            raise RegistryStorageError(f"SQLite registry transaction failed: {exc}") from exc

        return RegistrySyncResult(
            run_id=run_id,
            scanned_at=timestamp,
            artifact_count=len(prepared),
            parse_failure_count=sum(
                item.status == "metadata_parse_failed" for item in prepared
            ),
            unresolved_identity_count=sum(
                item.status == "metadata_read" and item.identity_status == "unresolved"
                for item in prepared
            ),
            scanner_issues=inventory.issues,
        )

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        """Return one path-specific artifact and its current observation."""
        query = """SELECT a.artifact_id, a.kind, a.path, a.source_root,
                    a.first_seen_at, a.last_seen_at, a.current_observation_id,
                    o.status, o.identity_status, o.model_id,
                    o.metadata_prefix_sha256, o.parse_issue_code
                FROM artifacts AS a
                LEFT JOIN artifact_observations AS o
                  ON o.observation_id = a.current_observation_id
                WHERE a.artifact_id = ?"""
        with self._connection() as connection:
            row = connection.execute(query, (artifact_id,)).fetchone()
        return ArtifactRecord(**dict(row)) if row is not None else None

    def list_artifacts(self) -> tuple[ArtifactRecord, ...]:
        """Return artifacts ordered by kind and absolute path."""
        query = """SELECT a.artifact_id, a.kind, a.path, a.source_root,
                    a.first_seen_at, a.last_seen_at, a.current_observation_id,
                    o.status, o.identity_status, o.model_id,
                    o.metadata_prefix_sha256, o.parse_issue_code
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
                    parse_issue_offset, parse_issue_detail"""
        if artifact_id is None:
            query = f"SELECT {columns} FROM artifact_observations ORDER BY artifact_id, first_seen_at, observation_id"
            parameters: tuple[str, ...] = ()
        else:
            query = f"SELECT {columns} FROM artifact_observations WHERE artifact_id = ? ORDER BY first_seen_at, observation_id"
            parameters = (artifact_id,)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(ArtifactObservationRecord(**dict(row)) for row in rows)

    def _prepare(self, artifact: LocalArtifact) -> _ObservationInput:
        if artifact.kind != "gguf_candidate":
            return _ObservationInput(
                artifact=artifact,
                model_id=None,
                status="runtime_candidate",
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
                parse_issue_offset, parse_issue_detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


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
        "parse_issue_code": item.parse_issue_code,
        "parsed_file_size_bytes": item.parsed_file_size_bytes,
        "parsed_file_modified_at_utc": item.parsed_file_modified_at_utc,
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"miro-observation-v1-{digest}"
