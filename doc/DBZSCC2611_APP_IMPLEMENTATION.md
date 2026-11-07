# DBZSCC 2611 — Anwendungsslice und Evidenz

> Die Anwendung ist ein getesteter MVP-/Engineering-Slice, **nicht** das produktionsreife Gesamtprodukt aus der Vision. Der Systemstatus, die verbindlichen Abnahmegates und die priorisierte Roadmap stehen im [Produktreifebericht](DBZSCC2611_PRODUCTION_READINESS.md).

## Was jetzt nutzbar ist

Die App startet als native PySide6-Desktopoberfläche mit acht Bereichen: Control Desk, Chat, Projekt, Workflows, Diff & Änderungen, MIRO · Modell-Vault, Runtime und Einstellungen. Der Chat hat den größten Arbeitsbereich, eine Projektdateiansicht links und einen Kontextbereich rechts. Dark und Light sind auswählbar. Gesprächsverläufe werden lokal in einer SQLite-Datei im Benutzerprofil gespeichert. Der aktuelle Startbildschirm und seine Beleggrenzen sind in den [Control-Desk-UI-Notizen](DBZSCC2611_CONTROL_DESK_UI.md) beschrieben.

Der Chat kann über einen austauschbaren OpenAI-kompatiblen Adapter mit `/v1/chat/completions` kommunizieren; Modellkennungen lassen sich über `/v1/models` prüfen. Standard ist `http://127.0.0.1:8080/v1`. Netzwerkzugriffe erfolgen nur nach Senden oder manuellem Runtime-Test und laufen außerhalb des GUI-Threads. Ein optionaler Schlüssel kommt ausschließlich aus `DBZSCC_LLM_API_KEY`, wird nicht gespeichert und wird nur über HTTPS oder an Loopback gesendet.

## Chat-Streaming

Normale Chat-Anfragen setzen `stream: true` und lesen OpenAI-kompatible SSE-`data`-Ereignisse bis zum Abschlussmarker `[DONE]`. Der Qt-Stream-Worker gibt Text-Deltas über korrelierte Qt-Signale an Slots des Hauptfensters weiter; Widgets werden dadurch in der GUI-Schleife aktualisiert. Die Nutzernachricht wird vor der Anfrage genau einmal lokal gespeichert. Die live angezeigte Assistant-Antwort bleibt ein flüchtiger UI-Entwurf und wird erst nach `[DONE]`, einer nichtleeren Abschlussantwort und Übereinstimmung mit den empfangenen Deltas genau einmal persistiert. Stop, HTTP-/Timeout-/Formatfehler, vorzeitiges Stream-Ende und leere Antworten verwerfen den Entwurf; die Nutzernachricht bleibt erhalten. Während eines Streams sind Unterhaltung und Modellwahl gesperrt, und ein zweiter Klick auf **Stoppen** fordert den Abbruch an. Bei blockierendem Netzwerklesen kann der Stop erst beim nächsten Datenereignis oder nach dem konfigurierten Socket-Timeout verarbeitet werden.

Die SSE-Verarbeitung ist mit kontrollierten Fake-Chunk-Responses und zusätzlich live über den verwalteten lokalen CPU-Server geprüft. Der separate Datei-Diff-Workflow verwendet weiterhin `complete()` ohne Streaming und bleibt vom normalen Chat-Senden getrennt. Der aktuelle Live-Nachweis steht am Ende dieses Dokuments.

## Ausgewählte lokale Projektdateien als Chat-Kontext

Im rechten Kontextbereich kann der Nutzer konkrete Dateien über einen Dateidialog ausdrücklich auswählen. Die App sammelt weder automatisch Projektbaum, Workspace oder versteckte Dateien ein noch durchsucht sie Verzeichnisse. Die Auswahl gilt einmalig; vor jedem Senden prüft der Dienst sie erneut und ein Bestätigungsdialog zeigt Loopback-Endpunkt, Projektwurzel und die konkreten Dateinamen. Wird die Bestätigung abgebrochen, bleibt auch die Chatfrage ungesendet.

Die Limits betragen höchstens **8 Dateien**, **32 KiB pro Datei** und **128 KiB insgesamt**. Abgelehnt werden Pfade mit `..`, Dateien außerhalb der Projektwurzel, Symlinks/Junctions/Reparse-Points, versteckte Pfadbestandteile, `CodeSources`, Nicht-Regulärdateien, nicht lesbare Dateien, ungültiges UTF-8 sowie Binärkontrollzeichen. Das Lesen ist read-only und prüft Dateimetadaten erneut gegen Ersetzungen während des Lesens.

