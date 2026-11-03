from pathlib import Path

import pytest

from dbzscc.miro import LocalInventoryScanner


def test_discovers_gguf_and_llama_tool_candidates_with_provenance(tmp_path: Path) -> None:
    models = tmp_path / "models"
    nested_models = models / "nested"
    runtime = tmp_path / "runtime"
    nested_models.mkdir(parents=True)
    runtime.mkdir()

    # Deliberately invalid contents: discovery is based on the filename only.
    (nested_models / "Coder.GgUf").write_bytes(b"not parsed")
    (runtime / "llama-server.EXE").write_bytes(b"not executed")
    (runtime / "other-tool.exe").write_bytes(b"ignored")

    scanner = LocalInventoryScanner()
    result = scanner.scan(model_roots=[models], runtime_roots=[runtime])
    artifacts = {artifact.name: artifact for artifact in result.artifacts}

    assert set(artifacts) == {"Coder.GgUf", "llama-server.EXE"}
    model = artifacts["Coder.GgUf"]
    assert model.kind == "gguf_candidate"
    assert model.path == str((nested_models / "Coder.GgUf").absolute())
    assert model.source_root == str(models.absolute())
    assert model.size_bytes == len(b"not parsed")
    assert model.modified_at.utcoffset().total_seconds() == 0
    assert model.evidence == "filename-extension:.gguf"

    tool = artifacts["llama-server.EXE"]
    assert tool.kind == "llama_executable_candidate"
    assert tool.evidence == "filename-pattern:llama-*"
    assert result.issues == ()

    repeated = scanner.scan(model_roots=[models], runtime_roots=[runtime])
    assert [item.artifact_id for item in result.artifacts] == [
        item.artifact_id for item in repeated.artifacts
    ]


def test_scans_only_explicit_roots_and_deduplicates_overlapping_roots(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    nested = root / "nested"
    nested.mkdir(parents=True)
    model = nested / "model.gguf"
    model.write_bytes(b"candidate")

    scanner = LocalInventoryScanner()
    empty_result = scanner.scan()
    assert empty_result.artifacts == ()

    result = scanner.scan(model_roots=[root, nested, root])
    assert len(result.artifacts) == 1
    assert result.artifacts[0].path == str(model.absolute())
    assert result.artifacts[0].source_root == str(root.absolute())


def test_runtime_candidates_are_narrowly_name_and_suffix_based(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in (
        "llama-cli",
        "llama-quantize.exe",
        "llama-backend.dll",
        "llama-wrapper.cmd",
        "not-llama-server.exe",
    ):
        (runtime / name).write_bytes(b"candidate data")

    result = LocalInventoryScanner().scan(runtime_roots=[runtime])

    assert {artifact.name for artifact in result.artifacts} == {
        "llama-cli",
        "llama-quantize.exe",
    }


def test_invalid_roots_are_reported_without_stopping_other_roots(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("not a directory", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "llama-cli").write_bytes(b"candidate")

    result = LocalInventoryScanner().scan(
        model_roots=[missing, not_a_directory], runtime_roots=[runtime]
    )

    assert {issue.code for issue in result.issues} == {
        "root_not_found",
        "root_not_directory",
    }
    assert [artifact.name for artifact in result.artifacts] == ["llama-cli"]


def test_scanner_depth_and_candidate_limits_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "models"
    first_level = root / "one"
    second_level = first_level / "two"
    second_level.mkdir(parents=True)
    (root / "root.gguf").write_bytes(b"root")
    (first_level / "first.gguf").write_bytes(b"first")
    (second_level / "deep.gguf").write_bytes(b"deep")

    depth_limited = LocalInventoryScanner(max_depth=1, max_candidates=10).scan(
        model_roots=[root]
    )

    assert {item.name for item in depth_limited.artifacts} == {"root.gguf", "first.gguf"}
    assert any(issue.code == "depth_limit_reached" for issue in depth_limited.issues)
    assert depth_limited.directories_visited == 2
    assert depth_limited.entries_seen == 4
    assert depth_limited.regular_files_seen == 2

    candidate_limited = LocalInventoryScanner(max_candidates=1).scan(
        model_roots=[root]
    )
    assert len(candidate_limited.artifacts) == 1
    assert any(issue.code == "candidate_limit_reached" for issue in candidate_limited.issues)


def test_scanner_traverses_virtualenv_directories_by_default(tmp_path: Path) -> None:
    root = tmp_path / "models"
    virtualenv = root / ".venv"
    actual_models = root / "GGUF_LIBRARY"
    (virtualenv / "Lib" / "site-packages").mkdir(parents=True)
    actual_models.mkdir(parents=True)
    (virtualenv / "Lib" / "site-packages" / "noise.gguf").write_bytes(b"noise")
    (actual_models / "visible.gguf").write_bytes(b"model")

    result = LocalInventoryScanner().scan(model_roots=[root])

    assert {item.name for item in result.artifacts} == {"visible.gguf", "noise.gguf"}
    assert result.issues == ()
    assert result.directories_visited == 5
    assert result.regular_files_seen == 2


def test_scanner_entry_and_time_limits_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    for name in ("a.gguf", "b.gguf", "c.gguf"):
        (root / name).write_bytes(b"candidate")

    entry_limited = LocalInventoryScanner(max_entries=2).scan(model_roots=[root])
    assert len(entry_limited.artifacts) <= 2
    assert any(issue.code == "entry_limit_reached" for issue in entry_limited.issues)

    time_limited = LocalInventoryScanner(max_elapsed_seconds=1e-12).scan(
        model_roots=[root]
    )
    assert time_limited.artifacts == ()
    assert any(issue.code == "scan_time_limit_reached" for issue in time_limited.issues)


def test_access_denied_directory_is_reported_and_not_traversed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    root = tmp_path / "models"
    denied = root / "locked"
    denied.mkdir(parents=True)
    (root / "visible.gguf").write_bytes(b"visible")
    (denied / "hidden.gguf").write_bytes(b"hidden")
    real_scandir = os.scandir

    def guarded_scandir(path):
        if Path(path).absolute() == denied.absolute():
            raise PermissionError("blocked by test")
        return real_scandir(path)

    monkeypatch.setattr("dbzscc.miro.local_inventory.os.scandir", guarded_scandir)
    result = LocalInventoryScanner().scan(model_roots=[root])

    assert [item.name for item in result.artifacts] == ["visible.gguf"]
    assert len(result.issues) == 1
    assert result.issues[0].code == "directory_access_denied"
    assert result.issues[0].path == str(denied.absolute())
    assert result.access_denied_directories == 1


def test_symlinked_directories_are_not_traversed(tmp_path: Path) -> None:
    root = tmp_path / "models"
    target = tmp_path / "outside"
    root.mkdir()
    target.mkdir()
    (target / "outside.gguf").write_bytes(b"candidate")
    link = root / "linked"

    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    result = LocalInventoryScanner().scan(model_roots=[root])

    assert result.artifacts == ()
    assert result.issues == ()
    assert result.reparse_points_skipped == 1
