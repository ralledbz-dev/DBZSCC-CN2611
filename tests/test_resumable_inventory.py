from __future__ import annotations

import json
from pathlib import Path

from dbzscc.miro import ResumableLocalInventoryScanner


def test_resumable_scan_finishes_all_candidates_across_bounded_pages(tmp_path: Path) -> None:
    root = tmp_path / "models"
    (root / "one" / "nested").mkdir(parents=True)
    (root / "two").mkdir()
    paths = (
        root / "root.gguf",
        root / "one" / "first.gguf",
        root / "one" / "nested" / "deep.gguf",
        root / "two" / "last.gguf",
    )
    for path in paths:
        path.write_bytes(b"candidate")

    scanner = ResumableLocalInventoryScanner(
        max_entries=2, max_candidates=20, max_elapsed_seconds=30
    )
    token = None
    discovered: set[str] = set()
    page_count = 0
    while True:
        result = scanner.scan(model_roots=[root], resume_token=token)
        page_count += 1
        discovered.update(item.path for item in result.artifacts)
        if result.complete:
            assert result.continuation_token is None
            break
        assert result.continuation_token
        token = result.continuation_token
        assert page_count < 20

    assert page_count > 1
    assert discovered == {str(path.absolute()) for path in paths}


def test_resume_cursor_cannot_escape_its_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "models"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.gguf").write_bytes(b"inside")
    (root / "ordinary.txt").write_text("not a model", encoding="utf-8")
    outside_model = outside / "outside.gguf"
    outside_model.write_bytes(b"outside")
    scanner = ResumableLocalInventoryScanner(
        max_entries=1, max_candidates=20, max_elapsed_seconds=30
    )

    first = scanner.scan(model_roots=[root])
    assert not first.complete
    assert first.continuation_token
    cursor = json.loads(first.continuation_token)
    cursor["current_directory"] = str(outside)

    resumed = scanner.scan(
        model_roots=[root],
        resume_token=json.dumps(cursor),
    )

    assert any(issue.code == "invalid_resume_token" for issue in resumed.issues)
    assert all(item.path != str(outside_model.absolute()) for item in resumed.artifacts)


def test_resume_cursor_rejects_lexical_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "models"
    sibling = tmp_path / "sibling"
    root.mkdir()
    sibling.mkdir()
    (root / "first.gguf").write_bytes(b"inside")
    (root / "second.txt").write_text("inside", encoding="utf-8")
    outside_model = sibling / "outside.gguf"
    outside_model.write_bytes(b"outside")
    scanner = ResumableLocalInventoryScanner(
        max_entries=1, max_candidates=20, max_elapsed_seconds=30
    )

    first = scanner.scan(model_roots=[root])
    assert not first.complete
    assert first.continuation_token
    cursor = json.loads(first.continuation_token)
    cursor["current_directory"] = str(root / ".." / "sibling")

    resumed = scanner.scan(model_roots=[root], resume_token=json.dumps(cursor))

    assert any(issue.code == "invalid_resume_token" for issue in resumed.issues)
    assert all(item.path != str(outside_model.absolute()) for item in resumed.artifacts)


def test_resume_token_is_bound_to_the_exact_root_manifest(tmp_path: Path) -> None:
    root = tmp_path / "models"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    for index in range(3):
        (root / f"{index}.gguf").write_bytes(b"candidate")
    scanner = ResumableLocalInventoryScanner(
        max_entries=1, max_candidates=20, max_elapsed_seconds=30
    )

    first = scanner.scan(model_roots=[root])
    assert first.continuation_token
    resumed = scanner.scan(model_roots=[other], resume_token=first.continuation_token)

    assert any(issue.code == "invalid_resume_token" for issue in resumed.issues)
    assert all(item.source_root == str(other.absolute()) for item in resumed.artifacts)
