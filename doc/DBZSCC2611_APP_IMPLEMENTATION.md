# DBZSCC 2611 — Anwendungsslice und Evidenz

## Was jetzt nutzbar ist

Die App startet als native PySide6-Desktopoberfläche mit sieben Bereichen: Übersicht, Chat, Projekte, Dateiänderungen, Modelle, Runtime und Einstellungen. Der Chat hat den größten Arbeitsbereich, eine Projektdateiansicht links und einen Kontextbereich rechts. Dark und Light sind auswählbar. Gesprächsverläufe werden lokal in einer SQLite-Datei im Benutzerprofil gespeichert.

Der Chat kann über einen austauschbaren OpenAI-kompatiblen Adapter mit `/v1/chat/completions` kommunizieren; Modellkennungen lassen sich über `/v1/models` prüfen. Standard ist `http://127.0.0.1:8080/v1`. Netzwerkzugriffe erfolgen nur nach Senden oder manuellem Runtime-Test und laufen außerhalb des GUI-Threads. Ein optionaler Schlüssel kommt ausschließlich aus `DBZSCC_LLM_API_KEY`, wird nicht gespeichert und wird nur über HTTPS oder an Loopback gesendet.

## Chat-Streaming

Normale Chat-Anfragen setzen `stream: true` und lesen OpenAI-kompatible SSE-`data`-Ereignisse bis zum Abschlussmarker `[DONE]`. Der Qt-Stream-Worker gibt Text-Deltas über korrelierte Qt-Signale an Slots des Hauptfensters weiter; Widgets werden dadurch in der GUI-Schleife aktualisiert. Die Nutzernachricht wird vor der Anfrage genau einmal lokal gespeichert. Die live angezeigte Assistant-Antwort bleibt ein flüchtiger UI-Entwurf und wird erst nach `[DONE]`, einer nichtleeren Abschlussantwort und Übereinstimmung mit den empfangenen Deltas genau einmal persistiert. Stop, HTTP-/Timeout-/Formatfehler, vorzeitiges Stream-Ende und leere Antworten verwerfen den Entwurf; die Nutzernachricht bleibt erhalten. Während eines Streams sind Unterhaltung und Modellwahl gesperrt, und ein zweiter Klick auf **Stoppen** fordert den Abbruch an. Bei blockierendem Netzwerklesen kann der Stop erst beim nächsten Datenereignis oder nach dem konfigurierten Socket-Timeout verarbeitet werden.

Die SSE-Verarbeitung ist mit kontrollierten Fake-Chunk-Responses und ohne Socket-/Server-Bindung getestet. Der separate Datei-Diff-Workflow verwendet weiterhin `complete()` ohne Streaming und bleibt vom normalen Chat-Senden getrennt. Ein echter Runtime-Stream oder eine Inferenz wurde nicht ausgeführt.

## Spracheingabe — nächste priorisierte Stufe

Spracheingabe ist **noch nicht implementiert**. Auf RAZOR wurden mit Python 3.15.2 und PySide6 6.12.0 die Python-Bindings `QAudioSource` und `QMediaDevices` aus QtMultimedia gefunden; dabei wurde weder ein Audiogerät geöffnet noch Mikrofonzugriff angefordert. In dieser Python-Umgebung sind `vosk`, `faster_whisper`, `whisper`, `ctranslate2`, `sounddevice`, `soundcard` und `pyaudio` nicht installiert; im geprüften App-Checkout wurde kein lokales Vosk-/Whisper-Modell gefunden. Es wurde kein Modell heruntergeladen oder gestartet. Eine heutige Spracheingabe wäre deshalb nicht ehrlich als funktionierend zu behaupten.

