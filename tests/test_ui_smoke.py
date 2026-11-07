from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QLabel

import dbzscc.ui.main_window as main_window_module
from dbzscc.app.chat_client import CatalogChatModel, ChatProviderError, ChatStreamCancelled
from dbzscc.app.config import AppSettings
from dbzscc.app.execution_events import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionRunRecord,
    ExecutionRunState,
)
from dbzscc.app.file_edit_service import capture_file
from dbzscc.app.local_rag_service import (
    LocalRagIndexSummary,
    LocalRagWorkerError,
    RetrievedProjectChunk,
)
from dbzscc.app.resource_orchestrator import PlanStatus, ResourcePlan
from dbzscc.miro.model_registry import ModelCatalogEntry
from dbzscc.ui.main_window import MainWindow


def _disable_startup_refresh() -> None:
    QSettings().setValue("miro/startup_refresh", False)


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first = luminance(foreground)
    second = luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def test_theme_preference_is_not_written_on_startup_but_is_on_user_change(
    tmp_path, monkeypatch
) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings")
    )
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Theme-Persistence-Test")
    app.setApplicationName("DBZSCC2611-Theme-Persistence-Test")
    preferences = QSettings()
    preferences.setValue("appearance/theme", "light")
    preferences.setValue("miro/startup_refresh", False)
    preferences.sync()
    writes = []

    class RecordingSettings:
        def __init__(self) -> None:
            self._settings = QSettings()

        def value(self, *args, **kwargs):
            return self._settings.value(*args, **kwargs)

        def setValue(self, key, value):
            writes.append((key, value))
            self._settings.setValue(key, value)

        def sync(self):
            self._settings.sync()

    monkeypatch.setattr(main_window_module, "QSettings", RecordingSettings)
    monkeypatch.setattr(
        main_window_module, "application_data_directory", lambda: tmp_path / "appdata"
    )
    window = MainWindow()
    try:
        assert window.theme_combo.currentData() == "light"
        assert writes == []
        assert preferences.value("appearance/theme") == "light"

        window.theme_combo.setCurrentIndex(window.theme_combo.findData("dark"))
        app.processEvents()

        assert writes == [("appearance/theme", "dark")]
        assert preferences.value("appearance/theme") == "dark"
    finally:
        window.close()
        app.processEvents()


def test_main_window_starts_without_runtime_probe_or_model_scan(tmp_path, monkeypatch) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Test")
    app.setApplicationName("DBZSCC2611-Test")
    _disable_startup_refresh()
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")

    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        assert window.pages.count() == 8
        assert window._page_by_name["Control Desk"] is not None
        assert window._runtime_status == "Noch nicht geprüft"
        assert window._last_discovery is None
        assert window._settings.model_roots == (r"D:\Models",)
        assert window._settings.runtime_roots == ()
        assert window.chat_output.toPlainText() == ""
        assert window._page_by_name["Chat"] is not None
        assert window._page_by_name["Projekt"] is not None
        assert window._page_by_name["Diff & Änderungen"] is not None
        assert window._page_by_name["Workflows"] is not None
        assert window._page_by_name["MIRO · Modell-Vault"] is not None
        assert window._page_by_name["Runtime"] is not None
        assert window._page_by_name["Einstellungen"] is not None
        assert "Nicht gemessen" in window.dashboard_resources.text()
        assert "Noch kein Run gestartet" in window.dashboard_run_state.text()
        assert "noch nicht runtime-geprüft" in window.dashboard_model.text().lower() or "kein modell" in window.dashboard_model.text().lower()
        assert not window.workflow_plan_button.isEnabled()
        assert "Gesperrt" in window.workflow_status_label.text()
        assert "Modellvorschlag allein startet nichts" in window.workflow_devserver_status.text()
        assert not window.file_edit_request_button.isEnabled()
        assert not window.file_edit_apply_button.isEnabled()
        assert "Nicht verfügbar" in window.file_edit_status.text()
        assert not window.chat_context_choose_button.isEnabled()
        assert "nichts automatisch" in window.chat_context_paths_label.text()
    finally:
        window.close()
        app.processEvents()


def test_startup_refresh_is_background_and_uses_only_configured_model_roots(
    tmp_path, monkeypatch
) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings")
    )
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Startup-Refresh-Test")
    app.setApplicationName("DBZSCC2611-Startup-Refresh-Test")
    QSettings().setValue("miro/startup_refresh", True)
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    started = threading.Event()
    release = threading.Event()
    observed = {}

    def fake_discover(self, *, model_roots, runtime_roots=(), cancel_event=None):
        observed["model_roots"] = tuple(model_roots)
        observed["runtime_roots"] = tuple(runtime_roots)
        observed["cancel_event"] = cancel_event
        started.set()
        release.wait(3)
        return SimpleNamespace(
            registry_sync=SimpleNamespace(
                run_id="startup-run",
                artifact_count=0,
                parse_failure_count=0,
                complete=False,
            ),
            scanner_issues=(SimpleNamespace(code="scan_cancelled"),),
            has_partial_results=True,
        )

    monkeypatch.setattr(main_window_module.MiroApplicationService, "discover", fake_discover)
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        assert started.wait(2)
        assert window._miro_scan_active
        assert "Hintergrund" in window.scan_status.text()
        assert observed["model_roots"] == (r"D:\Models",)
        assert observed["runtime_roots"] == ()
        assert observed["cancel_event"] is not None
        assert window.scan_cancel_button.isEnabled()

        window._cancel_miro_scan()
        assert observed["cancel_event"].is_set()
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and window._miro_scan_active:
            app.processEvents()
            time.sleep(0.01)
        assert not window._miro_scan_active
        assert window._last_discovery_run_id == "startup-run"
        assert "Abgebrochen" in window.scan_status.text()
    finally:
        release.set()
        window.close()
        app.processEvents()