Dateiinhalte werden mit der bestätigten Anfrage ausschließlich an `localhost`, `127.0.0.1` oder `::1` gesendet. Für diesen Request deaktiviert der Client Umgebungs-Proxys und blockiert HTTP-Redirects. Ein konfigurierter Remote-/Cloud-Endpunkt wird abgelehnt, **bevor** die App den Dateiinhalt liest. Dateiinhalt kommt nur in den flüchtigen Request; im lokalen Chat-Verlauf wird allein die normale Nutzerfrage gespeichert, und die App schreibt Dateiinhalte nicht in eigene Logs. Ob eine separat betriebene lokale Runtime Request-Inhalte protokolliert, ist nicht geprüft und durch die App nicht kontrollierbar; der Bestätigungsdialog weist darauf hin.

Dateitext wird als nicht vertrauenswürdiges Referenzmaterial markiert. Chat-Kontext führt weder den Datei-Diff-Ablauf aus noch darf er Code oder Dateien ändern. Die Seite **Dateiänderungen** bleibt unabhängig und benötigt weiterhin ihre eigene Auswahl, einen Diff-Vorschlag und eine separate Anwendungsfreigabe.

## Offline-RAG für ein ausdrücklich gewähltes Projekt

Finaler Teststand dieser Integration: 32 gezielte RAG-/Kontext-/UI-Tests bestanden; der breite Lauf ohne die beiden Speech-Testmodule meldete 290 bestandene Tests. Ruff bestand auf den geänderten Integrationsdateien. Die ausführlichen Testkommandos und Grenzen stehen in der [Offline-RAG-Baseline](DBZSCC2611_OFFLINE_RAG_CPU_BASELINE.md).

Im Chat-Kontextbereich kann der Nutzer **Ausgewähltes Projekt lokal indexieren …** anklicken. Erst nach Bestätigung wird genau die vorhandene Projektwurzel an den bestehenden `scan_workspace`-Allowlist-Scanner übergeben. Es gibt keinen Start-/Hintergrundscan; versteckte, sensible, Vendor-, Build- und Cachepfade bleiben nach den Regeln des RAG-Scanners ausgeschlossen. Die SQLite-Indexdatenbank liegt im lokalen App-Datenordner. Danach ist lokales Projektwissen weiterhin standardmäßig aus: ein eigener Haken gilt für genau die nächste Frage, und vor jeder Verwendung zeigt ein zweiter Dialog Projekt und Loopback-Endpunkt. Nur bis zu fünf Treffer mit relativen Quellenpfaden und Zeilen werden als untrusted Kontext übertragen.

Die Chat-Hauptumgebung verwendet CPython 3.15.2, während ONNX Runtime 1.31.1 für Windows hier kein cp315-Wheel anbietet. Daher importiert der Chat-Prozess keine ONNX-Runtime und ändert weder System-Python noch `PATH`: `LocalRagWorkerClient` ruft einen festen JSON-Worker unter `.work\venv314\Scripts\python.exe` auf. Der Aufruf ist `python.exe -I src/dbzscc/rag/worker.py`, `shell=False`, mit nur den festen Operationen `index` und `search`; es gibt keine übergebbaren Befehle, dynamischen Module oder Cloud-Fallbacks. Die Worker-Ergebnisse sind schema-validiert und auf E5/`CPUExecutionProvider` gebunden. Bei fehlendem Worker, Index oder Modell wird die lokale Anfrage abgebrochen, nicht an einen anderen Dienst umgeleitet.

Der Chat verwendet Treffer nur nach erneuter Bestätigung, über den bereits loopback-gebundenen `local_only=True`-Transport mit deaktivierten Proxys und blockierten Redirects. Die App persistiert weder Retrieval-Ausschnitte im Chatverlauf noch schreibt sie diese in eigene Logs. Sie zeigt die verwendeten relativen Quellen/Zeilen nur flüchtig im Kontextbereich an. Die separate lokale Runtime kann eigene Requests protokollieren; ihre Logging-Konfiguration bleibt außerhalb dieser App-Grenze. Der bewiesene Integrationsrand ist der Entwicklungscheckout: fehlt `.work\venv314`, Worker oder Manifest, schlägt RAG geschlossen fehl. Ein eigenständiges Installationspaket oder die Retrievalqualität gegen ein eingefrorenes Deutsch-/Englisch-/Code-Eval ist noch nicht nachgewiesen.

Belege: 32 gezielte RAG-/Kontext-/UI-Tests bestanden; Ruff bestand auf allen geänderten Integrationsdateien. Ein echter Roundtrip aus der Haupt-`.venv` indexierte zwei synthetische Dateien im temporären `.work`-Ordner, schloss eine versteckte `.env` aus und lieferte zwei CPU-Treffer (`docs/retrieval.md:1-3`). Es wurde kein Nutzerprojekt gescannt. Der reproduzierbare Smoke steht in `.work/rag-cpu-baseline/app_worker_integration_smoke.py`; Details und die genaue Testkommandozeile stehen in der [Offline-RAG-Baseline](DBZSCC2611_OFFLINE_RAG_CPU_BASELINE.md).

