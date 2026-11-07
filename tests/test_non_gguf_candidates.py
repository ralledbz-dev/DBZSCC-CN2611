from __future__ import annotations

from pathlib import Path

from dbzscc.miro import CatalogQuery, LocalModelRegistry


def test_non_gguf_weight_files_are_discovered_but_never_marked_startable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    for name in ("weights.safetensors", "pytorch_model.bin", "checkpoint.pt", "model.pth", "export.onnx"):
        (root / name).write_bytes(b"candidate")
    (root / "actual.gguf").write_bytes(b"not parsed by this fixture")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "llama-server.exe").write_bytes(b"never executed")

    registry = LocalModelRegistry(tmp_path / "registry.sqlite")
    registry.synchronize(model_roots=[root], runtime_roots=[runtime])

    catalog = registry.query_catalog(CatalogQuery(root=str(root)))
    non_gguf = [item for item in catalog if item.artifact.kind == "non_gguf_model_candidate"]
    gguf = next(item for item in catalog if item.artifact.kind == "gguf_candidate")

    assert len(non_gguf) == 5
    assert all(item.artifact.status == "non_gguf_format_candidate" for item in non_gguf)
    assert all(item.artifact.identity_status == "not_applicable" for item in non_gguf)
    assert all(item.capability_evidence_json.find("not_startable") >= 0 for item in non_gguf)
    assert gguf.artifact.status == "metadata_parse_failed"
    assert all(item.artifact.kind != "llama_executable_candidate" for item in catalog)
    assert any(
        item.artifact.kind == "llama_executable_candidate"
        for item in registry.query_catalog(CatalogQuery(include_runtime_artifacts=True))
    )
