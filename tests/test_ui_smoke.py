from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import dbzscc.ui.main_window as main_window_module
from dbzscc.app.chat_client import ChatProviderError, ChatStreamCancelled
from dbzscc.app.config import AppSettings
from dbzscc.app.file_edit_service import capture_file
from dbzscc.ui.main_window import MainWindow


def test_main_window_starts_without_runtime_probe_or_model_scan(tmp_path, monkeypatch) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Test")
    app.setApplicationName("DBZSCC2611-Test")
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")

    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        assert window.pages.count() == 8
        assert window._runtime_status == "Noch nicht geprüft"
        assert window._last_discovery is None
        assert window._settings.model_roots == ()
        assert window._settings.runtime_roots == ()
        assert window.chat_output.toPlainText() == ""
        assert window._page_by_name["Chat"] is not None
        assert window._page_by_name["Dateiänderungen"] is not None
        assert window._page_by_name["Workflows"] is not None
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


def test_project_root_selects_isolated_workflow_interpreter(tmp_path, monkeypatch) -> None:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Test")
    app.setApplicationName("DBZSCC2611-Test")
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
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    observed = {}

    class FakePlanner:
        def __init__(self, endpoint: str) -> None:
            observed["endpoint"] = endpoint

        def plan(self, model: str, request: str):
            observed["model"] = model
            observed["request"] = request
            return main_window_module.WorkflowPlan(
                actions=(main_window_module.WorkflowAction.START_DEVELOPMENT_SERVER,),
                rationale="Ein Server könnte für eine spätere Vorschau nützlich sein.",
                model_name=model,
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


def _make_stream_window(tmp_path, monkeypatch, app_name: str) -> tuple[QApplication, MainWindow]:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("DBZSCC-Stream-Test")
    app.setApplicationName(app_name)
    monkeypatch.setattr(main_window_module, "application_data_directory", lambda: tmp_path / "appdata")
    window = MainWindow()
    window._external_endpoint_confirmed = window._settings.model_endpoint.rstrip("/")
    window.model_combo.setEditText("local-test-model")
    window.message_input.setPlainText("Bitte antworte gestreamt")
    window.show()
    app.processEvents()
    return app, window


def test_chat_stream_ui_persists_one_complete_answer_from_worker(tmp_path, monkeypatch) -> None:
    def fake_stream(self, model, messages, on_delta, cancel_event):
        assert model == "local-test-model"
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
        window.send_button.click()
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
        assert model == "local-test-model"
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
