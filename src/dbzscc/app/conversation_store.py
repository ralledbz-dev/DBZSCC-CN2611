"""A small per-user SQLite store for local chat conversations and messages."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StoredMessage:
    message_id: str
    conversation_id: str
    role: str
    content: str
    created_at: str


class ConversationStore:
    """Persist chat text only in the explicitly supplied local database."""

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path)
        if not raw_path.strip() or raw_path == ":memory:":
            raise ValueError("A persistent local database path is required.")
        self.database_path = Path(raw_path).expanduser().absolute()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(conversation_id, created_at, message_id);
                """
            )

    def create_conversation(self, title: str = "Neue Unterhaltung") -> Conversation:
        now = _utc_now()
        conversation = Conversation(uuid.uuid4().hex, _clean_title(title), now, now)
        with self._connection() as connection, connection:
            connection.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?)",
                (
                    conversation.conversation_id,
                    conversation.title,
                    conversation.created_at,
                    conversation.updated_at,
                ),
            )
        return conversation

    def list_conversations(self) -> tuple[Conversation, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT conversation_id, title, created_at, updated_at "
                "FROM conversations ORDER BY updated_at DESC, conversation_id"
            ).fetchall()
        return tuple(Conversation(*row) for row in rows)

    def list_messages(self, conversation_id: str) -> tuple[StoredMessage, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT message_id, conversation_id, role, content, created_at "
                "FROM messages WHERE conversation_id = ? "
                "ORDER BY rowid",
                (conversation_id,),
            ).fetchall()
        return tuple(StoredMessage(*row) for row in rows)

    def add_message(
        self, conversation_id: str, role: str, content: str
    ) -> StoredMessage:
        if role not in {"user", "assistant"}:
            raise ValueError("Only user and assistant messages are persisted.")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message content must not be empty.")
        now = _utc_now()
        message = StoredMessage(
            uuid.uuid4().hex, conversation_id, role, content, now
        )
        with self._connection() as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise KeyError("Conversation does not exist.")
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                (
                    message.message_id,
                    message.conversation_id,
                    message.role,
                    message.content,
                    message.created_at,
                ),
            )
            if role == "user":
                count = connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
                if count == 1:
                    connection.execute(
                        "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                        (_clean_title(content), conversation_id),
                    )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )
        return message

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.database_path, timeout=5.0)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_title(value: str) -> str:
    title = " ".join(value.split())
    return title[:72] if title else "Neue Unterhaltung"