## Spracheingabe — lokale Push-to-talk-Eingabe

Der Chat-Composer hat jetzt eine **gedrückt halten → sprechen → loslassen**-Eingabe. Qt `QAudioSource` erfasst nur während der gedrückten Taste; beim Loslassen oder Abbrechen endet die Aufnahme. Die App fügt ein erkanntes Transkript als editierbaren Entwurf an den Composer an. Senden bleibt ein separater, manueller Klick. Abbruch, Hintergrundwechsel, Gerätefehler und lokale Erkennungsfehler erzeugen keine Vorschau und geben verständlichen Status aus.

Die Aufnahme wird in-memory auf höchstens 30 Sekunden beziehungsweise 12 MiB begrenzt und im CPU-Worker nach mono 16-kHz-PCM normalisiert. Die temporäre WAV liegt nur unter `models/stt/.tmp/` und wird nach Erfolg/Fehler/Abbruch gelöscht. Der Worker startet ausschließlich das verifizierte `whisper-cli.exe` mit `-ng` für CPU und `-l de`; Thread-Cancellation beendet den Prozess, und die GUI bleibt während der Erkennung reaktionsfähig. Es werden keine Audioinhalte in eigene Logs oder den Chatverlauf geschrieben. Die App öffnet beim Start kein Mikrofon und lädt beim Start kein Modell.

Nach ausdrücklichem Klick auf **Offline-STT laden / prüfen** lädt die App das offizielle whisper.cpp-CPU-x64-Archiv (`b5454`, SHA-256 `6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb`) sowie das multilingual/deutschfähige `ggml-base-q5_1.bin` (59.707.625 Bytes, SHA-256 `422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898`) projektlokal unter `models/stt/`. Beide Assets sind auf feste Upstream-Versionen/Revisionen gepinnt und werden vor der Installation vollständig gehasht. Teil-Downloads lassen sich fortsetzen; ZIP-Pfade, Reparse-Points und vorhandene nicht verifizierte Dateien werden abgewiesen statt überschrieben. Runtime und Gewichte sind MIT-lizenziert; die genauen Quellen, Größen, Prüfsummen und Abwägungen stehen in der [Offline-Spracheingabe-Anleitung](DBZSCC2611_OFFLINE_SPEECH_INPUT.md).