def test_project_root_selects_isolated_workflow_interpreter(tmp_path, monkeypatch) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Test")
    app.setApplicationName("DBZSCC2611-Test")
    _disable_startup_refresh()
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "fixture"\n', encoding="utf-8")
    python = root / ".work" / "venv314" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture interpreter")

    window = MainWindow()
    try:
        window._set_project_root(str(root), persist=False)
        assert window.workflow_python_input.text() == str(python)
    finally:
        window.close()
        app.processEvents()


def test_workflow_plan_requires_confirmation_and_never_starts_a_devserver(
    tmp_path, monkeypatch
) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings")
    )
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Workflow-Test")
    app.setApplicationName("DBZSCC2611-Workflow-Plan-Test")
    _disable_startup_refresh()
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    observed = {}

    model_path = tmp_path / "models" / "local-model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"current-model-fixture")
    snapshot = model_path.stat()
    observation_id = "observation-1"
    catalog_entry = ModelCatalogEntry(
        artifact=SimpleNamespace(
            artifact_id="artifact-1",
            path=str(model_path.absolute()),
            status="metadata_read",
            current_observation_id=observation_id,
            file_size_bytes=snapshot.st_size,
            file_modified_at_utc=datetime.fromtimestamp(
                snapshot.st_mtime, timezone.utc
            ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        ),
        model=SimpleNamespace(model_id="model-1", display_name="local-model"),
        observation=SimpleNamespace(
            observation_id=observation_id,
            status="metadata_read",
            metadata_prefix_sha256="a" * 64,
        ),
        selected_metadata=(),
        capability_evidence_json="{}",
        runtime_qualifications=(),
        multimodal_pairings=(),
    )

    class FakePlanner:
        def __init__(self, endpoint: str) -> None:
            observed["endpoint"] = endpoint

        def plan(self, model: str, request: str, *, model_catalog_entry=None):
            observed["model"] = model
            observed["request"] = request
            observed["model_catalog_entry"] = model_catalog_entry
            return main_window_module.WorkflowPlan(
                actions=(main_window_module.WorkflowAction.START_DEVELOPMENT_SERVER,),
                rationale="Ein Server könnte für eine spätere Vorschau nützlich sein.",
                model_name=model,
                model_artifact_id=model_catalog_entry.artifact.artifact_id,
                model_observation_id=model_catalog_entry.observation.observation_id,
                model_catalog_status="current_registry_record",
                model_catalog_entry=model_catalog_entry,
            )

    def confirm(_parent, _title, text, *_args):
        observed["confirmation"] = text
        return main_window_module.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(main_window_module, "LangChainWorkflowPlanner", FakePlanner)
    monkeypatch.setattr(main_window_module.QMessageBox, "question", confirm)
    monkeypatch.setattr(
        main_window_module,
        "workflow_runtime_availability",
        lambda: SimpleNamespace(available=True, detail="Testadapter verfügbar"),
    )
    window = MainWindow()
    try:
        monkeypatch.setattr(
            window._miro_service,
            "query_catalog",
            lambda _query: (catalog_entry,),
        )
        window._verified_endpoint = window._settings.model_endpoint
        window._verified_local_model_ids = frozenset({"local-model"})
        window.model_combo.setEditText("local-model")
        window.workflow_request.setPlainText("Bitte einen lokalen Devserver als Vorschlag prüfen.")
        window._update_workflow_controls()
        assert window.workflow_plan_button.isEnabled()

        window.workflow_plan_button.click()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            if "Manifest-, Ressourcen- und Befehlsbestätigung" in window.workflow_plan_output.toPlainText():
                break
            time.sleep(0.01)

        assert "Manifest-, Ressourcen- und Befehlsbestätigung" in window.workflow_plan_output.toPlainText()
        assert observed["endpoint"] == "http://127.0.0.1:8080/v1"
        assert observed["model"] == "local-model"
        assert observed["request"] in observed["confirmation"]
        assert observed["model_catalog_entry"] is catalog_entry
        assert "keine Projektdateien" in observed["confirmation"]
        assert not window._runtime_controller.has_owned_process
        assert not window._development_server.has_owned_process
        assert window._store.list_messages(window._active_conversation_id) == ()
    finally:
        window.close()
        app.processEvents()


def test_chat_send_does_not_change_file_and_edit_actions_stay_unavailable_without_runtime(
    tmp_path, monkeypatch
) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Test")
    app.setApplicationName("DBZSCC2611-File-Edit-Gate-Test")
    _disable_startup_refresh()
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    root = tmp_path / "explicit-root"
    root.mkdir()
    target = root / "note.txt"
    target.write_text("original\n", encoding="utf-8")

    window = MainWindow()
    try:
        window._file_edit_root = root.resolve()
        window._file_edit_snapshot = capture_file(root, target)
        window.file_edit_instruction.setPlainText("Change this file")
        window.model_combo.setEditText("")
        window.message_input.setPlainText("Please change the selected file")
        window._update_file_edit_controls()
        assert not window.file_edit_request_button.isEnabled()
        assert not window.file_edit_apply_button.isEnabled()

        window._send_message()

        assert window._file_edit_proposal is None
        assert target.read_text(encoding="utf-8") == "original\n"
    finally:
        window.close()
        app.processEvents()


def _wait_for_chat_status(app: QApplication, window: MainWindow, expected: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.processEvents()
        if expected in window.chat_status.text():
            return
        time.sleep(0.01)
    raise AssertionError(f"Chat-Status erreichte nicht {expected!r}: {window.chat_status.text()!r}")


def _make_stream_window(
    tmp_path, monkeypatch, app_name: str, *, runtime_ready: bool = True
) -> tuple[QApplication, MainWindow]:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Stream-Test")
    app.setApplicationName(app_name)
    _disable_startup_refresh()
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    window = MainWindow()
    model_path = tmp_path / "models" / "local-test-model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"current-model-fixture")
    snapshot = model_path.stat()
    observation_id = "observation-1"
    catalog_entry = ModelCatalogEntry(
        artifact=SimpleNamespace(
            artifact_id="artifact-1",
            path=str(model_path.absolute()),
            kind="gguf_candidate",
            identity_status="resolved",
            status="metadata_read",
            current_observation_id=observation_id,
            file_size_bytes=snapshot.st_size,
            file_modified_at_utc=datetime.fromtimestamp(
                snapshot.st_mtime, timezone.utc
            ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        ),
        model=SimpleNamespace(model_id="model-1", display_name="local-test-model"),
        observation=SimpleNamespace(
            observation_id=observation_id,
            status="metadata_read",
            metadata_prefix_sha256="b" * 64,
        ),
        selected_metadata=(),
        capability_evidence_json="{}",
        runtime_qualifications=(),
        multimodal_pairings=(),
    )
    monkeypatch.setattr(
        window._miro_service,
        "query_catalog",
        lambda _query: (catalog_entry,),
    )
    window._test_catalog_entry = catalog_entry
    window._external_endpoint_confirmed = window._settings.model_endpoint.rstrip("/")
    window.model_combo.setEditText("local-test-model")
    window.message_input.setPlainText("Bitte antworte gestreamt")
    if runtime_ready:
        window.runtime_model_combo.addItem("local-test-model", str(model_path.absolute()))
        window._runtime_plan = SimpleNamespace(
            run_id="runtime-run-fixture",
            admitted=True,
            backend=main_window_module.Backend(window.runtime_backend_combo.currentData()),
            model_path=model_path,
            model_artifact_id=catalog_entry.artifact.artifact_id,
            model_observation_id=catalog_entry.observation.observation_id,
            model_full_file_sha256=None,
            resource_plan=SimpleNamespace(
                status=SimpleNamespace(value="admitted"),
                host_memory_mib=512,
                accelerator_memory_mib=0,
                cpu_threads=2,
            ),
            target_id=None,
            target_name="CPU",
            port=8080,
            runtime_build_sha256="c" * 64,
        )
        window._verified_endpoint = window._settings.validated_endpoint()
        window._verified_local_model_ids = frozenset({"local-test-model"})
        monkeypatch.setattr(
            window._runtime_controller,
            "is_managed_healthy",
            lambda endpoint: endpoint == window._settings.validated_endpoint(),
        )
        monkeypatch.setattr(
            window,
            "_active_chat_endpoint",
            lambda: window._settings.validated_endpoint(),
        )
    window._update_chat_send_controls()
    window.show()
    app.processEvents()
    return app, window


def test_rag_indexing_requires_selected_project_and_explicit_confirmation(
    tmp_path, monkeypatch
) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings")
    )
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-RAG-Index-Consent-Test")
    app.setApplicationName("DBZSCC2611-RAG-Index-Consent-Test")
    _disable_startup_refresh()
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    root = tmp_path / "explicit-project"
    root.mkdir()
    window = MainWindow()
    queued = []
    indexed = []
    try:
        window._set_project_root(str(root), persist=False)
        window._run_worker = lambda function, success, failure: queued.append(
            (function, success, failure)
        )
        monkeypatch.setattr(
            main_window_module.QMessageBox,
            "question",
            lambda *_args, **_kwargs: main_window_module.QMessageBox.StandardButton.No,
        )
        window._index_selected_project_for_rag()
        assert queued == []

        dialogs = []
        monkeypatch.setattr(
            main_window_module.QMessageBox,
            "question",
            lambda *_args, **_kwargs: (
                dialogs.append(_args[2])
                or main_window_module.QMessageBox.StandardButton.Yes
            ),
        )
        monkeypatch.setattr(
            window._rag_worker_client,
            "index_workspace",
            lambda selected: indexed.append(selected)
            or LocalRagIndexSummary(
                project_id="workspace-test",
                chunk_count=1,
                included_file_count=1,
                included_bytes=10,
                excluded_counts=(),
                unchanged=False,
                model_id="intfloat/multilingual-e5-small",
                execution_provider="CPUExecutionProvider",
            ),
        )
        window._index_selected_project_for_rag()
        assert len(queued) == 1
        assert str(root.resolve()) in dialogs[0]
        assert "Nur freigegebene Textdateien" in dialogs[0]
        assert indexed == []
        result = queued[0][0]()
        assert indexed == [str(root.resolve())]
        queued[0][1](result)
        assert "1 Chunks" in window.chat_rag_status_label.text()
        assert "RAG-Worker-CPU-Zeit und RAM-Spitzen unbekannt." in window.chat_rag_status_label.text()
    finally:
        window.close()
        app.processEvents()


def test_chat_rag_sends_only_confirmed_local_hits_and_does_not_persist_excerpts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: main_window_module.QMessageBox.StandardButton.Yes,
    )
    app, window = _make_stream_window(tmp_path, monkeypatch, "DBZSCC2611-RAG-Chat-Test")
    root = tmp_path / "chosen-project"
    root.mkdir()
    window._set_project_root(str(root), persist=False)
    window.chat_rag_use_checkbox.setChecked(True)
    excerpt = "The local project uses CPU E5 vectors and FTS5."
    monkeypatch.setattr(
        window._rag_worker_client,
        "search_workspace",
        lambda selected, query: (
            RetrievedProjectChunk(
                relative_path="docs/retrieval.md",
                format="md",
                symbol="Offline retrieval",
                line_start=12,
                line_end=16,
                text=excerpt,
            ),
        )
        if selected == str(root.resolve()) and query == "Bitte antworte gestreamt"
        else (),
    )
    observed = {}

    def fake_stream(self, model, messages, on_delta, cancel_event, *, local_only=False):
        observed["model"] = model
        observed["messages"] = messages
        observed["local_only"] = local_only
        assert not cancel_event.is_set()
        on_delta("Antwort mit lokaler Quelle")
        return "Antwort mit lokaler Quelle"

    monkeypatch.setattr(main_window_module.OpenAICompatibleClient, "stream_complete", fake_stream)
    window._record_chat_runtime_test_evidence = lambda: None
    try:
        window._send_message()
        _wait_for_chat_status(app, window, "lokal gespeichert")

        request = observed["messages"][-1].content
        assert observed["local_only"] is True
        assert "docs/retrieval.md" in request
        assert excerpt in request
        assert "untrusted data" in request
        assert "docs/retrieval.md:12-16" in window.chat_rag_sources_label.text()
        assert "RAG-Worker-CPU-Zeit und RAM-Spitzen unbekannt." in window.chat_rag_sources_label.text()
        assert not window.chat_rag_use_checkbox.isChecked()
        stored = window._store.list_messages(window._active_conversation_id)
        assert [message.content for message in stored] == [
            "Bitte antworte gestreamt",
            "Antwort mit lokaler Quelle",
        ]
        assert all(excerpt not in message.content for message in stored)
    finally:
        window.close()
        app.processEvents()


def test_missing_rag_index_aborts_before_calling_chat_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: main_window_module.QMessageBox.StandardButton.Yes,
    )
    app, window = _make_stream_window(tmp_path, monkeypatch, "DBZSCC2611-RAG-No-Index-Test")
    root = tmp_path / "chosen-project"
    root.mkdir()
    window._set_project_root(str(root), persist=False)
    window.chat_rag_use_checkbox.setChecked(True)
    monkeypatch.setattr(
        window._rag_worker_client,
        "search_workspace",
        lambda *_args: (_ for _ in ()).throw(
            LocalRagWorkerError("Für dieses Projekt ist noch kein RAG-Index aktiv.")
        ),
    )
    runtime_calls = []

    def forbidden_runtime_call(*_args, **_kwargs):
        runtime_calls.append(True)
        raise AssertionError("No runtime call is allowed after local retrieval fails.")

    monkeypatch.setattr(
        main_window_module.OpenAICompatibleClient,
        "stream_complete",
        forbidden_runtime_call,
    )
    try:
        window._send_message()
        _wait_for_chat_status(app, window, "Lokale RAG-Suche fehlgeschlagen")
        assert runtime_calls == []
        assert "kein RAG-Index aktiv" in window.chat_status.text()
        stored = window._store.list_messages(window._active_conversation_id)
        assert [message.role for message in stored] == ["user"]
        assert stored[0].content == "Bitte antworte gestreamt"
    finally:
        window.close()
        app.processEvents()


