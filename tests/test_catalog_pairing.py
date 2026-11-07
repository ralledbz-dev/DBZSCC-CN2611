from __future__ import annotations

import struct
from pathlib import Path

from dbzscc.app.miro_service import MiroApplicationService
from dbzscc.miro import ResumableLocalInventoryScanner


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _gguf(path: Path, values: tuple[tuple[str, str], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = b"".join(
        _string(key) + _u32(8) + _string(value) for key, value in values
    )
    path.write_bytes(b"GGUF" + _u32(3) + _u64(0) + _u64(len(values)) + fields + b"tensor")


def test_pairing_is_persisted_across_scan_pages_and_separate_folders(tmp_path: Path) -> None:
    root = tmp_path / "models"
    _gguf(
        root / "llm" / "base.gguf",
        (("general.architecture", "qwen2vl"), ("general.basename", "family-a")),
    )
    _gguf(
        root / "vision" / "image-projector.gguf",
        (
            ("general.architecture", "clip"),
            ("general.basename", "family-a"),
            ("clip.projector_type", "mlp"),
        ),
    )
    service = MiroApplicationService(tmp_path / "registry.sqlite")
    scanner = ResumableLocalInventoryScanner(
        max_entries=1, max_candidates=20, max_elapsed_seconds=30
    )

    result = service.discover(model_roots=(root,), scanner=scanner)
    pages = 1
    while not result.complete:
        result = service.discover(model_roots=(root,), scanner=scanner)
        pages += 1
        assert pages < 20

    assert pages > 1
    assert len(result.multimodal_pairs) == 1
    pair = result.multimodal_pairs[0]
    assert pair.modality == "image"
    assert pair.runtime_verified is False
    assert pair.model_path.endswith("base.gguf")
    assert pair.projector_path.endswith("image-projector.gguf")

    model_entry = service.find_model_artifact(pair.model_path)
    assert model_entry is not None
    assert len(model_entry.multimodal_pairings) == 1
    assert model_entry.multimodal_pairings[0].status == "metadata_association_candidate"
    assert model_entry.multimodal_pairings[0].runtime_test_status == "not_tested"
    pair_record = model_entry.multimodal_pairings[0]
    assert model_entry.full_file_sha256 is None

    model_hash, projector_hash = service.ensure_multimodal_pair_full_hashes(
        pair_record.pair_id
    )
    hashed_entry = service.find_model_artifact(pair.model_path)
    assert hashed_entry is not None
    hashed_pair = hashed_entry.multimodal_pairings[0]
    assert hashed_pair.model_full_file_sha256 == model_hash
    assert hashed_pair.projector_full_file_sha256 == projector_hash
    assert hashed_pair.runtime_test_status == "not_tested"