Die Offscreen-Tests simulieren Audiogerät und Recognizer und prüfen Hintergrundausführung, Preview ohne Auto-Senden, Fehler-/Abbruchpfade, Temp-Cleanup, PCM-Downmix/Resampling, sichere Extraktion und Download-Resume/Hash-Fehler. Zusätzlich lief eine kurze, lokal mit „Microsoft Hedda Desktop“ synthetisierte deutsche Testphrase durch das echte Modell und die echte Runtime: „Guten Morgen. Diese Erkennung läuft vollständig lokal.“ Die Test-WAV wurde entfernt. Auf dem geprüften Gerät listet Qt das Focusrite-Standardgerät, aber `QAudioSource.start()` liefert für 48-kHz-Stereo und 16-kHz-Mono `OpenError`; es wurden dabei keine Audio-Bytes erfasst. Die Hardwareaufnahme ist daher noch nicht end-to-end bestätigt; die App zeigt diesen Geräte-/Berechtigungsfehler ausdrücklich an. Das Setup benötigt beim erstmaligen Laden eine Internetverbindung ausschließlich für die offiziellen Dateien; die anschließende Erkennung ist lokal. `ruff check src tests` besteht; der vollständige Lauf meldete 275 bestandene Tests (176 bestehende `asyncio.iscoroutinefunction`-Deprecation-Warnungen aus LangChain). Die öffentliche Qt-API-Dokumentation bestätigt `QAudioSource`-Capture und Gerätestatus ([Qt QAudioSource](https://doc.qt.io/qt-6/qaudiosource.html), [Qt QMediaDevices](https://doc.qt.io/qt-6/qmediadevices.html)).

Der Projektbaum öffnet sich erst nach expliziter Ordnerauswahl. Branch, HEAD und Git-Status werden nur auf Anforderung lesend abgefragt. Die Git-Sicherheitsfreigabe gilt nur für den jeweiligen Git-Prozess; globale Git-Einstellungen werden nicht verändert.

## Sichere Einzeldateiänderungen

Die Seite **Dateiänderungen** übernimmt die gespeicherte Projektwurzel nicht automatisch: In jeder Sitzung müssen Arbeitswurzel und bestehende Zieldatei ausdrücklich gewählt werden. Unterstützt werden reguläre UTF-8-Textdateien (mit oder ohne BOM) bis 256 KiB. Binärdaten, ungültiges UTF-8, Pfade außerhalb der Root, `..`-Pfade sowie Symlinks und Windows-Reparse-Points/Junctions werden abgelehnt. Es werden keine neuen, gelöschten oder umbenannten Dateien erstellt.

Ein Klick auf **Diff-Vorschlag anfordern** sendet die ausgewählte Datei und Änderungsbeschreibung an die konfigurierte Runtime. Das ist nur aktiv, wenn ein manueller `/v1/models`-Test erfolgreich war, der Endpunkt Loopback ist und die gewählte Modellkennung tatsächlich zurückgemeldet wurde. Die Antwort muss ein einziges JSON-Objekt mit `relative_path`, `base_sha256` und vollständigem `updated_content` sein. Die App führt daraus lokal einen Unified Diff her; Modelltext, Code und Shell-Befehle werden nie ausgeführt.


Die Vorschlagsfunktion bleibt an eine erfolgreich verifizierte lokale Runtime und eine konkret gemeldete Modellkennung gebunden. Die verwaltete Runtime und normale Chat-Inferenz wurden live geprüft; der separate Datei-Diff-/atomare Apply-Pfad wurde in diesem End-to-End-Lauf nicht bestätigt. Ein normales Chat-Senden löst weiterhin keine Dateiänderung aus.

## MIRO und Sicherheitsgrenzen

Modell- und Runtime-Roots müssen ausdrücklich eingetragen und anschließend per Button gescannt werden. Beim App-Start läuft weder eine Runtime-Prüfung noch ein Modell- oder Dateisystemscan. Die App ruft die bestehende MIRO-Discovery-Pipeline auf; sie dupliziert weder Scanner noch GGUF-Parser. Kandidaten, Parsergebnisse und vorhandene Capability-Evidenz werden angezeigt. Ein MIRO-Scan öffnet oder startet keine Runtime-Kandidaten.

Metadaten und Dateinamen belegen keine Inferenz, Modellqualität, Hardwarepassung oder Multimodalität. Die Live-Prüfung unten bestätigt Textinferenz mit Qwen über den verwalteten CPU-Server; sie belegt weder Bild-/Audio-Verarbeitung noch einen verwalteten CUDA-/Vulkan-Start. Cloud-Endpunkte sind als manuelle HTTPS-Konfiguration möglich, aber nicht der Standard und keine Kernabhängigkeit. Dateiinhalte aus dem neuen Kontextpfad dürfen unabhängig davon ausschließlich den bestätigten Loopback-Endpunkt erreichen.

## Architektur und verwendete Projektunterlagen

`app/` enthält Konfiguration, Chat-Adapter, die Qt-freien Services `chat_context_service.py`, `local_rag_service.py`, `action_evidence.py` und `file_edit_service.py`, lokalen Konversationsspeicher, lesenden Git-Service und MIRO-Anwendungsgrenze. `rag/worker.py` ist der feste Offline-JSON-Worker für den begrenzten Python-3.14-Prozess; der Chat-UI-Thread führt ihn nicht aus. `ui/` enthält den generischen Qt-Worker und einen separaten Chat-Stream-Worker mit Delta-/Abschluss-/Fehler-/Abbruchsignalen. `miro/` bleibt alleiniger Besitzer der vorhandenen Discovery-, GGUF- und Registry-Funktionen. Laufzeitdaten liegen außerhalb des Checkouts.

Die Umsetzung folgt den lokalen [UI-Vision](DBZSCC2611_UI_VISION.md)-, [Architekturleitplanken](ARCHITECTURE_GUARDRAILS.md)-, [MIRO-Registry](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md)- und [MIRO-Discovery](MIRO_LOCAL_DISCOVERY.md)-Unterlagen. Alter 2410-App-Code und `media/CodeSources` wurden nicht als Codevorlage benutzt.

## Foundation: Action Evidence Contract (S0a)

`src/dbzscc/app/action_evidence.py` ergänzt einen frameworkfreien, nur im Speicher erzeugten Vertrag für später integrierte Runtime-, Modell-, Dateisystem-, Start- und Testbeobachtungen. Datensätze führen Schema-Version, UUID-Evidence-/Correlation-IDs, Aktion, fest zugeordnete Herkunft, UTC-Zeit, Ergebnis, expliziten Unsicherheitscode und optional vollständige Git-Objekt-ID plus Worktree-Zustand. Fehler werden ausschließlich als feste Codes dargestellt; `to_record()` gibt nur eine feste Allowlist aus. Es gibt keine Freitextfelder für Pfade, Datei-/Prompt-Inhalte, Zugangsdaten oder rohe Provider-Ausnahmen.

Die Freshness-Prüfung meldet nur bei gleicher voller Revision und zwei sauberen Worktrees `current`; geänderte Revision oder Clean/Dirty-Wechsel sind `stale`. Dirty→Dirty, fehlende Revision und unbekannter Zustand bleiben `unknown`, weil die App keine Dateiinhalte hasht oder erneut liest. Das Modul führt **keine** SQLite-/Dateipersistenz ein und ist derzeit **noch nicht** mit den Runtime-, MIRO- oder Projektstatus-Aufrufen verdrahtet. Der vorhandene UI-Bericht erhält dadurch noch keine neuen Laufzeitdaten; G0 bleibt offen.

## Ergänzte bzw. geänderte Dateien

- Start und Paketierung: `README.md`, `pyproject.toml`, `src/dbzscc/__main__.py`.
- App-Services: `src/dbzscc/app/__init__.py`, `config.py`, `chat_client.py`, `chat_context_service.py`, `action_evidence.py`, `conversation_store.py`, `project_service.py`, `file_edit_service.py`, `miro_service.py`, `main.py`.
- Native UI: `src/dbzscc/ui/__init__.py`, `main_window.py`, `workers.py`.
- Neue bzw. erweiterte Tests: `tests/test_action_evidence.py`, `tests/test_chat_context_service.py`, `tests/test_app_chat_client.py`, `tests/test_file_edit_service.py`, `tests/test_ui_smoke.py`; bestehende App- und MIRO-Tests bleiben enthalten.
- Dokumentation: `README.md`, dieser Evidenznachweis und der [Produktreifebericht](DBZSCC2611_PRODUCTION_READINESS.md).

Die vorhandenen MIRO-Core-Module und deren bestehende Tests wurden für diesen App-Slice nicht umgeschrieben.

## Tatsächliche Prüfungen

Ausgeführt auf RAZOR, Windows, im Checkout `D:\Dev\DBZSCC2611` mit dem projektspezifischen `.venv`. Der erfasste Stand war Branch `master`, Basis-HEAD `95cf22138`, mit bewusst uncommitteten Änderungen. Der Git-Status meldete außerdem ein bereits vorhandenes untracked `.dbzs/`, das unangetastet blieb; beim Untracked-Scan gab Git eine Lesewarnung für `.tmp/pytest-initial/` aus. Es wurden keine Commits, Pushes oder Merges erstellt.

- `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q` → **Exitcode 0; 86 passed in 2,23 s**. Enthalten sind die qt-freien Kontext-Servicefälle, Loopback-/Proxy-/Redirect-Tests, Offscreen-UI-Tests für Zustimmung, Abbruch, Remote-Blockade und Nichtpersistenz sowie bestehende Datei-Diff-, Chat-, App- und MIRO-Tests. Netzwerk-/Modelltests verwenden kontrollierte Fakes; keine echte Runtime oder Inferenz wurde angesprochen.
- `.venv\Scripts\ruff.exe check src/dbzscc tests` → **Exitcode 0; All checks passed!**
- Echter `dbzscc.app.main:main`-Einstieg mit `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, 750-ms-Eventloop und temporärem `LOCALAPPDATA`/isolierten INI-`QSettings` → **Exitcode 0**; die isolierte SQLite-Datei wurde erzeugt und enthielt genau eine Start-Unterhaltung. Das ist ein Start-Smoke, kein manueller GUI-, Runtime- oder Inferenztest.
- Die UI-Tests bestätigen sieben Navigationsseiten, Runtime-Status „Noch nicht geprüft“, deaktivierte Dateiänderungen ohne manuelle Runtime-Prüfung und keinen MIRO-Scan beim App-Start.
- Nicht ausgeführt: mypy, Paketbuild/-installation, UI-Manuallauf, echter lokaler Runtime-/Modelltest, Hardware-/Performancebenchmark, Sicherheitsprüfung gegen serverseitige Runtime-Logs und Plattformmatrix.

## Neueste Foundation-Prüfung — 3. November 2026

Der neue S0a-Contract ist mit 10 fokussierten Tests geprüft. Danach liefen die **gesamte** Testsuite und Ruff erneut auf RAZOR/Windows, im Branch `master`, Basis-HEAD `95cf221382eea5bbaec45acab4e16f4631ed98fb`. Die Beobachtungszeiten unten sind RAZOR-Ortszeit (UTC+01:00):

- Herkunft `pytest`, `2026-11-03T13:34:41+01:00`: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_action_evidence.py` → **Exitcode 0; 10 passed**.
- Herkunft `pytest`, `2026-11-03T13:35:07+01:00`: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q` → **Exitcode 0; 96 passed in 1,66 s**.
- Herkunft `ruff`, `2026-11-03T13:35:09+01:00`: `.venv\Scripts\ruff.exe check src/dbzscc tests` → **Exitcode 0; All checks passed!**
- Herkunft `app_start`, `2026-11-03T13:35:34+01:00`: echter Einstieg `dbzscc.app.main:main` über `.work/scripts/offscreen_start_smoke.py`, `QT_QPA_PLATFORM=offscreen`, temporäres `LOCALAPPDATA`, separate INI-`QSettings`, 750-ms-Eventloop → **Exitcode 0**; isolierte SQLite-Datei vorhanden, genau eine Start-Unterhaltung. Unsicherheit: kein manueller GUI-, Runtime- oder Inferenztest.
- Echte Runtime-/Modellprüfung und MIRO-/Projekt-Scans hatten in diesem Slice den Status **nicht ausgeführt**; es gibt daher keine Capability- oder Endpunktbehauptung. Der Offscreen-Smoke schrieb nur die isolierte temporäre Startdatenbank; keine Projektdateiinhalte oder Credentials wurden in den Bericht aufgenommen.

Die Arbeitskopie war bereits verändert; `.dbzs/` blieb unberührt. Git meldete weiter eine Lesewarnung für `.tmp/pytest-initial/`, das nicht verändert wurde. Die Lesestatusabfrage nutzte eine prozesslokale `safe.directory`-Angabe; globale Git-Einstellungen wurden nicht geändert. Es gab keine Commits, Pushes oder Merges.

Die frühere LangChain-Architekturprüfung ist inzwischen durch einen minimalen optionalen `LangChainWorkflowPlanner` ergänzt: Er verwendet eine lokale `RunnableSequence`, `PydanticOutputParser` und eine feste Aktions-Allowlist. Ein echter Qwen-Planrequest wurde im Python-3.14.4-venv mit `langchain-core 0.3.86` ausgeführt und validiert. Python 3.15.2 bleibt fail-closed deaktiviert; allgemeine Tool-Ausführung, RAG, LangGraph und Checkpointing sind nicht implementiert. Details und verbleibende Abnahmekriterien stehen im [Produktreifebericht](DBZSCC2611_PRODUCTION_READINESS.md).

Brave Search ist ebenfalls nur als künftiges Feature dokumentiert, nicht implementiert. In diesem Arbeitsschritt wurden weder `BRAVE_SEARCH_API_KEY` ausgelesen noch eine externe Brave-Anfrage gesendet. Die Roadmap legt Query-only-Übertragung, Nutzerfreigabe, sichere Header-/Key-Behandlung und sichtbare Quellen-/Rate-Limit-Befunde als Abnahmekriterien fest.

## Noch offen

Offen bleiben Agenten-/Job-Orchestrierung, allgemeine Projektprozesse außerhalb der festen pytest-/Ruff-Allowlist und der manifestgebundenen Server-/Debug-Aktionen, Mehrdatei-/Patchserien-Änderungen, eine vollständige Ausführungs-/TestEvidence-Kette, Hybrid Memory/RAG, vollständige Code Intelligence und Spracheingabe. Manifestgebundene PySide6-/Uvicorn-Server- und Debug-Aktionen sind nach separater Bestätigung ausführbar; die UI-SSE-Chatstrecke und der CPU-Qwen-Pfad sind live geprüft. Der Action-Evidence-Vertrag ist noch nicht in alle Runtime-/MIRO-/Projektaktionen verdrahtet; S0/G0 bleibt offen. Der [Produktreifebericht](DBZSCC2611_PRODUCTION_READINESS.md) enthält die G0–G8-Abnahmekriterien und die verbleibenden Slices.


## Runtime-Integration und neue Live-Prüfungen — 3. November 2026

Dieser Abschnitt aktualisiert ausschließlich den zeitabhängigen Runtime-Status aus den vorherigen Abschnitten: Lokale Builds und begrenzte Inferenz wurden inzwischen geprüft. Die ältere Binärintegration startete und wählte noch keine Runtime; danach wurde ein app-eigener `RuntimeController` mit expliziter Backend-/Modellauswahl, Ressourcen-Preflight, Loopback-Healthcheck sowie Start/Stopp integriert. Es gibt weiterhin keinen automatischen Start oder automatische Build-Auswahl. Die Builds stammen aus Nutzerordnern und wurden nicht kryptografisch gegen offizielle Upstream-Archive authentifiziert.

Die drei getrennten Windows-x64-Varianten (CPU, CUDA 13.1, Vulkan) liegen checkout-lokal unter `runtimes/llama.cpp`. Die kopierten Binaries melden `8842 (455d8e4be)`. Ein kleiner Qwen2.5-0.5B-Instruct-Test antwortete mit CPU, CUDA auf der GTX 1650 SUPER sowie einem eng begrenzten Vulkan-Aufbau mit zwei sichtbaren Geräten und zwei GPU-Layern/CPU-Rest jeweils `OK`; die Vulkan-Laufzeit zeigte nichtnull Modellpuffer auf AMD `Vulkan0` und NVIDIA `Vulkan1`. Das ist kein Nachweis für einen gemeinsamen VRAM-Pool oder allgemeine Cross-Backend-Verteilung. Live-Ergebnisse, Gerätelimits, lokale SHA-256-Manifestdatei, Lizenzgrenzen und die fehlende Upstream-Authentifizierung stehen im [Runtime-Integrationsbericht](LLAMA_CPP_RUNTIME_INTEGRATION_STATUS.md).

Nach dem Kopieren liefen die nicht-inferenzierenden `--version`- und `--list-devices`-Smokes in allen drei Zielordnern; CUDA-DLLs wurden neben den App-Binaries aufgelöst, ohne den PATH zu ändern. Die vollständige Projekt-Suite lief erneut mit **155 passed in 2,26 s** und Ruff meldete **All checks passed**. Die Offscreen-UI-Tests bestätigen weiterhin, dass App-Start keinen Runtime-Probe und keinen Modellscan auslöst. Es wurden keine globalen Treiber/Pakete installiert und keine Commits oder Pushes erstellt.


## Verwalteter Runtime- und LangChain-Live-Smoke — 3. November 2026

Der MIRO-Inventarpfad `D:\Models\ModelVault\models\llm\qwen\qwen2-5-0-5b-instruct-gguf\weights\Qwen2.5-0.5B-Instruct-Q8_0.gguf` existierte beim Preflight (531.068.224 Byte). Der lokale GGUF-Parser bestätigte Version 3, 290 Tensoren, 34 Metadatenfelder, Architektur `qwen2`, 24 Blöcke, Kontextdeklaration 32.768 und 896 Embedding-Dimensionen. Es wurde genau diese Datei verwendet.

Der app-eigene CPU-`RuntimeController` erstellte einen frischen zugelassenen Ressourcenplan, startete `llama-server` auf einem freien Loopback-Port und meldete die tatsächliche GGUF-Modellkennung über `/v1/models`. Die instrumentierte lokale `GET /health`-Probe beobachtete HTTP 503 mit `Loading model`, danach HTTP 200 mit `{"status":"ok"}`. Anschließend lieferte die echte lokale LangChain-`RunnableSequence` einen Pydantic-validierten Allowlist-Plan mit `inspect_project` und `run_tests`. Die Modellbegründung paraphrasierte den Auftrag; die Nützlichkeit des kleinen Modells wurde nicht bewertet. Der Controller beendete nur seinen eigenen Prozess; sein erfolgreicher `stop()`-Rückgabewert war `True` (Windows meldete beim absichtlichen Terminate Exitcode 1).

Die erste Probe im eingeschränkten lokalen Kommando-Kontext scheiterte mit Windows-Socketfehler 10013; ein separater Loopback-Selbsttest reproduzierte dies. Mit ausschließlich auf `127.0.0.1` begrenzter Freigabe verlief der spätere Live-Lauf; es wurden keine Firewall-/Portregeln, globalen Python-Pakete, Treiber oder System-PATH geändert. Die persistierten Runtime-Ereignisse liegen als neue SQLite-Datei unter `.work/integration_verify_20261103`; stdout/stderr, Status, Health und Toolresultat wurden strukturiert gespeichert. Rohlogs bleiben durch die Implementierung auf 160 Ereignisse/24.000 Zeichen begrenzt.

Prüfungen zum damaligen Live-Smoke: im lokalen Python-3.14.4-venv (`langchain-core 0.3.86`, `langsmith 0.14.17`, `pydantic 2.14.2`) **208 passed, 139 DeprecationWarnings**; Ruff: **All checks passed**. Unter der App-Python-3.15.2-Umgebung: **199 passed, 9 skipped**; die ausgelassenen Workflowfälle waren dort wegen des dokumentierten Python-/LangChain-Gates deaktiviert. `tests/test_ui_smoke.py` war im vollständigen Lauf enthalten. Nicht live geprüft wurden UI-SSE-Chat über den verwalteten Server, CUDA-/Vulkan-Serverstart, Bild-/Audio-Inferenz und der separate Datei-Diff-/Apply-Ablauf.

## Aktuelle End-to-End-Abnahme: Runtime, Chat und Debugserver — 3. November 2026

Der vorhandene Qwen2.5-0.5B-Instruct-Q8_0-GGUF-Kandidat (531.068.224 Byte) wurde über den echten `RuntimeController` mit dem lokalen CPU-Build `llama-server 8842 (455d8e4be)` auf dem Standardport `8080` geladen. Ressourcen-Preflight, HTTP-Healthcheck und `/v1/models`-Modellzuordnung bestanden; die normale UI-SSE-Chatstrecke gab `OK` zurück und speicherte die Antwort samt Lauf-/Ressourcenmetadaten. Der app-eigene Controller stoppte den Server; anschließend lauschte auf Port 8080 kein Prozess mehr. Das belegt einen kleinen erfolgreichen Textlauf, keine Modellqualitäts- oder Performancebewertung.

Der echte Workflow-UI-Pfad startete nach Planprüfung und separater Freigabe den Manifestzieltyp `pyside6-module`/`dbzscc` mit Python 3.14.4 und festem Debug-`argv`: `python -B -X dev -X faulthandler -m dbzscc`. Ein frischer Ressourcenplan (1.536 MiB Hostbedarf, zwei CPU-Threads) wurde zugelassen; Prozess-Liveness und Ereignispersistierung funktionierten. Der UI-Stop beendete den app-eigenen Prozessbaum; `has_owned_process` war danach `False`, Port 18080 blieb geschlossen. Für diesen Smoke lief der Kindprozess mit Qt-Offscreen in isolierten App-Daten; es wurde kein sichtbares zweites Fenster geöffnet.

Dabei gefundene Blocker wurden behoben: Qt-`QComboBox.currentData()` liefert bei `str`-Enums rohe Zeichenketten; Runtime-Backend und `WorkflowAction` werden nun normalisiert. Der Ausführungsereignis-Formatter beschriftet alle aktuellen Enum-Werte und fällt für künftige Werte sicher zurück. Ruff meldet **All checks passed**; die gesamte Suite auf Python 3.14.4 besteht mit **229 passed**. 139 DeprecationWarnings stammen aus den installierten LangChain-/LangSmith-Abhängigkeiten (`asyncio.iscoroutinefunction`), nicht aus fehlgeschlagenen Tests. Der separate Datei-Diff-/Apply-Livepfad, verwalteter GPU-Start, Bild-/Audio-Inferenz und allgemeine Shell-Ausführung bleiben ungeprüft bzw. außerhalb des Umfangs.


## Abschlussverifikation — isolierter Dateiänderungszyklus und GPU-Grenzen — 3. November 2026

Der Service `file_edit_service` wurde mit einer neu angelegten temporären Fixture geprüft, nicht mit Projekt- oder Nutzerdaten: Der erste pytest-Lauf schlug erwartungsgemäß fehl; ein hashgebundener Vorschlag erzeugte zunächst nur eine Diff-Vorschau; danach wurde der Vorschlag atomar ausschließlich auf die Fixture-Datei angewendet; der frische pytest-Retest bestand mit **1 passed**. Es blieb keine `.dbzscc-edit-*.tmp`-Datei zurück. Das ist ein isolierter Service-/Retest-Smoke, kein sichtbarer UI-End-to-End-Nachweis des Modellvorschlags und der Bestätigungsdialogs.

Fokussierte Dateiedit-/UI-Smokes: **23 passed** (13 Dependency-DeprecationWarnings). Vollständige Suite im unveränderten isolierten `.work\\venv314` mit Python 3.14.4: **229 passed**, **139 DeprecationWarnings** aus dem LangChain-/LangSmith-Stack (`asyncio.iscoroutinefunction`); keine Testfehler. Ruff auf `src` und `tests`: **All checks passed**. Pytest nutzte einen frisch erzeugten temporären Basetemp außerhalb des Checkouts; Bytecode- und Pytest-Caches waren deaktiviert.

Begrenzte echte CUDA- und Vulkan-CLI-Modellstarts wurden jeweils erst nach frischem RAM-/Commit-/CPU-/GPU-Preflight nacheinander versucht. Beide liefen in den 90-Sekunden-Timeout und wurden beendet, ohne dass ein erfolgreicher Modellabschluss oder Antworttext bestätigt werden konnte. Die laufenden Modelle sind daher **nicht GPU-verifiziert**. Nachprüfung: keine eigenen llama-Prozesse und keine Listener auf 8080/18080. Die Metadaten-Pairing-Tests verwenden synthetische GGUFs und melden `runtime_verified == False`; ein echter Bild-/Audio-/mmproj-Lauf fehlt. Keine multimodale Nutzbarkeit wird behauptet.

Die GPU-/Metadaten-Timeouts sind im [Runtime-Integrationsbericht](LLAMA_CPP_RUNTIME_INTEGRATION_STATUS.md) mit separaten Preflightwerten und Aufrufgrenzen festgehalten. Die app-eigene CPU-Runtime, UI-SSE-Chatantwort `OK` und der manifestgebundene Development-/Debug-Stop bleiben wie zuvor bestätigt. Es wurden keine Quellen, Modelle, Runtime-Binaries, `.venv`, `.work`, `.tmp`, `.dbzs` oder globalen Einstellungen durch diese Verifikationen verändert.