def test_chat_send_stays_disabled_until_a_verified_managed_runtime_is_bound(
    tmp_path, monkeypatch
) -> None:
    app, window = _make_stream_window(
        tmp_path,
        monkeypatch,
        "DBZSCC2611-Chat-Readiness-Gate-Test",
        runtime_ready=False,
    )
    try:
        assert not window.send_button.isEnabled()
        assert "Aktueller /v1-Endpunkt wurde noch nicht erreichbar geprüft" in window.chat_send_help.text()
        assert "App-gesteuerte Runtime mit Lauf-/Modell-/Backendbindung fehlt" in window.chat_send_help.text()

        window._send_message()

        assert window._store.list_messages(window._active_conversation_id) == ()
        assert not window._runtime_controller.has_owned_process

        # A successful external /v1/models response alone cannot prove the managed run/backend binding.
        window._verified_endpoint = window._settings.validated_endpoint()
        window._verified_local_model_ids = frozenset({"local-test-model"})
        window._update_chat_send_controls()

        assert not window.send_button.isEnabled()
        assert "Lauf-/Modell-/Backendbindung fehlt" in window.chat_send_help.text()
        assert window._store.list_messages(window._active_conversation_id) == ()
    finally:
        window.close()
        app.processEvents()


def test_chat_send_rejects_runtime_plan_with_stale_backend_or_model_path(
    tmp_path, monkeypatch
) -> None:
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Chat-Binding-Mismatch-Test"
    )
    try:
        assert window.send_button.isEnabled()
        assert window._chat_send_blockers() == ()

        window._runtime_plan.backend = main_window_module.Backend.VULKAN
        window._update_chat_send_controls()
        assert not window.send_button.isEnabled()
        assert "Backend" in window.chat_send_help.text()

        window._runtime_plan.backend = window.runtime_backend_combo.currentData()
        window._runtime_plan.model_path = tmp_path / "different-model.gguf"
        window._update_chat_send_controls()
        assert not window.send_button.isEnabled()
        assert "Modellpfad" in window.chat_send_help.text()
        assert "Katalogbeobachtung" in window.chat_send_help.text()
    finally:
        window.close()
        app.processEvents()


