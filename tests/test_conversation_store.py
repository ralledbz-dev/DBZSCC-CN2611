from __future__ import annotations

from pathlib import Path

import pytest

from dbzscc.app.conversation_store import ConversationStore


def test_conversations_and_messages_persist_in_order(tmp_path: Path) -> None:
    database = tmp_path / "data" / "chat.sqlite"
    store = ConversationStore(database)
    conversation = store.create_conversation()
    store.add_message(conversation.conversation_id, "user", "  Bitte analysieren  ")
    store.add_message(conversation.conversation_id, "assistant", "Ergebnis")

    reopened = ConversationStore(database)
    messages = reopened.list_messages(conversation.conversation_id)

    assert [item.role for item in messages] == ["user", "assistant"]
    assert [item.content for item in messages] == ["  Bitte analysieren  ", "Ergebnis"]
    listed = reopened.list_conversations()
    assert listed[0].conversation_id == conversation.conversation_id
    assert listed[0].title == "Bitte analysieren"
    assert listed[0].updated_at >= listed[0].created_at


def test_managed_runtime_build_hash_round_trips_in_chat_metadata(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "chat.sqlite")
    conversation = store.create_conversation()
    metadata = {"runtime_build_sha256": "a" * 64}

    stored = store.add_message(
        conversation.conversation_id,
        "assistant",
        "Vollständige Runtimeantwort",
        metadata=metadata,
    )

    reloaded = store.get_message(stored.message_id)
    assert reloaded is not None
    assert dict(reloaded.metadata) == metadata


def test_rejects_empty_messages_invalid_roles_and_unknown_conversations(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "chat.sqlite")
    conversation = store.create_conversation()

    with pytest.raises(ValueError):
        store.add_message(conversation.conversation_id, "system", "hidden prompt")
    with pytest.raises(ValueError):
        store.add_message(conversation.conversation_id, "user", "  ")
    with pytest.raises(KeyError):
        store.add_message("unknown", "user", "message")
