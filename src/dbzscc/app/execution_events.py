"""Typed, bounded, redacted execution events for the chat activity stream.

This module deliberately stores concise decision summaries rather than hidden
chain-of-thought. Raw tool output is a separate event type and is bounded before
it reaches persistence or the UI.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class ExecutionEventType(str, Enum):
    """Stable event vocabulary shared by runtimes and future developer tools."""

    WORKFLOW_STARTED = "workflow_started"
    TERMINAL_COMMAND = "terminal_command"
    COT_COMPACT_RATIONALE = "cot_compact_rationale"
    TOT_PLAN_OPTION = "tot_plan_option"
    TOT_PLAN_SELECTED = "tot_plan_selected"
    # Legacy values remain readable for previously persisted event streams.
    REASONING_SUMMARY = "reasoning_summary"
    PLAN_OPTION = "plan_option"
    PLAN_SELECTED = "plan_selected"
    STATUS = "status"
    PROGRESS = "progress"
    STDOUT = "stdout"
    STDERR = "stderr"
    EXIT_CODE = "exit_code"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    TRUNCATED = "truncated"


@dataclass(frozen=True)
class ExecutionEvent:
    """One user-readable event, correlated with a conversation and workflow."""

    conversation_id: str
    workflow_id: str
    run_id: str
    event_type: ExecutionEventType
    text: str
    source: str = "runtime"
    exit_code: int | None = None
    provenance: tuple[tuple[str, str], ...] = ()
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )

    def __post_init__(self) -> None:
        if not self.conversation_id.strip():
            raise ValueError("conversation_id must not be empty")
        if not self.workflow_id.strip() or not self.run_id.strip():
            raise ValueError("workflow_id and run_id must not be empty")
        if not isinstance(self.event_type, ExecutionEventType):
            raise TypeError("event_type must be an ExecutionEventType")
        if not isinstance(self.text, str):
            raise TypeError("event text must be text")

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|"
    r"authorization|credential)\b\s*[:=]\s*([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[A-Z]:\\|\\\\)[^\r\n\t\"<>|]+")
_UNIX_PATH = re.compile(r"(?<![\w])/(?:[^\s\r\n]+/)*[^\s\r\n]+")
_SECRET_ENV_NAME = re.compile(r"(?i)(?:API|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)")


def redact_text(
    value: str,
    *,
    secret_values: Iterable[str] | None = None,
    scoped_paths: Iterable[str | os.PathLike[str]] = (),
    max_chars: int = 1200,
) -> str:
    """Remove credentials, terminal control sequences and scoped absolute paths.

    Environment values are never rendered. Values whose names suggest a secret
    are used only as exact redaction candidates and are never persisted.
    """
    if max_chars < 32:
        raise ValueError("max_chars must be at least 32")
    text = _ANSI_ESCAPE.sub("", str(value)).replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _BEARER_TOKEN.sub("Bearer [redacted]", text)

    candidates = list(secret_values or ())
    candidates.extend(
        value
        for name, value in os.environ.items()
        if _SECRET_ENV_NAME.search(name) and value and len(value) >= 4
    )
    for candidate in sorted({item for item in candidates if item}, key=len, reverse=True):
        text = text.replace(candidate, "[redacted]")

    for path in scoped_paths:
        raw_path = os.fspath(path)
        if raw_path:
            text = re.sub(re.escape(raw_path), "[scoped path]", text, flags=re.IGNORECASE)
            alternate = raw_path.replace("\\", "/")
            if alternate != raw_path:
                text = re.sub(re.escape(alternate), "[scoped path]", text, flags=re.IGNORECASE)

    text = _WINDOWS_PATH.sub("[local path]", text)
    text = _UNIX_PATH.sub("[local path]", text)
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    text = text.strip()
    if len(text) > max_chars:
        text = text[: max_chars - 24].rstrip() + " … [Ausgabe gekürzt]"
    return text


class ExecutionEventStore:
    """SQLite persistence for bounded workflow events, separate from chat prompts."""

    MAX_EVENT_CHARS = 1200
    MAX_EVENTS_PER_RUN = 180
    MAX_EVENTS_PER_CONVERSATION = 1600

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    text TEXT NOT NULL,
                    exit_code INTEGER,
                    provenance_json TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_events_conversation "
                "ON execution_events(conversation_id, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_events_run "
                "ON execution_events(run_id, id)"
            )

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        """Redact and persist one event, pruning old raw output to fixed limits."""
        safe_text = redact_text(event.text, max_chars=self.MAX_EVENT_CHARS)
        safe_source = redact_text(event.source, max_chars=120)
        safe_provenance = tuple(
            (redact_text(key, max_chars=80), redact_text(value, max_chars=200))
            for key, value in event.provenance
        )
        safe_event = ExecutionEvent(
            conversation_id=event.conversation_id,
            workflow_id=event.workflow_id,
            run_id=event.run_id,
            event_type=event.event_type,
            text=safe_text,
            source=safe_source,
            exit_code=event.exit_code,
            provenance=safe_provenance,
            event_id=event.event_id,
            created_at_utc=event.created_at_utc,
        )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM execution_events WHERE run_id = ? AND event_type = ? "
                "AND text LIKE 'Ältere Ausgaben dieses Laufs%' LIMIT 1",
                (safe_event.run_id, ExecutionEventType.TRUNCATED.value),
            ).fetchone()
            count_row = connection.execute(
                "SELECT COUNT(*) AS n FROM execution_events WHERE run_id = ?",
                (safe_event.run_id,),
            ).fetchone()
            count = int(count_row["n"])
            if count >= self.MAX_EVENTS_PER_RUN:
                if existing is None:
                    keep = max(0, self.MAX_EVENTS_PER_RUN - 2)
                    connection.execute(
                        "DELETE FROM execution_events WHERE id IN ("
                        "SELECT id FROM execution_events WHERE run_id = ? "
                        "ORDER BY id ASC LIMIT ?)",
                        (safe_event.run_id, max(1, count - keep)),
                    )
                    marker = ExecutionEvent(
                        conversation_id=safe_event.conversation_id,
                        workflow_id=safe_event.workflow_id,
                        run_id=safe_event.run_id,
                        event_type=ExecutionEventType.TRUNCATED,
                        text="Ältere Ausgaben dieses Laufs wurden wegen des Größenlimits gekürzt.",
                        source="execution-event-store",
                        provenance=safe_event.provenance,
                    )
                    self._insert(connection, marker)
                else:
                    delete_count = max(1, count - (self.MAX_EVENTS_PER_RUN - 1))
                    connection.execute(
                        "DELETE FROM execution_events WHERE id IN ("
                        "SELECT id FROM execution_events WHERE run_id = ? "
                        "AND event_type != ? ORDER BY id ASC LIMIT ?)",
                        (
                            safe_event.run_id,
                            ExecutionEventType.TRUNCATED.value,
                            delete_count,
                        ),
                    )
            self._insert(connection, safe_event)
            connection.execute(
                "DELETE FROM execution_events WHERE id IN ("
                "SELECT id FROM execution_events WHERE conversation_id = ? ORDER BY id DESC "
                "LIMIT -1 OFFSET ?)"
                ,
                (safe_event.conversation_id, self.MAX_EVENTS_PER_CONVERSATION),
            )
        return safe_event

    @staticmethod
    def _insert(connection: sqlite3.Connection, event: ExecutionEvent) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO execution_events (
                event_id, conversation_id, workflow_id, run_id, created_at_utc,
                event_type, source, text, exit_code, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.conversation_id,
                event.workflow_id,
                event.run_id,
                event.created_at_utc,
                event.event_type.value,
                event.source,
                event.text,
                event.exit_code,
                json.dumps(event.provenance, ensure_ascii=False),
            ),
        )

    def list_for_conversation(
        self, conversation_id: str, *, limit: int = 500
    ) -> tuple[ExecutionEvent, ...]:
        if not conversation_id.strip():
            return ()
        bounded_limit = max(1, min(limit, self.MAX_EVENTS_PER_CONVERSATION))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM (SELECT * FROM execution_events WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (conversation_id, bounded_limit),
            ).fetchall()
        result: list[ExecutionEvent] = []
        for row in rows:
            try:
                event_type = ExecutionEventType(row["event_type"])
                raw_provenance = json.loads(row["provenance_json"])
                provenance = tuple((str(key), str(value)) for key, value in raw_provenance)
                result.append(
                    ExecutionEvent(
                        conversation_id=row["conversation_id"],
                        workflow_id=row["workflow_id"],
                        run_id=row["run_id"],
                        event_type=event_type,
                        text=row["text"],
                        source=row["source"],
                        exit_code=row["exit_code"],
                        provenance=provenance,
                        event_id=row["event_id"],
                        created_at_utc=row["created_at_utc"],
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(result)


def format_execution_event(event: ExecutionEvent) -> str:
    """Render a short, visibly typed chat entry without mixing raw output kinds."""
    labels = {
        ExecutionEventType.WORKFLOW_STARTED: "Ablauf gestartet",
        ExecutionEventType.TERMINAL_COMMAND: "Terminalbefehl",
        ExecutionEventType.COT_COMPACT_RATIONALE: "Begründung · Kurzfassung",
        ExecutionEventType.TOT_PLAN_OPTION: "Planoption",
        ExecutionEventType.TOT_PLAN_SELECTED: "Ausgewählter Zweig",
        ExecutionEventType.REASONING_SUMMARY: "Begründung · Kurzfassung",
        ExecutionEventType.PLAN_OPTION: "Planoption",
        ExecutionEventType.PLAN_SELECTED: "Ausgewählter Zweig",
        ExecutionEventType.STATUS: "Status",
        ExecutionEventType.PROGRESS: "Fortschritt",
        ExecutionEventType.STDOUT: "Terminal stdout",
        ExecutionEventType.STDERR: "Terminal stderr",
        ExecutionEventType.EXIT_CODE: "Exitcode",
        ExecutionEventType.TOOL_RESULT: "Tool-Ergebnis",
        ExecutionEventType.ERROR: "Fehler",
        ExecutionEventType.TRUNCATED: "Gekürzte Ausgabe",
    }
    provenance = " · ".join(f"{key}={value}" for key, value in event.provenance)
    title = labels.get(event.event_type, event.event_type.value.replace("_", " ").title())
    if event.exit_code is not None and event.event_type is not ExecutionEventType.EXIT_CODE:
        title += f" · Exit {event.exit_code}"
    details = f" · {provenance}" if provenance else ""
    return f"[AUSFÜHRUNG · {title}{details}]\n{event.text}"