def test_disabled_buttons_and_sidebar_fit_both_themes_and_supported_sizes(
    tmp_path, monkeypatch
) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings")
    )
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Disabled-Style-Test")
    app.setApplicationName("DBZSCC2611-Disabled-Style-Test")
    _disable_startup_refresh()
    monkeypatch.setattr(
        main_window_module, "application_data_directory", lambda: tmp_path / "appdata"
    )
    window = MainWindow()
    try:
        window.show()
        caption = window.findChild(QLabel, "sidebarCaption")
        assert caption is not None
        assert caption.wordWrap()
        assert not window.workflow_plan_button.isEnabled()
        assert not window.workflow_execute_button.isEnabled()
        assert not window.devserver_start_button.isEnabled()

        for width, height in ((1024, 700), (1440, 900)):
            window.resize(width, height)
            app.processEvents()
            assert (window.width(), window.height()) == (width, height)
            assert caption.width() <= 203
            metrics = QFontMetrics(caption.font())
            assert max(
                metrics.horizontalAdvance(line) for line in caption.text().splitlines()
            ) <= caption.contentsRect().width()
            assert caption.height() >= len(caption.text().splitlines()) * metrics.height()

        for theme, foreground, background in (
            ("light", "#56616c", "#e1e5e9"),
            ("dark", "#c2cad2", "#343b43"),
        ):
            window._apply_theme(theme)
            app.processEvents()
            stylesheet = window.styleSheet()
            assert "QPushButton:disabled" in stylesheet
            assert "#primaryButton:disabled" in stylesheet
            assert f"color: {foreground}" in stylesheet
            assert f"background: {background}" in stylesheet
            assert _contrast_ratio(foreground, background) >= 4.5
            assert stylesheet.index("#primaryButton:disabled") > stylesheet.index(
                "QPushButton:checked, #primaryButton"
            )
    finally:
        window.close()
        app.processEvents()


