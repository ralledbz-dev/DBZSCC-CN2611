from __future__ import annotations

import hashlib
import json
import os
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbzscc.miro import CatalogQuery, LocalModelRegistry
from dbzscc.miro.model_registry import RegistryError


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _gguf(path: Path, *, name: str = "Catalog Model") -> Path:
    fields: dict[str, str | int] = {
        "general.architecture": "qwen3",
        "general.name": name,
        "qwen3.context_length": 4096,
        "tokenizer.ggml.model": "gpt2",
    }
    encoded = []
    for key, value in fields.items():
        if isinstance(value, str):
            encoded.append(_string(key) + _u32(8) + _string(value))
        else:
            encoded.append(_string(key) + _u32(4) + _u32(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"GGUF" + _u32(3) + _u64(1) + _u64(len(encoded)) + b"".join(encoded) + b"tensor"
    )
    return path


def _record(registry: LocalModelRegistry, model_path: Path):
    registry.synchronize(model_roots=[model_path.parent])
    return registry.query_catalog()[0]


def _qualification(
    registry: LocalModelRegistry,
    artifact_id: str,
    *,
    status: str,
    at: datetime,
):
    entry = next(item for item in registry.query_catalog() if item.artifact.artifact_id == artifact_id)
    assert entry.observation is not None
    full_hash = registry.ensure_full_file_sha256(
        artifact_id, expected_observation_id=entry.observation.observation_id
    )
    build_hash = "a" * 64
    common = {
        "backend": "cpu",
        "runtime_id": "llama.cpp/cpu-x64",
        "runtime_version": f"sha256:{build_hash}",
        "settings_profile": {"context_tokens": 512, "cpu_threads": 4, "target_id": "cpu"},
        "resource_profile": {"host_memory_mib": 1024, "cpu_threads": 4},
        "modality": "text",
        "source": "managed-chat-complete-response",
        "model_full_file_sha256": full_hash,
        "device_physical_id": "cpu:fixture-x64",
        "runtime_parameters": {"context_tokens": 512, "threads": 4, "target_id": "cpu"},
    }
    if status == "qualified":
        for index in (1, 2):
            answer = f"complete answer {index}"
            registry.record_runtime_qualification(
                artifact_id,
                **common,
                status="runtime_tested",
                evidence={
                    "run_id": f"stored-run-{index}",
                    "response_complete": True,
                    "response_persisted": True,
                    "response_message_id": f"stored-message-{index}",
                    "response_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    "model_observation_id": entry.observation.observation_id,
                    "runtime_build_sha256": build_hash,
                },
                observed_at=at + timedelta(seconds=index),
            )
        qualified = registry.query_catalog(CatalogQuery(require_qualified=True))
        return next(
            evidence
            for item in qualified
            if item.artifact.artifact_id == artifact_id
            for evidence in item.runtime_qualifications
            if evidence.status == "qualified"
        )
    return registry.record_runtime_qualification(
        artifact_id,
        **common,
        status=status,
        evidence={"run_id": "timeout-run", "response_complete": False},
        observed_at=at,
    )


def test_catalog_persists_typed_metadata_and_capability_evidence(tmp_path: Path) -> None:
    model = _gguf(tmp_path / "models" / "model.gguf")
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")

    entry = _record(registry, model)

    assert entry.artifact.path == str(model.absolute())
    assert entry.model is not None and entry.model.display_name == "Catalog Model"
    assert entry.observation is not None
    assert entry.observation.parser_version == "gguf-prefix-v1"
    profile = json.loads(entry.capability_evidence_json)
    assert profile["parser_version"] == "gguf-prefix-v1"
    assert profile["runtime_test_status"] == "not_tested"
    assert profile["image"]["status"] == "not_indicated"
    assert dict(entry.selected_metadata)["general.architecture"] == "qwen3"
    assert entry.full_file_sha256 is None


def test_selected_full_hash_is_lazy_persisted_and_detects_same_snapshot_tensor_edit(
    tmp_path: Path,
) -> None:
    model = _gguf(tmp_path / "models" / "model.gguf")
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")
    entry = _record(registry, model)
    assert entry.observation is not None

    original = registry.ensure_full_file_sha256(
        entry.artifact.artifact_id,
        expected_observation_id=entry.observation.observation_id,
    )
    assert registry.query_catalog()[0].full_file_sha256 == original
    original_stat = model.stat()
    payload = model.read_bytes()
    model.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    os.utime(model, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(RegistryError, match="bytes changed"):
        registry.ensure_full_file_sha256(
            entry.artifact.artifact_id,
            expected_observation_id=entry.observation.observation_id,
        )


def test_latest_runtime_evidence_wins_for_the_exact_qualification_key(tmp_path: Path) -> None:
    model = _gguf(tmp_path / "models" / "model.gguf")
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")
    entry = _record(registry, model)
    assert entry.observation is not None
    qualified_at = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    timed_out_at = datetime.now(timezone.utc) + timedelta(minutes=1)

    qualified = _qualification(
        registry, entry.artifact.artifact_id, status="qualified", at=qualified_at
    )
    _qualification(registry, entry.artifact.artifact_id, status="timeout", at=timed_out_at)

    latest = registry.query_catalog(
        CatalogQuery(
            backend="cpu",
            runtime_id="llama.cpp/cpu-x64",
            runtime_version="sha256:" + "a" * 64,
            settings_profile_hash=qualified.settings_profile_hash,
        )
    )[0].runtime_qualifications
    assert len(latest) == 1
    assert latest[0].status == "timeout"
    assert registry.query_catalog(
        CatalogQuery(
            backend="cpu",
            runtime_id="llama.cpp/cpu-x64",
            runtime_version="sha256:" + "a" * 64,
            settings_profile_hash=qualified.settings_profile_hash,
            require_qualified=True,
        )
    ) == ()


def test_file_snapshot_change_hides_stale_runtime_qualification(tmp_path: Path) -> None:
    model = _gguf(tmp_path / "models" / "model.gguf")
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")
    entry = _record(registry, model)
    assert entry.observation is not None
    qualified = _qualification(
        registry,
        entry.artifact.artifact_id,
        status="qualified",
        at=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
    )

    _gguf(model, name="Changed Model")
    registry.synchronize(model_roots=[model.parent])

    assert registry.query_catalog(
        CatalogQuery(
            backend="cpu",
            runtime_id="llama.cpp/cpu-x64",
            runtime_version="sha256:" + "a" * 64,
            settings_profile_hash=qualified.settings_profile_hash,
            require_qualified=True,
        )
    ) == ()
    with registry._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_qualifications").fetchone()[0] == 3


def test_two_independent_runtime_tested_runs_create_qualified_evidence(tmp_path: Path) -> None:
    model = _gguf(tmp_path / "models" / "model.gguf")
    registry = LocalModelRegistry(tmp_path / "registry.sqlite")
    entry = _record(registry, model)
    settings = {"context_tokens": 512, "cpu_threads": 4, "target_id": "cpu"}
    assert entry.observation is not None
    full_hash = registry.ensure_full_file_sha256(
        entry.artifact.artifact_id,
        expected_observation_id=entry.observation.observation_id,
    )
    build_hash = "b" * 64

    def record_test(run_id: str, *, complete: bool = True) -> None:
        response_sha256 = hashlib.sha256(run_id.encode()).hexdigest()
        registry.record_runtime_qualification(
            entry.artifact.artifact_id,
            backend="cpu",
            runtime_id="llama.cpp/cpu-x64",
            runtime_version=f"sha256:{build_hash}",
            settings_profile=settings,
            resource_profile={"host_memory_mib": 1024, "cpu_threads": 4},
            modality="text",
            status="runtime_tested",
            source="managed-chat-complete-response",
            model_full_file_sha256=full_hash,
            device_physical_id="cpu:fixture-x64",
            runtime_parameters={
                "backend": "cpu",
                "context_tokens": 512,
                "cpu_threads": 4,
                "target_id": "cpu",
            },
            evidence={
                "run_id": run_id,
                "response_complete": complete,
                "response_persisted": True,
                "response_message_id": f"stored-message-{run_id}",
                "response_sha256": response_sha256,
                "model_observation_id": entry.observation.observation_id,
                "runtime_build_sha256": build_hash,
            },
        )

    record_test("chat-run-1")
    record_test("chat-run-1")
    record_test("incomplete-run", complete=False)
    assert registry.query_catalog(CatalogQuery(require_qualified=True)) == ()
    record_test("chat-run-2")

    qualified = registry.query_catalog(CatalogQuery(require_qualified=True))
    assert len(qualified) == 1
    evidence = qualified[0].runtime_qualifications
    assert len(evidence) == 1
    assert evidence[0].status == "qualified"
    derived = json.loads(evidence[0].evidence_json)
    assert sorted(derived["successful_answers"]) == ["chat-run-1", "chat-run-2"]
