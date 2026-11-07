from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbzscc.app.chat_client import CatalogChatModel, ChatMessage
from dbzscc.app.workflow_runtime import (
    LangChainWorkflowPlanner,
    WorkflowAction,
    WorkflowPlan,
    WorkflowRuntimeError,
    WorkflowSelection,
    workflow_runtime_availability,
)
from dbzscc.miro.model_registry import ModelCatalogEntry

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


def _catalog_entry(*, status: str = "metadata_read") -> ModelCatalogEntry:
    observation = SimpleNamespace(
        observation_id="observation-1",
        status=status,
        metadata_prefix_sha256="prefix-sha256" if status == "metadata_read" else None,
    )
    artifact = SimpleNamespace(
        artifact_id="artifact-1",
        current_observation_id="observation-1",
        status=status,
    )
    return ModelCatalogEntry(
        artifact=artifact,
        model=SimpleNamespace(model_id="miro-model-1"),
        observation=observation,
        selected_metadata=(),
        capability_evidence_json="{}",
        runtime_qualifications=(),
        multimodal_pairings=(),
    )


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


def test_python_315_workflow_runtime_is_available_with_the_optional_package(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(workflow_module, "PYTHON_VERSION", (3, 15))
    monkeypatch.setattr(workflow_module, "find_spec", lambda _name: object())
    monkeypatch.setattr(workflow_module, "distribution_version", lambda _name: "0.3.63")
    status = workflow_runtime_availability()
    assert status.available
    assert "Python 3.15" in status.detail
    assert "0.3.63" in status.detail


def test_planner_is_not_blocked_on_python_315_before_client_call(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(workflow_module, "PYTHON_VERSION", (3, 15))
    monkeypatch.setattr(
        workflow_module,
        "workflow_runtime_availability",
        lambda: SimpleNamespace(available=True, detail="test"),
    )
    client = _FakeLocalClient("{}")
    LangChainWorkflowPlanner("http://127.0.0.1:8080/v1", client=client)
    assert client.calls == []


@pytest.mark.skipif(sys.version_info < (3, 15), reason="Regression smoke targets Python 3.15 lazy imports.")
def test_python315_real_runnable_smoke_after_pyside_import() -> None:
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["LANGSMITH_TRACING"] = "false"
    script = """
import concurrent.futures as futures
import PySide6
from PySide6.QtCore import QObject
assert not callable(futures.ThreadPoolExecutor)
from dbzscc.app.workflow_runtime import _prepare_langsmith_thread_pool
_prepare_langsmith_thread_pool()
assert callable(futures.ThreadPoolExecutor)
from langchain_core.runnables import RunnableLambda, RunnableParallel
chain = RunnableParallel(value=RunnableLambda(lambda item: item['value'])) | RunnableLambda(lambda item: item['value'] + 1)
assert chain.invoke({'value': 41}) == 42
print('Python 3.15 Runnable smoke passed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Python 3.15 Runnable smoke passed" in result.stdout


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


@pytest.mark.skipif(not _LANGCHAIN_AVAILABLE, reason=_LANGCHAIN_REQUIRED)
def test_workflow_plan_persists_current_miro_model_evidence(monkeypatch) -> None:
    entry = _catalog_entry()
    client = _FakeLocalClient(_response(actions=["run_tests"]))
    planner = LangChainWorkflowPlanner(
        "http://127.0.0.1:8080/v1", client=client
    )

    plan = planner.plan("local-model", "Run the tests.", model_catalog_entry=entry)

    assert plan.model_catalog_status == "current_registry_record"
    assert plan.model_artifact_id == "artifact-1"
    assert plan.model_observation_id == "observation-1"
    assert plan.model_identity_id == "miro-model-1"
    assert plan.model_metadata_prefix_sha256 == "prefix-sha256"
    assert plan.model_catalog_entry is entry
    assert len(client.calls) == 1
    selected_model, messages = client.calls[0]
    assert isinstance(selected_model, CatalogChatModel)
    assert selected_model.model_catalog_entry is entry
    assert "artifact-1" in messages[1].content
    assert "prefix-sha256" in messages[1].content


def test_workflow_plan_rejects_stale_miro_entry_before_model_call(monkeypatch) -> None:
    import dbzscc.app.workflow_runtime as workflow_module

    monkeypatch.setattr(
        workflow_module,
        "workflow_runtime_availability",
        lambda: SimpleNamespace(available=True, detail="test"),
    )
    monkeypatch.setattr(LangChainWorkflowPlanner, "_build_chain", lambda _self: object())
    entry = _catalog_entry(status="metadata_parse_failed")
    planner = LangChainWorkflowPlanner("http://127.0.0.1:8080/v1", client=_FakeLocalClient(""))

    with pytest.raises(WorkflowRuntimeError, match="aktuellen geparsten MIRO-Modellbeleg"):
        planner.plan("local-model", "Run the tests.", model_catalog_entry=entry)
