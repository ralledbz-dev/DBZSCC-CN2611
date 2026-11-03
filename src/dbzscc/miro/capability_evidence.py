"""Conservative, read-only capability evidence from parsed GGUF metadata.

This module reports selected metadata properties; it does not infer model
quality, tool use, modality support, or runtime compatibility.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .gguf_metadata import GGUFMetadataDocument, GGUFMetadataField
from .model_registry import NormalizedModelIdentity, normalize_gguf_identity

_INTEGER_TYPES = frozenset(
    {"uint8", "uint16", "uint32", "uint64", "int8", "int16", "int32", "int64"}
)
_ARCHITECTURE_PROPERTIES = (
    ("declared_context_length", "context_length"),
    ("embedding_dimension", "embedding_length"),
    ("transformer_block_count", "block_count"),
    ("attention_head_count", "attention.head_count"),
    ("key_value_head_count", "attention.head_count_kv"),
)
_IMAGE_ARCHITECTURES = frozenset(
    {
        "deepseek2",
        "gemma3",
        "internvl",
        "llava",
        "llava_next",
        "mllama",
        "minicpmv",
        "ovis",
        "pixtral",
        "qwen2vl",
        "qwen2_5vl",
        "qwen3vl",
        "qwen3_5vl",
    }
)
_AUDIO_ARCHITECTURES = frozenset(
    {
        "audio",
        "audiocpp",
        "kokoro",
        "parakeet",
        "qwen2audio",
        "qwen2_audio",
        "whisper",
        "wav",
    }
)
_IMAGE_PROJECTOR_KEYS = frozenset(
    {"clip.projector_type", "clip.has_vision_encoder", "clip.has_llava_projector"}
)
_AUDIO_PROJECTOR_KEYS = frozenset(
    {"audio.projector_type", "audio.has_projector", "audio.projector"}
)


@dataclass(frozen=True)
class CapabilityEvidence:
    """One allowlisted metadata property with its source and evidence class."""

    property_name: str
    value: int | str
    evidence_class: str
    source_key: str
    source_type_name: str
    source_offset: int
    content_sha256: str | None = None
    content_length: int | None = None


@dataclass(frozen=True)
class CapabilityExtractionIssue:
    """A recognized field that could not be safely interpreted."""

    source_key: str
    code: str
    source_offset: int


@dataclass(frozen=True)
class CapabilityProfile:
    """Metadata evidence kept separate from capabilities verified at runtime."""

    identity: NormalizedModelIdentity
    metadata_evidence: tuple[CapabilityEvidence, ...]
    issues: tuple[CapabilityExtractionIssue, ...]
    runtime_test_status: str = "not_tested"
    runtime_tested_capabilities: tuple[str, ...] = ()
    multimodal_status: str = "not_verified"
    multimodal_verification_requirements: tuple[str, ...] = (
        "matching_model_projector_pair",
        "image_end_to_end_test",
    )
    image_status: str = "not_indicated"
    image_role: str = "not_indicated"
    image_evidence_keys: tuple[str, ...] = ()
    audio_status: str = "not_indicated"
    audio_role: str = "not_indicated"
    audio_evidence_keys: tuple[str, ...] = ()
    text_only_status: str = "not_verified"


def extract_gguf_capability_evidence(
    document: GGUFMetadataDocument,
) -> CapabilityProfile:
    """Extract a small set of declared/technical properties from one GGUF.

    Architecture-specific fields are considered only when the existing model
    identity normalizer resolves an architecture. File names and model display
    names are never inspected for capability claims. Metadata evidence is not a
    runtime test; in particular, this function never verifies multimodal use.
    """
    identity = normalize_gguf_identity(document)
    fields = {item.key: item for item in document.fields}
    image_status, image_role, image_keys, audio_status, audio_role, audio_keys = (
        _classify_modality_metadata(fields, identity.normalized_architecture)
    )
    evidence: list[CapabilityEvidence] = []
    issues: list[CapabilityExtractionIssue] = []

    if identity.architecture is not None:
        for property_name, suffix in _ARCHITECTURE_PROPERTIES:
            source_key = f"{identity.architecture}.{suffix}"
            field = fields.get(source_key)
            if field is None:
                continue
            value = _positive_integer(field)
            if value is None:
                issues.append(
                    CapabilityExtractionIssue(
                        source_key=source_key,
                        code="expected_positive_integer",
                        source_offset=field.offset,
                    )
                )
                continue
            evidence.append(
                CapabilityEvidence(
                    property_name=property_name,
                    value=value,
                    evidence_class="technical_metadata",
                    source_key=source_key,
                    source_type_name=field.type_name,
                    source_offset=field.offset,
                )
            )

    tokenizer = fields.get("tokenizer.ggml.model")
    if tokenizer is not None:
        value = _non_empty_string(tokenizer)
        if value is None:
            issues.append(
                CapabilityExtractionIssue(
                    source_key=tokenizer.key,
                    code="expected_non_empty_string",
                    source_offset=tokenizer.offset,
                )
            )
        else:
            evidence.append(
                CapabilityEvidence(
                    property_name="declared_tokenizer_model",
                    value=value,
                    evidence_class="declared_metadata",
                    source_key=tokenizer.key,
                    source_type_name=tokenizer.type_name,
                    source_offset=tokenizer.offset,
                )
            )

    template = fields.get("tokenizer.chat_template")
    if template is not None:
        value = _non_empty_string(template)
        if value is None:
            issues.append(
                CapabilityExtractionIssue(
                    source_key=template.key,
                    code="expected_non_empty_string",
                    source_offset=template.offset,
                )
            )
        else:
            encoded = value.encode("utf-8")
            evidence.append(
                CapabilityEvidence(
                    property_name="chat_template_declared",
                    value="present",
                    evidence_class="declared_metadata",
                    source_key=template.key,
                    source_type_name=template.type_name,
                    source_offset=template.offset,
                    content_sha256=hashlib.sha256(encoded).hexdigest(),
                    content_length=len(value),
                )
            )

    return CapabilityProfile(
        identity=identity,
        metadata_evidence=tuple(sorted(evidence, key=lambda item: item.source_offset)),
        issues=tuple(sorted(issues, key=lambda item: item.source_offset)),
        image_status=image_status,
        image_role=image_role,
        image_evidence_keys=image_keys,
        audio_status=audio_status,
        audio_role=audio_role,
        audio_evidence_keys=audio_keys,
    )


def _classify_modality_metadata(
    fields: dict[str, GGUFMetadataField], architecture: str | None
) -> tuple[str, str, tuple[str, ...], str, str, tuple[str, ...]]:
    """Classify metadata candidates only; this never asserts runtime support."""
    image_keys = tuple(
        sorted(
            key
            for key in fields
            if key in _IMAGE_PROJECTOR_KEYS
            or key.startswith(("clip.vision.", "vision.", "image."))
        )
    )
    image_architecture_match = architecture in _IMAGE_ARCHITECTURES
    projector_marker = bool(image_keys)
    if image_architecture_match and projector_marker:
        image_status, image_role = "metadata_candidate", "base_and_projector_candidate"
    elif image_architecture_match:
        image_status, image_role = "metadata_candidate", "base_model_candidate"
    elif projector_marker:
        image_status, image_role = "metadata_candidate", "projector_candidate"
    else:
        image_status, image_role = "not_indicated", "not_indicated"

    audio_keys = tuple(
        sorted(
            key
            for key in fields
            if key.startswith(("audio.", "whisper.", "encoder.audio.", "decoder.audio."))
        )
    )
    audio_architecture_match = architecture in _AUDIO_ARCHITECTURES
    audio_projector_marker = any(key in _AUDIO_PROJECTOR_KEYS for key in audio_keys)
    if audio_projector_marker:
        audio_status, audio_role = "metadata_candidate", "projector_candidate"
    elif audio_architecture_match or audio_keys:
        audio_status, audio_role = "metadata_candidate", "base_model_candidate"
    else:
        audio_status, audio_role = "not_indicated", "not_indicated"

    return (
        image_status,
        image_role,
        image_keys,
        audio_status,
        audio_role,
        audio_keys,
    )


def _positive_integer(field: GGUFMetadataField) -> int | None:
    if field.type_name not in _INTEGER_TYPES or type(field.value) is not int:
        return None
    if field.value <= 0:
        return None
    return field.value


def _non_empty_string(field: GGUFMetadataField) -> str | None:
    if field.type_name != "string" or not isinstance(field.value, str):
        return None
    value = field.value.strip()
    return value or None
