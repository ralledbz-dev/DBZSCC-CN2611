"""Read-only discovery of local GGUF files and llama.cpp tool candidates.

This module intentionally records filename-based candidates only. It does not
open artifact contents, execute tools, or infer that a discovered file works.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import time
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
    directories_visited: int = 0
    entries_seen: int = 0
    regular_files_seen: int = 0
    reparse_points_skipped: int = 0
    access_denied_directories: int = 0


@dataclass
class _ScanBudget:
    deadline: float
    entries: int = 0
    candidates: int = 0
    directories_visited: int = 0
    regular_files_seen: int = 0
    reparse_points_skipped: int = 0
    access_denied_directories: int = 0
    exhausted: bool = False
    limit_reported: bool = False


class LocalInventoryScanner:
    """Discover candidate files under explicitly supplied roots, without writes.

    Model roots are searched for regular files ending in ``.gguf``. Runtime
    roots are searched for files named ``llama-*`` with no suffix or a
    ``.exe`` suffix. These naming rules are discovery evidence, not validation.
    Symlinks and Windows reparse points are not traversed.
    """

    def __init__(
        self,
        *,
        max_depth: int | None = None,
        max_entries: int = 100_000,
        max_candidates: int = 2_000,
        max_elapsed_seconds: float = 120.0,
        excluded_directory_names: Iterable[str] = (),
    ) -> None:
        for name, value in (
            ("max_entries", max_entries),
            ("max_candidates", max_candidates),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if max_depth is not None and (
            isinstance(max_depth, bool)
            or not isinstance(max_depth, int)
            or max_depth <= 0
        ):
            raise ValueError("max_depth must be None or a positive integer.")
        if (
            isinstance(max_elapsed_seconds, bool)
            or not isinstance(max_elapsed_seconds, (int, float))
            or not math.isfinite(max_elapsed_seconds)
            or max_elapsed_seconds <= 0
        ):
            raise ValueError("max_elapsed_seconds must be finite and positive.")
        if isinstance(excluded_directory_names, (str, bytes)):
            raise TypeError("excluded_directory_names must be an iterable of names.")
        excluded = tuple(excluded_directory_names)
        if not all(
            isinstance(name, str)
            and name
            and name not in {".", ".."}
            and "/" not in name
            and "\\" not in name
            for name in excluded
        ):
            raise ValueError("excluded_directory_names must contain simple directory names.")
        self._max_depth = max_depth
        self._max_entries = max_entries
        self._max_candidates = max_candidates
        self._max_elapsed_seconds = max_elapsed_seconds
        self._excluded_directory_names = frozenset(name.casefold() for name in excluded)

    def scan(
        self,
        model_roots: Iterable[RootInput] = (),
        runtime_roots: Iterable[RootInput] = (),
    ) -> InventoryScan:
        artifacts: dict[str, LocalArtifact] = {}
        issues = []
        visited_roots = set()
        budget = _ScanBudget(deadline=time.monotonic() + self._max_elapsed_seconds)

        for category, configured_roots in (
            ("model", model_roots),
            ("runtime", runtime_roots),
        ):
            if budget.exhausted:
                break
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
                if budget.exhausted:
                    break
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
                            code=(
                                "root_access_denied"
                                if isinstance(exc, PermissionError)
                                else "root_stat_failed"
                            ),
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
                    budget=budget,
                    max_depth=self._max_depth,
                    max_entries=self._max_entries,
                    max_candidates=self._max_candidates,
                    excluded_directory_names=self._excluded_directory_names,
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
            directories_visited=budget.directories_visited,
            entries_seen=budget.entries,
            regular_files_seen=budget.regular_files_seen,
            reparse_points_skipped=budget.reparse_points_skipped,
            access_denied_directories=budget.access_denied_directories,
        )

    @staticmethod
    def _scan_root(
        root: Path,
        root_display: str,
        category: str,
        artifacts: dict[str, LocalArtifact],
        issues: list[ScanIssue],
        budget: _ScanBudget,
        max_depth: int | None,
        max_entries: int,
        max_candidates: int,
        excluded_directory_names: frozenset[str],
    ) -> None:
        pending_directories = [root]
        depth_issue_reported = False

        def stop_for_limit(code: str, path: str, detail: str) -> None:
            budget.exhausted = True
            if not budget.limit_reported:
                issues.append(
                    ScanIssue(code=code, root=root_display, path=path, detail=detail)
                )
                budget.limit_reported = True

        while pending_directories and not budget.exhausted:
            directory = pending_directories.pop()
            budget.directories_visited += 1
            entries = []
            time_expired = False
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        if time.monotonic() >= budget.deadline:
                            stop_for_limit(
                                "scan_time_limit_reached",
                                os.path.abspath(os.fspath(directory)),
                                "Discovery stopped after reaching its time budget.",
                            )
                            time_expired = True
                            break
                        if budget.entries >= max_entries:
                            stop_for_limit(
                                "entry_limit_reached",
                                os.path.abspath(os.fspath(directory)),
                                "Discovery stopped after reaching its entry budget.",
                            )
                            break
                        budget.entries += 1
                        entries.append(entry)
            except OSError as exc:
                if isinstance(exc, PermissionError):
                    budget.access_denied_directories += 1
                issues.append(
                    ScanIssue(
                        code=(
                            "directory_access_denied"
                            if isinstance(exc, PermissionError)
                            else "directory_read_failed"
                        ),
                        root=root_display,
                        path=os.path.abspath(os.fspath(directory)),
                        detail=_error_detail(exc),
                    )
                )
                continue

            if time_expired:
                return
            entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
            child_directories = []
            for entry in entries:
                if time.monotonic() >= budget.deadline:
                    stop_for_limit(
                        "scan_time_limit_reached",
                        os.path.abspath(os.fspath(directory)),
                        "Discovery stopped after reaching its time budget.",
                    )
                    break
                try:
                    file_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    issues.append(
                        ScanIssue(
                            code=(
                                "entry_access_denied"
                                if isinstance(exc, PermissionError)
                                else "entry_stat_failed"
                            ),
                            root=root_display,
                            path=os.path.abspath(entry.path),
                            detail=_error_detail(exc),
                        )
                    )
                    continue

                if _is_link_or_reparse_point(file_stat):
                    budget.reparse_points_skipped += 1
                    continue
                if stat.S_ISDIR(file_stat.st_mode):
                    if entry.name.casefold() in excluded_directory_names:
                        continue
                    depth = len(Path(directory).relative_to(root).parts)
                    if max_depth is not None and depth >= max_depth:
                        if not depth_issue_reported:
                            issues.append(
                                ScanIssue(
                                    code="depth_limit_reached",
                                    root=root_display,
                                    path=os.path.abspath(entry.path),
                                    detail="Deeper directories were not traversed.",
                                )
                            )
                            depth_issue_reported = True
                    else:
                        child_directories.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                budget.regular_files_seen += 1

                classification = _classify_candidate(entry.name, category)
                if classification is None:
                    continue
                if budget.candidates >= max_candidates:
                    stop_for_limit(
                        "candidate_limit_reached",
                        os.path.abspath(entry.path),
                        "Discovery stopped after reaching its candidate budget.",
                    )
                    break

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
                budget.candidates += 1

            if budget.exhausted:
                return
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
