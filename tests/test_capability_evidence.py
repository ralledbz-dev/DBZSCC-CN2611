from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from dbzscc.miro import GGUFMetadataReader, extract_gguf_capability_evidence


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _entry(key: str, value_type: int, value: bytes) -> bytes:
    return _string(key) + _u32(value_type) + value


def _gguf(path: Path, entries: list[bytes]) -> Path:
    header = b"GGUF" + _u32(3) + _u64(0) + _u64(len(entries))
    path.write_bytes(header + b"".join(entries) + b"synthetic tensor payload")
    return path


def _read_profile(path: Path):
    result = GGUFMetadataReader().read(path)
    assert result.ok
    return extract_gguf_capability_evidence(result.metadata)


def test_extracts_allowlisted_metadata_with_provenance_but_no_runtime_claims(
    tmp_path: Path,
) -> None:
    template = "{% if tools %}tool-routing template{% endif %}"
    path = _gguf(
        tmp_path / "coder-vision-tool-calling.gguf",
        [
            _entry("general.architecture", 8, _string("qwen3")),
            _entry("general.name", 8, _string("Vision Tool-Calling Reasoning Coder")),
            _entry("qwen3.context_length", 4, _u32(32768)),
            _entry("qwen3.embedding_length", 4, _u32(4096)),
            _entry("qwen3.block_count", 4, _u32(36)),
            _entry("qwen3.attention.head_count", 4, _u32(32)),
            _entry("qwen3.attention.head_count_kv", 4, _u32(8)),
            _entry("tokenizer.ggml.model", 8, _string("gpt2")),
            _entry("tokenizer.chat_template", 8, _string(template)),
        ],
    )

    profile = _read_profile(path)
    properties = {item.property_name: item for item in profile.metadata_evidence}

    assert profile.identity.status == "resolved"
    assert profile.identity.normalized_architecture == "qwen3"
    assert properties["declared_context_length"].value == 32768
    assert properties["embedding_dimension"].value == 4096
    assert properties["transformer_block_count"].value == 36
    assert properties["attention_head_count"].value == 32
    assert properties["key_value_head_count"].value == 8
    assert properties["declared_tokenizer_model"].value == "gpt2"
    assert properties["declared_tokenizer_model"].evidence_class == "declared_metadata"
    assert properties["declared_context_length"].evidence_class == "technical_metadata"
    assert properties["chat_template_declared"].value == "present"
    assert properties["chat_template_declared"].content_sha256 == hashlib.sha256(
        template.encode("utf-8")
    ).hexdigest()
    assert properties["chat_template_declared"].content_length == len(template)
    assert all(item.source_offset > 0 for item in profile.metadata_evidence)
    assert tuple(item.source_offset for item in profile.metadata_evidence) == tuple(
        sorted(item.source_offset for item in profile.metadata_evidence)
    )

    assert profile.runtime_test_status == "not_tested"
    assert profile.runtime_tested_capabilities == ()
    assert profile.multimodal_status == "not_verified"
    assert profile.multimodal_verification_requirements == (
        "matching_model_projector_pair",
        "image_end_to_end_test",
    )


def test_filename_and_model_display_name_do_not_create_capability_claims(
    tmp_path: Path,
) -> None:
    path = _gguf(
        tmp_path / "Vision-Tool-Calling-Reasoning-Coder.gguf",
        [
            _entry("general.architecture", 8, _string("qwen3")),
            _entry("general.name", 8, _string("Vision Tool-Calling Reasoning Coder")),
        ],
    )

    profile = _read_profile(path)

    assert profile.identity.display_name == "Vision Tool-Calling Reasoning Coder"
    assert profile.metadata_evidence == ()
    assert profile.runtime_tested_capabilities == ()
    assert profile.multimodal_status == "not_verified"
    assert profile.image_status == "not_indicated"
    assert profile.audio_status == "not_indicated"
    assert profile.text_only_status == "not_verified"


def test_image_and_audio_candidates_require_separate_gguf_metadata_evidence(
    tmp_path: Path,
) -> None:
    image_model = _gguf(
        tmp_path / "ordinary-model.gguf",
        [
            _entry("general.architecture", 8, _string("qwen2vl")),
            _entry("general.name", 8, _string("Neutral model label")),
        ],
    )
    projector = _gguf(
        tmp_path / "ordinary-projector.gguf",
        [
            _entry("general.architecture", 8, _string("clip")),
            _entry("clip.projector_type", 8, _string("mlp")),
        ],
    )
    audio_model = _gguf(
        tmp_path / "not-an-audio-name.gguf",
        [_entry("general.architecture", 8, _string("whisper"))],
    )
    name_only = _gguf(
        tmp_path / "vision-audio-mmproj.gguf",
        [
            _entry("general.architecture", 8, _string("qwen3")),
            _entry("general.name", 8, _string("Vision audio projector model")),
        ],
    )

    image_profile = _read_profile(image_model)
    projector_profile = _read_profile(projector)
    audio_profile = _read_profile(audio_model)
    name_only_profile = _read_profile(name_only)

    assert image_profile.image_status == "metadata_candidate"
    assert image_profile.image_role == "base_model_candidate"
    assert image_profile.audio_status == "not_indicated"
    assert image_profile.multimodal_status == "not_verified"
    assert projector_profile.image_status == "metadata_candidate"
    assert projector_profile.image_role == "projector_candidate"
    assert "clip.projector_type" in projector_profile.image_evidence_keys
    assert projector_profile.audio_status == "not_indicated"
    assert audio_profile.audio_status == "metadata_candidate"
    assert audio_profile.audio_role == "base_model_candidate"
    assert audio_profile.image_status == "not_indicated"
    assert name_only_profile.image_status == "not_indicated"
    assert name_only_profile.audio_status == "not_indicated"
    assert name_only_profile.text_only_status == "not_verified"


def test_rejects_wrong_types_and_non_positive_architecture_values(tmp_path: Path) -> None:
    path = _gguf(
        tmp_path / "invalid-values.gguf",
        [
            _entry("general.architecture", 8, _string("qwen3")),
            _entry("general.name", 8, _string("Fixture")),
            _entry("qwen3.context_length", 8, _string("32768")),
            _entry("qwen3.embedding_length", 4, _u32(0)),
            _entry("tokenizer.ggml.model", 4, _u32(2)),
            _entry("tokenizer.chat_template", 8, _string("  ")),
        ],
    )

    profile = _read_profile(path)

    assert profile.metadata_evidence == ()
    assert {(item.source_key, item.code) for item in profile.issues} == {
        ("qwen3.context_length", "expected_positive_integer"),
        ("qwen3.embedding_length", "expected_positive_integer"),
        ("tokenizer.ggml.model", "expected_non_empty_string"),
        ("tokenizer.chat_template", "expected_non_empty_string"),
    }
    assert all(item.source_offset > 0 for item in profile.issues)
    assert profile.runtime_test_status == "not_tested"
    assert profile.multimodal_status == "not_verified"


def test_architecture_specific_fields_are_not_guessed_without_identity_architecture(
    tmp_path: Path,
) -> None:
    path = _gguf(
        tmp_path / "context-only.gguf",
        [
            _entry("llama.context_length", 4, _u32(4096)),
            _entry("general.name", 8, _string("Unresolved")),
        ],
    )

    profile = _read_profile(path)

    assert profile.identity.status == "unresolved"
    assert profile.metadata_evidence == ()
    assert profile.runtime_tested_capabilities == ()
