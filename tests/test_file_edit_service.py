from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dbzscc.app.file_edit_service import (
    MAX_FILE_BYTES,
    DiffProposal,
    apply_reviewed_proposal,
    build_model_request,
    capture_file,
    parse_model_proposal,
    validate_working_root,
)


def _proposal(root: Path, target: Path, updated_text: str) -> DiffProposal:
    snapshot = capture_file(root, target)
    response = json.dumps(
        {
            "relative_path": snapshot.relative_path,
            "base_sha256": snapshot.sha256,
            "updated_content": updated_text,
        }
    )
    return parse_model_proposal(snapshot, response)


def test_preview_is_derived_from_strict_structured_response_and_explicit_apply(tmp_path: Path) -> None:
    target = tmp_path / "hello.py"
    target.write_text('value = "old"\n', encoding="utf-8")
    proposal = _proposal(tmp_path, target, 'value = "new"\n')

    assert "--- original" in proposal.unified_diff
    assert "+++ vorgeschlagen" in proposal.unified_diff
    assert '-value = "old"' in proposal.unified_diff
    assert '+value = "new"' in proposal.unified_diff
    assert target.read_text(encoding="utf-8") == 'value = "old"\n'

    written = apply_reviewed_proposal(proposal)

    assert written == target.resolve()
    assert target.read_text(encoding="utf-8") == 'value = "new"\n'
    assert list(tmp_path.glob(".dbzscc-edit-*.tmp")) == []


def test_model_request_includes_exact_target_hash_and_source_without_executing_it(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    source = 'raise RuntimeError("not executed")\n'
    target.write_bytes(source.encode("utf-8"))
    snapshot = capture_file(tmp_path, target)

    request = json.loads(build_model_request(snapshot, "Add a comment"))

    assert request["target"] == {
        "relative_path": "source.py",
        "base_sha256": snapshot.sha256,
    }
    assert request["file_content"] == source
    assert target.read_text(encoding="utf-8") == source


def test_rejects_paths_outside_root_and_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch", encoding="utf-8")

    with pytest.raises(ValueError, match="außerhalb"):
        capture_file(root, outside)
    with pytest.raises(ValueError, match=r"\.\."):
        capture_file(root, "../outside.txt")

    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_rejects_symlinked_target_and_symlinked_parent(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch", encoding="utf-8")
    linked_file = root / "file-link.txt"
    linked_dir = root / "dir-link"
    try:
        os.symlink(outside, linked_file)
        os.symlink(outside.parent, linked_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks are not available in this Windows environment: {exc}")

    with pytest.raises(ValueError, match="Symlink|Junction"):
        capture_file(root, linked_file)
    with pytest.raises(ValueError, match="Symlink|Junction"):
        capture_file(root, linked_dir / outside.name)
    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_rejects_a_symlinked_working_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    root_link = tmp_path / "root-link"
    try:
        os.symlink(actual, root_link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks are not available in this Windows environment: {exc}")

    with pytest.raises(ValueError, match="Arbeitswurzel.*Symlink|Junction"):
        validate_working_root(root_link)


def test_rejects_oversize_binary_and_non_utf8_files(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="Größe"):
        capture_file(tmp_path, oversized)

    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"a\x00b")
    with pytest.raises(ValueError, match="binär"):
        capture_file(tmp_path, binary)

    non_utf8 = tmp_path / "latin1.txt"
    non_utf8.write_bytes(b"caf\xe9")
    with pytest.raises(ValueError, match="UTF-8"):
        capture_file(tmp_path, non_utf8)


def test_preserves_utf8_bom_and_crlf(tmp_path: Path) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfline one\r\n")
    proposal = _proposal(tmp_path, target, "line two\r\n")

    apply_reviewed_proposal(proposal)

    assert target.read_bytes() == b"\xef\xbb\xbfline two\r\n"


def test_rejects_malformed_duplicate_wrong_target_or_wrong_hash_proposals(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    snapshot = capture_file(tmp_path, target)
    valid = {
        "relative_path": snapshot.relative_path,
        "base_sha256": snapshot.sha256,
        "updated_content": "new\n",
    }
    duplicate_key_payload = (
        '{"relative_path":"file.txt","relative_path":"file.txt",'
        f'"base_sha256":"{snapshot.sha256}","updated_content":"new\\n"}}'
    )
    bad_payloads = [
        "not json",
        json.dumps({**valid, "extra": "no"}),
        json.dumps({**valid, "relative_path": "other.txt"}),
        json.dumps({**valid, "base_sha256": "0" * 64}),
        duplicate_key_payload,
    ]

    for response in bad_payloads:
        with pytest.raises(ValueError):
            parse_model_proposal(snapshot, response)
    assert target.read_text(encoding="utf-8") == "old\n"

def test_rejects_file_changed_while_model_response_is_pending(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    snapshot = capture_file(tmp_path, target)
    target.write_text("changed while waiting\n", encoding="utf-8")
    response = json.dumps(
        {
            "relative_path": snapshot.relative_path,
            "base_sha256": snapshot.sha256,
            "updated_content": "suggested\n",
        }
    )

    with pytest.raises(ValueError, match="geändert"):
        parse_model_proposal(snapshot, response)
    assert target.read_text(encoding="utf-8") == "changed while waiting\n"


def test_rejects_unchanged_file_hash_before_apply_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    proposal = _proposal(tmp_path, target, "new\n")
    target.write_text("external change\n", encoding="utf-8")

    with pytest.raises(ValueError, match="geändert"):
        apply_reviewed_proposal(proposal)

    assert target.read_text(encoding="utf-8") == "external change\n"
    assert list(tmp_path.glob(".dbzscc-edit-*.tmp")) == []


def test_rejects_tampered_preview_and_oversize_model_content(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    snapshot = capture_file(tmp_path, target)
    valid = json.dumps(
        {
            "relative_path": snapshot.relative_path,
            "base_sha256": snapshot.sha256,
            "updated_content": "new\n",
        }
    )
    proposal = parse_model_proposal(snapshot, valid)
    tampered = DiffProposal(snapshot, proposal.updated_text, "--- different preview\n")
    with pytest.raises(ValueError, match="Vorschau"):
        apply_reviewed_proposal(tampered)

    oversized = json.dumps(
        {
            "relative_path": snapshot.relative_path,
            "base_sha256": snapshot.sha256,
            "updated_content": "x" * (MAX_FILE_BYTES + 1),
        }
    )
    with pytest.raises(ValueError, match="überschreitet"):
        parse_model_proposal(snapshot, oversized)
    assert target.read_text(encoding="utf-8") == "old\n"