Eine vollständig lokale/offline Umsetzung ist technisch möglich: QtMultimedia kann den Audioeingang bereitstellen ([`QAudioSource`-Dokumentation](https://doc.qt.io/qt-6/qaudiosource.html)); ein STT-Backend kann die aufgenommenen PCM-Daten anschließend lokal verarbeiten, ohne Audio an einen Cloud-Dienst zu senden. Zwei konkrete Kandidaten:

- **Vosk** passt mit einer Streaming-API zu Push-to-talk und arbeitet laut [Projektseite offline](https://alphacephei.com/vosk/). Für Deutsch listet die [offizielle Modellübersicht](https://alphacephei.com/vosk/models) ein kleines 45-MB-Modell unter Apache 2.0. Die Vosk-Laufzeit und dieses Modell fehlen hier; vor Auswahl muss zusätzlich die Paket-/Wheel-Kompatibilität mit Python 3.15 geprüft werden.
- **whisper.cpp** nennt Windows und CPU-only-Ausführung als unterstützt und kann lokal mit einer separat bereitgestellten ggml-Modell-Datei laufen ([offizielles Projekt](https://github.com/ggml-org/whisper.cpp)). Der README-Richtwert für das mehrsprachige `base`-Modell beträgt 142 MiB auf Datenträger und ungefähr 388 MB Speicher. Weder Binary noch Modell sind im Checkout installiert.

Der nächste priorisierte App-Schnitt nach dem Chat-Streaming ist daher eine **explizit gestartete lokale Push-to-talk-Eingabe**: Engine/Modell und lokale Verfügbarkeit/Lizenz festlegen; Audio nur nach Tastendruck über QtMultimedia aufnehmen; in-memory an einen Worker geben; Transkript als sichtbare Vorschau in den Composer übernehmen, aber niemals automatisch senden; Aufnahme, Abbruch und Laufzeitfehler klar anzeigen. Tests sollen Audiochunks und Recognizer kontrolliert faken; vor einer Funktionszusage braucht es zusätzlich eine manuelle Mikrofon-/Modellprüfung und einen Nachweis, dass die Transkription ohne Netzwerk auskommt. Keine Cloud-STT, kein Always-on-Mikrofon und keine automatische Modellinstallation.

Der Projektbaum öffnet sich erst nach expliziter Ordnerauswahl. Branch, HEAD und Git-Status werden nur auf Anforderung lesend abgefragt. Die Git-Sicherheitsfreigabe gilt nur für den jeweiligen Git-Prozess; globale Git-Einstellungen werden nicht verändert.

## Sichere Einzeldateiänderungen

Die Seite **Dateiänderungen** übernimmt die gespeicherte Projektwurzel nicht automatisch: In jeder Sitzung müssen Arbeitswurzel und bestehende Zieldatei ausdrücklich gewählt werden. Unterstützt werden reguläre UTF-8-Textdateien (mit oder ohne BOM) bis 256 KiB. Binärdaten, ungültiges UTF-8, Pfade außerhalb der Root, `..`-Pfade sowie Symlinks und Windows-Reparse-Points/Junctions werden abgelehnt. Es werden keine neuen, gelöschten oder umbenannten Dateien erstellt.

Ein Klick auf **Diff-Vorschlag anfordern** sendet die ausgewählte Datei und Änderungsbeschreibung an die konfigurierte Runtime. Das ist nur aktiv, wenn ein manueller `/v1/models`-Test erfolgreich war, der Endpunkt Loopback ist und die gewählte Modellkennung tatsächlich zurückgemeldet wurde. Die Antwort muss ein einziges JSON-Objekt mit `relative_path`, `base_sha256` und vollständigem `updated_content` sein. Die App führt daraus lokal einen Unified Diff her; Modelltext, Code und Shell-Befehle werden nie ausgeführt.

Der Diff bleibt zunächst reine Vorschau. **Diff separat freigeben und atomar anwenden** öffnet eine zweite Bestätigung; erst ein Ja löst das Schreiben aus. Direkt davor werden Root-/Symlink-/Größen-/Encoding-Regeln sowie der unveränderte SHA-256-Ausgangshash erneut geprüft. Geschrieben wird per temporärer Datei im Zielordner, `fsync` und `os.replace`. Bei Abweichung wird abgebrochen und die Datei bleibt unangetastet. Es gibt kein automatisches Backup; vor folgenreichen Änderungen sollte eine Versionsverwaltung oder ein anderes Backup bestehen. Ein normales Chat-Senden ruft diesen Ablauf nicht auf.

In diesem Arbeitsschritt wurde keine lokale Runtime geprüft und keine Inferenz ausgeführt. Deshalb ist die Vorschlagsfunktion auf diesem Rechner derzeit **nicht verfügbar**, bis der Nutzer eine lokale Runtime startet und sie über die Runtime-Seite manuell prüft. Die Tests verwenden weder ein Scheinmodell noch verändern sie Projektdateien.

## MIRO und Sicherheitsgrenzen

Modell- und Runtime-Roots müssen ausdrücklich eingetragen und anschließend per Button gescannt werden. Beim App-Start läuft weder eine Runtime-Prüfung noch ein Modell- oder Dateisystemscan. Die App ruft die bestehende MIRO-Discovery-Pipeline auf; sie dupliziert weder Scanner noch GGUF-Parser. Kandidaten, Parsergebnisse und vorhandene Capability-Evidenz werden angezeigt. Runtime-Kandidaten werden nicht geöffnet oder gestartet.

Metadaten und Dateinamen belegen keine Inferenz, Modellqualität, Hardwarepassung oder Multimodalität. Auf diesem Gerät wurde weder eine echte Runtime noch Modellinferenz verifiziert. Cloud-Endpunkte sind als manuelle HTTPS-Konfiguration möglich, aber nicht der Standard und keine Kernabhängigkeit.

## Architektur und verwendete Projektunterlagen

`app/` enthält Konfiguration, Chat-Adapter, lokalen Konversationsspeicher, lesenden Git-Service, den Qt-freien `file_edit_service.py` und die MIRO-Anwendungsgrenze. `ui/` enthält den generischen Qt-Worker und einen separaten Chat-Stream-Worker mit Delta-/Abschluss-/Fehler-/Abbruchsignalen. `miro/` bleibt alleiniger Besitzer der vorhandenen Discovery-, GGUF- und Registry-Funktionen. Laufzeitdaten liegen außerhalb des Checkouts.

Die Umsetzung folgt den lokalen [UI-Vision](DBZSCC2611_UI_VISION.md)-, [Architekturleitplanken](ARCHITECTURE_GUARDRAILS.md)-, [MIRO-Registry](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md)- und [MIRO-Discovery](MIRO_LOCAL_DISCOVERY.md)-Unterlagen. Alter 2410-App-Code und `media/CodeSources` wurden nicht als Codevorlage benutzt.

## Ergänzte bzw. geänderte Dateien

- Start und Paketierung: `README.md`, `pyproject.toml`, `src/dbzscc/__main__.py`.
- App-Services: `src/dbzscc/app/__init__.py`, `config.py`, `chat_client.py`, `conversation_store.py`, `project_service.py`, `file_edit_service.py`, `miro_service.py`, `main.py`.
- Native UI: `src/dbzscc/ui/__init__.py`, `main_window.py`, `workers.py`.
- Neue bzw. erweiterte Tests: `tests/test_app_chat_client.py`, `tests/test_file_edit_service.py`, `tests/test_ui_smoke.py`; bestehende App- und MIRO-Tests bleiben enthalten.
- Dieser Evidenznachweis: `doc/DBZSCC2611_APP_IMPLEMENTATION.md`.

Die vorhandenen MIRO-Core-Module und deren bestehende Tests wurden für diesen App-Slice nicht umgeschrieben.

## Tatsächliche Prüfungen

Im Checkout `D:\Dev\DBZSCC2611`, mit Python 3.15.2 und PySide6 auf RAZOR:

- `python -m pytest -p no:cacheprovider -q` → **64 passed** in 1,20 s. Socketfreie SSE-Fake-Chunks prüfen normales `[DONE]`-Ende, zerteilte UTF-8-Chunks, HTTP-Fehler, Zeitlimit, vorzeitiges Ende, leere Antwort und Abbruch. Offscreen-UI-Tests prüfen genau eine vollständige Assistant-Persistenz sowie Fehler/Stop ohne Teilantwort; bestehende Datei-Diff- und MIRO-Tests bleiben enthalten. Es wurde keine lokale Runtime oder Inferenz verwendet.
- `ruff check src/dbzscc tests` → **All checks passed**.
- Echter `dbzscc.app.main:main`-Einstieg mit `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, 500-ms-Eventloop und temporärem `LOCALAPPDATA`/QSettings → Exitcode **0**; die temporäre SQLite-Datei enthielt genau eine Start-Unterhaltung. GUI-Smoke-Tests bestätigen **7** Navigationsseiten, Runtime „Noch nicht geprüft“, deaktivierte Dateiänderungen ohne manuelle Runtime-Prüfung und keinen MIRO-Scan beim Start.
- Der lesende Git-Service wurde am Checkout geprüft: Branch `master`, kein vorhandener HEAD-Commit, acht ungetrackte Pfade. Es wurden keine Commits, Pushes oder Merges erstellt.
- TOML- und App-Imports wurden erfolgreich geprüft. Die Python-Dateien wurden zusätzlich mit `ast.parse` syntaktisch validiert.

## Noch offen

Agenten, Job-Orchestrierung, Terminal/Werkzeuge, Memory/RAG, Datei-Anhänge, **Spracheingabe**, Mehrdatei-/Patchserien-Änderungen und Runtime-Lifecycle sind nicht implementiert. Chat-Streaming wurde nur mit Fake-Streams geprüft; echte Runtime-Inferenz ist nicht verifiziert. Die nächste priorisierte Produktstufe ist lokale Push-to-talk-STT gemäß obigem Plan. Davon getrennt bleiben ein manueller Verbindungstest gegen eine vom Nutzer gestartete lokale llama.cpp-kompatible Runtime und ein kontrollierter Ein-Datei-Vorschlag mit menschlicher Prüfung und Freigabe offen.
