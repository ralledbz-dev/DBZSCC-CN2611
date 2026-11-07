from __future__ import annotations

from dbzscc.app.execution_events import (
    ExecutionEvent,
    ExecutionEventStore,
    ExecutionEventType,
    ExecutionRunRecord,
    ExecutionRunState,
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


def test_run_store_persists_binding_state_and_structured_step_event(tmp_path) -> None:
    store = ExecutionEventStore(tmp_path / "runs.sqlite")
    initial = ExecutionRunRecord(
        conversation_id="chat-run",
        workflow_id="workflow-run",
        run_id="persistent-run",
        project_root=str(tmp_path / "project"),
        project_fingerprint="sha256-before",
        model_artifact_id="artifact-current",
        model_observation_id="observation-current",
        model_identity_id="local-model",
        model_metadata_prefix_sha256="prefix-hash",
    )

    created = store.bind_run(initial)
    running = store.transition_run(created.run_id, ExecutionRunState.RUNNING)
    event = store.append(
        ExecutionEvent(
            conversation_id=created.conversation_id,
            workflow_id=created.workflow_id,
            run_id=created.run_id,
            event_type=ExecutionEventType.STDOUT,
            text="pytest passed",
            state="running",
            step_id="run_tests",
        )
    )
    loaded = store.list_for_conversation(created.conversation_id)
    refreshed = store.bind_run(
        ExecutionRunRecord(
            conversation_id=created.conversation_id,
            workflow_id=created.workflow_id,
            run_id=created.run_id,
            project_root=initial.project_root,
            project_fingerprint="sha256-after-reviewed-patch",
            model_artifact_id=initial.model_artifact_id,
            model_observation_id=initial.model_observation_id,
            model_identity_id=initial.model_identity_id,
            model_metadata_prefix_sha256=initial.model_metadata_prefix_sha256,
        )
    )

    assert running.state is ExecutionRunState.RUNNING
    assert event.state == "running" and event.step_id == "run_tests"
    assert loaded[0].state == "running" and loaded[0].step_id == "run_tests"
    assert refreshed.state is ExecutionRunState.RUNNING
    assert refreshed.project_root == initial.project_root
    assert refreshed.project_fingerprint == "sha256-after-reviewed-patch"
    assert refreshed.model_artifact_id == "artifact-current"
    assert store.get_run(created.run_id) == refreshed
    store.transition_run(created.run_id, ExecutionRunState.SUCCEEDED)


def test_run_store_rejects_invalid_transition_and_rebinding_to_another_root(tmp_path) -> None:
    store = ExecutionEventStore(tmp_path / "invalid-transitions.sqlite")
    record = ExecutionRunRecord(
        conversation_id="chat",
        workflow_id="workflow",
        run_id="run",
        project_root=str(tmp_path / "project"),
        project_fingerprint="fingerprint",
    )
    store.bind_run(record)
    store.transition_run(record.run_id, ExecutionRunState.RUNNING)

    try:
        store.transition_run(record.run_id, ExecutionRunState.PATCH_APPLIED)
    except ValueError as exc:
        assert "invalid run-state transition" in str(exc)
    else:
        raise AssertionError("an invalid lifecycle transition must be rejected")

    try:
        store.bind_run(
            ExecutionRunRecord(
                conversation_id="chat",
                workflow_id="workflow",
                run_id="run",
                project_root=str(tmp_path / "different-project"),
                project_fingerprint="fingerprint-2",
            )
        )
    except ValueError as exc:
        assert "different project" in str(exc)
    else:
        raise AssertionError("a run must remain bound to its original project root")


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