def test_runtime_model_picker_uses_only_current_typed_catalog_entries(tmp_path, monkeypatch) -> None:
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Catalog-Picker-Test"
    )
    entry = window._test_catalog_entry
    window.runtime_model_root_input.setText(str(Path(entry.artifact.path).parent))
    sync = SimpleNamespace(
        run_id="picker-run",
        complete=True,
        artifact_count=1,
        directories_visited=1,
        entries_seen=1,
        regular_files_seen=1,
        non_gguf_artifact_count=0,
        reparse_points_skipped=0,
        access_denied_directories=0,
        parse_failure_count=0,
    )
    discovery = SimpleNamespace(
        registry_sync=sync,
        scanner_issues=(),
        has_partial_results=False,
    )
    try:
        window._runtime_models_loaded((discovery, (entry,)))

        assert window._last_discovery_run_id == "picker-run"
        assert window.runtime_model_combo.count() == 1
        assert window.runtime_model_combo.itemData(0) == entry.artifact.path
        assert "vollständig" in window.runtime_plan_summary.text()
    finally:
        window.close()
        app.processEvents()


def test_chat_stream_ui_persists_one_complete_answer_from_worker(tmp_path, monkeypatch) -> None:
    def fake_stream(self, model, messages, on_delta, cancel_event):
        assert isinstance(model, CatalogChatModel)
        assert model.model_name == "local-test-model"
        assert model.model_catalog_entry is window._test_catalog_entry
        assert messages[-1].content == "Bitte antworte gestreamt"
        assert not cancel_event.is_set()
        on_delta("Vollständig ")
        on_delta("gestreamt")
        return "Vollständig gestreamt"

    monkeypatch.setattr(main_window_module.OpenAICompatibleClient, "stream_complete", fake_stream)
    app, window = _make_stream_window(tmp_path, monkeypatch, "DBZSCC2611-Stream-Complete-Test")
    try:
        window._send_message()
        assert window.send_button.text() == "Stoppen"
        assert not window.new_thread_button.isEnabled()
        _wait_for_chat_status(app, window, "lokal gespeichert")

        messages = window._store.list_messages(window._active_conversation_id)
        assert [message.role for message in messages] == ["user", "assistant"]
        assert [message.content for message in messages] == [
            "Bitte antworte gestreamt",
            "Vollständig gestreamt",
        ]
        assert window.chat_output.toPlainText().count("Vollständig gestreamt") == 1
        assert "Antwort läuft" not in window.chat_output.toPlainText()
        assert window.send_button.text() == "Senden"
        assert window.new_thread_button.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_chat_stream_ui_cancel_discards_draft_but_keeps_user_message(tmp_path, monkeypatch) -> None:
    started = threading.Event()

    def fake_stream(self, model, messages, on_delta, cancel_event):
        on_delta("Nicht gespeicherter Teil")
        started.set()
        while not cancel_event.wait(0.01):
            pass
        raise ChatStreamCancelled("abgebrochen")

    monkeypatch.setattr(main_window_module.OpenAICompatibleClient, "stream_complete", fake_stream)
    app, window = _make_stream_window(tmp_path, monkeypatch, "DBZSCC2611-Stream-Cancel-Test")
    try:
        window._send_message()
        assert started.wait(2)
        assert window.send_button.text() == "Stoppen"
        assert window.send_button.isEnabled()
        window.send_button.click()
        assert window._chat_cancel_event is not None and window._chat_cancel_event.is_set()
        _wait_for_chat_status(app, window, "keine Teilantwort gespeichert")

        messages = window._store.list_messages(window._active_conversation_id)
        assert [(message.role, message.content) for message in messages] == [
            ("user", "Bitte antworte gestreamt")
        ]
        assert "Nicht gespeicherter Teil" not in window.chat_output.toPlainText()
        assert window.send_button.text() == "Senden"
        assert window.message_input.toPlainText() == ""
    finally:
        window.close()
        app.processEvents()


def test_chat_stream_ui_error_after_delta_does_not_persist_partial_answer(tmp_path, monkeypatch) -> None:
    def fake_stream(self, model, messages, on_delta, cancel_event):
        on_delta("Teilantwort vor Fehler")
        raise ChatProviderError("Stream abgerissen")

    monkeypatch.setattr(main_window_module.OpenAICompatibleClient, "stream_complete", fake_stream)
    app, window = _make_stream_window(tmp_path, monkeypatch, "DBZSCC2611-Stream-Error-Test")
    try:
        window._send_message()
        _wait_for_chat_status(app, window, "Runtime-Fehler")

        messages = window._store.list_messages(window._active_conversation_id)
        assert [message.role for message in messages] == ["user"]
        assert "Teilantwort vor Fehler" not in window.chat_output.toPlainText()
        assert window.send_button.text() == "Senden"
    finally:
        window.close()
        app.processEvents()


