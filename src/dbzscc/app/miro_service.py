"""Application boundary for explicitly requested local MIRO discovery."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from pathlib import Path
from threading import Event
from typing import Any

from dbzscc.miro.discovery_pipeline import LocalDiscoveryResult, run_local_discovery
from dbzscc.miro.gguf_metadata import GGUFMetadataReader
from dbzscc.miro.local_inventory import LocalInventoryScanner, RootInput
from dbzscc.miro.model_registry import (
    CatalogQuery,
    DiscoveryRunRecord,
    LocalModelRegistry,
    ModelCatalogEntry,
    RuntimeQualificationRecord,
)

from .config import application_data_directory

_SCAN_LOCK_GUARD = threading.Lock()
_DATABASE_SCAN_LOCKS: dict[str, threading.Lock] = {}


class MiroScanAlreadyRunningError(RuntimeError):
    """Raised instead of duplicating a scan against the same local registry."""


class MiroApplicationService:
    """Use the existing MIRO scanner, parser and registry without duplication."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else (
            application_data_directory() / "miro-models.sqlite"
        )
        self.database_path = self.database_path.expanduser().absolute()

    def discover(
        self,
        *,
        model_roots: Iterable[RootInput] | RootInput,
        runtime_roots: Iterable[RootInput] | RootInput = (),
        scanner: LocalInventoryScanner | None = None,
        reader: GGUFMetadataReader | None = None,
        cancel_event: Event | None = None,
    ) -> LocalDiscoveryResult:
        models = _root_values(model_roots)
        runtimes = _root_values(runtime_roots)
        if not models and not runtimes:
            raise ValueError("Wähle zuerst mindestens einen Modell- oder Runtime-Ordner.")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        lock = _scan_lock_for(self.database_path)
        if not lock.acquire(blocking=False):
            raise MiroScanAlreadyRunningError(
                "Ein MIRO-Scan für diesen zentralen Katalog läuft bereits."
            )
        try:
            resumable = scanner is None or bool(getattr(scanner, "supports_resume", False))
            resume = (
                LocalModelRegistry(self.database_path).get_resumable_scan(models, runtimes)
                if resumable
                else None
            )
            return run_local_discovery(
                database_path=self.database_path,
                model_roots=models,
                runtime_roots=runtimes,
                scanner=scanner,
                reader=reader,
                resume_token=resume.continuation_token if resume is not None else None,
                run_id=resume.run_id if resume is not None else None,
                cancel_event=cancel_event,
            )
        finally:
            lock.release()

    def query_catalog(
        self, query: CatalogQuery | None = None
    ) -> tuple[ModelCatalogEntry, ...]:
        """Return centrally persisted model records without starting a new scan."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        return LocalModelRegistry(self.database_path).query_catalog(query)

    def ensure_full_file_sha256(
        self, artifact_id: str, *, expected_observation_id: str | None = None
    ) -> str:
        """Hash and persist one explicitly selected model against its observation."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        return LocalModelRegistry(self.database_path).ensure_full_file_sha256(
            artifact_id, expected_observation_id=expected_observation_id
        )

    def ensure_multimodal_pair_full_hashes(self, pair_id: str) -> tuple[str, str]:
        """Hash the model and projector only for the selected current pair."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        return LocalModelRegistry(self.database_path).ensure_multimodal_pair_full_hashes(
            pair_id
        )

    def get_discovery_run(self, run_id: str) -> DiscoveryRunRecord | None:
        """Read a persisted scan summary from the same central registry."""
        return LocalModelRegistry(self.database_path).get_discovery_run(run_id)

    def discover_model_catalog(
        self, root: RootInput, *, cancel_event: Event | None = None
    ) -> tuple[LocalDiscoveryResult, tuple[ModelCatalogEntry, ...]]:
        """Run one recursive scan and return the same persisted root catalog."""
        root_path = os.path.abspath(os.fspath(Path(root).expanduser()))
        result = self.discover(model_roots=(root_path,), cancel_event=cancel_event)
        return result, self.query_catalog(
            CatalogQuery(root=root_path, run_id=result.registry_sync.run_id)
        )

    def find_model_artifact(self, path: str | os.PathLike[str]) -> ModelCatalogEntry | None:
        """Resolve one exact GGUF path from current persisted registry state."""
        normalized = os.path.normcase(os.path.abspath(os.fspath(Path(path).expanduser())))
        root = os.path.dirname(normalized)
        return next(
            (
                entry
                for entry in self.query_catalog(CatalogQuery(root=root))
                if os.path.normcase(os.path.abspath(entry.artifact.path)) == normalized
            ),
            None,
        )

    def record_runtime_qualification(self, artifact_id: str, **evidence: Any) -> RuntimeQualificationRecord:
        """Persist one actual test result using the shared registry schema."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        return LocalModelRegistry(self.database_path).record_runtime_qualification(
            artifact_id, **evidence
        )


def _root_values(roots: Iterable[RootInput] | RootInput) -> tuple[RootInput, ...]:
    if isinstance(roots, (str, os.PathLike)):
        return (roots,)
    return tuple(roots)


def _scan_lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _SCAN_LOCK_GUARD:
        return _DATABASE_SCAN_LOCKS.setdefault(key, threading.Lock())
