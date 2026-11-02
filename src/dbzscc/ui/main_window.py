"""Compact native chat-first desktop workspace for DBZSCC 2611."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.parse import urlsplit

from PySide6.QtCore import QSettings, Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from dbzscc.app.chat_client import ChatMessage, OpenAICompatibleClient
from dbzscc.app.config import AppSettings, application_data_directory
from dbzscc.app.conversation_store import ConversationStore
from dbzscc.app.file_edit_service import (
    FILE_EDIT_SYSTEM_PROMPT,
    DiffProposal,
    FileSnapshot,
    apply_reviewed_proposal,
    build_model_request,
    capture_file,
    parse_model_proposal,
    validate_working_root,
)
from dbzscc.app.miro_service import MiroApplicationService
from dbzscc.app.project_service import ProjectSnapshot, inspect_project
from dbzscc.ui.workers import ChatStreamWorker, FunctionWorker


class MainWindow(QMainWindow):
    """Application shell; UI delegates IO and domain work to services."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DBZSCC 2611")
        self.resize(1440, 900)
        self._preferences = QSettings()
        self._settings = self._load_settings()
        self._data_dir = application_data_directory()
        self._store = ConversationStore(self._data_dir / "conversations.sqlite")
        self._active_conversation_id: str | None = None
        self._workers: set[FunctionWorker | ChatStreamWorker] = set()
        self._chat_operation_sequence = 0
        self._chat_active_operation_id: int | None = None
        self._chat_conversation_id: str | None = None
        self._chat_cancel_event: Event | None = None
        self._chat_partial_response = ""
        self._chat_worker: ChatStreamWorker | None = None
        self._runtime_status = "Noch nicht geprüft"
        self._project_snapshot: ProjectSnapshot | None = None
        self._last_discovery = None
        self._verified_local_model_ids: frozenset[str] = frozenset()
        self._file_edit_root: Path | None = None
        self._file_edit_snapshot: FileSnapshot | None = None
        self._file_edit_proposal: DiffProposal | None = None
        self._file_edit_request_running = False
        self._file_edit_request_signature: tuple[str, str, str, str] | None = None
        self._file_model = QFileSystemModel(self)
        self._build_shell()
        self._load_conversations()
        if self._settings.project_root:
            self._set_project_root(self._settings.project_root, persist=False)
        self._apply_theme(self._preferences.value("appearance/theme", "dark"))
        self._refresh_dashboard()
        self._update_file_edit_controls()

    def _build_shell(self) -> None:
        root = QWidget(self)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(205)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(16, 20, 16, 16)
        side_layout.setSpacing(8)
        brand = QLabel("DBZSCC\n2611")
        brand.setObjectName("brand")
        side_layout.addWidget(brand)
        caption = QLabel("LOCAL ENGINEERING WORKSPACE")
        caption.setObjectName("muted")
        side_layout.addWidget(caption)
        side_layout.addSpacing(18)

        self.pages = QStackedWidget()
        self._page_by_name: dict[str, QWidget] = {}
        self._navigation_buttons: list[QPushButton] = []
        page_builders: tuple[tuple[str, Callable[[], QWidget]], ...] = (
            ("Übersicht", self._build_dashboard),
            ("Chat", self._build_chat),
            ("Projekte", self._build_projects),
            ("Dateiänderungen", self._build_file_edits),
            ("Modelle", self._build_models),
            ("Runtime", self._build_runtime),
            ("Einstellungen", self._build_settings_page),
        )
        for name, builder in page_builders:
            page = builder()
            self._page_by_name[name] = page
            self.pages.addWidget(page)
            button = QPushButton(name)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, key=name: self._navigate(key))
            side_layout.addWidget(button)
            self._navigation_buttons.append(button)

        side_layout.addStretch(1)
        planned = QLabel("Agenten · Jobs\nWerkzeuge · Memory\nNoch nicht implementiert")
        planned.setObjectName("muted")
        planned.setWordWrap(True)
        side_layout.addWidget(planned)
        footer = QLabel("Lokale Einzelnutzer-App")
        footer.setObjectName("muted")
        side_layout.addWidget(footer)

        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Bereit · keine Runtime-Anfrage oder Modellscans beim Start")
        self._navigate("Übersicht")

    def _build_dashboard(self) -> QWidget:
        page, body = self._new_page(
            "Arbeitsübersicht",
            "Lokaler Entwicklungsarbeitsplatz. Zustände werden nur nach tatsächlichen Prüfungen angezeigt.",
        )
        self.dashboard_project = self._add_status_row(body, "Projekt", "Kein Projektordner ausgewählt")
        self.dashboard_runtime = self._add_status_row(body, "Runtime", "Noch nicht geprüft")
        self.dashboard_models = self._add_status_row(body, "MIRO-Modellbestand", "Noch kein Scan ausgeführt")
        actions = QHBoxLayout()
        self._action_button(actions, "Chat öffnen", lambda: self._navigate("Chat"))
        self._action_button(actions, "Runtime prüfen", lambda: self._navigate("Runtime"))
        self._action_button(actions, "Modellpfade verwalten", lambda: self._navigate("Einstellungen"))
        body.addLayout(actions)
        note = QLabel(
            "Sicherheitsverhalten: keine automatische Vollplattensuche, keine Runtime-Starts, "
            "keine Downloads und keine Modellinferenz ohne bestätigte Runtime-Verbindung."
        )
        note.setObjectName("callout")
        note.setWordWrap(True)
        body.addWidget(note)
        body.addStretch(1)
        return page

    def _build_chat(self) -> QWidget:
        page, body = self._new_page(
            "Chat-Workspace",
            "Chat ist der Hauptarbeitsplatz. Runtime und Modell werden erst nach Ihrer Konfiguration angesprochen.",
        )
        columns = QSplitter(Qt.Orientation.Horizontal)

        project_panel = QWidget()
        project_layout = QVBoxLayout(project_panel)
        project_layout.setContentsMargins(0, 0, 8, 0)
        project_layout.addWidget(QLabel("PROJEKTDATEIEN"))
        self.chat_project_label = QLabel("Kein Ordner ausgewählt")
        self.chat_project_label.setObjectName("muted")
        self.chat_project_label.setWordWrap(True)
        project_layout.addWidget(self.chat_project_label)
        choose_project = QPushButton("Projektordner wählen …")
        choose_project.clicked.connect(self._choose_project_root)
        project_layout.addWidget(choose_project)
        self.chat_tree = QTreeView()
        self.chat_tree.setModel(self._file_model)
        self.chat_tree.setHeaderHidden(True)
        for column in (1, 2, 3):
            self.chat_tree.hideColumn(column)
        project_layout.addWidget(self.chat_tree, 1)

        chat_panel = QWidget()
        chat_layout = QVBoxLayout(chat_panel)
        chat_layout.setContentsMargins(8, 0, 8, 0)
        thread_row = QHBoxLayout()
        thread_row.addWidget(QLabel("Unterhaltung"))
        self.conversation_combo = QComboBox()
        self.conversation_combo.currentIndexChanged.connect(self._select_conversation)
        thread_row.addWidget(self.conversation_combo, 1)
        self.new_thread_button = QPushButton("Neu")
        self.new_thread_button.clicked.connect(self._new_conversation)
        thread_row.addWidget(self.new_thread_button)
        chat_layout.addLayout(thread_row)

        self.chat_output = QPlainTextEdit()
        self.chat_output.setObjectName("chatOutput")
        self.chat_output.setReadOnly(True)
        self.chat_output.setPlaceholderText("Wählen Sie ein Projekt oder beginnen Sie mit einer Frage.")
        chat_layout.addWidget(self.chat_output, 1)
        self.chat_status = QLabel("Runtime noch nicht geprüft")
        self.chat_status.setObjectName("muted")
        chat_layout.addWidget(self.chat_status)
        composer_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(180)
        self.model_combo.setToolTip("Modellkennung aus /v1/models oder manuell eingetragen")
        self.model_combo.setEditText(self._settings.model_name)
        self.model_combo.currentTextChanged.connect(self._edit_model_changed)
        composer_row.addWidget(self.model_combo)
        self.message_input = QPlainTextEdit()
        self.message_input.setMaximumHeight(100)
        self.message_input.setPlaceholderText("Nachricht an die konfigurierte Chat-Runtime …")
        composer_row.addWidget(self.message_input, 1)
        self.send_button = QPushButton("Senden")
        self.send_button.setObjectName("primaryButton")
        self.send_button.clicked.connect(self._send_message)
        composer_row.addWidget(self.send_button)
        chat_layout.addLayout(composer_row)

        context_panel = QWidget()
        context_layout = QVBoxLayout(context_panel)
        context_layout.setContentsMargins(8, 0, 0, 0)
        context_layout.addWidget(QLabel("KONTEXT"))
        context_layout.addWidget(QLabel("Modell"))
        self.context_model = QLabel(self._settings.model_name or "Nicht ausgewählt")
        self.context_model.setObjectName("contextValue")
        self.context_model.setWordWrap(True)
        context_layout.addWidget(self.context_model)
        context_layout.addWidget(QLabel("Runtime-Status"))
        self.context_runtime = QLabel(self._runtime_status)
        self.context_runtime.setObjectName("contextValue")
        self.context_runtime.setWordWrap(True)
        context_layout.addWidget(self.context_runtime)
        context_layout.addWidget(QLabel("Modellpfade"))
        model_paths = "\n".join(self._settings.model_roots) or "Noch keine eingetragen"
        self.context_roots = QLabel(model_paths)
        self.context_roots.setObjectName("contextValue")
        self.context_roots.setWordWrap(True)
        context_layout.addWidget(self.context_roots)
        context_layout.addStretch(1)
        disclosure = QLabel("Metadaten sind kein Laufzeit- oder Qualitätsnachweis.")
        disclosure.setObjectName("muted")
        disclosure.setWordWrap(True)
        context_layout.addWidget(disclosure)

        columns.addWidget(project_panel)
        columns.addWidget(chat_panel)
        columns.addWidget(context_panel)
        columns.setStretchFactor(0, 2)
        columns.setStretchFactor(1, 6)
        columns.setStretchFactor(2, 2)
        body.addWidget(columns, 1)
        return page

    def _build_projects(self) -> QWidget:
        page, body = self._new_page(
            "Projekte",
            "Wählen Sie einen Ordner explizit. Die Git-Ansicht ist lesend und wird nur auf Anforderung aktualisiert.",
        )
        controls = QHBoxLayout()
        self.project_root_label = QLabel("Kein Projektordner ausgewählt")
        self.project_root_label.setObjectName("contextValue")
        controls.addWidget(self.project_root_label, 1)
        self._action_button(controls, "Ordner wählen …", self._choose_project_root)
        self._action_button(controls, "Git-Status aktualisieren", self._refresh_project_status)
        body.addLayout(controls)
        self.project_tree = QTreeView()
        self.project_tree.setModel(self._file_model)
        self.project_tree.setHeaderHidden(True)
        for column in (1, 2, 3):
            self.project_tree.hideColumn(column)
        body.addWidget(self.project_tree, 1)
        self.project_git_status = QPlainTextEdit()
        self.project_git_status.setReadOnly(True)
        self.project_git_status.setMaximumHeight(145)
        self.project_git_status.setPlaceholderText("Git-Status wurde noch nicht abgefragt.")
        body.addWidget(self.project_git_status)
        return page

    def _build_file_edits(self) -> QWidget:
        page, body = self._new_page(
            "Geprüfte Dateiänderungen",
            "Nur eine ausdrücklich gewählte Arbeitswurzel und eine einzelne bestehende UTF-8-Datei. "
            "Ein Vorschlag wird nie beim Chat-Senden angewendet.",
        )
        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Arbeitswurzel"))
        self.file_edit_root_label = QLabel("Noch nicht ausdrücklich gewählt")
        self.file_edit_root_label.setObjectName("contextValue")
        self.file_edit_root_label.setWordWrap(True)
        root_row.addWidget(self.file_edit_root_label, 1)
        choose_root = QPushButton("Arbeitswurzel wählen …")
        choose_root.clicked.connect(self._choose_file_edit_root)
        root_row.addWidget(choose_root)
        body.addLayout(root_row)

        target_row = QHBoxLayout()
        choose_file = QPushButton("Datei ausdrücklich wählen …")
        choose_file.clicked.connect(self._choose_file_edit_target)
        target_row.addWidget(choose_file)
        self.file_edit_target_label = QLabel("Keine Datei ausgewählt")
        self.file_edit_target_label.setObjectName("muted")
        self.file_edit_target_label.setWordWrap(True)
        target_row.addWidget(self.file_edit_target_label, 1)
        body.addLayout(target_row)

        disclosure = QLabel(
            "Beim Anfordern wird der Dateiinhalt an die konfigurierte Runtime gesendet. "
            "Nur eine erfolgreich geprüfte Loopback-Runtime mit tatsächlich gemeldeter Modellkennung "
            "ist zugelassen. Modelltext wird ausschließlich als strukturiertes JSON gelesen; "
            "es gibt weder Shell-Aufruf noch Codeausführung."
        )
        disclosure.setObjectName("callout")
        disclosure.setWordWrap(True)
        body.addWidget(disclosure)

        body.addWidget(QLabel("Gewünschte Änderung"))
        self.file_edit_instruction = QPlainTextEdit()
        self.file_edit_instruction.setMaximumHeight(105)
        self.file_edit_instruction.setPlaceholderText(
            "Beschreiben Sie die gewünschte Änderung. Der Dateiinhalt wird erst beim Anfordern gesendet."
        )
        self.file_edit_instruction.textChanged.connect(self._file_edit_instruction_changed)
        body.addWidget(self.file_edit_instruction)

        controls = QHBoxLayout()
        self.file_edit_request_button = QPushButton("Diff-Vorschlag anfordern")
        self.file_edit_request_button.setObjectName("primaryButton")
        self.file_edit_request_button.clicked.connect(self._request_file_edit_proposal)
        controls.addWidget(self.file_edit_request_button)
        self.file_edit_status = QLabel("Nicht verfügbar bis eine lokale Runtime tatsächlich geprüft ist.")
        self.file_edit_status.setObjectName("muted")
        self.file_edit_status.setWordWrap(True)
        controls.addWidget(self.file_edit_status, 1)
        body.addLayout(controls)

        self.file_edit_diff_title = QLabel("Vorschau: kein Diff vorhanden")
        self.file_edit_diff_title.setObjectName("contextValue")
        body.addWidget(self.file_edit_diff_title)
        self.file_edit_diff = QPlainTextEdit()
        self.file_edit_diff.setReadOnly(True)
        self.file_edit_diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.file_edit_diff.setPlaceholderText("Ein prüfbarer Diff erscheint erst nach einem Modellvorschlag.")
        body.addWidget(self.file_edit_diff, 1)

        self.file_edit_apply_button = QPushButton("Diff separat freigeben und atomar anwenden …")
        self.file_edit_apply_button.clicked.connect(self._apply_file_edit_proposal)
        self.file_edit_apply_button.setEnabled(False)
        body.addWidget(self.file_edit_apply_button)
        return page

    def _build_models(self) -> QWidget:
        page, body = self._new_page(
            "MIRO · lokale Modelle",
            "Nur ausdrücklich konfigurierte Ordner werden geprüft. Kandidaten stammen aus der vorhandenen MIRO-Pipeline.",
        )
        self.models_roots_label = QLabel(self._format_roots())
        self.models_roots_label.setObjectName("muted")
        self.models_roots_label.setWordWrap(True)
        body.addWidget(self.models_roots_label)
        controls = QHBoxLayout()
        self.scan_button = QPushButton("Konfigurierte Pfade jetzt scannen")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self._scan_models)
        controls.addWidget(self.scan_button)
        self.scan_status = QLabel("Noch kein Scan ausgeführt")
        self.scan_status.setObjectName("muted")
        controls.addWidget(self.scan_status, 1)
        body.addLayout(controls)
        self.models_table = QTableWidget(0, 4)
        self.models_table.setHorizontalHeaderLabels(["Typ", "Kandidat", "Status", "Details"])
        self.models_table.horizontalHeader().setStretchLastSection(True)
        self.models_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.models_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        body.addWidget(self.models_table, 1)
        note = QLabel("Runtime-Kandidaten werden nicht gestartet. Metadaten und Dateinamen sind kein Fähigkeitsnachweis.")
        note.setObjectName("muted")
        body.addWidget(note)
        return page

    def _build_runtime(self) -> QWidget:
        page, body = self._new_page(
            "Runtime-Verbindung",
            "Read-only-Prüfung einer manuell konfigurierten OpenAI-kompatiblen /v1-Schnittstelle; kein Dienststart.",
        )
        self.runtime_endpoint_label = QLabel(self._settings.model_endpoint)
        self.runtime_endpoint_label.setObjectName("contextValue")
        body.addWidget(QLabel("Konfigurierter Endpunkt"))
        body.addWidget(self.runtime_endpoint_label)
        controls = QHBoxLayout()
        self.runtime_check_button = QPushButton("Runtime jetzt prüfen")
        self.runtime_check_button.setObjectName("primaryButton")
        self.runtime_check_button.clicked.connect(self._check_runtime)
        controls.addWidget(self.runtime_check_button)
        self.runtime_status_label = QLabel(self._runtime_status)
        self.runtime_status_label.setObjectName("muted")
        controls.addWidget(self.runtime_status_label, 1)
        body.addLayout(controls)
        body.addWidget(QLabel("Tatsächlich zurückgegebene Modellkennungen"))
        self.runtime_models = QListWidget()
        body.addWidget(self.runtime_models, 1)
        body.addStretch(1)
        return page

    def _build_settings_page(self) -> QWidget:
        page, body = self._new_page(
            "Einstellungen",
            "Bevorzugter Standard ist eine lokale llama.cpp-kompatible Runtime. Keine Zugangsdaten werden hier gespeichert.",
        )
        body.addWidget(QLabel("OpenAI-kompatibler /v1-Endpunkt"))
        self.endpoint_input = QLineEdit(self._settings.model_endpoint)
        self.endpoint_input.setPlaceholderText("http://127.0.0.1:8080/v1")
        body.addWidget(self.endpoint_input)
        body.addWidget(QLabel("Modellkennung (optional bis zum Runtime-Test)"))
        self.settings_model_input = QLineEdit(self._settings.model_name)
        body.addWidget(self.settings_model_input)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Darstellung"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        current_theme = self._preferences.value("appearance/theme", "dark")
        theme_index = self.theme_combo.findData(current_theme)
        self.theme_combo.setCurrentIndex(max(theme_index, 0))
        self.theme_combo.currentIndexChanged.connect(
            lambda: self._apply_theme(self.theme_combo.currentData())
        )
        theme_row.addWidget(self.theme_combo)
        theme_row.addStretch(1)
        body.addLayout(theme_row)

        body.addWidget(QLabel("Ausdrücklich gewählte Modellordner"))
        self.model_roots_list = QListWidget()
        for root in self._settings.model_roots:
            self.model_roots_list.addItem(root)
        body.addWidget(self.model_roots_list)
        model_root_buttons = QHBoxLayout()
        self._action_button(model_root_buttons, "Modellordner hinzufügen …", lambda: self._add_root(self.model_roots_list))
        self._action_button(model_root_buttons, "Ausgewählten entfernen", lambda: self._remove_root(self.model_roots_list))
        body.addLayout(model_root_buttons)

        body.addWidget(QLabel("Ausdrücklich gewählte Runtime-Ordner (nur Kandidatensuche)"))
        self.runtime_roots_list = QListWidget()
        for root in self._settings.runtime_roots:
            self.runtime_roots_list.addItem(root)
        body.addWidget(self.runtime_roots_list)
        runtime_root_buttons = QHBoxLayout()
        self._action_button(runtime_root_buttons, "Runtime-Ordner hinzufügen …", lambda: self._add_root(self.runtime_roots_list))
        self._action_button(runtime_root_buttons, "Ausgewählten entfernen", lambda: self._remove_root(self.runtime_roots_list))
        body.addLayout(runtime_root_buttons)

        env_note = QLabel(
            "Optionaler API-Schlüssel: Umgebungsvariable DBZSCC_LLM_API_KEY. "
            "Sie wird nur über HTTPS oder an localhost gesendet; Zugangsdaten gehören nicht in URLs."
        )
        env_note.setObjectName("muted")
        env_note.setWordWrap(True)
        body.addWidget(env_note)
        save = QPushButton("Einstellungen speichern")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save_settings)
        body.addWidget(save)
        body.addStretch(1)
        return page

    def _new_page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        body = QVBoxLayout(page)
        body.setContentsMargins(28, 24, 28, 22)
        body.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        body.addWidget(heading)
        description = QLabel(subtitle)
        description.setObjectName("muted")
        description.setWordWrap(True)
        body.addWidget(description)
        return page, body

    @staticmethod
    def _add_status_row(layout: QVBoxLayout, title: str, value: str) -> QLabel:
        row = QFrame()
        row.setObjectName("statusRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 12, 14, 12)
        label = QLabel(title)
        label.setObjectName("contextValue")
        value_label = QLabel(value)
        value_label.setObjectName("muted")
        value_label.setWordWrap(True)
        row_layout.addWidget(label, 1)
        row_layout.addWidget(value_label, 3)
        layout.addWidget(row)
        return value_label

    @staticmethod
    def _action_button(layout: QHBoxLayout, text: str, callback: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _navigate(self, name: str) -> None:
        page = self._page_by_name.get(name)
        if page is None:
            return
        self.pages.setCurrentWidget(page)
        for button in self._navigation_buttons:
            button.setChecked(button.text() == name)
        if name == "Übersicht":
            self._refresh_dashboard()
        self.statusBar().showMessage(f"Bereich: {name}")

    def _load_settings(self) -> AppSettings:
        model_roots = _string_list(self._preferences.value("miro/model_roots", []))
        runtime_roots = _string_list(self._preferences.value("miro/runtime_roots", []))
        return AppSettings(
            model_endpoint=str(
                self._preferences.value("runtime/endpoint", AppSettings.model_endpoint)
            ),
            model_name=str(self._preferences.value("runtime/model", "")),
            model_roots=tuple(model_roots),
            runtime_roots=tuple(runtime_roots),
            project_root=str(self._preferences.value("project/root", "")),
        )

    def _save_settings(self) -> None:
        candidate = AppSettings(
            model_endpoint=self.endpoint_input.text().strip(),
            model_name=self.settings_model_input.text().strip(),
            model_roots=tuple(self._list_values(self.model_roots_list)),
            runtime_roots=tuple(self._list_values(self.runtime_roots_list)),
            project_root=self._settings.project_root,
        )
        try:
            endpoint = candidate.validated_endpoint()
        except ValueError as exc:
            QMessageBox.warning(self, "Ungültige Runtime-Adresse", str(exc))
            return
        self._settings = AppSettings(
            model_endpoint=endpoint,
            model_name=candidate.model_name,
            model_roots=candidate.model_roots,
            runtime_roots=candidate.runtime_roots,
            project_root=candidate.project_root,
        )
        self._preferences.setValue("runtime/endpoint", endpoint)
        self._preferences.setValue("runtime/model", candidate.model_name)
        self._preferences.setValue("miro/model_roots", list(candidate.model_roots))
        self._preferences.setValue("miro/runtime_roots", list(candidate.runtime_roots))
        self._preferences.setValue("appearance/theme", self.theme_combo.currentData())
        self._preferences.sync()
        self.runtime_endpoint_label.setText(endpoint)
        self.models_roots_label.setText(self._format_roots())
        self.context_model.setText(candidate.model_name or "Nicht ausgewählt")
        self.context_roots.setText("\n".join(candidate.model_roots) or "Noch keine eingetragen")
        self.model_combo.setEditText(candidate.model_name)
        self._runtime_status = "Noch nicht geprüft"
        self._verified_local_model_ids = frozenset()
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self._set_runtime_status(self._runtime_status)
        self.statusBar().showMessage("Einstellungen gespeichert", 4000)
        self._update_file_edit_controls()

    def _load_conversations(self) -> None:
        conversations = self._store.list_conversations()
        if not conversations:
            self._store.create_conversation()
            conversations = self._store.list_conversations()
        self.conversation_combo.blockSignals(True)
        self.conversation_combo.clear()
        for conversation in conversations:
            self.conversation_combo.addItem(conversation.title, conversation.conversation_id)
        if self._active_conversation_id:
            index = self.conversation_combo.findData(self._active_conversation_id)
            if index >= 0:
                self.conversation_combo.setCurrentIndex(index)
        elif self.conversation_combo.count():
            self.conversation_combo.setCurrentIndex(0)
            self._active_conversation_id = self.conversation_combo.currentData()
        self.conversation_combo.blockSignals(False)
        self._render_conversation()

    def _select_conversation(self, index: int) -> None:
        if index < 0 or self._chat_active_operation_id is not None:
            return
        self._active_conversation_id = self.conversation_combo.itemData(index)
        self._render_conversation()

    def _new_conversation(self) -> None:
        if self._chat_active_operation_id is not None:
            return
        conversation = self._store.create_conversation()
        self._active_conversation_id = conversation.conversation_id
        self._load_conversations()

    def _render_conversation(self) -> None:
        if not self._active_conversation_id:
            self.chat_output.clear()
            return
        messages = self._store.list_messages(self._active_conversation_id)
        chunks = []
        for message in messages:
            speaker = "DU" if message.role == "user" else "DBZSCC"
            chunks.append(f"{speaker}\n{message.content.strip()}\n")
        if (
            self._chat_active_operation_id is not None
            and self._chat_conversation_id == self._active_conversation_id
            and self._chat_partial_response
        ):
            chunks.append(f"DBZSCC · Antwort läuft\n{self._chat_partial_response}\n")
        self.chat_output.setPlainText("\n────────────────────────────────────────\n\n".join(chunks))
        scrollbar = self.chat_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _send_message(self) -> None:
        if self._chat_active_operation_id is not None:
            self._cancel_chat_stream()
            return
        if not self._active_conversation_id:
            self._new_conversation()
        content = self.message_input.toPlainText().strip()
        model = self.model_combo.currentText().strip()
        if not content:
            return
        if not model:
            self.chat_status.setText("Modellkennung fehlt · zuerst einstellen oder Runtime prüfen")
            self._navigate("Einstellungen")
            return
        try:
            client = OpenAICompatibleClient(self._settings.validated_endpoint())
        except ValueError as exc:
            self.chat_status.setText(str(exc))
            self._navigate("Einstellungen")
            return
        conversation_id = self._active_conversation_id
        self._store.add_message(conversation_id, "user", content)
        self.message_input.clear()
        self._load_conversations()
        self._render_conversation()
        history = tuple(
            ChatMessage(message.role, message.content)
            for message in self._store.list_messages(conversation_id)
        )
        self._chat_operation_sequence += 1
        operation_id = self._chat_operation_sequence
        cancel_event = Event()
        self._chat_active_operation_id = operation_id
        self._chat_conversation_id = conversation_id
        self._chat_cancel_event = cancel_event
        self._chat_partial_response = ""
        self.conversation_combo.setEnabled(False)
        self.new_thread_button.setEnabled(False)
        self.model_combo.setEnabled(False)
        self.send_button.setText("Stoppen")
        self.send_button.setToolTip("Stream abbrechen; nur vollständige Antworten werden gespeichert")
        self.chat_status.setText("Anfrage läuft · erste Antwort wird erwartet …")
        self._render_conversation()
        worker = ChatStreamWorker(
            operation_id,
            lambda on_delta, event: client.stream_complete(
                model, history, on_delta, event
            ),
            cancel_event,
        )
        self._chat_worker = worker
        self._workers.add(worker)
        worker.signals.delta.connect(self._chat_delta)
        worker.signals.completed.connect(self._chat_completed)
        worker.signals.failed.connect(self._chat_failed)
        worker.signals.cancelled.connect(self._chat_cancelled)
        from PySide6.QtCore import QThreadPool

        QThreadPool.globalInstance().start(worker)

    def _cancel_chat_stream(self) -> None:
        if self._chat_active_operation_id is None or self._chat_cancel_event is None:
            return
        self._chat_cancel_event.set()
        self.send_button.setEnabled(False)
        self.send_button.setText("Abbruch …")
        self.chat_status.setText(
            "Abbruch angefordert · warte auf Runtime oder Zeitlimit; "
            "die Nutzernachricht bleibt gespeichert."
        )

    @Slot(int, str)
    def _chat_delta(self, operation_id: int, text: str) -> None:
        if operation_id != self._chat_active_operation_id or not text:
            return
        self._chat_partial_response += text
        self.chat_status.setText("Antwort wird gestreamt …")
        self._render_conversation()

    @Slot(int, object)
    def _chat_completed(self, operation_id: int, reply: object) -> None:
        if operation_id != self._chat_active_operation_id:
            return
        if self._chat_cancel_event is not None and self._chat_cancel_event.is_set():
            self._chat_cancelled(operation_id)
            return
        if not isinstance(reply, str) or not reply.strip():
            self._chat_failed(operation_id, "Die Runtime lieferte eine leere Antwort.")
            return
        if self._chat_partial_response.strip() != reply.strip():
            self._chat_failed(
                operation_id,
                "Stream-Deltas und Abschlussantwort stimmen nicht überein.",
            )
            return
        conversation_id = self._chat_conversation_id
        if conversation_id is None:
            self._chat_failed(operation_id, "Die Unterhaltung ist nicht mehr verfügbar.")
            return
        try:
            self._store.add_message(conversation_id, "assistant", reply)
        except Exception:  # noqa: BLE001 -- persistence errors must not leave a partial UI draft.
            self._finish_chat_stream()
            self.chat_status.setText(
                "Antwort vollständig empfangen, aber nicht sicher gespeichert; "
                "Verlauf neu laden, bevor Sie erneut senden."
            )
            self._load_conversations()
            return
        self._finish_chat_stream()
        self.chat_status.setText("Antwort vollständig empfangen und lokal gespeichert")
        self._load_conversations()
        self._select_conversation_by_id(conversation_id)

    @Slot(int, str)
    def _chat_failed(self, operation_id: int, error: str) -> None:
        if operation_id != self._chat_active_operation_id:
            return
        if self._chat_cancel_event is not None and self._chat_cancel_event.is_set():
            self._chat_cancelled(operation_id)
            return
        self._finish_chat_stream()
        self.chat_status.setText(f"Runtime-Fehler: {error}")
        self.statusBar().showMessage(
            "Stream fehlgeschlagen; keine Teilantwort gespeichert. "
            "Die Nutzernachricht bleibt im Verlauf.",
            7000,
        )

    @Slot(int)
    def _chat_cancelled(self, operation_id: int) -> None:
        if operation_id != self._chat_active_operation_id:
            return
        self._finish_chat_stream()
        self.chat_status.setText(
            "Abgebrochen · keine Teilantwort gespeichert; "
            "die Nutzernachricht bleibt im Verlauf."
        )

    def _finish_chat_stream(self) -> None:
        if self._chat_worker is not None:
            self._workers.discard(self._chat_worker)
        self._chat_worker = None
        self._chat_active_operation_id = None
        self._chat_conversation_id = None
        self._chat_cancel_event = None
        self._chat_partial_response = ""
        self.send_button.setEnabled(True)
        self.send_button.setText("Senden")
        self.send_button.setToolTip("")
        self.conversation_combo.setEnabled(True)
        self.new_thread_button.setEnabled(True)
        self.model_combo.setEnabled(True)
        self._render_conversation()

    def _select_conversation_by_id(self, conversation_id: str) -> None:
        index = self.conversation_combo.findData(conversation_id)
        if index >= 0:
            self.conversation_combo.setCurrentIndex(index)

    def _check_runtime(self) -> None:
        self._verified_local_model_ids = frozenset()
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self._update_file_edit_controls()
        try:
            client = OpenAICompatibleClient(self._settings.validated_endpoint(), timeout_seconds=8.0)
        except ValueError as exc:
            self._set_runtime_status(f"Konfiguration ungültig: {exc}")
            self._navigate("Einstellungen")
            return
        self.runtime_check_button.setEnabled(False)
        self._set_runtime_status("Prüfung läuft …")
        self._run_worker(
            client.list_models,
            self._runtime_checked,
            self._runtime_failed,
        )

    def _runtime_checked(self, result: object) -> None:
        models = tuple(
            item for item in result if isinstance(item, str) and item.strip()
        ) if isinstance(result, (tuple, list)) else ()
        self._verified_local_model_ids = (
            frozenset(models) if _is_loopback_endpoint(self._settings.model_endpoint) else frozenset()
        )
        self.runtime_check_button.setEnabled(True)
        if models:
            self._runtime_status = f"Erreichbar · {len(models)} Modellkennung(en) gemeldet"
            self.runtime_models.clear()
            self.runtime_models.addItems(models)
            if not self.model_combo.currentText().strip():
                self.model_combo.setEditText(models[0])
            self._settings = AppSettings(
                model_endpoint=self._settings.model_endpoint,
                model_name=self.model_combo.currentText().strip(),
                model_roots=self._settings.model_roots,
                runtime_roots=self._settings.runtime_roots,
                project_root=self._settings.project_root,
            )
            self.context_model.setText(self._settings.model_name)
        else:
            self._runtime_status = "Endpunkt antwortet, meldet aber keine Modellkennungen"
            self.runtime_models.clear()
        self._set_runtime_status(self._runtime_status)
        self.chat_status.setText(self._runtime_status)
        self._update_file_edit_controls()

    def _runtime_failed(self, error: str) -> None:
        self.runtime_check_button.setEnabled(True)
        self.runtime_models.clear()
        self._verified_local_model_ids = frozenset()
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self._runtime_status = f"Nicht erreichbar · {error}"
        self._set_runtime_status(self._runtime_status)
        self._update_file_edit_controls()

    def _set_runtime_status(self, value: str) -> None:
        if hasattr(self, "runtime_status_label"):
            self.runtime_status_label.setText(value)
        if hasattr(self, "context_runtime"):
            self.context_runtime.setText(value)
        if hasattr(self, "dashboard_runtime"):
            self.dashboard_runtime.setText(value)
        if hasattr(self, "chat_status") and self._runtime_status != "Noch nicht geprüft":
            self.chat_status.setText(value)

    def _scan_models(self) -> None:
        if not self._settings.model_roots and not self._settings.runtime_roots:
            self.scan_status.setText("Keine Pfade konfiguriert · Einstellungen öffnen")
            self._navigate("Einstellungen")
            return
        service = MiroApplicationService()
        self.scan_button.setEnabled(False)
        self.scan_status.setText("Scan der ausdrücklich konfigurierten Pfade läuft …")
        self.models_table.setRowCount(0)
        self._run_worker(
            lambda: service.discover(
                model_roots=self._settings.model_roots,
                runtime_roots=self._settings.runtime_roots,
            ),
            self._discovery_completed,
            self._discovery_failed,
        )

    def _discovery_completed(self, result: object) -> None:
        self.scan_button.setEnabled(True)
        self._last_discovery = result
        artifacts = getattr(result, "artifacts", ())
        self.models_table.setRowCount(len(artifacts))
        for row, item in enumerate(artifacts):
            record = item.registry_record
            status = record.status if record else "Registry-Eintrag fehlt"
            detail_parts = []
            if record and record.identity_status:
                detail_parts.append(f"Identität: {record.identity_status}")
            if item.capability_profile:
                detail_parts.extend(
                    f"{evidence.property_name}={evidence.value}"
                    for evidence in item.capability_profile.metadata_evidence
                )
                detail_parts.append(
                    f"Runtime-Test: {item.capability_profile.runtime_test_status}"
                )
            if item.issues:
                detail_parts.append("Probleme: " + ", ".join(item.issues))
            self._set_table_cell(row, 0, item.candidate.kind)
            self._set_table_cell(row, 1, f"{item.candidate.name}\n{item.candidate.path}")
            self._set_table_cell(row, 2, status)
            self._set_table_cell(row, 3, "; ".join(detail_parts) or item.candidate.evidence)
        sync = getattr(result, "registry_sync", None)
        count = getattr(sync, "artifact_count", len(artifacts))
        partial = getattr(result, "has_partial_results", False)
        issues = len(getattr(result, "scanner_issues", ()))
        state = "Teilresultate · prüfen" if partial else "Abgeschlossen"
        self.scan_status.setText(f"{state} · {count} Kandidat(en) · {issues} Scanproblem(e)")
        self.dashboard_models.setText(f"{count} Kandidat(en) im letzten Scan")
        self.statusBar().showMessage("MIRO-Discovery abgeschlossen", 5000)

    def _discovery_failed(self, error: str) -> None:
        self.scan_button.setEnabled(True)
        self.scan_status.setText(f"Discovery fehlgeschlagen: {error}")

    def _set_table_cell(self, row: int, column: int, text: str) -> None:
        cell = QTableWidgetItem(text)
        cell.setToolTip(text)
        self.models_table.setItem(row, column, cell)

    def _choose_project_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Projektordner wählen", self._settings.project_root)
        if selected:
            self._set_project_root(selected)

    def _choose_file_edit_root(self) -> None:
        start = str(self._file_edit_root) if self._file_edit_root else self._settings.project_root
        selected = QFileDialog.getExistingDirectory(self, "Arbeitswurzel ausdrücklich wählen", start)
        if not selected:
            return
        try:
            self._file_edit_root = validate_working_root(selected)
        except ValueError as exc:
            self.file_edit_status.setText(f"Arbeitswurzel abgelehnt: {exc}")
            return
        self._file_edit_snapshot = None
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self.file_edit_root_label.setText(str(self._file_edit_root))
        self.file_edit_target_label.setText("Keine Datei ausgewählt")
        self._update_file_edit_controls()

    def _choose_file_edit_target(self) -> None:
        if self._file_edit_root is None:
            self.file_edit_status.setText("Wählen Sie zuerst ausdrücklich eine Arbeitswurzel.")
            return
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Bestehende Datei ausdrücklich wählen",
            str(self._file_edit_root),
            "Alle Dateien (*)",
        )
        if not selected:
            return
        self._file_edit_snapshot = None
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        try:
            self._file_edit_snapshot = capture_file(self._file_edit_root, selected)
        except ValueError as exc:
            self.file_edit_target_label.setText("Keine Datei ausgewählt")
            self._update_file_edit_controls()
            self.file_edit_status.setText(f"Datei abgelehnt: {exc}")
            return
        snapshot = self._file_edit_snapshot
        self.file_edit_target_label.setText(
            f"{snapshot.relative_path} · SHA-256 {snapshot.sha256} · "
            f"{len(snapshot.original_text.encode(snapshot.encoding))} Bytes"
        )
        self._update_file_edit_controls()

    def _file_edit_instruction_changed(self) -> None:
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self._update_file_edit_controls()

    def _edit_model_changed(self, _model: str) -> None:
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self._update_file_edit_controls()

    def _clear_file_edit_proposal(self) -> None:
        self._file_edit_proposal = None
        if hasattr(self, "file_edit_diff"):
            self.file_edit_diff.clear()
            self.file_edit_diff_title.setText("Vorschau: kein Diff vorhanden")
            self.file_edit_apply_button.setEnabled(False)

    def _update_file_edit_controls(self) -> None:
        if not hasattr(self, "file_edit_request_button"):
            return
        model = self.model_combo.currentText().strip() if hasattr(self, "model_combo") else ""
        loopback = _is_loopback_endpoint(self._settings.model_endpoint)
        runtime_available = loopback and bool(model) and model in self._verified_local_model_ids
        if not loopback:
            message = "Nicht verfügbar: Dateiänderungen benötigen eine Loopback-Runtime."
        elif not self._verified_local_model_ids:
            message = "Nicht verfügbar: Runtime und Modellkennung wurden noch nicht tatsächlich geprüft."
        elif not runtime_available:
            message = "Nicht verfügbar: Wählen Sie eine von der Runtime tatsächlich gemeldete Modellkennung."
        elif self._file_edit_root is None:
            message = "Runtime geprüft; wählen Sie jetzt ausdrücklich eine Arbeitswurzel."
        elif self._file_edit_snapshot is None:
            message = "Arbeitswurzel gewählt; wählen Sie ausdrücklich eine bestehende UTF-8-Datei."
        elif self._file_edit_request_running:
            message = "Der strukturierte Modellvorschlag wird erstellt; es wurde nichts geschrieben."
        elif not self.file_edit_instruction.toPlainText().strip():
            message = "Beschreiben Sie die gewünschte Änderung. Chat-Senden verändert keine Datei."
        elif self._file_edit_proposal is not None:
            message = "Diff bereit zur Prüfung; es wurde noch nichts geschrieben."
        else:
            message = "Bereit: Vorschlag wird nur auf ausdrückliche Anfrage erstellt."
        self.file_edit_status.setText(message)
        can_request = (
            runtime_available
            and self._file_edit_root is not None
            and self._file_edit_snapshot is not None
            and bool(self.file_edit_instruction.toPlainText().strip())
            and not self._file_edit_request_running
        )
        self.file_edit_request_button.setEnabled(can_request)
        self.file_edit_apply_button.setEnabled(
            runtime_available and self._file_edit_proposal is not None
        )

    def _request_file_edit_proposal(self) -> None:
        self._update_file_edit_controls()
        if not self.file_edit_request_button.isEnabled():
            return
        snapshot = self._file_edit_snapshot
        if snapshot is None:
            return
        instruction = self.file_edit_instruction.toPlainText().strip()
        model = self.model_combo.currentText().strip()
        endpoint = self._settings.model_endpoint
        try:
            request = build_model_request(snapshot, instruction)
            client = OpenAICompatibleClient(self._settings.validated_endpoint())
        except ValueError as exc:
            self._file_edit_snapshot = None
            self._clear_file_edit_proposal()
            self._update_file_edit_controls()
            self.file_edit_status.setText(f"Vorschlag nicht angefordert: {exc}")
            return
        self._file_edit_request_signature = (snapshot.sha256, instruction, model, endpoint)
        self._file_edit_request_running = True
        self._clear_file_edit_proposal()
        self._update_file_edit_controls()
        self._run_worker(
            lambda: parse_model_proposal(
                snapshot,
                client.complete(
                    model,
                    (
                        ChatMessage("system", FILE_EDIT_SYSTEM_PROMPT),
                        ChatMessage("user", request),
                    ),
                ),
            ),
            self._file_edit_proposal_ready,
            self._file_edit_request_failed,
        )

    def _file_edit_proposal_ready(self, result: object) -> None:
        self._file_edit_request_running = False
        current_signature = None
        if self._file_edit_snapshot is not None:
            current_signature = (
                self._file_edit_snapshot.sha256,
                self.file_edit_instruction.toPlainText().strip(),
                self.model_combo.currentText().strip(),
                self._settings.model_endpoint,
            )
        if (
            not isinstance(result, DiffProposal)
            or self._file_edit_request_signature != current_signature
            or result.snapshot != self._file_edit_snapshot
        ):
            self._file_edit_request_signature = None
            self._clear_file_edit_proposal()
            self._update_file_edit_controls()
            self.file_edit_status.setText("Veralteter oder ungültiger Vorschlag wurde verworfen.")
            return
        self._file_edit_request_signature = None
        self._file_edit_proposal = result
        self.file_edit_diff_title.setText(
            f"Diff-Vorschau · {result.snapshot.relative_path} · Ausgangs-Hash {result.snapshot.sha256}"
        )
        self.file_edit_diff.setPlainText(result.unified_diff)
        self._update_file_edit_controls()

    def _file_edit_request_failed(self, error: str) -> None:
        self._file_edit_request_running = False
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self._update_file_edit_controls()
        self.file_edit_status.setText(f"Vorschlag fehlgeschlagen: {error}")

    def _apply_file_edit_proposal(self) -> None:
        self._update_file_edit_controls()
        proposal = self._file_edit_proposal
        if proposal is None or not self.file_edit_apply_button.isEnabled():
            return
        try:
            current = capture_file(proposal.snapshot.root, proposal.snapshot.target)
            if current.sha256 != proposal.snapshot.sha256:
                raise ValueError("Die Datei hat sich seit Erstellung der Vorschau geändert.")
        except ValueError as exc:
            self._clear_file_edit_proposal()
            self._update_file_edit_controls()
            self.file_edit_status.setText(f"Anwendung gestoppt: {exc} Bitte Datei neu auswählen.")
            return
        answer = QMessageBox.question(
            self,
            "Dateiänderung separat freigeben",
            f"Ziel: {proposal.snapshot.relative_path}\n"
            f"Ausgangs-SHA-256: {proposal.snapshot.sha256}\n\n"
            "Sie haben den Diff in der Vorschau geprüft. Soll ausschließlich dieser Inhalt "
            "die bestehende Datei atomar ersetzen? Code wird nicht ausgeführt.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            written = apply_reviewed_proposal(proposal)
        except ValueError as exc:
            self._clear_file_edit_proposal()
            self._update_file_edit_controls()
            self.file_edit_status.setText(f"Datei nicht geändert: {exc}")
            return
        self._file_edit_snapshot = None
        self._file_edit_request_signature = None
        self._clear_file_edit_proposal()
        self.file_edit_target_label.setText("Angewendet; Datei für den nächsten Vorschlag neu auswählen.")
        self._update_file_edit_controls()
        self.statusBar().showMessage(f"Datei nach separater Freigabe atomar ersetzt: {written}", 8000)

    def _set_project_root(self, root: str, *, persist: bool = True) -> None:
        path = Path(root).expanduser()
        if not path.exists() or not path.is_dir():
            self.statusBar().showMessage("Der gespeicherte Projektordner ist nicht verfügbar.", 6000)
            return
        resolved = str(path.resolve())
        self._settings = AppSettings(
            model_endpoint=self._settings.model_endpoint,
            model_name=self._settings.model_name,
            model_roots=self._settings.model_roots,
            runtime_roots=self._settings.runtime_roots,
            project_root=resolved,
        )
        index = self._file_model.setRootPath(resolved)
        self.chat_tree.setRootIndex(index)
        self.project_tree.setRootIndex(index)
        self.chat_project_label.setText(resolved)
        self.project_root_label.setText(resolved)
        self.dashboard_project.setText(resolved)
        if persist:
            self._preferences.setValue("project/root", resolved)
            self._preferences.sync()
        self._project_snapshot = None

    def _refresh_project_status(self) -> None:
        if not self._settings.project_root:
            self.project_git_status.setPlainText("Wähle zuerst einen Projektordner.")
            return
        self.project_git_status.setPlainText("Git-Status wird lesend abgefragt …")
        self._run_worker(
            lambda: inspect_project(self._settings.project_root),
            self._project_status_ready,
            lambda error: self.project_git_status.setPlainText(f"Abfrage fehlgeschlagen: {error}"),
        )

    def _project_status_ready(self, result: object) -> None:
        if not isinstance(result, ProjectSnapshot):
            self.project_git_status.setPlainText("Unerwartete Statusantwort.")
            return
        self._project_snapshot = result
        if not result.is_git_repository:
            text = f"{result.root}\n{result.detail or 'Kein Git-Repository'}"
        else:
            head = result.head or "(noch kein Commit)"
            text = (
                f"Root: {result.root}\nBranch: {result.branch}\nHEAD: {head}\n"
                f"Geänderte/ungetrackte Pfade: {result.dirty_file_count}"
            )
            if result.detail:
                text += f"\nHinweis: {result.detail}"
        self.project_git_status.setPlainText(text)

    def _add_root(self, widget: QListWidget) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Ordner auswählen")
        if not selected:
            return
        normalized = str(Path(selected).resolve())
        if normalized not in self._list_values(widget):
            widget.addItem(normalized)
        self._refresh_roots_preview()

    @staticmethod
    def _remove_root(widget: QListWidget) -> None:
        row = widget.currentRow()
        if row >= 0:
            widget.takeItem(row)

    def _refresh_roots_preview(self) -> None:
        model_roots = self._list_values(self.model_roots_list)
        runtime_roots = self._list_values(self.runtime_roots_list)
        self.models_roots_label.setText(
            "Modelle:\n" + ("\n".join(model_roots) or "(keine)")
            + "\nRuntime-Kandidaten:\n"
            + ("\n".join(runtime_roots) or "(keine)")
        )

    def _format_roots(self) -> str:
        models = "\n".join(self._settings.model_roots) or "(keine Modellordner)"
        runtimes = "\n".join(self._settings.runtime_roots) or "(keine Runtimeordner)"
        return f"Modelle:\n{models}\nRuntime-Kandidaten:\n{runtimes}"

    @staticmethod
    def _list_values(widget: QListWidget) -> list[str]:
        return [widget.item(index).text() for index in range(widget.count())]

    def _refresh_dashboard(self) -> None:
        if hasattr(self, "dashboard_project") and not self._settings.project_root:
            self.dashboard_project.setText("Kein Projektordner ausgewählt")
        if hasattr(self, "dashboard_runtime"):
            self.dashboard_runtime.setText(self._runtime_status)

    def _run_worker(
        self,
        function: Callable[[], object],
        on_success: Callable[[object], None],
        on_failure: Callable[[str], None],
    ) -> None:
        worker = FunctionWorker(function)
        self._workers.add(worker)
        worker.signals.completed.connect(
            lambda result, current=worker: self._worker_succeeded(current, result, on_success)
        )
        worker.signals.failed.connect(
            lambda error, current=worker: self._worker_failed(current, error, on_failure)
        )
        from PySide6.QtCore import QThreadPool

        QThreadPool.globalInstance().start(worker)

    def _worker_succeeded(
        self, worker: FunctionWorker, result: object, callback: Callable[[object], None]
    ) -> None:
        self._workers.discard(worker)
        callback(result)

    def _worker_failed(
        self, worker: FunctionWorker, error: str, callback: Callable[[str], None]
    ) -> None:
        self._workers.discard(worker)
        callback(error)

    def _apply_theme(self, theme: object) -> None:
        selected = "light" if theme == "light" else "dark"
        if hasattr(self, "theme_combo"):
            self._preferences.setValue("appearance/theme", selected)
        if selected == "light":
            background, surface, text, muted, border, accent = (
                "#f4f6f8", "#ffffff", "#17212b", "#66717d", "#d7dde3", "#1769aa"
            )
        else:
            background, surface, text, muted, border, accent = (
                "#181d23", "#222931", "#e8edf2", "#98a4b1", "#38434e", "#5aa4e6"
            )
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background: {background}; color: {text}; font-size: 10pt; }}
            #sidebar {{ background: {surface}; border-right: 1px solid {border}; }}
            #brand {{ font-size: 21pt; font-weight: 700; color: {accent}; }}
            #pageTitle {{ font-size: 20pt; font-weight: 650; }}
            #muted {{ color: {muted}; }}
            #contextValue {{ font-weight: 600; }}
            #statusRow, #callout {{ background: {surface}; border: 1px solid {border}; border-radius: 6px; }}
            #callout {{ padding: 12px; }}
            QPushButton {{ background: {surface}; border: 1px solid {border}; border-radius: 5px; padding: 7px 11px; }}
            QPushButton:hover {{ border-color: {accent}; }}
            QPushButton:checked, #primaryButton {{ background: {accent}; color: white; border: 1px solid {accent}; font-weight: 600; }}
            QPushButton:disabled {{ color: {muted}; }}
            QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QListWidget, QTreeView, QTableWidget {{
                background: {surface}; border: 1px solid {border}; border-radius: 4px; padding: 5px;
                selection-background-color: {accent};
            }}
            QHeaderView::section {{ background: {surface}; border: 1px solid {border}; padding: 5px; }}
            QStatusBar {{ border-top: 1px solid {border}; }}
            """
        )


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (tuple, list)):
        return [str(item) for item in value if str(item)]
    return []


def _is_loopback_endpoint(endpoint: str) -> bool:
    try:
        host = urlsplit(endpoint).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