def test_qt_combo_box_string_enums_are_normalized_for_runtime_and_devserver(
    tmp_path, monkeypatch
) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings")
    )
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Combo-Test")
    app.setApplicationName("DBZSCC2611-Combo-Test")
    _disable_startup_refresh()
    monkeypatch.setattr(
        main_window_module, "application_data_directory", lambda: tmp_path / "appdata"
    )
    window = MainWindow()
    try:
        model_path = tmp_path / "selected.gguf"
        model_path.write_bytes(b"test")
        window.runtime_model_root_input.setText(str(tmp_path))
        window.runtime_model_combo.addItem(model_path.name, str(model_path))
        window.runtime_backend_combo.setCurrentIndex(
            window.runtime_backend_combo.findData(main_window_module.Backend.CPU)
        )

        selection = window._runtime_selection()

        assert selection[2] is main_window_module.Backend.CPU
        assert window.runtime_backend_combo.currentData() == "cpu"
        window.devserver_action_combo.setCurrentIndex(
            window.devserver_action_combo.findData(
                main_window_module.WorkflowAction.DEBUG_PROJECT
            )
        )
        assert (
            window._selected_devserver_action()
            is main_window_module.WorkflowAction.DEBUG_PROJECT
        )
    finally:
        window.close()
        app.processEvents()


def test_devserver_stop_completion_remains_visible(tmp_path, monkeypatch) -> None:
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Devserver-Stop-Status-Test"
    )
    try:
        window._devserver_operation = "stop"
        window._update_devserver_controls()
        assert "Stop läuft" in window.devserver_status_label.text()

        window._devserver_stopped(True)

        assert window.devserver_status_label.text() == "App-eigener Entwicklungsprozess beendet."
        assert not window._development_server.has_owned_process
        assert not window.devserver_stop_button.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_chat_context_is_confirmed_local_once_and_never_persisted(tmp_path, monkeypatch) -> None:
    root = tmp_path / "explicit-project"
    root.mkdir()
    selected = root / "module.py"
    selected.write_text("LOCAL_CONTEXT_SENTINEL = 'private'\n", encoding="utf-8")
    other = root / "not-selected.txt"
    other.write_text("MUST_NOT_BE_INCLUDED", encoding="utf-8")
    captured = {}

    def fake_dialog(*_args, **_kwargs):
        return [str(selected)], "All files (*)"

    def fake_confirmation(_parent, title, text, *_args):
        captured["title"] = title
        captured["confirmation"] = text
        return main_window_module.QMessageBox.StandardButton.Yes

    def fake_stream(self, model, messages, on_delta, cancel_event, *, local_only=False):
        assert isinstance(model, CatalogChatModel)
        assert model.model_name == "local-test-model"
        assert model.model_catalog_entry is window._test_catalog_entry
        assert local_only is True
        assert not cancel_event.is_set()
        assert messages[0].role == "system"
        assert "untrusted reference material" in messages[0].content
        assert messages[-1].role == "user"
        assert "module.py" in messages[-1].content
        assert "LOCAL_CONTEXT_SENTINEL" in messages[-1].content
        assert "MUST_NOT_BE_INCLUDED" not in messages[-1].content
        captured["request_text"] = messages[-1].content
        on_delta("Antwort ohne Dateiänderung")
        return "Antwort ohne Dateiänderung"

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileNames", fake_dialog)
    monkeypatch.setattr(main_window_module.QMessageBox, "question", fake_confirmation)
    monkeypatch.setattr(main_window_module.OpenAICompatibleClient, "stream_complete", fake_stream)
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Chat-Context-Once-Test"
    )
    try:
        window._set_project_root(str(root), persist=False)
        window._choose_chat_context_files()
        assert "module.py" in window.chat_context_paths_label.text()
        assert "not-selected.txt" not in window.chat_context_paths_label.text()

        window.message_input.setPlainText("Explain the selected file")
        window._send_message()
        _wait_for_chat_status(app, window, "lokal gespeichert")

        assert "module.py" in captured["confirmation"]
        assert "127.0.0.1:8080" in captured["confirmation"]
        assert "LOCAL_CONTEXT_SENTINEL" not in captured["confirmation"]
        assert "LOCAL_CONTEXT_SENTINEL" in captured["request_text"]
        assert "Letzte bestätigte Anfrage" in window.chat_context_paths_label.text()
        assert "module.py" in window.chat_context_paths_label.text()
        assert window._chat_context_paths == ()
        messages = window._store.list_messages(window._active_conversation_id)
        assert [(message.role, message.content) for message in messages] == [
            ("user", "Explain the selected file"),
            ("assistant", "Antwort ohne Dateiänderung"),
        ]
        assert all("LOCAL_CONTEXT_SENTINEL" not in message.content for message in messages)
        assert selected.read_text(encoding="utf-8") == "LOCAL_CONTEXT_SENTINEL = 'private'\n"
        assert window._file_edit_proposal is None
        assert not window.file_edit_apply_button.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_chat_context_confirmation_cancel_keeps_prompt_unsent(tmp_path, monkeypatch) -> None:
    root = tmp_path / "explicit-project"
    root.mkdir()
    selected = root / "module.py"
    selected.write_text("cancelled context", encoding="utf-8")
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Chat-Context-Cancel-Test"
    )
    calls = []
    try:
        window._set_project_root(str(root), persist=False)
        window._chat_context_paths = (selected.resolve(),)
        window._update_chat_context_view()
        window.message_input.setPlainText("Do not send until I approve")
        monkeypatch.setattr(
            main_window_module.QMessageBox,
            "question",
            lambda *_args, **_kwargs: main_window_module.QMessageBox.StandardButton.No,
        )
        monkeypatch.setattr(
            main_window_module.OpenAICompatibleClient,
            "stream_complete",
            lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        window._send_message()

        assert calls == []
        assert window.message_input.toPlainText() == "Do not send until I approve"
        assert window._chat_context_paths == (selected.resolve(),)
        assert window._store.list_messages(window._active_conversation_id) == ()
        assert selected.read_text(encoding="utf-8") == "cancelled context"
    finally:
        window.close()
        app.processEvents()


def test_chat_context_never_reads_or_sends_to_remote_endpoint(tmp_path, monkeypatch) -> None:
    root = tmp_path / "explicit-project"
    root.mkdir()
    selected = root / "module.py"
    selected.write_text("REMOTE_MUST_NOT_SEE_THIS", encoding="utf-8")
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Chat-Context-Remote-Block-Test"
    )
    try:
        window._settings = AppSettings(
            model_endpoint="https://api.example.invalid/v1",
            model_name="local-test-model",
        )
        window._set_project_root(str(root), persist=False)
        window._chat_context_paths = (selected.resolve(),)
        window._update_chat_context_view()
        reads = []

        def forbidden_capture(*_args, **_kwargs):
            reads.append(True)
            raise AssertionError("remote endpoint must be rejected before file content is read")

        monkeypatch.setattr(main_window_module, "capture_context_files", forbidden_capture)
        window.message_input.setPlainText("Question that must not be sent with file context")
        window._send_message()

        assert reads == []
        assert "Loopbackserver" in window.chat_status.text()
        assert window._chat_context_paths == (selected.resolve(),)
        messages = window._store.list_messages(window._active_conversation_id)
        assert messages == ()
        assert selected.read_text(encoding="utf-8") == "REMOTE_MUST_NOT_SEE_THIS"
    finally:
        window.close()
        app.processEvents()


