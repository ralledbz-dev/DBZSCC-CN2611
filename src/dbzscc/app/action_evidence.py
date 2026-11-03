"""Privacy-preserving, framework-free evidence contracts for local actions.

This module defines an in-memory schema only. It deliberately has no persistence
adapter and accepts no free-form path, error, model output, credential, or file
content fields. Callers can serialize only the allowlisted fields via
:meth:`ActionEvidence.to_record`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class EvidenceAction(str, Enum):
    """A bounded action category, not a user-supplied description."""

    RUNTIME_PROBE = "runtime_probe"
    MODEL_DISCOVERY = "model_discovery"
    FILESYSTEM_INSPECTION = "filesystem_inspection"
    APPLICATION_START = "application_start"
    TEST_RUN = "test_run"


class EvidenceOrigin(str, Enum):
    """Allowlisted producer of an observation."""

    APPLICATION = "application"
    RUNTIME_API = "runtime_api"
    MIRO_REGISTRY = "miro_registry"
    GIT_READ_ONLY = "git_read_only"
    PYTEST = "pytest"
    RUFF = "ruff"


class EvidenceOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_RUN = "not_run"


class UncertaintyCode(str, Enum):
    """Controlled limitations; intentionally no arbitrary diagnostic text."""

    NOT_ASSESSED = "not_assessed"
    EXTERNAL_RUNTIME_LOGS_UNVERIFIED = "external_runtime_logs_unverified"
    MODEL_METADATA_NOT_CAPABILITY_PROOF = "model_metadata_not_capability_proof"
    INFERENCE_NOT_PERFORMED = "inference_not_performed"
    FILESYSTEM_CONTENT_NOT_RECORDED = "filesystem_content_not_recorded"
    DIRTY_WORKTREE_DETAILS_NOT_CAPTURED = "dirty_worktree_details_not_captured"
    EXTERNAL_SIDE_EFFECTS_NOT_CHECKED = "external_side_effects_not_checked"
    PARTIAL_RESULT = "partial_result"
    SOURCE_NOT_AVAILABLE = "source_not_available"


class EvidenceErrorCode(str, Enum):
    """Safe, bounded failure identifiers instead of provider exception text."""

    INVALID_CONFIGURATION = "invalid_configuration"
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"
    INVALID_RESPONSE = "invalid_response"
    MODEL_SCAN_FAILED = "model_scan_failed"
    PARTIAL_SCAN = "partial_scan"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    GIT_STATUS_FAILED = "git_status_failed"
    STARTUP_FAILED = "startup_failed"
    TEST_FAILED = "test_failed"
    LINT_FAILED = "lint_failed"


class WorktreeState(str, Enum):
    CLEAN = "clean"
    DIRTY = "dirty"
    UNKNOWN = "unknown"


class Freshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


_REVISION_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_ALLOWED_ORIGINS = {
    EvidenceAction.RUNTIME_PROBE: frozenset({EvidenceOrigin.RUNTIME_API}),
    EvidenceAction.MODEL_DISCOVERY: frozenset({EvidenceOrigin.MIRO_REGISTRY}),
    EvidenceAction.FILESYSTEM_INSPECTION: frozenset({EvidenceOrigin.GIT_READ_ONLY}),
    EvidenceAction.APPLICATION_START: frozenset({EvidenceOrigin.APPLICATION}),
    EvidenceAction.TEST_RUN: frozenset({EvidenceOrigin.PYTEST, EvidenceOrigin.RUFF}),
}


@dataclass(frozen=True)
class ActionEvidence:
    """One versioned observation with provenance and explicit uncertainty.

    Only enum values, UUIDs, an optional Git object ID and a UTC timestamp are
    accepted. The record is intentionally unsuitable for retaining sensitive
    file names, file contents, prompts, API credentials, or raw exception text.
    """

    evidence_id: str
    correlation_id: str
    action: EvidenceAction
    origin: EvidenceOrigin
    observed_at: str
    outcome: EvidenceOutcome
    uncertainty: UncertaintyCode
    repository_revision: str | None = None
    worktree_state: WorktreeState = WorktreeState.UNKNOWN
    error_code: EvidenceErrorCode | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported evidence schema version.")
        object.__setattr__(self, "evidence_id", _canonical_uuid(self.evidence_id))
        object.__setattr__(self, "correlation_id", _canonical_uuid(self.correlation_id))
        object.__setattr__(self, "action", _enum_value(EvidenceAction, self.action))
        object.__setattr__(self, "origin", _enum_value(EvidenceOrigin, self.origin))
        if self.origin not in _ALLOWED_ORIGINS[self.action]:
            raise ValueError("Evidence origin does not match the action category.")
        object.__setattr__(self, "outcome", _enum_value(EvidenceOutcome, self.outcome))
        object.__setattr__(
            self, "uncertainty", _enum_value(UncertaintyCode, self.uncertainty)
        )
        object.__setattr__(
            self, "worktree_state", _enum_value(WorktreeState, self.worktree_state)
        )
        if self.error_code is not None:
            object.__setattr__(
                self,
                "error_code",
                _enum_value(EvidenceErrorCode, self.error_code),
            )
        object.__setattr__(self, "observed_at", _canonical_utc(self.observed_at))
        object.__setattr__(
            self,
            "repository_revision",
            _validated_revision(self.repository_revision),
        )

    def to_record(self) -> dict[str, str | int | None]:
        """Return the exact allowlisted, JSON-compatible record fields."""
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "correlation_id": self.correlation_id,
            "action": self.action.value,
            "origin": self.origin.value,
            "observed_at": self.observed_at,
            "outcome": self.outcome.value,
            "uncertainty": self.uncertainty.value,
            "repository_revision": self.repository_revision,
            "worktree_state": self.worktree_state.value,
            "error_code": self.error_code.value if self.error_code else None,
        }


def create_action_evidence(
    *,
    action: EvidenceAction,
    origin: EvidenceOrigin,
    outcome: EvidenceOutcome,
    uncertainty: UncertaintyCode,
    observed_at: datetime | None = None,
    correlation_id: str | None = None,
    repository_revision: str | None = None,
    worktree_state: WorktreeState = WorktreeState.UNKNOWN,
    error_code: EvidenceErrorCode | None = None,
) -> ActionEvidence:
    """Create one validated evidence record without writing it anywhere."""
    timestamp = datetime.now(timezone.utc) if observed_at is None else observed_at
    if not isinstance(timestamp, datetime):
        raise TypeError("Evidence timestamp must be a timezone-aware datetime.")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Evidence timestamp must be timezone-aware.")
    return ActionEvidence(
        evidence_id=str(uuid.uuid4()),
        correlation_id=(correlation_id if correlation_id is not None else str(uuid.uuid4())),
        action=action,
        origin=origin,
        observed_at=timestamp.isoformat(timespec="microseconds"),
        outcome=outcome,
        uncertainty=uncertainty,
        repository_revision=repository_revision,
        worktree_state=worktree_state,
        error_code=error_code,
    )


def assess_freshness(
    evidence: ActionEvidence,
    *,
    current_revision: str | None,
    current_worktree_state: WorktreeState,
) -> Freshness:
    """Conservatively compare Git evidence without hashing or reading file content.

    A changed revision or a clean/dirty transition is stale. An unchanged dirty
    worktree remains unknown because porcelain state does not prove that file
    contents have not changed since the observation. Only equal revisions with
    both worktrees clean can be called current.
    """
    revision_now = _validated_revision(current_revision)
    worktree_now = _enum_value(WorktreeState, current_worktree_state)
    if (
        evidence.worktree_state is not WorktreeState.UNKNOWN
        and worktree_now is not WorktreeState.UNKNOWN
        and evidence.worktree_state is not worktree_now
    ):
        return Freshness.STALE
    if (
        evidence.repository_revision is not None
        and revision_now is not None
        and evidence.repository_revision != revision_now
    ):
        return Freshness.STALE
    if evidence.repository_revision is None or revision_now is None:
        return Freshness.UNKNOWN
    if (
        evidence.worktree_state is WorktreeState.CLEAN
        and worktree_now is WorktreeState.CLEAN
    ):
        return Freshness.CURRENT
    return Freshness.UNKNOWN


def _enum_value(enum_type: type[Enum], value: object):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Evidence contains an unsupported controlled value.") from exc


def _canonical_uuid(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Evidence identifiers must be UUIDs.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Evidence identifiers must be UUIDs.") from exc
    return str(parsed)


def _canonical_utc(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Evidence timestamp must be a UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Evidence timestamp must be a UTC timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Evidence timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _validated_revision(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _REVISION_PATTERN.fullmatch(value) is None:
        raise ValueError("Repository revision must be a full Git object ID.")
    return value.lower()
