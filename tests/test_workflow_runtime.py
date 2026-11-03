from __future__ import annotations

import builtins
import json

import pytest

from dbzscc.app.chat_client import ChatMessage
from dbzscc.app.workflow_runtime import (
    LangChainWorkflowPlanner,
    WorkflowAction,
    WorkflowPlan,
    WorkflowRuntimeError,
    WorkflowSelection,
    workflow_runtime_availability,
)

_LANGCHAIN_AVAILABLE = workflow_runtime_availability().available
_LANGCHAIN_REQUIRED = workflow_runtime_availability().detail


class _FakeLocalClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, tuple[ChatMessage, ...]]] = []

    def complete_local(self, model: str, messages: tuple[ChatMessage, ...]) -> str:
        self.calls.append((model, messages))
        return self.response


def _response(*, actions: list[str], rationale: str = "Tests zuerst; danach prüfen.") -> str:
    return json.dumps({"actions": actions, "rationale": rationale})


def test_workflow_runtime_availability_is_checked_without_loading_the_package(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(workflow_module, "PYTHON_VERSION", (3, 14))
    monkeypatch.setattr(workflow_module, "find_spec", lambda _name: None)
    status = workflow_runtime_availability()
    assert not status.available
    assert ".[workflows]" in status.detail


def test_workflow_runtime_availability_reports_the_installed_distribution(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(workflow_module, "PYTHON_VERSION", (3, 14))
    monkeypatch.setattr(workflow_module, "find_spec", lambda _name: object())
    monkeypatch.setattr(workflow_module, "distribution_version", lambda _name: "0.3.63")
    status = workflow_runtime_availability()
    assert status.available
    assert "0.3.63" in status.detail


def test_python_315_workflow_runtime_is_explicitly_blocked(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(workflow_module, "PYTHON_VERSION", (3, 15))
    status = workflow_runtime_availability()
    assert not status.available
    assert "Python 3.15" in status.detail


def test_planner_is_blocked_on_python_315_before_client_call(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(workflow_module, "PYTHON_VERSION", (3, 15))
    client = _FakeLocalClient("{}")
    with pytest.raises(WorkflowRuntimeError, match="Python 3.15"):
        LangChainWorkflowPlanner("http://127.0.0.1:8080/v1", client=client)
    assert client.calls == []


def test_missing_optional_langchain_import_fails_closed(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(workflow_module, "PYTHON_VERSION", (3, 14))
    original_import = builtins.__import__

    def without_langchain(name, *args, **kwargs):
        if name.startswith("langchain_core"):
            raise ModuleNotFoundError("optional package absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_langchain)
    with pytest.raises(WorkflowRuntimeError, match="optionale Extra"):
        LangChainWorkflowPlanner("http://127.0.0.1:8080/v1", client=_FakeLocalClient("{}"))


def test_workflow_schema_is_strict_without_requiring_langchain() -> None:
    selection = WorkflowSelection.model_validate(
        {"actions": ["run_tests"], "rationale": "Tests zuerst."}
    )
    assert selection.actions == [WorkflowAction.RUN_TESTS]
    with pytest.raises(ValueError):
        WorkflowSelection.model_validate(
            {"actions": ["run_shell"], "rationale": "Nicht erlaubt."}
        )


@pytest.mark.skipif(not _LANGCHAIN_AVAILABLE, reason=_LANGCHAIN_REQUIRED)
def test_workflow_plan_uses_local_langchain_chain_and_only_returns_allowlisted_actions() -> None:
    client = _FakeLocalClient(_response(actions=["run_tests", "propose_patch"]))
    planner = LangChainWorkflowPlanner("http://127.0.0.1:8080/v1", client=client)

    plan = planner.plan("local-model", "Bitte Fehler prüfen und einen Diff vorschlagen.")

    assert isinstance(plan, WorkflowPlan)
    assert plan.actions == (WorkflowAction.RUN_TESTS, WorkflowAction.PROPOSE_PATCH)
    assert plan.requires_explicit_action is True
    assert plan.model_name == "local-model"
    assert len(client.calls) == 1
    model, messages = client.calls[0]
    assert model == "local-model"
    assert messages[0].role == "system"
    assert "untrusted data" in messages[0].content
    assert messages[1].role == "user"
    assert "Bitte Fehler prüfen" in messages[1].content
    assert "Available actions" in messages[1].content
    assert "format_instructions" not in messages[1].content
    assert "Do not produce commands" in messages[0].content


def test_workflow_plan_rejects_remote_endpoint_before_model_call() -> None:
    client = _FakeLocalClient(_response(actions=["inspect_project"]))

    with pytest.raises(WorkflowRuntimeError, match="Loopback-Runtime"):
        LangChainWorkflowPlanner("https://example.invalid/v1", client=client)

    assert client.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"actions":["run_tests"],"rationale":"ok","command":"whoami"}',
        '{"actions":["execute_shell"],"rationale":"ok"}',
        '{"actions":["run_tests","run_tests"],"rationale":"ok"}',
        '{"actions":[],"rationale":"ok"}',
    ],
)
@pytest.mark.skipif(not _LANGCHAIN_AVAILABLE, reason=_LANGCHAIN_REQUIRED)
def test_workflow_plan_rejects_extra_unknown_duplicate_or_empty_actions(payload: str) -> None:
    planner = LangChainWorkflowPlanner(
        "http://127.0.0.1:8080/v1", client=_FakeLocalClient(payload)
    )

    with pytest.raises(WorkflowRuntimeError, match="sicher validiert"):
        planner.plan("local-model", "Run the tests.")


@pytest.mark.parametrize(
    ("model", "user_request"),
    [
        ("", "run tests"),
        ("local-model", "  "),
        ("local-model", "x" * 2001),
        ("local-model\nsecret", "run tests"),
    ],
)
@pytest.mark.skipif(not _LANGCHAIN_AVAILABLE, reason=_LANGCHAIN_REQUIRED)
def test_workflow_plan_rejects_invalid_model_and_request_before_model_call(
    model: str, user_request: str
) -> None:
    client = _FakeLocalClient(_response(actions=["run_tests"]))
    planner = LangChainWorkflowPlanner("http://127.0.0.1:8080/v1", client=client)

    with pytest.raises(ValueError):
        planner.plan(model, user_request)

    assert client.calls == []