def test_repair_retest_uses_existing_approval_without_second_dialog(tmp_path, monkeypatch) -> None:
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Repair-Retest-Approval-Test"
    )
    started = []
    dialogs = []
    monkeypatch.setattr(window, "_start_project_execution", lambda plans: started.append(plans))
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: dialogs.append(True),
    )
    try:
        window._confirm_project_plans((), repair_retest=True)

        assert started == [()]
        assert dialogs == []
    finally:
        window.close()
        app.processEvents()


def test_repair_retest_has_no_three_iteration_cap(tmp_path, monkeypatch) -> None:
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Repair-No-Cap-Test"
    )
    scheduled = []
    window._repair_workflow_plan = SimpleNamespace(
        actions=(main_window_module.WorkflowAction.RUN_TESTS,)
    )
    window._repair_attempts = 7
    window._repair_loop_active = True
    window._repair_project_root = tmp_path
    window._repair_python_executable = tmp_path / "python.exe"
    window._repair_conversation_id = "chat-no-cap"
    window._repair_workflow_id = "workflow-no-cap"
    monkeypatch.setattr(window, "_run_worker", lambda *args: scheduled.append(args))
    try:
        window._prepare_repair_retest()

        assert len(scheduled) == 1
        assert window._project_execution_mode == "repair_retest"
        assert window._project_execution_cancel_event is not None
    finally:
        window._project_execution_active = False
        window._project_execution_mode = None
        window._project_execution_cancel_event = None
        window._allow_close = True
        window.close()
        app.processEvents()


def test_repair_failure_signature_detects_whitespace_stable_cycles() -> None:
    first = MainWindow._repair_failure_signature("FAILED test_a.py::test_x\nE assert 1 == 2\n")
    same = MainWindow._repair_failure_signature("FAILED   test_a.py::test_x E assert 1 == 2")
    different = MainWindow._repair_failure_signature("FAILED test_a.py::test_y E assert 1 == 2")

    assert first == same
    assert first != different


def test_control_desk_current_project_empty_run_and_resource_evidence_states(
    tmp_path, monkeypatch
) -> None:
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Control-Desk-Evidence-Test"
    )
    project = tmp_path / "selected-project"
    project.mkdir()
    try:
        window._set_project_root(str(project), persist=False)
        window._refresh_dashboard()

        assert window.dashboard_project.text() == str(project)
        assert "ausgewählt" in window.dashboard_project_state.text().lower()
        assert window.dashboard_run_state.text() == "Noch kein Run gestartet"
        assert window.workflow_run_id_label.text() == "Run-ID: —"
        assert window.workflow_terminal_output.toPlainText() == ""
        assert "Nicht gemessen" in window.dashboard_resources.text()
        assert "MiB" not in window.dashboard_resources.text()

        partial = ResourcePlan(
            workload_id="fixture-partial",
            model_id="fixture-model",
            status=PlanStatus.REDUCED,
            host_memory_mib=2048,
            host_capacity_mib_before=4096,
            cpu_threads=2,
            cpu_capacity_threads_before=4,
            uncertainties=("Testtelemetrie teilweise",),
        )
        window._set_dashboard_resource_plan(partial, "Testfixture")
        partial_text = window.dashboard_resources.text()
        assert "Teilweise zugelassen · Plan reduziert" in partial_text
        assert "Host-Kapazität 4096 MiB" in partial_text
        assert "Hinweis: Testtelemetrie teilweise" in partial_text
        assert "kein Live-Monitor" in partial_text

        healthy = ResourcePlan(
            workload_id="fixture-admitted",
            model_id="fixture-model",
            status=PlanStatus.ADMITTED,
            host_memory_mib=1024,
            host_capacity_mib_before=8192,
            cpu_threads=2,
            cpu_capacity_threads_before=8,
        )
        window._set_dashboard_resource_plan(healthy, "Testfixture")
        healthy_text = window.dashboard_resources.text()
        assert "Geprüfter Plan · zugelassen" in healthy_text
        assert "Host-Bedarf 1024 MiB" in healthy_text
        assert "Host-Kapazität 8192 MiB" in healthy_text
        assert "GPU" not in healthy_text
    finally:
        window.close()
        app.processEvents()


