"""Read-only Git project snapshot service for the local project workspace."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectSnapshot:
    root: str
    is_git_repository: bool
    branch: str | None = None
    head: str | None = None
    dirty_file_count: int | None = None
    detail: str | None = None


def inspect_project(root: str | Path, *, timeout_seconds: float = 5.0) -> ProjectSnapshot:
    """Read branch, HEAD and porcelain status without changing repository state."""
    path = Path(root).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Der Projektpfad ist nicht verfügbar.") from exc
    if not resolved.is_dir():
        raise ValueError("Der Projektpfad ist kein Ordner.")

    safe_root = _repository_marker_root(resolved)
    top = _git(
        resolved,
        "rev-parse",
        "--show-toplevel",
        timeout=timeout_seconds,
        safe_directory=safe_root,
    )
    if top is None:
        return ProjectSnapshot(
            root=str(resolved),
            is_git_repository=False,
            detail="Kein lesbares Git-Repository am gewählten Pfad erkannt.",
        )
    repository_root = Path(top).resolve()
    branch = _git(repository_root, "branch", "--show-current", timeout=timeout_seconds)
    head = _git(repository_root, "rev-parse", "--verify", "HEAD", timeout=timeout_seconds)
    status = _git(repository_root, "status", "--porcelain", timeout=timeout_seconds)
    if branch is None or status is None:
        return ProjectSnapshot(
            root=str(repository_root),
            is_git_repository=True,
            detail="Git ist vorhanden, aber mindestens eine Statusabfrage schlug fehl.",
        )
    return ProjectSnapshot(
        root=str(repository_root),
        is_git_repository=True,
        branch=branch or "(detached HEAD)",
        head=head,
        dirty_file_count=sum(bool(line.strip()) for line in status.splitlines()),
        detail=(
            "Kein HEAD-Commit festgestellt; das Repository kann noch leer sein."
            if head is None
            else None
        ),
    )


def _repository_marker_root(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return path


def _git(
    path: Path,
    *args: str,
    timeout: float,
    safe_directory: Path | None = None,
) -> str | None:
    # Trust only this explicitly selected repository for this one Git process;
    # never change global configuration on machines with a different run-as user.
    trusted_path = safe_directory or path
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={trusted_path.as_posix()}",
                "-C",
                str(path),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
