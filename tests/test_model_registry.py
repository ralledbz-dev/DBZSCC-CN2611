from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dbzscc.miro import (
    LocalInventoryScanner,
    LocalModelRegistry,
    RegistryStorageError,
    normalize_gguf_identity,
)
from dbzscc.miro.gguf_metadata import GGUFMetadataReader


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _string(value: str) -> bytes:
    data = value.encode("utf-8")
    return _u64(len(data)) + data


def _gguf(path: Path, metadata: dict[str, str | int]) -> Path:
    entries = []
    for key, value in metadata.items():
        if isinstance(value, str):
            entries.append(_string(key) + _u32(8) + _string(value))
        else:
            entries.append(_string(key) + _u32(4) + _u32(value))
    prefix = b"GGUF" + _u32(3) + _u64(1) + _u64(len(entries)) + b"".join(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + b"synthetic tensor payload")
    return path


def _metadata(name: str = "Demo Coder", architecture: str = "qwen3") -> dict[str, str | int]:
    return {
        "general.architecture": architecture,
        "general.name": name,
        "general.organization": "Example Org",
        "general.file_type": 15,
        "general.quantization_version": 2,
    }


def test_identity_is_stable_and_quantization_is_not_model_identity(tmp_path: Path) -> None:
    first_path = _gguf(tmp_path / "first.gguf", _metadata())
    second_values = _metadata(name="  demo   coder ", architecture="QWEN3")
    second_values["general.file_type"] = 14
    second_path = _gguf(tmp_path / "second.gguf", second_values)
    reader = GGUFMetadataReader()
    first = normalize_gguf_identity(reader.read(first_path).metadata)
    second = normalize_gguf_identity(reader.read(second_path).metadata)

    assert first.model_id == second.model_id
    assert first.status == second.status == "resolved"
    assert first.normalized_name == "demo coder"
    assert first.normalized_architecture == "qwen3"
    assert "general.architecture" in first.evidence
    assert "general.name" in first.evidence
    assert dict(first.selected_metadata)["general.file_type"] == 15


def test_basename_fallback_is_partial_and_never_uses_filename(tmp_path: Path) -> None:
    path = _gguf(
        tmp_path / "Misleading-Filename.gguf",
        {"general.architecture": "test-arch", "general.basename": "Recorded Base"},
    )
    result = normalize_gguf_identity(GGUFMetadataReader().read(path).metadata)

    assert result.model_id is not None
    assert result.display_name == "Recorded Base"
    assert result.status == "partial"
    assert result.uncertainty == ("name_from_general_basename",)
    assert "Misleading-Filename" not in result.display_name


def test_missing_identity_metadata_is_persisted_as_unresolved(tmp_path: Path) -> None:
    model_path = _gguf(tmp_path / "models" / "unknown.gguf", {"general.name": "Named"})
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")

    result = registry.synchronize(model_roots=[model_path.parent])
    artifact = registry.list_artifacts()[0]
    observation = registry.list_observations()[0]

    assert result.unresolved_identity_count == 1
    assert artifact.model_id is None
    assert artifact.status == "metadata_read"
    assert artifact.identity_status == "unresolved"
    assert json.loads(observation.identity_uncertainty_json) == [
        "missing_general_architecture"
    ]
    assert registry.list_models() == ()


def test_repeated_sync_upserts_without_duplicate_artifacts_or_observations(
    tmp_path: Path,
) -> None:
    model_path = _gguf(tmp_path / "models" / "model.gguf", _metadata())
    registry_path = tmp_path / "registry.sqlite"
    registry = LocalModelRegistry(registry_path)
    first_time = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    second_time = datetime(2026, 11, 2, 12, 5, tzinfo=timezone.utc)

    first = registry.synchronize(model_roots=[model_path.parent], observed_at=first_time)
    first_artifact = registry.list_artifacts()[0]
    first_observation = registry.list_observations()[0]
    second = registry.synchronize(model_roots=[model_path.parent], observed_at=second_time)

    artifacts = registry.list_artifacts()
    models = registry.list_models()
    observations = registry.list_observations()
    assert first.artifact_count == second.artifact_count == 1
    assert len(artifacts) == len(models) == len(observations) == 1
    assert first_artifact.artifact_id == artifacts[0].artifact_id
    assert first_observation.observation_id == observations[0].observation_id
    assert artifacts[0].first_seen_at == "2026-11-02T12:00:00.000000Z"
    assert artifacts[0].last_seen_at == "2026-11-02T12:05:00.000000Z"
    assert observations[0].first_seen_at == "2026-11-02T12:00:00.000000Z"
    assert observations[0].last_seen_at == "2026-11-02T12:05:00.000000Z"
    parsed = GGUFMetadataReader().read(model_path).metadata
    assert artifacts[0].metadata_prefix_sha256 == parsed.metadata_prefix_sha256
    assert artifacts[0].metadata_prefix_sha256 != hashlib.sha256(
        model_path.read_bytes()
    ).hexdigest()


