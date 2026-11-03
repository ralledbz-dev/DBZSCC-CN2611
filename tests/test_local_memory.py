from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from dbzscc.app.local_memory import (
    KnowledgeArea,
    KnowledgeKind,
    KnowledgeStatus,
    LocalMemoryStore,
    MemoryProvenance,
)

SCOPE_ID = str(uuid.uuid4())
SOURCE_ID = str(uuid.uuid4())
CREATED = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
CONFIRMED = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)


def _propose(store: LocalMemoryStore, summary: str = "SQLite keeps the project decision locally."):
    return store.propose(
        area=KnowledgeArea.PROJECT,
        scope_id=SCOPE_ID,
        kind=KnowledgeKind.DECISION,
        summary=summary,
        provenance=MemoryProvenance.DECISION_RECORD,
        source_ref=SOURCE_ID,
        confidence=0.85,
        created_at=CREATED,
    )


def test_pending_review_is_not_a_fact_and_confirmed_entry_is_traceable(tmp_path) -> None:
    store = LocalMemoryStore(tmp_path / "memory.sqlite")
    try:
        pending = _propose(store)

        assert pending.status is KnowledgeStatus.PENDING_REVIEW
        assert pending.created_at == "2026-11-03T12:00:00.000000Z"
        assert pending.last_confirmed_at is None
        assert store.list_entries(area=KnowledgeArea.PROJECT, scope_id=SCOPE_ID) == ()
        assert store.search(
            area=KnowledgeArea.PROJECT, scope_id=SCOPE_ID, query="SQLite decision"
        ) == ()

        confirmed = store.confirm(pending.memory_id, confirmed_at=CONFIRMED)
        results = store.search(
            area=KnowledgeArea.PROJECT, scope_id=SCOPE_ID, query="SQLite decision"
        )

        assert confirmed.status is KnowledgeStatus.CONFIRMED
        assert confirmed.last_confirmed_at == "2026-11-03T13:00:00.000000Z"
        assert len(results) == 1
        assert results[0].entry.memory_id == pending.memory_id
        assert results[0].entry.source_ref == SOURCE_ID
        assert results[0].entry.provenance is MemoryProvenance.DECISION_RECORD
        assert results[0].matched_terms == ("sqlite", "decision")
    finally:
        store.close()


def test_stale_unknown_and_pending_entries_are_never_returned_as_facts(tmp_path) -> None:
    store = LocalMemoryStore(tmp_path / "memory.sqlite")
    try:
        first = store.confirm(_propose(store).memory_id, confirmed_at=CONFIRMED)
        second = store.confirm(_propose(store, "The current workflow uses local SQLite.").memory_id)
        store.mark_stale(first.memory_id)
        store.mark_unknown(second.memory_id)
        pending = _propose(store, "A proposed change still needs review.")

        assert store.list_entries(area=KnowledgeArea.PROJECT, scope_id=SCOPE_ID) == ()
        audit = store.list_entries(
            area=KnowledgeArea.PROJECT, scope_id=SCOPE_ID, include_non_facts=True
        )
        assert {entry.status for entry in audit} == {
            KnowledgeStatus.STALE,
            KnowledgeStatus.UNKNOWN,
            KnowledgeStatus.PENDING_REVIEW,
        }
        assert store.get(first.memory_id).last_confirmed_at == first.last_confirmed_at
        assert pending.status is KnowledgeStatus.PENDING_REVIEW
    finally:
        store.close()


def test_store_survives_reopen_and_hard_delete_removes_entry(tmp_path) -> None:
    database = tmp_path / "memory.sqlite"
    store = LocalMemoryStore(database)
    entry = store.confirm(_propose(store).memory_id, confirmed_at=CONFIRMED)
    store.close()

    reopened = LocalMemoryStore(database)
    try:
        assert reopened.get(entry.memory_id) == entry
        assert reopened.delete(entry.memory_id) is True
        assert reopened.get(entry.memory_id) is None
        assert reopened.delete(entry.memory_id) is False
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "summary",
    [
        "Contact ralf@example.com for this decision.",
        "Bearer: abcdef0123456789",
        "api_key=very-secret-value",
        "The file is at D:\\Users\\ralle\\private.txt",
        "phone +49 170 12345678",
        "First line\nSecond line",
    ],
)
def test_candidate_rejects_common_personal_secret_path_and_raw_multiline_text(
    tmp_path, summary: str
) -> None:
    store = LocalMemoryStore(tmp_path / "memory.sqlite")
    try:
        with pytest.raises(ValueError):
            _propose(store, summary)
        assert store.list_entries(area=KnowledgeArea.PROJECT, scope_id=SCOPE_ID) == ()
    finally:
        store.close()


def test_confidence_and_references_are_validated(tmp_path) -> None:
    store = LocalMemoryStore(tmp_path / "memory.sqlite")
    try:
        with pytest.raises(ValueError, match="Confidence"):
            store.propose(
                area=KnowledgeArea.PROJECT,
                scope_id=SCOPE_ID,
                kind=KnowledgeKind.VERIFIED_FACT,
                summary="A bounded technical abstraction.",
                provenance=MemoryProvenance.TEST_EVIDENCE,
                source_ref=SOURCE_ID,
                confidence=1.5,
            )
        with pytest.raises(ValueError, match="UUID"):
            store.propose(
                area=KnowledgeArea.PROJECT,
                scope_id=SCOPE_ID,
                kind=KnowledgeKind.VERIFIED_FACT,
                summary="A bounded technical abstraction.",
                provenance=MemoryProvenance.TEST_EVIDENCE,
                source_ref="D:\\private\\file.txt",
            )
    finally:
        store.close()
