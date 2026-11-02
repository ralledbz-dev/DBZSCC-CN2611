from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

from dbzscc.miro import GGUFMetadataReader, LocalInventoryScanner
from dbzscc.miro.discovery_pipeline import run_local_discovery


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _entry(key: str, value_type: int, encoded_value: bytes) -> bytes:
    return _string(key) + _u32(value_type) + encoded_value


def _gguf(path: Path, *, valid: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not valid:
        path.write_bytes(b"not a GGUF file")
        return path

    entries = [
        _entry("general.architecture", 8, _string("qwen3")),
        _entry("general.name", 8, _string("Synthetic Model")),
        _entry("qwen3.context_length", 4, _u32(8192)),
        _entry("tokenizer.chat_template", 8, _string("synthetic template")),
    ]
    prefix = b"GGUF" + _u32(3) + _u64(1) + _u64(len(entries)) + b"".join(entries)
    path.write_bytes(prefix + b"synthetic tensor payload")
    return path


class _CountingReader(GGUFMetadataReader):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def read(self, path: str | Path):
        self.paths.append(str(Path(path).absolute()))
        return super().read(path)


def test_one_pass_joins_inventory_parse_registry_and_capability_evidence(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models"
    runtime = tmp_path / "runtime"
    valid = _gguf(models / "valid.gguf")
    _gguf(models / "broken.gguf", valid=False)
    runtime.mkdir()
    runtime_tool = runtime / "llama-cli.exe"
    runtime_tool.write_bytes(b"must not be opened or executed")
    missing_root = tmp_path / "missing-models"
    reader = _CountingReader()
    observed_at = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    result = run_local_discovery(
        database_path=tmp_path / "registry.sqlite",
        model_roots=[models, missing_root],
        runtime_roots=[runtime],
        observed_at=observed_at,
        reader=reader,
    )

    assert [item.candidate.name for item in result.artifacts] == [
        "broken.gguf",
        "valid.gguf",
        "llama-cli.exe",
    ]
    assert result.registry_sync.artifact_count == 3
    assert result.registry_sync.parse_failure_count == 1
    assert len(result.scanner_issues) == 1
    assert result.scanner_issues[0].code == "root_not_found"
    assert result.has_partial_results

    broken, parsed, runtime_item = result.artifacts
    assert broken.registry_record is not None
    assert broken.registry_record.status == "metadata_parse_failed"
    assert broken.parse_result is not None
    assert broken.parse_result.issue is not None
    assert broken.parse_result.issue.code == "invalid_magic"
    assert broken.capability_profile is None

    assert parsed.registry_record is not None
    assert parsed.registry_record.status == "metadata_read"
    assert parsed.registry_record.identity_status == "resolved"
    assert parsed.parse_result is not None and parsed.parse_result.ok
    assert parsed.parse_result.metadata is not None
    assert parsed.capability_profile is not None
    profile = parsed.capability_profile
    assert profile.identity.model_id == parsed.registry_record.model_id
    evidence = {item.property_name: item for item in profile.metadata_evidence}
    assert evidence["declared_context_length"].value == 8192
    assert evidence["chat_template_declared"].value == "present"
    assert profile.runtime_test_status == "not_tested"
    assert profile.runtime_tested_capabilities == ()
    assert profile.multimodal_status == "not_verified"
    assert parsed.registry_record.metadata_prefix_sha256 == (
        parsed.parse_result.metadata.metadata_prefix_sha256
    )

    assert runtime_item.registry_record is not None
    assert runtime_item.registry_record.status == "runtime_candidate"
    assert runtime_item.parse_result is None
    assert runtime_item.capability_profile is None
    assert reader.paths == [str((models / "broken.gguf").absolute()), str(valid.absolute())]
    assert runtime_tool.exists()

    repeated = run_local_discovery(
        database_path=tmp_path / "registry.sqlite",
        model_roots=[models, missing_root],
        runtime_roots=[runtime],
        observed_at=observed_at,
    )
    assert [item.candidate.artifact_id for item in repeated.artifacts] == [
        item.candidate.artifact_id for item in result.artifacts
    ]
    assert [
        tuple((item.property_name, item.value) for item in artifact.capability_profile.metadata_evidence)
        if artifact.capability_profile is not None
        else ()
        for artifact in repeated.artifacts
    ] == [
        tuple((item.property_name, item.value) for item in artifact.capability_profile.metadata_evidence)
        if artifact.capability_profile is not None
        else ()
        for artifact in result.artifacts
    ]


class _MutatingScanner(LocalInventoryScanner):
    def __init__(self, path: Path) -> None:
        self.path = path

    def scan(self, model_roots=(), runtime_roots=()):
        inventory = super().scan(model_roots=model_roots, runtime_roots=runtime_roots)
        with self.path.open("ab") as stream:
            stream.write(b"changed after inventory snapshot")
        return inventory


def test_changed_inventory_snapshot_suppresses_identity_and_capability_hints(
    tmp_path: Path,
) -> None:
    model = _gguf(tmp_path / "models" / "model.gguf")
    result = run_local_discovery(
        database_path=tmp_path / "registry.sqlite",
        model_roots=[model.parent],
        observed_at=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc),
        scanner=_MutatingScanner(model),
    )

    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.parse_result is not None and artifact.parse_result.ok
    assert artifact.registry_record is not None
    assert artifact.registry_record.status == "artifact_changed_during_scan"
    assert artifact.registry_record.model_id is None
    assert artifact.capability_profile is None
    assert result.has_partial_results
