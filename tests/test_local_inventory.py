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
