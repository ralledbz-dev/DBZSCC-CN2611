"""Resumable, bounded recursive inventory over explicitly configured roots.

Continuation tokens are untrusted input: they are versioned, bound to the exact
root list, and every resumed directory is checked again for containment and
reparse points before it is opened.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

from .local_inventory import (
    InventoryScan,
    LocalArtifact,
    LocalInventoryScanner,
    RootInput,
    ScanIssue,
    _artifact_id,
    _classify_candidate,
    _error_detail,
    _is_link_or_reparse_point,
)


class ResumableLocalInventoryScanner(LocalInventoryScanner):
    """Recursive scanner whose bounded partial results can be resumed safely."""

    supports_resume = True
    supports_cancel = True

    def scan(
        self,
        model_roots: Iterable[RootInput] = (),
        runtime_roots: Iterable[RootInput] = (),
        *,
        resume_token: str | None = None,
        cancel_event: Event | None = None,
    ) -> InventoryScan:
        roots, issues = _normalize_roots(model_roots, runtime_roots)
        cursor = _decode_cursor(resume_token, roots)
        if resume_token is not None and cursor is None:
            issues.append(
                ScanIssue(
                    code="invalid_resume_token",
                    root="",
                    path=None,
                    detail="The continuation token is invalid or belongs to different roots.",
                )
            )

        artifacts: dict[str, LocalArtifact] = {}
        visited_roots: set[tuple[str, str]] = set()
        started = time.monotonic()
        deadline = started + self._max_elapsed_seconds
        entries_seen = 0
        regular_files_seen = 0
        directories_visited = 0
        reparse_points_skipped = 0
        access_denied_directories = 0
        candidates_seen = 0

        root_index = cursor["root_index"] if cursor else 0
        for index in range(root_index, len(roots)):
            category, root_display = roots[index]
            root = Path(root_display)
            root_key = (category, os.path.normcase(root_display))
            if root_key in visited_roots:
                continue
            visited_roots.add(root_key)

            continuing_root = cursor is not None and index == root_index
            if continuing_root:
                pending = [Path(value) for value in cursor["pending_directories"]]
                current_directory = (
                    Path(cursor["current_directory"])
                    if cursor["current_directory"] is not None
                    else None
                )
                seen_names = set(cursor["seen_names"])
                child_directories = [Path(value) for value in cursor["child_directories"]]
                try:
                    _validate_directory(root, current_directory or (pending[-1] if pending else root))
                    for path in pending:
                        _validate_directory(root, path)
                    for path in child_directories:
                        _validate_directory(root, path)
                except OSError as exc:
                    issues.append(
                        ScanIssue(
                            code="invalid_resume_directory",
                            root=root_display,
                            path=str(current_directory or root),
                            detail=_error_detail(exc),
                        )
                    )
                    cursor = None
                    pending = [root]
                    current_directory = None
                    seen_names = set()
                    child_directories = []
            else:
                current_directory = None
                seen_names = set()
                child_directories = []
                pending = [root]

            try:
                root_stat = root.lstat()
            except FileNotFoundError as exc:
                issues.append(ScanIssue("root_not_found", root_display, None, _error_detail(exc)))
                continue
            except OSError as exc:
                issues.append(
                    ScanIssue(
                        "root_access_denied" if isinstance(exc, PermissionError) else "root_stat_failed",
                        root_display,
                        None,
                        _error_detail(exc),
                    )
                )
                continue
            if _is_link_or_reparse_point(root_stat):
                issues.append(
                    ScanIssue(
                        "linked_root_skipped",
                        root_display,
                        root_display,
                        "Root is a symlink or filesystem reparse point.",
                    )
                )
                continue
            if not stat.S_ISDIR(root_stat.st_mode):
                issues.append(
                    ScanIssue("root_not_directory", root_display, root_display, "Configured root is not a directory.")
                )
                continue

            while current_directory is not None or pending:
                if cancel_event is not None and cancel_event.is_set():
                    issues.append(
                        ScanIssue(
                            "scan_cancelled",
                            root_display,
                            os.path.abspath(os.fspath(current_directory or (pending[-1] if pending else root))),
                            "Discovery was cancelled at a safe checkpoint; resume with the returned continuation token.",
                        )
                    )
                    return _scan_result(
                        artifacts,
                        issues,
                        directories_visited,
                        entries_seen,
                        regular_files_seen,
                        reparse_points_skipped,
                        access_denied_directories,
                        _encode_cursor(
                            roots,
                            index,
                            pending,
                            current_directory,
                            seen_names,
                            child_directories,
                        ),
                        complete=False,
                    )
                if current_directory is None:
                    current_directory = pending.pop()
                    seen_names = set()
                    child_directories = []
                directory = current_directory
                try:
                    _validate_directory(root, directory)
                    directories_visited += 1
                    with os.scandir(directory) as iterator:
                        for entry in iterator:
                            if cancel_event is not None and cancel_event.is_set():
                                issues.append(
                                    ScanIssue(
                                        "scan_cancelled",
                                        root_display,
                                        os.path.abspath(os.fspath(directory)),
                                        "Discovery was cancelled at a safe checkpoint; resume with the returned continuation token.",
                                    )
                                )
                                return _scan_result(
                                    artifacts,
                                    issues,
                                    directories_visited,
                                    entries_seen,
                                    regular_files_seen,
                                    reparse_points_skipped,
                                    access_denied_directories,
                                    _encode_cursor(
                                        roots,
                                        index,
                                        pending,
                                        directory,
                                        seen_names,
                                        child_directories,
                                    ),
                                    complete=False,
                                )
                            if entry.name in seen_names:
                                continue
                            entry_path = directory / entry.name
                            limit_code = None
                            if time.monotonic() >= deadline:
                                limit_code = "scan_time_limit_reached"
                            elif entries_seen >= self._max_entries:
                                limit_code = "entry_limit_reached"
                            if limit_code is not None:
                                issues.append(
                                    ScanIssue(
                                        limit_code,
                                        root_display,
                                        os.path.abspath(os.fspath(directory)),
                                        "Discovery stopped at its explicit budget; resume with the returned continuation token.",
                                    )
                                )
                                return _scan_result(
                                    artifacts,
                                    issues,
                                    directories_visited,
                                    entries_seen,
                                    regular_files_seen,
                                    reparse_points_skipped,
                                    access_denied_directories,
                                    _encode_cursor(
                                        roots,
                                        index,
                                        pending,
                                        directory,
                                        seen_names,
                                        child_directories,
                                    ),
                                    complete=False,
                                )
                            entries_seen += 1
                            try:
                                file_stat = entry.stat(follow_symlinks=False)
                            except OSError as exc:
                                issues.append(
                                    ScanIssue(
                                        "entry_access_denied" if isinstance(exc, PermissionError) else "entry_stat_failed",
                                        root_display,
                                        os.path.abspath(os.fspath(entry_path)),
                                        _error_detail(exc),
                                    )
                                )
                                seen_names.add(entry.name)
                                continue

                            if _is_link_or_reparse_point(file_stat):
                                reparse_points_skipped += 1
                                seen_names.add(entry.name)
                                continue
                            if stat.S_ISDIR(file_stat.st_mode):
                                if entry.name.casefold() not in self._excluded_directory_names:
                                    depth = len(directory.relative_to(root).parts)
                                    if self._max_depth is not None and depth >= self._max_depth:
                                        issues.append(
                                            ScanIssue(
                                                "depth_limit_reached",
                                                root_display,
                                                os.path.abspath(os.fspath(entry_path)),
                                                "Deeper directories were not traversed.",
                                            )
                                        )
                                    else:
                                        child_directories.append(entry_path)
                                seen_names.add(entry.name)
                                continue
                            if not stat.S_ISREG(file_stat.st_mode):
                                seen_names.add(entry.name)
                                continue
                            regular_files_seen += 1
                            classification = _classify_candidate(entry.name, category)
                            if classification is not None:
                                if candidates_seen >= self._max_candidates:
                                    issues.append(
                                        ScanIssue(
                                            "candidate_limit_reached",
                                            root_display,
                                            os.path.abspath(os.fspath(entry_path)),
                                            "Discovery stopped at its explicit candidate budget; resume with the returned continuation token.",
                                        )
                                    )
                                    # This entry is deliberately not checkpointed, so resume retries it.
                                    return _scan_result(
                                        artifacts,
                                        issues,
                                        directories_visited,
                                        entries_seen,
                                        regular_files_seen,
                                        reparse_points_skipped,
                                        access_denied_directories,
                                        _encode_cursor(
                                            roots,
                                            index,
                                            pending,
                                            directory,
                                            seen_names,
                                            child_directories,
                                        ),
                                        complete=False,
                                    )
                                kind, evidence = classification
                                absolute_path = os.path.abspath(os.fspath(entry_path))
                                artifact = LocalArtifact(
                                    artifact_id=_artifact_id(kind, absolute_path),
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
                                artifacts.setdefault(artifact.artifact_id, artifact)
                                candidates_seen += 1
                            seen_names.add(entry.name)
                except OSError as exc:
                    if isinstance(exc, PermissionError):
                        access_denied_directories += 1
                    issues.append(
                        ScanIssue(
                            "directory_access_denied" if isinstance(exc, PermissionError) else "directory_read_failed",
                            root_display,
                            os.path.abspath(os.fspath(directory)),
                            _error_detail(exc),
                        )
                    )
                    current_directory = None
                    seen_names = set()
                    child_directories = []
                    continue

                pending.extend(reversed(child_directories))
                current_directory = None
                seen_names = set()
                child_directories = []
                if time.monotonic() >= deadline and (pending or index + 1 < len(roots)):
                    issues.append(
                        ScanIssue(
                            "scan_time_limit_reached",
                            root_display,
                            os.path.abspath(os.fspath(directory)),
                            "Discovery stopped at its explicit time budget; resume with the returned continuation token.",
                        )
                    )
                    return _scan_result(
                        artifacts,
                        issues,
                        directories_visited,
                        entries_seen,
                        regular_files_seen,
                        reparse_points_skipped,
                        access_denied_directories,
                        _encode_cursor(roots, index, pending, None, (), ()),
                        complete=False,
                    )
            cursor = None

        return _scan_result(
            artifacts,
            issues,
            directories_visited,
            entries_seen,
            regular_files_seen,
            reparse_points_skipped,
            access_denied_directories,
            None,
            complete=True,
        )


def _normalize_roots(
    model_roots: Iterable[RootInput], runtime_roots: Iterable[RootInput]
) -> tuple[list[tuple[str, str]], list[ScanIssue]]:
    roots: list[tuple[str, str]] = []
    issues: list[ScanIssue] = []
    for category, configured in (("model", model_roots), ("runtime", runtime_roots)):
        if isinstance(configured, (str, os.PathLike)):
            values = (configured,)
        else:
            try:
                values = iter(configured)
            except TypeError:
                issues.append(
                    ScanIssue("invalid_root_collection", str(configured), None, "Expected a path or iterable of paths.")
                )
                continue
        for raw_root in values:
            try:
                root = os.path.abspath(os.fspath(Path(raw_root).expanduser()))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                issues.append(ScanIssue("invalid_root", str(raw_root), None, _error_detail(exc) if isinstance(exc, OSError) else type(exc).__name__))
                continue
            item = (category, root)
            if item not in roots:
                roots.append(item)
    return roots, issues


def _decode_cursor(token: str | None, roots: list[tuple[str, str]]) -> dict[str, Any] | None:
    if token is None:
        return None
    try:
        payload = json.loads(token)
        expected_roots = [[category, root] for category, root in roots]
        if payload.get("version") != 1 or payload.get("roots") != expected_roots:
            return None
        root_index = payload["root_index"]
        if type(root_index) is not int or not 0 <= root_index < max(1, len(roots)):
            return None
        active_root = Path(roots[root_index][1]) if roots else Path(".")
        pending = payload.get("pending_directories", [])
        children = payload.get("child_directories", [])
        current = payload.get("current_directory")
        seen = payload.get("seen_names", [])
        if not isinstance(pending, list) or not isinstance(children, list) or not isinstance(seen, list):
            return None
        paths = [Path(item) for item in pending + children]
        if current is not None:
            paths.append(Path(current))
        for path in paths:
            relative = path.relative_to(active_root)
            if ".." in relative.parts:
                return None
        if not all(isinstance(item, str) and item for item in seen):
            return None
        return {
            "root_index": root_index,
            "pending_directories": pending,
            "current_directory": current,
            "seen_names": seen,
            "child_directories": children,
        }
    except (AttributeError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def _encode_cursor(
    roots: list[tuple[str, str]],
    root_index: int,
    pending: list[Path],
    current: Path | None,
    seen_names: Iterable[str],
    children: list[Path],
) -> str:
    payload = {
        "version": 1,
        "roots": roots,
        "root_index": root_index,
        "pending_directories": [str(path) for path in pending],
        "current_directory": str(current) if current is not None else None,
        "seen_names": sorted(set(seen_names)),
        "child_directories": [str(path) for path in children],
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _validate_directory(root: Path, directory: Path) -> None:
    relative = directory.relative_to(root)
    current = root
    parts = ((),) if not relative.parts else tuple((part,) for part in relative.parts)
    for part_group in parts:
        if part_group:
            current = current / part_group[0]
        info = current.lstat()
        if _is_link_or_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("Resumed path is no longer a safe directory beneath its configured root.")


def _scan_result(
    artifacts: dict[str, LocalArtifact],
    issues: list[ScanIssue],
    directories_visited: int,
    entries_seen: int,
    regular_files_seen: int,
    reparse_points_skipped: int,
    access_denied_directories: int,
    continuation_token: str | None,
    *,
    complete: bool,
) -> InventoryScan:
    return InventoryScan(
        scanned_at=datetime.now(timezone.utc),
        artifacts=tuple(
            sorted(artifacts.values(), key=lambda item: (item.kind, item.path.casefold(), item.path))
        ),
        issues=tuple(issues),
        directories_visited=directories_visited,
        entries_seen=entries_seen,
        regular_files_seen=regular_files_seen,
        reparse_points_skipped=reparse_points_skipped,
        access_denied_directories=access_denied_directories,
        continuation_token=continuation_token,
        complete=complete,
    )
