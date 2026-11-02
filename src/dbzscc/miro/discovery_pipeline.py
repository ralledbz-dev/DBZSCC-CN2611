"""Compose one safe local MIRO discovery pass from existing services.

The registry remains the owner of scanning, GGUF parsing and SQLite writes.
Small recording adapters expose the exact inventory and parse results from
that same pass so capability evidence is never derived from a second scan or
read. No runtime candidate is opened or executed.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .capability_evidence import CapabilityProfile, extract_gguf_capability_evidence
from .gguf_metadata import GGUFMetadataReader, GGUFParseResult
from .local_inventory import (
    InventoryScan,
    LocalArtifact,
    LocalInventoryScanner,
    RootInput,
    ScanIssue,
)
from .model_registry import ArtifactRecord, LocalModelRegistry, RegistrySyncResult


@dataclass(frozen=True)
class LocalDiscoveryArtifact:
    """One discovered candidate, its persisted state and any safe evidence."""

    candidate: LocalArtifact
    registry_record: ArtifactRecord | None
    parse_result: GGUFParseResult | None
    capability_profile: CapabilityProfile | None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalDiscoveryResult:
    """Results from one registry-backed local discovery pass."""

    registry_sync: RegistrySyncResult
    artifacts: tuple[LocalDiscoveryArtifact, ...]
    scanner_issues: tuple[ScanIssue, ...]

    @property
    def has_partial_results(self) -> bool:
        """Whether any scan, parse, identity or evidence issue was observed."""
        if self.scanner_issues:
            return True
        for item in self.artifacts:
            if item.issues or item.registry_record is None:
                return True
            if item.registry_record.status in {
                "metadata_parse_failed",
                "artifact_changed_during_scan",
            }:
                return True
            if item.registry_record.identity_status not in {
                "resolved",
                "not_applicable",
            }:
                return True
            if item.capability_profile is not None and item.capability_profile.issues:
                return True
        return False


class _RecordingScanner:
    """Capture the exact inventory returned to the existing registry service."""

    def __init__(self, scanner: LocalInventoryScanner) -> None:
        self._scanner = scanner
        self.inventory: InventoryScan | None = None

    def scan(
        self,
        model_roots: Iterable[RootInput] = (),
        runtime_roots: Iterable[RootInput] = (),
    ) -> InventoryScan:
        self.inventory = self._scanner.scan(
            model_roots=model_roots,
            runtime_roots=runtime_roots,
        )
        return self.inventory


class _RecordingReader:
    """Capture exact parser results consumed by the existing registry service."""

    def __init__(self, reader: GGUFMetadataReader) -> None:
        self._reader = reader
        self.results: dict[str, GGUFParseResult] = {}

    def read(self, path: str | os.PathLike[str]) -> GGUFParseResult:
        result = self._reader.read(path)
        self.results[_path_key(path)] = result
        return result


def run_local_discovery(
    database_path: str | os.PathLike[str],
    model_roots: Iterable[RootInput] | RootInput = (),
    runtime_roots: Iterable[RootInput] | RootInput = (),
    *,
    observed_at: datetime | None = None,
    scanner: LocalInventoryScanner | None = None,
    reader: GGUFMetadataReader | None = None,
) -> LocalDiscoveryResult:
    """Scan explicit local roots, parse GGUF metadata and persist one registry pass.

    Only ``database_path`` is written. Roots are never inferred. The registry's
    existing synchronization performs the sole scan, metadata reads and atomic
    SQLite upsert. Capability evidence is derived only from that pass's parsed
    documents when its file snapshot matches the inventoried artifact.

    Scanner issues and parse failures are preserved alongside successful items.
    SQLite storage failures retain the registry's existing behavior and are
    raised after its transaction rolls back. Runtime candidates are inventory
    records only; they are not opened or executed.
    """
    recording_scanner = _RecordingScanner(scanner or LocalInventoryScanner())
    recording_reader = _RecordingReader(reader or GGUFMetadataReader())
    registry = LocalModelRegistry(
        database_path,
        scanner=recording_scanner,  # type: ignore[arg-type]
        reader=recording_reader,  # type: ignore[arg-type]
    )
    sync_result = registry.synchronize(
        model_roots=model_roots,
        runtime_roots=runtime_roots,
        observed_at=observed_at,
    )

    inventory = recording_scanner.inventory
    if inventory is None:
        # LocalModelRegistry.synchronize must invoke its injected scanner once.
        raise RuntimeError("Registry synchronization returned without an inventory.")

    records = {item.artifact_id: item for item in registry.list_artifacts()}
    discovered: list[LocalDiscoveryArtifact] = []
    for candidate in inventory.artifacts:
        record = records.get(candidate.artifact_id)
        issues: list[str] = []
        if record is None:
            issues.append("registry_record_missing")
        elif record.path != candidate.path or record.kind != candidate.kind:
            issues.append("registry_record_mismatch")

        parse_result = None
        profile = None
        if candidate.kind == "gguf_candidate":
            parse_result = recording_reader.results.get(_path_key(candidate.path))
            if parse_result is None:
                issues.append("metadata_result_missing")
            elif (parse_result.metadata is None) == (parse_result.issue is None):
                issues.append("inconsistent_parse_result")
            elif (
                parse_result.ok
                and parse_result.metadata is not None
                and record is not None
                and not issues
                and record.status == "metadata_read"
            ):
                profile = extract_gguf_capability_evidence(parse_result.metadata)

        discovered.append(
            LocalDiscoveryArtifact(
                candidate=candidate,
                registry_record=record,
                parse_result=parse_result,
                capability_profile=profile,
                issues=tuple(issues),
            )
        )

    return LocalDiscoveryResult(
        registry_sync=sync_result,
        artifacts=tuple(discovered),
        scanner_issues=inventory.issues,
    )


def _path_key(path: str | os.PathLike[str]) -> str:
    normalized = os.path.abspath(os.fspath(Path(path).expanduser()))
    return os.path.normcase(normalized)
