from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import dbzscc.ui.main_window as main_window_module
from dbzscc.app.chat_client import ChatProviderError, ChatStreamCancelled
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
        assert window.pages.count() == 7
        assert window._runtime_status == "Noch nicht geprüft"
        assert window._last_discovery is None
        assert window._settings.model_roots == ()
        assert window._settings.runtime_roots == ()
        assert window.chat_output.toPlainText() == ""
        assert window._page_by_name["Chat"] is not None
        assert window._page_by_name["Dateiänderungen"] is not None
        assert not window.file_edit_request_button.isEnabled()
        assert not window.file_edit_apply_button.isEnabled()
        assert "Nicht verfügbar" in window.file_edit_status.text()
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