def test_pipeline_uses_persisted_run_events_for_progress_cancel_review_and_retest(
    tmp_path, monkeypatch
) -> None:
    app, window = _make_stream_window(
        tmp_path, monkeypatch, "DBZSCC2611-Pipeline-Persisted-Events-Test"
    )
    conversation_id = window._active_conversation_id
    workflow_id = "workflow-ui-fixture"
    project = tmp_path / "pipeline-project"
    project.mkdir()
    window._set_project_root(str(project), persist=False)
    window._workflow_plan = SimpleNamespace(
        actions=(
            main_window_module.WorkflowAction.RUN_TESTS,
            main_window_module.WorkflowAction.PROPOSE_PATCH,
        )
    )
    window._workflow_conversation_id = conversation_id

    def append_state(run_id: str, state: ExecutionRunState, *, iteration: int = 0, stop_reason=None) -> None:
        existing = window._execution_store.get_run(run_id)
        if existing is None:
            window._execution_store.bind_run(
                ExecutionRunRecord(
                    conversation_id=conversation_id,
                    workflow_id=workflow_id,
                    run_id=run_id,
                    project_root=str(project),
                    project_fingerprint="test-fingerprint",
                    state=state,
                    iteration=iteration,
                    stop_reason=stop_reason,
                )
            )
        else:
            window._execution_store.transition_run(
                run_id,
                state,
                iteration=iteration,
                stop_reason=stop_reason,
            )
        window._execution_store.append(
            ExecutionEvent(
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                run_id=run_id,
                event_type=ExecutionEventType.STATUS,
                text=f"Run-Zustand: {state.value}",
                source="project-run-state",
                state=state.value,
                step_id="run_tests",
            )
        )

    try:
        cancel_run = "run-cancel-fixture"
        append_state(cancel_run, ExecutionRunState.RUNNING, iteration=1)
        window._execution_store.append(
            ExecutionEvent(
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                run_id=cancel_run,
                event_type=ExecutionEventType.STDOUT,
                text="collected 4 items",
                source="pytest",
                step_id="run_tests",
            )
        )
        window._project_execution_active = True
        window._refresh_workflow_pipeline()
        assert window.workflow_run_id_label.text() == f"Run-ID: {cancel_run}"
        assert "Läuft" in window.workflow_pipeline_summary.text()
        assert window.workflow_pipeline_stages["test"].text() == "Läuft"
        assert "STDOUT" in window.workflow_terminal_output.toPlainText()
        assert "collected 4 items" in window.workflow_terminal_output.toPlainText()

        window._execution_store.transition_run(
            cancel_run,
            ExecutionRunState.CANCELLED,
            iteration=1,
            stop_reason="user_cancelled",
        )
        window._execution_store.append(
            ExecutionEvent(
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                run_id=cancel_run,
                event_type=ExecutionEventType.STATUS,
                text="Run-Zustand: cancelled",
                source="project-run-state",
                state="cancelled",
            )
        )
        window._project_execution_active = False
        window._refresh_workflow_pipeline()
        assert "Abgebrochen" in window.workflow_pipeline_summary.text()
        assert "user_cancelled" in window.workflow_pipeline_summary.text()
        assert window.workflow_pipeline_stages["test"].text() == "Abgebrochen"

        patch_run = "run-patch-fixture"
        append_state(patch_run, ExecutionRunState.CREATED)
        append_state(patch_run, ExecutionRunState.RUNNING, iteration=1)
        append_state(patch_run, ExecutionRunState.DIAGNOSING, iteration=1)
        append_state(patch_run, ExecutionRunState.PROPOSING_PATCH, iteration=1)
        append_state(patch_run, ExecutionRunState.AWAITING_PATCH_REVIEW, iteration=1)
        window._file_edit_proposal = SimpleNamespace()
        window._repair_loop_active = True
        window._repair_conversation_id = conversation_id
        window._workflow_plan_run_id = patch_run
        window._refresh_workflow_pipeline()

        assert window.workflow_run_id_label.text() == f"Run-ID: {patch_run}"
        assert window.workflow_pipeline_stages["code"].text() == "Diff wartet auf Review"
        assert window.workflow_pipeline_stages["test"].text() == "Fehler erkannt · Diff-Review nötig"
        assert window.workflow_continue_button.isEnabled()

        window._execution_store.append(
            ExecutionEvent(
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                run_id=patch_run,
                event_type=ExecutionEventType.STDERR,
                text="FAILED test_fixture.py::test_case",
                source="pytest",
                exit_code=1,
                step_id="run_tests",
            )
        )
        append_state(patch_run, ExecutionRunState.PATCH_APPLIED, iteration=1)
        window._refresh_workflow_pipeline()
        assert window.workflow_pipeline_stages["test"].text() == "Apply bestätigt · Retest folgt"

        append_state(patch_run, ExecutionRunState.RETESTING, iteration=1)
        window._refresh_workflow_pipeline()
        assert window.workflow_pipeline_stages["test"].text() == "pytest-Retest läuft"
        assert "STDERR" in window.workflow_terminal_output.toPlainText()
        assert "Exit 1" in window.workflow_terminal_output.toPlainText()
        assert cancel_run not in window.workflow_terminal_output.toPlainText()

        window._execution_store.append(
            ExecutionEvent(
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                run_id=patch_run,
                event_type=ExecutionEventType.STDOUT,
                text="1 passed in 0.05s",
                source="pytest",
                step_id="run_tests",
            )
        )
        window._execution_store.append(
            ExecutionEvent(
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                run_id=patch_run,
                event_type=ExecutionEventType.EXIT_CODE,
                text="pytest completed",
                source="pytest",
                exit_code=0,
                step_id="run_tests",
            )
        )
        append_state(patch_run, ExecutionRunState.SUCCEEDED, iteration=1)
        window._refresh_workflow_pipeline()

        assert "Erfolgreich abgeschlossen" in window.workflow_pipeline_summary.text()
        assert window.workflow_pipeline_stages["code"].text() == "Patch übernommen · Run beendet"
        assert window.workflow_pipeline_stages["test"].text() == "Bestanden"
        assert "Exit 0" in window.workflow_terminal_output.toPlainText()
        assert "1 passed in 0.05s" in window.workflow_terminal_output.toPlainText()
        assert not window.workflow_continue_button.isEnabled()
        window._refresh_dashboard()
        assert window.dashboard_run_state.text() == "Erfolgreich abgeschlossen"
        assert "run run-patc" in window.dashboard_activity_list.toPlainText().lower()
    finally:
        window._project_execution_active = False
        window._repair_loop_active = False
        window._repair_conversation_id = None
        window._allow_close = True
        window.close()
        app.processEvents()
