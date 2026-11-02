from __future__ import annotations

from pathlib import Path

import pytest

from dbzscc.app.project_service import inspect_project


def test_reports_non_git_folder_without_mutating_it(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("user content", encoding="utf-8")

    snapshot = inspect_project(tmp_path)

    assert snapshot.root == str(tmp_path.resolve())
    assert snapshot.is_git_repository is False
    assert snapshot.branch is None
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "user content"


def test_rejects_missing_or_non_directory_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        inspect_project(tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        inspect_project(file_path)
