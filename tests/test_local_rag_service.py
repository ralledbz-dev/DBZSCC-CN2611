from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import dbzscc.app.local_rag_service as rag_service
from dbzscc.app.chat_context_service import build_rag_context_user_message
from dbzscc.app.local_rag_service import (
    LocalRagResourceObservation,
    LocalRagWorkerClient,
    LocalRagWorkerError,
    RetrievedProjectChunk,
)
from dbzscc.rag.worker import _process_resource_observation


def _project_id(root: Path) -> str:
    normalized = str(root.resolve()).casefold()
    return "workspace-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _index_response(project_id: str) -> dict[str, object]:
    return {
        "ok": True,
        "result": {
            "operation": "index",
            "project_id": project_id,
            "chunk_count": 7,
            "included_file_count": 3,
            "included_bytes": 1_024,
            "excluded_counts": {"hidden_path": 2, "sensitive_content": 1},
            "unchanged": False,
            "created_at": "2026-11-05T08:00:00Z",
            "model_id": "intfloat/multilingual-e5-small",
            "execution_provider": "CPUExecutionProvider",
            "resource_observation": {
                "status": "measured",
                "cpu_time_seconds": 0.125,
                "peak_working_set_bytes": 5_000_000,
                "peak_commit_bytes": 6_000_000,
                "source": "windows_process_counters",
            },
        },
    }


def test_index_invokes_only_the_fixed_project_worker_and_selected_root(tmp_path, monkeypatch) -> None:
    root = tmp_path / "chosen-project"
    root.mkdir()
    (root / "README.md").write_text("Only the selected root may be scanned.", encoding="utf-8")
    client = LocalRagWorkerClient(tmp_path / "app-data" / "rag.sqlite3")
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        request = json.loads(kwargs["input"])
        observed["request"] = request
        return SimpleNamespace(
            stdout=json.dumps(_index_response(_project_id(root))),
            returncode=0,
        )

    monkeypatch.setattr(rag_service.subprocess, "run", fake_run)
    summary = client.index_workspace(root)

    repo_root = Path(rag_service.__file__).resolve().parents[3]
    command = observed["command"]
    kwargs = observed["kwargs"]
    request = observed["request"]
    assert command == [
        str(repo_root / ".work" / "venv314" / "Scripts" / "python.exe"),
        "-I",
        str(repo_root / "src" / "dbzscc" / "rag" / "worker.py"),
    ]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(repo_root)
    assert "env" not in kwargs
    assert request["operation"] == "index"
    assert request["project_root"] == str(root.resolve())
    assert request["project_id"] == _project_id(root)
    assert str(root) not in request["project_id"]
    assert request["database_path"] == str(tmp_path / "app-data" / "rag.sqlite3")
    assert request["manifest_path"] == str(
        repo_root / "models" / "rag" / "multilingual-e5-small" / "manifest.json"
    )
    assert summary.chunk_count == 7
    assert summary.included_file_count == 3
    assert summary.excluded_counts == (("hidden_path", 2), ("sensitive_content", 1))
    assert summary.execution_provider == "CPUExecutionProvider"
    assert summary.resource_observation.cpu_time_seconds == 0.125
    assert summary.resource_observation.peak_working_set_bytes == 5_000_000


def test_index_uses_the_portable_bundle_layout_when_frozen(tmp_path, monkeypatch) -> None:
    root = tmp_path / "chosen-project"
    root.mkdir()
    bundle = tmp_path / "bundle"
    worker = bundle / "src" / "dbzscc" / "rag" / "worker.py"
    interpreter = bundle / "runtimes" / "rag-worker" / "python.exe"
    manifest = bundle / "models" / "rag" / "multilingual-e5-small" / "manifest.json"
    for path in (worker, interpreter, manifest):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test asset")
    monkeypatch.setattr(rag_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(rag_service.sys, "_MEIPASS", str(bundle), raising=False)
    client = LocalRagWorkerClient(tmp_path / "rag.sqlite3")
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        request = json.loads(kwargs["input"])
        return SimpleNamespace(
            stdout=json.dumps(_index_response(request["project_id"])),
            returncode=0,
        )

    monkeypatch.setattr(rag_service.subprocess, "run", fake_run)
    client.index_workspace(root)

    kwargs = observed["kwargs"]
    assert observed["command"] == [str(interpreter), "-I", str(worker)]
    assert kwargs["cwd"] == str(bundle)
    assert json.loads(kwargs["input"])["manifest_path"] == str(manifest)


def test_search_validates_project_scoped_relative_source_results(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    client = LocalRagWorkerClient(tmp_path / "rag.sqlite3")
    project_id = _project_id(root)

    def fake_run(_command, **kwargs):
        request = json.loads(kwargs["input"])
        assert request["operation"] == "search"
        assert request["project_id"] == project_id
        assert request["query"] == "How does local retrieval work?"
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": {
                        "operation": "search",
                        "project_id": project_id,
                        "model_id": "intfloat/multilingual-e5-small",
                        "execution_provider": "CPUExecutionProvider",
                        "resource_observation": {
                            "status": "measured",
                            "cpu_time_seconds": 0.25,
                            "peak_working_set_bytes": 8_000_000,
                            "peak_commit_bytes": 9_000_000,
                            "source": "windows_process_counters",
                        },
                        "results": [
                            {
                                "relative_path": "src/retrieval.py",
                                "format": "py",
                                "symbol": "retrieve [python_ast]",
                                "line_start": 10,
                                "line_end": 18,
                                "text": "Local FTS5 results are fused with CPU E5 vectors.",
                            }
                        ],
                    },
                }
            ),
            returncode=0,
        )

    monkeypatch.setattr(rag_service.subprocess, "run", fake_run)
    results = client.search_workspace(root, "How does local retrieval work?")

    assert results == (
        RetrievedProjectChunk(
            relative_path="src/retrieval.py",
            format="py",
            symbol="retrieve [python_ast]",
            line_start=10,
            line_end=18,
            text="Local FTS5 results are fused with CPU E5 vectors.",
        ),
    )
    assert client.last_resource_observation.cpu_time_seconds == 0.25
    request_message = build_rag_context_user_message(
        "How does local retrieval work?", (), results
    )
    assert '"path": "src/retrieval.py"' in request_message
    assert '"line_start": 10' in request_message
    assert "untrusted data" in request_message


