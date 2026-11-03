from __future__ import annotations

import os
from pathlib import Path

import pytest

import dbzscc.app.chat_context_service as context_service
from dbzscc.app.chat_context_service import (
    MAX_CONTEXT_FILE_BYTES,
    MAX_CONTEXT_FILES,
    MAX_CONTEXT_TOTAL_BYTES,
    build_context_user_message,
    capture_context_files,
    validate_context_selection,
)


def test_reads_only_explicit_files_and_builds_a_single_request_payload(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source = root / "src" / "module.py"
    other = root / "notes.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'print("selected")\n')
    other.write_text("not selected", encoding="utf-8")

    files = capture_context_files(root, [source])
    message = build_context_user_message("Explain this file", files)

    assert [(item.relative_path, item.content) for item in files] == [
        ("src/module.py", 'print("selected")\n')
    ]
    assert '"selected_project_files"' in message
    assert '"not selected"' not in message
    assert other.read_text(encoding="utf-8") == "not selected"


def test_selection_validation_does_not_open_or_read_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "readme.md"
    target.write_text("content stays local", encoding="utf-8")

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("selection validation must not read file contents")

    monkeypatch.setattr(context_service.os, "open", forbidden_open)

    assert validate_context_selection(root, [target]) == (target.resolve(),)


def test_rejects_parent_traversal_and_files_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ValueError, match=r"\.\."):
        capture_context_files(root, ["../outside.txt"])
    with pytest.raises(ValueError, match="außerhalb"):
        capture_context_files(root, [outside])

    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_rejects_symlinked_file_parent_and_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not read", encoding="utf-8")
    linked_file = root / "file-link.txt"
    linked_dir = root / "dir-link"
    linked_root = tmp_path / "root-link"
    try:
        os.symlink(outside, linked_file)
        os.symlink(outside.parent, linked_dir, target_is_directory=True)
        os.symlink(root, linked_root, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks are unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="Symlink|Junction"):
        capture_context_files(root, [linked_file])
    with pytest.raises(ValueError, match="Symlink|Junction"):
        capture_context_files(root, [linked_dir / outside.name])
    with pytest.raises(ValueError, match="Symlink|Junction"):
        capture_context_files(linked_root, [outside])
    assert outside.read_text(encoding="utf-8") == "do not read"


def test_rejects_hidden_files_and_codesources_at_any_selected_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    hidden = root / ".env"
    code_source = root / "media" / "CodeSources" / "old.py"
    hidden.parent.mkdir(parents=True)
    code_source.parent.mkdir(parents=True)
    hidden.write_text("secret-like", encoding="utf-8")
    code_source.write_text("old source", encoding="utf-8")

    with pytest.raises(ValueError, match="Versteckte"):
        capture_context_files(root, [hidden])
    with pytest.raises(ValueError, match="CodeSources"):
        capture_context_files(root, [code_source])
    with pytest.raises(ValueError, match="CodeSources"):
        capture_context_files(code_source.parent, [code_source])


def test_enforces_file_count_single_file_and_total_byte_limits(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    small_files = []
    for index in range(MAX_CONTEXT_FILES + 1):
        target = root / f"file-{index}.txt"
        target.write_text("x", encoding="utf-8")
        small_files.append(target)

    with pytest.raises(ValueError, match="höchstens"):
        validate_context_selection(root, small_files)

    oversized = root / "oversized.txt"
    oversized.write_bytes(b"x" * (MAX_CONTEXT_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="Limit"):
        validate_context_selection(root, [oversized])

    total_files = []
    for index in range(5):
        target = root / f"total-{index}.txt"
        target.write_bytes(b"x" * (27 * 1024))
        total_files.append(target)
    with pytest.raises(ValueError, match="insgesamt"):
        validate_context_selection(root, total_files)
    assert MAX_CONTEXT_TOTAL_BYTES == 128 * 1024


def test_rejects_invalid_utf8_and_binary_control_bytes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    invalid_utf8 = root / "legacy.txt"
    invalid_utf8.write_bytes(b"caf\xe9")
    binary_controls = root / "data.bin"
    binary_controls.write_bytes(b"valid utf-8\x00but binary")

    with pytest.raises(ValueError, match="UTF-8"):
        capture_context_files(root, [invalid_utf8])
    with pytest.raises(ValueError, match="Binärdaten"):
        capture_context_files(root, [binary_controls])


def test_reports_unreadable_file_without_exposing_file_contents(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "private.txt"
    target.write_text("sensitive content", encoding="utf-8")
    original_open = os.open

    def unreadable(path, flags, *args, **kwargs):
        if Path(path) == target.resolve():
            raise PermissionError("private content must not leak")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(context_service.os, "open", unreadable)

    with pytest.raises(ValueError, match="nicht lesbar") as error:
        capture_context_files(root, [target])
    assert "sensitive content" not in str(error.value)
    assert "private content" not in str(error.value)


def test_rejects_duplicate_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "note.txt"
    target.write_text("one copy only", encoding="utf-8")

    with pytest.raises(ValueError, match="mehrfach"):
        capture_context_files(root, [target, root / "." / "note.txt"])
