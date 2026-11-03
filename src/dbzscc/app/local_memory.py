"""Explicit, local-only persistence for reviewed abstract project knowledge.

This module is deliberately not connected to chat, file indexing, model calls, or
application startup. Callers must submit a short abstraction, never a transcript,
credential, personal detail, or raw file contents. New entries are pending review
and are excluded from factual retrieval until explicitly confirmed.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

_MAX_SUMMARY_CHARS = 500
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?=(?:\D*\d){9,})\+?\d[\d\s().-]{7,}\d\b")
_SECRET_RE = re.compile(
    r"(?i)\b(?:bearer|api[_ -]?key|access[_ -]?token|password|passwd|secret)"
    r"\s*[:=]\s*\S+"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE
)
_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:\\|\\\\[\w.-]+\\|(?:^|\s)/(?:home|users|tmp|var|etc)/)",
    re.IGNORECASE,
)


class KnowledgeArea(str, Enum):
    """Bounded namespaces; no person/profile namespace is provided."""

    PROJECT = "project"
    TASK = "task"
    WORKFLOW = "workflow"
    TECHNICAL = "technical"


class KnowledgeKind(str, Enum):
    DECISION = "decision"
    CONSTRAINT = "constraint"
    VERIFIED_FACT = "verified_fact"
    TASK_LEARNING = "task_learning"


class MemoryProvenance(str, Enum):
    USER_REVIEW = "user_review"
    TASK_RESULT = "task_result"
    TEST_EVIDENCE = "test_evidence"
    DECISION_RECORD = "decision_record"


class KnowledgeStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MemoryEntry:
    """Stored abstraction and traceable, non-content provenance."""

    memory_id: str
    area: KnowledgeArea
    scope_id: str
    kind: KnowledgeKind
    summary: str
    provenance: MemoryProvenance
    source_ref: str
    confidence: float
    status: KnowledgeStatus
    created_at: str
    last_confirmed_at: str | None
    schema_version: int = 1


@dataclass(frozen=True)
class MemorySearchResult:
    """A confirmed memory plus the terms that made it retrievable."""

    entry: MemoryEntry
    matched_terms: tuple[str, ...]


class LocalMemoryStore:
    """Small SQLite store with explicit review, provenance, and deletion controls.

    The database path is supplied by the caller. This service never discovers a
    default location or reads chat history/files, and it does not train model
    weights. Its API accepts only bounded one-line abstractions and opaque UUID
    references; its pattern checks are a conservative guard, not a PII detector.
    """

    def __init__(self, database: str | Path) -> None:
        if not isinstance(database, (str, Path)) or not str(database):
            raise ValueError("A local SQLite database path is required.")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(database), timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS local_memory_entries (
                    memory_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    area TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    abstract_summary TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_confirmed_at TEXT,
                    CHECK(status != 'confirmed' OR last_confirmed_at IS NOT NULL)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_local_memory_scope_status "
                "ON local_memory_entries(area, scope_id, status)"
            )
            self._connection.commit()

    def propose(
        self,
        *,
        area: KnowledgeArea,
        scope_id: str,
        kind: KnowledgeKind,
        summary: str,
        provenance: MemoryProvenance,
        source_ref: str,
        confidence: float = 0.5,
        created_at: datetime | None = None,
    ) -> MemoryEntry:
        """Persist a new abstraction as pending review, never as an instant fact."""
        area = _enum_value(KnowledgeArea, area)
        kind = _enum_value(KnowledgeKind, kind)
        provenance = _enum_value(MemoryProvenance, provenance)
        scope_id = _canonical_uuid(scope_id, "scope")
        source_ref = _canonical_uuid(source_ref, "source")
        summary = _validate_abstract_summary(summary)
        confidence = _validate_confidence(confidence)
        timestamp = _utc_timestamp(created_at)
        memory_id = str(uuid.uuid4())
        with self._lock:
            self._connection.execute(
                """INSERT INTO local_memory_entries (
                    memory_id, schema_version, area, scope_id, kind,
                    abstract_summary, provenance, source_ref, confidence,
                    status, created_at, last_confirmed_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    memory_id,
                    area.value,
                    scope_id,
                    kind.value,
                    summary,
                    provenance.value,
                    source_ref,
                    confidence,
                    KnowledgeStatus.PENDING_REVIEW.value,
                    timestamp,
                ),
            )
            self._connection.commit()
            return self._get_unlocked(memory_id)

    def confirm(
        self, memory_id: str, *, confirmed_at: datetime | None = None
    ) -> MemoryEntry:
        """Promote a reviewed candidate to a fact, recording its latest review time."""
        memory_id = _canonical_uuid(memory_id, "memory")
        timestamp = _utc_timestamp(confirmed_at)
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM local_memory_entries WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Memory entry does not exist.")
            if row["status"] == KnowledgeStatus.CONFIRMED.value:
                raise ValueError("Memory entry is already confirmed.")
            self._connection.execute(
                "UPDATE local_memory_entries SET status = ?, last_confirmed_at = ? "
                "WHERE memory_id = ?",
                (KnowledgeStatus.CONFIRMED.value, timestamp, memory_id),
            )
            self._connection.commit()
            return self._get_unlocked(memory_id)

    def mark_stale(self, memory_id: str) -> MemoryEntry:
        """Mark knowledge as outdated so factual retrieval will exclude it."""
        return self._set_status(memory_id, KnowledgeStatus.STALE)

    def mark_unknown(self, memory_id: str) -> MemoryEntry:
        """Mark knowledge as uncertain so factual retrieval will exclude it."""
        return self._set_status(memory_id, KnowledgeStatus.UNKNOWN)

    def get(self, memory_id: str) -> MemoryEntry | None:
        memory_id = _canonical_uuid(memory_id, "memory")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM local_memory_entries WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            return _entry_from_row(row) if row is not None else None

    def list_entries(
        self, *, area: KnowledgeArea, scope_id: str, include_non_facts: bool = False
    ) -> tuple[MemoryEntry, ...]:
        """List confirmed facts by default; opt in to audit non-factual statuses."""
        area = _enum_value(KnowledgeArea, area)
        scope_id = _canonical_uuid(scope_id, "scope")
        status_clause = "" if include_non_facts else " AND status = ?"
        parameters: tuple[str, ...] = (area.value, scope_id)
        if not include_non_facts:
            parameters += (KnowledgeStatus.CONFIRMED.value,)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM local_memory_entries WHERE area = ? AND scope_id = ?"
                + status_clause
                + " ORDER BY created_at, memory_id",
                parameters,
            ).fetchall()
            return tuple(_entry_from_row(row) for row in rows)

    def search(
        self,
        *,
        area: KnowledgeArea,
        scope_id: str,
        query: str,
        limit: int = 10,
    ) -> tuple[MemorySearchResult, ...]:
        """Search only confirmed entries and return provenance with matched terms."""
        area = _enum_value(KnowledgeArea, area)
        scope_id = _canonical_uuid(scope_id, "scope")
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("Search text must be a short string.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Search limit must be between 1 and 50.")
        terms = tuple(dict.fromkeys(token.casefold() for token in _TOKEN_RE.findall(query)))
        if not terms:
            return ()
        entries = self.list_entries(area=area, scope_id=scope_id)
        matches: list[MemorySearchResult] = []
        for entry in entries:
            summary_tokens = {token.casefold() for token in _TOKEN_RE.findall(entry.summary)}
            matched = tuple(term for term in terms if term in summary_tokens)
            if matched:
                matches.append(MemorySearchResult(entry=entry, matched_terms=matched))
        matches.sort(key=lambda item: item.entry.last_confirmed_at or "", reverse=True)
        matches.sort(key=lambda item: -len(item.matched_terms))
        return tuple(matches[:limit])

    def delete(self, memory_id: str) -> bool:
        """Hard-delete an entry and its stored abstraction."""
        memory_id = _canonical_uuid(memory_id, "memory")
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM local_memory_entries WHERE memory_id = ?", (memory_id,)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def close(self) -> None:
        """Close this store's local SQLite connection."""
        with self._lock:
            self._connection.close()

    def _set_status(
        self, memory_id: str, status: KnowledgeStatus
    ) -> MemoryEntry:
        memory_id = _canonical_uuid(memory_id, "memory")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE local_memory_entries SET status = ? WHERE memory_id = ?",
                (status.value, memory_id),
            )
            if cursor.rowcount == 0:
                self._connection.rollback()
                raise KeyError("Memory entry does not exist.")
            self._connection.commit()
            return self._get_unlocked(memory_id)

    def _get_unlocked(self, memory_id: str) -> MemoryEntry:
        row = self._connection.execute(
            "SELECT * FROM local_memory_entries WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Memory entry does not exist.")
        return _entry_from_row(row)


