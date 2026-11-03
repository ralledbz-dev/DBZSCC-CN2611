from __future__ import annotations

import struct
from pathlib import Path

from dbzscc.app.miro_service import MiroApplicationService
from dbzscc.miro import LocalInventoryScanner, pair_multimodal_candidates


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _entry(key: str, value: str) -> bytes:
    return _string(key) + _u32(8) + _string(value)


def _gguf(path: Path, values: tuple[tuple[str, str], ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = b"".join(_entry(key, value) for key, value in values)
    header = b"GGUF" + _u32(3) + _u64(0) + _u64(len(values))
    path.write_bytes(header + entries + b"synthetic tensor payload")
    return path


def test_pairs_separated_image_and_audio_candidates_from_metadata_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    _gguf(
        root / "deep" / "base-a.gguf",
        (
            ("general.architecture", "qwen2vl"),
            ("general.basename", "family-a"),
        ),
    )
    _gguf(
        root / "elsewhere" / "unrelated-projector-name.gguf",
        (
            ("general.architecture", "clip"),
            ("general.basename", "family-a"),
            ("clip.projector_type", "mlp"),
        ),
    )
    _gguf(
        root / "audio" / "base-b.gguf",
        (("general.architecture", "whisper"),),
    )
    _gguf(
        root / "audio" / "projector-b.gguf",
        (
            ("general.architecture", "audio"),
            ("audio.projector_type", "whisper"),
            ("audio.model_architecture", "whisper"),
        ),
    )

    result = MiroApplicationService(tmp_path / "registry.sqlite").discover(
        model_roots=(root,),
        runtime_roots=(),
        scanner=LocalInventoryScanner(
            max_depth=None,
            max_entries=1000,
            max_candidates=100,
            max_elapsed_seconds=10.0,
            excluded_directory_names=(),
        ),
    )
    pairs = pair_multimodal_candidates(result.artifacts)

    assert {(item.modality, item.evidence) for item in pairs} == {
        ("image", ("shared_general.basename_metadata",)),
        ("audio", ("audio.model_architecture_matches_general.architecture",)),
    }
    assert len(pairs) == 2
    assert all(item.runtime_verified is False for item in pairs)
    assert {
        item.modality: (item.status, item.compatibility_status) for item in pairs
    } == {
        "image": (
            "metadata_association_candidate",
            "association_only_compatibility_unestablished",
        ),
        "audio": (
            "metadata_architecture_candidate",
            "architecture_candidate_not_runtime_verified",
        ),
    }
    image_pair = next(item for item in pairs if item.modality == "image")
    assert "deep" in image_pair.model_path
    assert "elsewhere" in image_pair.projector_path


def test_matching_filenames_do_not_create_a_pair_without_metadata_match(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    _gguf(
        root / "base" / "mmproj.gguf",
        (
            ("general.architecture", "gemma3"),
            ("general.basename", "family-a"),
        ),
    )
    _gguf(
        root / "projector" / "mmproj.gguf",
        (
            ("general.architecture", "clip"),
            ("general.basename", "different-family"),
            ("clip.projector_type", "gemma3"),
        ),
    )

    result = MiroApplicationService(tmp_path / "registry.sqlite").discover(
        model_roots=(root,),
        runtime_roots=(),
    )

    assert pair_multimodal_candidates(result.artifacts) == ()
