"""Conservative metadata-only pairing of multimodal GGUF artifacts.

Pairs are candidates, not runtime compatibility claims. Artifact paths are
returned for traceability but are never used to decide whether two files match.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from .discovery_pipeline import LocalDiscoveryArtifact
from .gguf_metadata import GGUFMetadataDocument, GGUFMetadataField


@dataclass(frozen=True)
class MultimodalPairCandidate:
    """A metadata-supported model/projector pairing awaiting runtime proof."""

    pair_id: str
    modality: str
    model_artifact_id: str
    projector_artifact_id: str
    model_path: str
    projector_path: str
    evidence: tuple[str, ...]
    status: str
    compatibility_status: str
    runtime_verified: bool = False


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
    image_models: list[LocalDiscoveryArtifact] = []
    image_projectors: list[LocalDiscoveryArtifact] = []
    audio_models: list[LocalDiscoveryArtifact] = []
    audio_projectors: list[LocalDiscoveryArtifact] = []

    for item in artifacts:
        profile = item.capability_profile
        if profile is None or item.parse_result is None or item.parse_result.metadata is None:
            continue
        if profile.image_role == "base_model_candidate":
            image_models.append(item)
        elif profile.image_role == "projector_candidate":
            image_projectors.append(item)
        if profile.audio_role == "base_model_candidate":
            audio_models.append(item)
        elif profile.audio_role == "projector_candidate":
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
    models: list[LocalDiscoveryArtifact],
    projectors: list[LocalDiscoveryArtifact],
    modality: str,
) -> list[MultimodalPairCandidate]:
    result = []
    for model in models:
        model_document = model.parse_result.metadata
        model_architecture = _text_value(model_document, "general.architecture")
        if not model_architecture:
            continue
        for projector in projectors:
            projector_document = projector.parse_result.metadata
            evidence = _compatibility_evidence(
                model_document, projector_document, model_architecture, modality
            )
            if not evidence:
                continue
            pair_material = "\0".join(
                (
                    modality,
                    model.candidate.artifact_id,
                    projector.candidate.artifact_id,
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
                    model_artifact_id=model.candidate.artifact_id,
                    projector_artifact_id=projector.candidate.artifact_id,
                    model_path=model.candidate.path,
                    projector_path=projector.candidate.path,
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
    model: GGUFMetadataDocument,
    projector: GGUFMetadataDocument,
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


def _text_value(document: GGUFMetadataDocument, key: str) -> str | None:
    field: GGUFMetadataField | None = document.field(key)
    if field is None or field.type_name != "string" or not isinstance(field.value, str):
        return None
    value = field.value.strip()
    return value or None


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)