def _validate_abstract_summary(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Knowledge must be supplied as an abstract text string.")
    summary = " ".join(value.split())
    if not summary or len(summary) > _MAX_SUMMARY_CHARS:
        raise ValueError("Abstract knowledge must contain 1 to 500 characters.")
    if "\n" in value or "\r" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Knowledge must be one plain-text line without control characters.")
    if any(
        pattern.search(summary)
        for pattern in (_EMAIL_RE, _PHONE_RE, _SECRET_RE, _JWT_RE, _PRIVATE_KEY_RE, _PATH_RE)
    ):
        raise ValueError("Knowledge resembles personal data, a secret, or a local path.")
    return summary


def _validate_confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Confidence must be a finite number between 0 and 1.")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("Confidence must be a finite number between 0 and 1.")
    return confidence


def _utc_timestamp(value: datetime | None) -> str:
    timestamp = datetime.now(timezone.utc) if value is None else value
    if not isinstance(timestamp, datetime):
        raise TypeError("Timestamp must be a timezone-aware datetime.")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical_uuid(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label.capitalize()} reference must be a UUID.")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{label.capitalize()} reference must be a UUID.") from exc


def _enum_value(enum_type: type[Enum], value: object):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Memory contains an unsupported controlled value.") from exc


def _entry_from_row(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        memory_id=row["memory_id"],
        area=KnowledgeArea(row["area"]),
        scope_id=row["scope_id"],
        kind=KnowledgeKind(row["kind"]),
        summary=row["abstract_summary"],
        provenance=MemoryProvenance(row["provenance"]),
        source_ref=row["source_ref"],
        confidence=float(row["confidence"]),
        status=KnowledgeStatus(row["status"]),
        created_at=row["created_at"],
        last_confirmed_at=row["last_confirmed_at"],
        schema_version=row["schema_version"],
    )
