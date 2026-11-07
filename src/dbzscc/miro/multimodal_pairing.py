"""Conservative metadata-only pairing of multimodal GGUF artifacts.

Pairs are candidates, not runtime compatibility claims. Artifact paths are
returned for traceability but are never used to decide whether two files match.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .discovery_pipeline import LocalDiscoveryArtifact
from .gguf_metadata import GGUFMetadataDocument, GGUFMetadataField
from .model_registry import ModelCatalogEntry


@dataclass(frozen=True)
class MultimodalPairCandidate:
    """A metadata-supported model/projector pairing awaiting runtime proof."""

    pair_id: str
    modality: str
    model_artifact_id: str
    projector_artifact_id: str
    model_metadata_sha256: str
    projector_metadata_sha256: str
    model_path: str
    projector_path: str
    evidence: tuple[str, ...]
    status: str
    compatibility_status: str
    runtime_verified: bool = False


@dataclass(frozen=True)
class _PairInput:
    artifact_id: str
    path: str
    metadata: Mapping[str, Any]
    image_role: str
    audio_role: str
    metadata_sha256: str


def pair_multimodal_candidates(
    artifacts: Iterable[LocalDiscoveryArtifact],
) -> tuple[MultimodalPairCandidate, ...]:
    """Pair only when GGUF metadata supplies an explicit compatibility signal.

    Image projectors are recognized from parsed ``clip.*`` metadata; audio
    projectors require explicit ``audio.projector_type`` metadata. A pair needs
    either a shared non-empty ``general.basename`` metadata value or an
    explicit projector-to-model architecture field. Generic ``projector_type``
    labels are not compatibility links. File names and directory proximity are
    intentionally not consulted.
    """
    inputs = []
    for item in artifacts:
        profile = item.capability_profile
        if profile is None or item.parse_result is None or item.parse_result.metadata is None:
            continue
        if item.registry_record is None or not item.registry_record.metadata_prefix_sha256:
            continue
        inputs.append(
            _PairInput(
                artifact_id=item.candidate.artifact_id,
                path=item.candidate.path,
                metadata={
                    field.key: field.value for field in item.parse_result.metadata.fields
                },
                image_role=profile.image_role,
                audio_role=profile.audio_role,
                metadata_sha256=item.registry_record.metadata_prefix_sha256,
            )
        )
    return _pair_inputs(inputs)


def pair_multimodal_catalog_entries(
    entries: Iterable[ModelCatalogEntry],
) -> tuple[MultimodalPairCandidate, ...]:
    """Pair current persisted GGUF evidence across folders and scan pages."""
    inputs = []
    for entry in entries:
        observation = entry.observation
        if (
            observation is None
            or observation.status != "metadata_read"
            or not observation.metadata_prefix_sha256
        ):
            continue
        inputs.append(
            _PairInput(
                artifact_id=entry.artifact.artifact_id,
                path=entry.artifact.path,
                metadata=dict(entry.selected_metadata),
                image_role=entry.image_role,
                audio_role=entry.audio_role,
                metadata_sha256=observation.metadata_prefix_sha256,
            )
        )
    return _pair_inputs(inputs)


def _pair_inputs(inputs: list[_PairInput]) -> tuple[MultimodalPairCandidate, ...]:
    image_models: list[_PairInput] = []
    image_projectors: list[_PairInput] = []
    audio_models: list[_PairInput] = []
    audio_projectors: list[_PairInput] = []
    for item in inputs:
        if item.image_role == "base_model_candidate":
            image_models.append(item)
        elif item.image_role == "projector_candidate":
            image_projectors.append(item)
        if item.audio_role == "base_model_candidate":
            audio_models.append(item)
        elif item.audio_role == "projector_candidate":
            audio_projectors.append(item)

    pairs = []
    pairs.extend(_pair_group(image_models, image_projectors, "image"))
    pairs.extend(_pair_group(audio_models, audio_projectors, "audio"))
    return tuple(
        sorted(
            pairs,
            key=lambda item: (
                item.modality,
                item.model_path.casefold(),
                item.projector_path.casefold(),
            ),
        )
    )


def _pair_group(
    models: list[_PairInput],
    projectors: list[_PairInput],
    modality: str,
) -> list[MultimodalPairCandidate]:
    result = []
    for model in models:
        model_architecture = _text_value(model.metadata, "general.architecture")
        if not model_architecture:
            continue
        for projector in projectors:
            evidence = _compatibility_evidence(
                model.metadata, projector.metadata, model_architecture, modality
            )
            if not evidence:
                continue
            pair_material = "\0".join(
                (
                    modality,
                    model.artifact_id,
                    projector.artifact_id,
                    model.metadata_sha256,
                    projector.metadata_sha256,
                    "|".join(evidence),
                )
            )
            pair_id = hashlib.sha256(pair_material.encode("utf-8")).hexdigest()[:24]
            explicit_architecture_link = any(
                item.endswith("_matches_general.architecture")
                for item in evidence
            )
            result.append(
                MultimodalPairCandidate(
                    pair_id=pair_id,
                    modality=modality,
                    model_artifact_id=model.artifact_id,
                    projector_artifact_id=projector.artifact_id,
                    model_metadata_sha256=model.metadata_sha256,
                    projector_metadata_sha256=projector.metadata_sha256,
                    model_path=model.path,
                    projector_path=projector.path,
                    evidence=evidence,
                    status=(
                        "metadata_architecture_candidate"
                        if explicit_architecture_link
                        else "metadata_association_candidate"
                    ),
                    compatibility_status=(
                        "architecture_candidate_not_runtime_verified"
                        if explicit_architecture_link
                        else "association_only_compatibility_unestablished"
                    ),
                )
            )
    return result


def _compatibility_evidence(
    model: Mapping[str, Any],
    projector: Mapping[str, Any],
    model_architecture: str,
    modality: str,
) -> tuple[str, ...]:
    evidence: list[str] = []
    model_basename = _text_value(model, "general.basename")
    projector_basename = _text_value(projector, "general.basename")
    if (
        model_basename
        and projector_basename
        and _normalize(model_basename) == _normalize(projector_basename)
    ):
        evidence.append("shared_general.basename_metadata")

    if modality == "image":
        compatibility_keys = (
            "clip.model_architecture",
            "clip.projector_architecture",
        )
    else:
        compatibility_keys = (
            "audio.model_architecture",
            "audio.projector_architecture",
        )

    for key in compatibility_keys:
        value = _text_value(projector, key)
        if value and _normalize(value) == _normalize(model_architecture):
            evidence.append(f"{key}_matches_general.architecture")
    return tuple(sorted(set(evidence)))


def _text_value(document: Mapping[str, Any] | GGUFMetadataDocument, key: str) -> str | None:
    if isinstance(document, Mapping):
        value = document.get(key)
    else:
        field: GGUFMetadataField | None = document.field(key)
        if field is None or field.type_name != "string":
            return None
        value = field.value
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)
