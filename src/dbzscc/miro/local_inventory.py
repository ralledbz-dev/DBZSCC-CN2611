"""Read-only discovery of local GGUF files and llama.cpp tool candidates.

This module intentionally records filename-based candidates only. It does not
open artifact contents, execute tools, or infer that a discovered file works.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_RUNTIME_TOOL_PREFIX = "llama-"
_RUNTIME_TOOL_SUFFIXES = frozenset(("", ".exe"))

RootInput = Union[str, os.PathLike]


@dataclass(frozen=True)
class LocalArtifact:
    """A local file candidate with path-specific identity and provenance."""

    artifact_id: str
    kind: str
    name: str
    path: str
    source_root: str
    size_bytes: int
    modified_at: datetime
    evidence: str


@dataclass(frozen=True)
class ScanIssue:
    """A non-fatal issue encountered while scanning one configured root."""

    code: str
    root: str
    path: str | None
    detail: str


@dataclass(frozen=True)
class InventoryScan:
    """Immutable result of one local discovery pass."""

    scanned_at: datetime
    artifacts: tuple[LocalArtifact, ...]
    issues: tuple[ScanIssue, ...]


class LocalInventoryScanner:
    """Discover candidate files under explicitly supplied roots, without writes.

    Model roots are searched for regular files ending in ``.gguf``. Runtime
    roots are searched for files named ``llama-*`` with no suffix or a
    ``.exe`` suffix. These naming rules are discovery evidence, not validation.
    Symlinks and Windows reparse points are not traversed.
    """

    def scan(
        self,
        model_roots: Iterable[RootInput] = (),
        runtime_roots: Iterable[RootInput] = (),
    ) -> InventoryScan:
        artifacts: dict[str, LocalArtifact] = {}
        issues = []
        visited_roots = set()

        for category, configured_roots in (
            ("model", model_roots),
            ("runtime", runtime_roots),
        ):
            if isinstance(configured_roots, (str, os.PathLike)):
                roots = (configured_roots,)
            else:
                try:
                    roots = iter(configured_roots)
                except TypeError:
                    issues.append(
                        ScanIssue(
                            code="invalid_root_collection",
                            root=str(configured_roots),
                            path=None,
                            detail="Expected a path or iterable of paths.",
                        )
                    )
                    continue

            for raw_root in roots:
                try:
                    root = Path(raw_root).expanduser()
                    root_display = os.path.abspath(os.fspath(root))
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    issues.append(
                        ScanIssue(
                            code="invalid_root",
                            root=str(raw_root),
                            path=None,
                            detail=_error_detail(exc),
                        )
                    )
                    continue

                root_key = (category, os.path.normcase(root_display))
                if root_key in visited_roots:
                    continue
                visited_roots.add(root_key)

                try:
                    root_stat = root.lstat()
                except FileNotFoundError as exc:
                    issues.append(
                        ScanIssue(
                            code="root_not_found",
                            root=root_display,
                            path=None,
                            detail=_error_detail(exc),
                        )
                    )
                    continue
                except OSError as exc:
                    issues.append(
                        ScanIssue(
                            code="root_stat_failed",
                            root=root_display,
                            path=None,
                            detail=_error_detail(exc),
                        )
                    )
                    continue

                if _is_link_or_reparse_point(root_stat):
                    issues.append(
                        ScanIssue(
                            code="linked_root_skipped",
                            root=root_display,
                            path=root_display,
                            detail="Root is a symlink or filesystem reparse point.",
                        )
                    )
                    continue
                if not stat.S_ISDIR(root_stat.st_mode):
                    issues.append(
                        ScanIssue(
                            code="root_not_directory",
                            root=root_display,
                            path=root_display,
                            detail="Configured root is not a directory.",
                        )
                    )
                    continue

                self._scan_root(
                    root=root,
                    root_display=root_display,
                    category=category,
                    artifacts=artifacts,
                    issues=issues,
                )

        ordered_artifacts = tuple(
            sorted(
                artifacts.values(),
                key=lambda item: (item.kind, item.path.casefold(), item.path),
            )
        )
        return InventoryScan(
            scanned_at=datetime.now(timezone.utc),
            artifacts=ordered_artifacts,
            issues=tuple(issues),
        )

    @staticmethod
    def _scan_root(
        root: Path,
        root_display: str,
        category: str,
        artifacts: dict[str, LocalArtifact],
        issues: list[ScanIssue],
    ) -> None:
        pending_directories = [root]

        while pending_directories:
            directory = pending_directories.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError as exc:
                issues.append(
                    ScanIssue(
                        code="directory_read_failed",
                        root=root_display,
                        path=os.path.abspath(os.fspath(directory)),
                        detail=_error_detail(exc),
                    )
                )
                continue

            entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
            child_directories = []
            for entry in entries:
                try:
                    file_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    issues.append(
                        ScanIssue(
                            code="entry_stat_failed",
                            root=root_display,
                            path=os.path.abspath(entry.path),
                            detail=_error_detail(exc),
                        )
                    )
                    continue

                if _is_link_or_reparse_point(file_stat):
                    continue
                if stat.S_ISDIR(file_stat.st_mode):
                    child_directories.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    continue

                classification = _classify_candidate(entry.name, category)
                if classification is None:
                    continue

                kind, evidence = classification
                absolute_path = os.path.abspath(entry.path)
                artifact_id = _artifact_id(kind, absolute_path)
                artifact = LocalArtifact(
                    artifact_id=artifact_id,
                    kind=kind,
                    name=entry.name,
                    path=absolute_path,
                    source_root=root_display,
                    size_bytes=file_stat.st_size,
                    modified_at=datetime.fromtimestamp(
                        file_stat.st_mtime, tz=timezone.utc
                    ),
                    evidence=evidence,
                )
                # Overlapping configured roots should not duplicate one artifact.
                # The first configured root remains the recorded provenance.
                artifacts.setdefault(artifact_id, artifact)

            pending_directories.extend(reversed(child_directories))


def _classify_candidate(
    filename: str, category: str
) -> tuple[str, str] | None:
    if category == "model" and filename.casefold().endswith(".gguf"):
        return "gguf_candidate", "filename-extension:.gguf"

    if category == "runtime":
        stem, suffix = os.path.splitext(filename)
        if (
            stem.casefold().startswith(_RUNTIME_TOOL_PREFIX)
            and len(stem) > len(_RUNTIME_TOOL_PREFIX)
            and suffix.casefold() in _RUNTIME_TOOL_SUFFIXES
        ):
            return "llama_executable_candidate", "filename-pattern:llama-*"

    return None


def _artifact_id(kind: str, path: str) -> str:
    """Return a stable ID for this path, not a canonical model identity."""
    normalized_path = os.path.normcase(os.path.abspath(path))
    identity = f"{kind}\0{normalized_path}"
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass"))
    return digest.hexdigest()[:24]


def _is_link_or_reparse_point(file_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(file_stat.st_mode):
        return True
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _error_detail(exc: OSError) -> str:
    return getattr(exc, "strerror", None) or type(exc).__name__