def test_unknown_resource_observation_stays_unknown(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    client = LocalRagWorkerClient(tmp_path / "rag.sqlite3")
    project_id = _project_id(root)
    response = _index_response(project_id)
    response["result"]["resource_observation"] = {
        "status": "unknown",
        "cpu_time_seconds": None,
        "peak_working_set_bytes": None,
        "peak_commit_bytes": None,
        "source": "unavailable",
    }

    monkeypatch.setattr(
        rag_service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(response), returncode=0),
    )
    summary = client.index_workspace(root)

    assert summary.resource_observation == LocalRagResourceObservation(
        status="unknown",
        cpu_time_seconds=None,
        peak_working_set_bytes=None,
        peak_commit_bytes=None,
        source="unavailable",
    )
    assert "unbekannt" in summary.resource_observation.status_text()


def test_worker_resource_observation_is_measured_or_explicitly_unknown() -> None:
    observation = _process_resource_observation()
    assert set(observation) == {
        "status",
        "cpu_time_seconds",
        "peak_working_set_bytes",
        "peak_commit_bytes",
        "source",
    }
    if observation["status"] == "measured":
        assert observation["source"] == "windows_process_counters"
        assert observation["cpu_time_seconds"] >= 0
        assert observation["peak_working_set_bytes"] > 0
        assert observation["peak_commit_bytes"] > 0
    else:
        assert observation == {
            "status": "unknown",
            "cpu_time_seconds": None,
            "peak_working_set_bytes": None,
            "peak_commit_bytes": None,
            "source": "unavailable",
        }


def test_worker_response_cannot_escape_project_or_change_embedding_provider(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    client = LocalRagWorkerClient(tmp_path / "rag.sqlite3")
    project_id = _project_id(root)

    def unsafe_path(_command, **_kwargs):
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": {
                        "operation": "search",
                        "project_id": project_id,
                        "model_id": "intfloat/multilingual-e5-small",
                        "execution_provider": "CPUExecutionProvider",
                        "resource_observation": {
                            "status": "unknown",
                            "cpu_time_seconds": None,
                            "peak_working_set_bytes": None,
                            "peak_commit_bytes": None,
                            "source": "unavailable",
                        },
                        "results": [
                            {
                                "relative_path": "../outside.txt",
                                "format": "txt",
                                "symbol": "text [line_fallback]",
                                "line_start": 1,
                                "line_end": 1,
                                "text": "must be rejected",
                            }
                        ],
                    },
                }
            ),
            returncode=0,
        )

    monkeypatch.setattr(rag_service.subprocess, "run", unsafe_path)
    with pytest.raises(LocalRagWorkerError, match="unsicheren Quellpfad"):
        client.search_workspace(root, "query")


def test_rejects_unselected_or_linked_project_roots_before_worker_launch(tmp_path, monkeypatch) -> None:
    client = LocalRagWorkerClient(tmp_path / "rag.sqlite3")
    called = False

    def forbidden_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("The worker must not run for an invalid project root.")

    monkeypatch.setattr(rag_service.subprocess, "run", forbidden_run)
    with pytest.raises(ValueError, match="ausdrücklich"):
        client.index_workspace("")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-project"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")
    with pytest.raises(ValueError, match="reales Verzeichnis"):
        client.index_workspace(link)
    assert not called