def test_same_model_at_distinct_paths_creates_two_artifacts_one_model(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models"
    first_path = _gguf(models / "one.gguf", _metadata())
    second_values = _metadata(name="demo coder", architecture="QWEN3")
    second_values["general.file_type"] = 14
    _gguf(models / "two.gguf", second_values)
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")

    registry.synchronize(model_roots=[models])

    assert len(registry.list_artifacts()) == 2
    assert len(registry.list_models()) == 1
    artifact_records = registry.list_artifacts()
    assert artifact_records[0].artifact_id != artifact_records[1].artifact_id
    assert artifact_records[0].model_id == artifact_records[1].model_id
    assert first_path.exists()


def test_parse_failure_is_recorded_without_creating_model_identity(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    broken = models / "broken.gguf"
    broken.write_bytes(b"not a GGUF file")
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")

    result = registry.synchronize(model_roots=[models])
    artifact = registry.list_artifacts()[0]
    observation = registry.list_observations()[0]

    assert result.parse_failure_count == 1
    assert artifact.status == "metadata_parse_failed"
    assert artifact.identity_status == "unresolved"
    assert artifact.parse_issue_code == "invalid_magic"
    assert artifact.metadata_prefix_sha256 is None
    assert observation.model_id is None
    assert observation.parse_issue_code == "invalid_magic"
    assert registry.list_models() == ()


def test_database_failure_rolls_back_every_registry_write(tmp_path: Path) -> None:
    models = tmp_path / "models"
    first = _gguf(models / "a.gguf", _metadata(name="Model A"))
    second = _gguf(models / "z.gguf", _metadata(name="Model Z"))
    registry_path = tmp_path / "registry.sqlite"
    registry = LocalModelRegistry(registry_path)
    second_artifact = next(
        item
        for item in LocalInventoryScanner().scan(model_roots=[models]).artifacts
        if item.path == str(second.absolute())
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute(
            f"""CREATE TRIGGER fail_second_artifact BEFORE INSERT ON artifacts
            WHEN NEW.artifact_id = '{second_artifact.artifact_id}'
            BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END"""
        )

    with pytest.raises(RegistryStorageError, match="injected transaction failure"):
        registry.synchronize(model_roots=[models])

    assert registry.list_artifacts() == ()
    assert registry.list_models() == ()
    assert registry.list_observations() == ()
    assert first.exists()


def test_registry_data_survives_reopen_and_runtime_candidates_stay_unlinked(
    tmp_path: Path,
) -> None:
    model_path = _gguf(tmp_path / "models" / "model.gguf", _metadata())
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "llama-cli.exe").write_bytes(b"not executed")
    registry_path = tmp_path / "registry.sqlite"
    LocalModelRegistry(registry_path).synchronize(
        model_roots=[model_path.parent], runtime_roots=[runtime]
    )

    reopened = LocalModelRegistry(registry_path)
    records = reopened.list_artifacts()
    runtime_record = next(item for item in records if item.kind == "llama_executable_candidate")
    model_record = next(item for item in records if item.kind == "gguf_candidate")

    assert len(records) == 2
    assert runtime_record.status == "runtime_candidate"
    assert runtime_record.identity_status == "not_applicable"
    assert runtime_record.model_id is None
    assert runtime_record.metadata_prefix_sha256 is None
    assert model_record.status == "metadata_read"
    assert model_record.model_id is not None
    assert len(reopened.list_models()) == 1


def test_rejects_non_persistent_memory_database_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="persistent file path"):
        LocalModelRegistry(":memory:")
