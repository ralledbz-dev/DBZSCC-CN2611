from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from dbzscc.app.action_evidence import (
    ActionEvidence,
    EvidenceAction,
    EvidenceErrorCode,
    EvidenceOrigin,
    EvidenceOutcome,
    Freshness,
    UncertaintyCode,
    WorktreeState,
    assess_freshness,
    create_action_evidence,
)

REVISION_A = "a" * 40
REVISION_B = "b" * 40
OBSERVED_AT = datetime(2026, 11, 3, 12, 30, tzinfo=timezone.utc)


def _record(**overrides) -> ActionEvidence:
    arguments = {
        "action": EvidenceAction.FILESYSTEM_INSPECTION,
        "origin": EvidenceOrigin.GIT_READ_ONLY,
        "outcome": EvidenceOutcome.SUCCEEDED,
        "uncertainty": UncertaintyCode.FILESYSTEM_CONTENT_NOT_RECORDED,
        "observed_at": OBSERVED_AT,
        "repository_revision": REVISION_A,
        "worktree_state": WorktreeState.CLEAN,
    }
    arguments.update(overrides)
    return create_action_evidence(**arguments)


def test_record_has_correlatable_ids_utc_time_and_explicit_provenance() -> None:
    correlation_id = str(uuid.uuid4())
    record = _record(correlation_id=correlation_id)

    assert uuid.UUID(record.evidence_id)
    assert record.correlation_id == correlation_id
    assert record.action is EvidenceAction.FILESYSTEM_INSPECTION
    assert record.origin is EvidenceOrigin.GIT_READ_ONLY
    assert record.observed_at == "2026-11-03T12:30:00.000000Z"
    assert record.outcome is EvidenceOutcome.SUCCEEDED
    assert record.uncertainty is UncertaintyCode.FILESYSTEM_CONTENT_NOT_RECORDED
    assert record.schema_version == 1


def test_factory_normalizes_non_utc_timestamps_to_utc() -> None:
    local_time = datetime(
        2026, 11, 3, 14, 30, tzinfo=timezone(timedelta(hours=2))
    )

    record = _record(observed_at=local_time)

    assert record.observed_at == "2026-11-03T12:30:00.000000Z"


def test_record_serialization_is_allowlisted_and_has_no_content_or_secrets() -> None:
    record = _record(
        action=EvidenceAction.RUNTIME_PROBE,
        origin=EvidenceOrigin.RUNTIME_API,
        outcome=EvidenceOutcome.FAILED,
        uncertainty=UncertaintyCode.EXTERNAL_RUNTIME_LOGS_UNVERIFIED,
        error_code=EvidenceErrorCode.ENDPOINT_UNREACHABLE,
    )

    serialized = record.to_record()

    assert set(serialized) == {
        "schema_version",
        "evidence_id",
        "correlation_id",
        "action",
        "origin",
        "observed_at",
        "outcome",
        "uncertainty",
        "repository_revision",
        "worktree_state",
        "error_code",
    }
    assert "content" not in serialized
    assert "path" not in serialized
    assert "credential" not in serialized
    assert "token" not in serialized
    assert serialized["error_code"] == "endpoint_unreachable"


def test_free_text_cannot_be_used_as_origin_uncertainty_or_error() -> None:
    secret_like = "Bearer private-token-at-example"

    with pytest.raises(ValueError, match="controlled"):
        _record(origin=secret_like)
    with pytest.raises(ValueError, match="controlled"):
        _record(uncertainty=secret_like)
    with pytest.raises(ValueError, match="controlled"):
        _record(error_code=secret_like)


def test_rejects_invalid_ids_timestamps_and_non_full_revisions() -> None:
    with pytest.raises(ValueError, match="UUID"):
        _record(correlation_id="C:\\private\\project")
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(observed_at=datetime(2026, 11, 3, 12, 30))
    with pytest.raises(ValueError, match="full Git object ID"):
        _record(repository_revision="deadbeef")


def test_failure_evidence_keeps_only_a_bounded_error_code() -> None:
    record = _record(
        action=EvidenceAction.MODEL_DISCOVERY,
        origin=EvidenceOrigin.MIRO_REGISTRY,
        outcome=EvidenceOutcome.PARTIAL,
        uncertainty=UncertaintyCode.PARTIAL_RESULT,
        error_code=EvidenceErrorCode.PARTIAL_SCAN,
    )

    serialized = record.to_record()
    assert serialized["outcome"] == "partial"
    assert serialized["error_code"] == "partial_scan"
    assert not any(isinstance(value, str) and "C:\\" in value for value in serialized.values())


def test_freshness_requires_same_revision_and_clean_worktrees() -> None:
    record = _record()

    assert assess_freshness(
        record,
        current_revision=REVISION_A,
        current_worktree_state=WorktreeState.CLEAN,
    ) is Freshness.CURRENT
    assert assess_freshness(
        record,
        current_revision=REVISION_B,
        current_worktree_state=WorktreeState.CLEAN,
    ) is Freshness.STALE
    assert assess_freshness(
        record,
        current_revision=REVISION_A,
        current_worktree_state=WorktreeState.DIRTY,
    ) is Freshness.STALE


def test_dirty_to_dirty_and_missing_revision_remain_unknown_not_current() -> None:
    dirty_record = _record(worktree_state=WorktreeState.DIRTY)

    assert assess_freshness(
        dirty_record,
        current_revision=REVISION_A,
        current_worktree_state=WorktreeState.DIRTY,
    ) is Freshness.UNKNOWN
    assert assess_freshness(
        _record(repository_revision=None),
        current_revision=REVISION_A,
        current_worktree_state=WorktreeState.CLEAN,
    ) is Freshness.UNKNOWN
    assert assess_freshness(
        _record(),
        current_revision=None,
        current_worktree_state=WorktreeState.CLEAN,
    ) is Freshness.UNKNOWN


def test_unsupported_schema_and_worktree_values_are_rejected() -> None:
    record = _record()
    values = record.to_record()
    values["schema_version"] = 2

    with pytest.raises(ValueError, match="schema version"):
        ActionEvidence(**values)
    with pytest.raises(ValueError, match="controlled"):
        _record(worktree_state="dirty; C:\\private")


def test_rejects_inconsistent_origin_and_invalid_timestamp_type() -> None:
    with pytest.raises(ValueError, match="origin"):
        _record(action=EvidenceAction.RUNTIME_PROBE)
    with pytest.raises(TypeError, match="timestamp"):
        _record(observed_at=0)
