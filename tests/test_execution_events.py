from __future__ import annotations

from dbzscc.app.execution_events import (
    ExecutionEvent,
    ExecutionEventStore,
    ExecutionEventType,
    format_execution_event,
)


def test_execution_events_are_redacted_before_persistence(tmp_path) -> None:
    store = ExecutionEventStore(tmp_path / "events.sqlite")
    event = ExecutionEvent(
        conversation_id="chat-1",
        workflow_id="workflow-1",
        run_id="run-1",
        event_type=ExecutionEventType.STDERR,
        text="api_key=sk-test-123456 model=C:\\Users\\Ralf\\Models\\tiny.gguf",
        source="C:\\Users\\Ralf\\runtimes\\llama-server.exe",
    )

    stored = store.append(event)
    loaded = store.list_for_conversation("chat-1")

    assert len(loaded) == 1
    assert loaded[0].event_id == event.event_id
    assert "sk-test-123456" not in stored.text
    assert "C:\\Users\\Ralf" not in stored.text
    assert "C:\\Users\\Ralf" not in stored.source
    assert "[redacted]" in stored.text
    assert "[local path]" in stored.text


def test_execution_event_store_keeps_a_truncation_marker_under_run_limit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(ExecutionEventStore, "MAX_EVENTS_PER_RUN", 4)
    store = ExecutionEventStore(tmp_path / "bounded.sqlite")

    for index in range(12):
        store.append(
            ExecutionEvent(
                conversation_id="chat-2",
                workflow_id="workflow-2",
                run_id="run-2",
                event_type=ExecutionEventType.STDOUT,
                text=f"output line {index}",
            )
        )

    events = store.list_for_conversation("chat-2", limit=100)
    run_events = tuple(item for item in events if item.run_id == "run-2")
    assert len(run_events) <= 4
    assert any(item.event_type is ExecutionEventType.TRUNCATED for item in run_events)
    assert run_events[-1].text == "output line 11"


def test_event_formatter_supports_every_current_and_legacy_event_type() -> None:
    for event_type in ExecutionEventType:
        event = ExecutionEvent(
            conversation_id="chat-formatter",
            workflow_id="workflow-formatter",
            run_id="run-formatter",
            event_type=event_type,
            text="diagnostic event",
        )

        rendered = format_execution_event(event)

        assert "[AUSFÜHRUNG · " in rendered
        assert event.text in rendered
