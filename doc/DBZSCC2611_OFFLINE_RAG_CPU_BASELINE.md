# DBZSCC 2611: Offline-RAG-CPU-Baseline

## Ergebnis

Die lokale Retrieval-Baseline ist implementiert: ein explizit ausgewählter Workspace wird sicher gelesen und in Python-/Markdown-orientierte Chunks aufgeteilt; der lokale multilingual-E5-ONNX-Encoder erzeugt CPU-Vektoren; SQLite FTS5 liefert lexikalische Treffer; eine begrenzte Cosine-Suche und Reciprocal Rank Fusion (RRF, `k=60`) kombinieren die Ranglisten. Index-Generationen werden vollständig aufgebaut und erst nach Konsistenzprüfungen atomar aktiviert.

Die Anwendung führt weder Modelltraining aus noch verwendet sie Cloud-Embeddings, einen Fallback oder Netzwerkzugriffe zur Laufzeit. Ein Nutzer-Workspace wurde in den Tests nicht gelesen: der echte Modell-Smoke verwendete ausschließlich synthetische Markdown-Dateien in einem temporären Unterordner von `.work`.

## Modell und lokale Installation

Das Modell ist `intfloat/multilingual-e5-small`, MIT-lizenziert. Seine E5-Suche benötigt die Präfixe `query: ` und `passage: `, Eingaben bis 512 Tokens, Attention-Mask-Mittelwert-Pooling und L2-Normalisierung; siehe [offizielle Modellkarte](https://huggingface.co/intfloat/multilingual-e5-small). Die gewählte Standard-ONNX-Datei ist CPU-generisch; die AVX512-VNNI-spezifische quantisierte Variante wurde nicht gewählt.

Nur diese beiden Artefakte wurden vom offiziellen, unveränderlichen Commit `ffdcc22a9a5c973ef0470385cef91e1ecb461d9f` geladen: [ONNX-Dateibaum dieses Commits](https://huggingface.co/intfloat/multilingual-e5-small/tree/ffdcc22a9a5c973ef0470385cef91e1ecb461d9f/onnx).

| Projektdatei | Bytes | SHA-256 |
|---|---:|---|
| `models/rag/multilingual-e5-small/onnx/model.onnx` | 470,268,510 | `ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665` |
| `models/rag/multilingual-e5-small/onnx/tokenizer.json` | 17,082,730 | `0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39` |

`models/rag/multilingual-e5-small/manifest.json` bindet Revision, lokale Pfade, Lizenz, beide SHA-256-Werte und E5-Vertrag. Der Laufzeit-Loader verifiziert die Hashes, akzeptiert ausschließlich `CPUExecutionProvider` und verwendet keine Hub-API.

Der einmalige HF-CLI-Transfer hinterließ `.cache/huggingface`-Metadaten im Modellordner für Download-Fortsetzung; der RAG-Loader verwendet sie nicht.

Die Pakete sind im bereits vorhandenen Projektpfad `.work\venv314` mit CPython 3.14.4 (64-bit) installiert: `onnxruntime==1.31.1`, `tokenizers==0.23.3`, `numpy==2.5.4`; zusätzlich `huggingface_hub==1.28.0` ausschließlich für den einmaligen CLI-Download. Die nötigen Paketabhängigkeiten wurden mitinstalliert. `pip check` meldet: `No broken requirements found.`

## Daten- und Sicherheitsgrenzen

Der Scanner verlangt einen ausdrücklich angegebenen absoluten lokalen Workspace-Pfad und eine stabile Projekt-ID. Er liest nur zugelassene Text-Endungen; versteckte oder sensible Namen, ausgeschlossene Build-/Vendor-Verzeichnisse, Symlinks/Reparse-Points, Nicht-UTF-8-Dateien und erkannte geheime Inhalte werden ausgeschlossen. Dateigröße, Gesamtumfang und Chunkzahl sind begrenzt. Dateipfade, Projekt-ID und aktive Indexgeneration bleiben bei SQLite-, FTS5- und Vektorsuchen voneinander getrennt; FTS-Abfragen werden aus begrenzten, gequoteten Tokens erstellt.

Die Windows-Scandir-Prüfung nutzt `lstat()` für verlässliche Datei-IDs: `DirEntry.stat()` lieferte in beiden vorhandenen Python-Versionen reproduzierbar `st_dev=0` und `st_ino=0`. Für eine bestehende Indexdatenbank werden Scan-Zähler, die nicht persistiert wurden, als unbekannt (`None`) ausgegeben — nicht als gemessene Nullwerte.

## App-Integration: fester lokaler Workerpfad

Die Haupt-App läuft mit CPython 3.15.2; ONNX Runtime 1.31.1 hat auf Windows AMD64 kein stabiles cp315-Wheel ([PyPI-Dateien](https://pypi.org/project/onnxruntime/1.31.1/)), und die offizielle [Python-3.15-Wheel-Anfrage](https://github.com/microsoft/onnxruntime/issues/33271) ist offen. Ein cp314-Binary wird daher nicht in die Haupt-`.venv` gezwungen. Im Entwicklungscheckout ruft `LocalRagWorkerClient` den fest verdrahteten Interpreter `.work\venv314\Scripts\python.exe` mit `-I` und `src/dbzscc/rag/worker.py` auf. Ein eingefrorenes Paket verwendet stattdessen den festen Sidecar-Pfad `runtimes/rag-worker/python.exe`; Worker und Modellmanifest bleiben in beiden Layouts an feste relative Pfade gebunden. Der Prozess erhält genau einen JSON-Auftrag über stdin (`index` oder `search`), läuft mit `shell=False`, ohne dynamische Imports, Shell-Kommandos, `PATH`- oder Systemänderungen. Zeitlimits sind 600 s für Indexierung und 120 s für Suche. Assetpfade werden innerhalb der Anwendungswurzel auf Links/Reparse-Points und Containment geprüft; Rohfehler, Stderr und absolute Quellenpfade werden nicht in den Chat übernommen.

Indexierung startet nur über **„Ausgewähltes Projekt lokal indexieren …“** im Chat-Kontextbereich und erst nach einer Bestätigung, die den ausgewählten Pfad und die lokalen Ausschlüsse nennt. Es gibt keinen Start-/Hintergrundscan. Die App übergibt nur ihre ausdrücklich gewählte Projektwurzel; der vorhandene `scan_workspace`-Allowlist-Scanner bleibt alleiniger Leser der Quelldateien. Pro Projekt wird eine nicht umkehrbare, pfadbasierte ID verwendet; der absolute Root wird nicht als Indexidentität gespeichert. Die SQLite-Datenbank liegt im lokalen App-Datenordner.

RAG-Kontext ist standardmäßig ausgeschaltet und nur für die **nächste** Nachricht aktivierbar. Vor der Suche bestätigt der Nutzer Endpunkt und ausgewähltes Projekt erneut. Der Worker durchsucht ausschließlich den aktiven Index dieser Projekt-ID, liefert höchstens fünf Ausschnitte mit relativen Pfaden und Zeilen zurück und übergibt sie als untrusted Referenz an den Chat. Die Runtime-Anfrage ist zusätzlich auf Loopback beschränkt und verwendet den vorhandenen `local_only=True`-Transport mit deaktivierten Proxys und blockierten Redirects. Eine fehlende oder ungültige Index-/Modellinstallation stoppt die Anfrage; es gibt weder Cloud-, anderes Modell- noch heuristisches Retrieval-Fallback. Dateiinhalt und RAG-Ausschnitte werden nicht in den App-Chatverlauf oder App-Logs geschrieben; die separat betriebene Runtime kann eigene Request-Inhalte protokollieren.

Der nachgewiesene Integrationsrand bleibt der lokale Entwicklungscheckout: fehlt `.work\venv314`, der Worker oder das gepinnte Modellmanifest, schlägt RAG geschlossen fehl. Das portable Bundle-Layout ist durch Tests mit synthetischen Dateipfaden abgedeckt, aber nicht als eigenständiger Installer gebaut oder Ende-zu-Ende ausgeführt; die vorhandene Setuptools-Konfiguration packt weder den cp314-Sidecar noch das Modell samt Laufzeit ein. Installations- oder Produktionsreife ist deshalb **nicht** belegt. Ein eingefrorenes synthetisches Deutsch-/Englisch-/Code-Eval wurde mit dem festen CPU-Worker ausgeführt und bestand die vorab gesetzten Gates; die Werte und Aussagegrenzen stehen in `DBZSCC2611_OFFLINE_RAG_EVALUATION.md`. Dieser kleine Test belegt keine allgemeine Retrievalqualität für Nutzer-Workspaces.

Erfolgreiche Workerantworten enthalten zusätzlich `resource_observation`. Unter Windows liefern `GetProcessTimes` die CPU-Zeit dieses Workerprozesses und `GetProcessMemoryInfo` dessen prozesslebenslange Spitzen für Working Set und Commit. Das ist weder die CPU-Auslastung noch der Gesamtverbrauch von App und System. Falls die Betriebssystemmessung fehlt oder fehlschlägt, bleiben alle drei Zahlen `null` und der Status `unknown`; die Chat-UI zeigt diesen Zustand ausdrücklich an. Die Werte werden noch nicht zur Admission oder Lastbegrenzung verwendet.

## Tatsächliche Prüfungen

- Vor der App-Einbindung bestand die RAG-CPU-Baseline `tests/test_local_rag.py` unter der Haupt-`.venv` (Python 3.15.2) mit **13 Tests**; der damals vollständige Suite-Lauf meldete **293 bestanden**. Diese älteren Zähler sind nicht der aktuelle Integrationslauf.
- Echter CPU-Smoke mit dem installierten Modell in `.work\venv314`: **PASS**. CPython 3.14.4, ONNX Runtime 1.31.1, Tokenizers 0.23.3, `CPUExecutionProvider`, 384 Dimensionen; gemessene Vektornormen `1.00000001` und `0.99999995`. FTS5 indexierte 2 synthetische Chunks, RRF lieferte `retrieval.md` und `e5.md`, projektbegrenzt. Die Cosine `0.86149837` stammt nur aus diesem synthetischen Smoke-Paar und ist kein Qualitätsbenchmark. Wiederholbares Skript: `.work/rag-cpu-baseline/e5_cpu_rag_smoke.py`.
- App-Worker-Roundtrip aus der Haupt-`.venv`: **PASS**. `.work\rag-cpu-baseline\app_worker_integration_smoke.py` rief `LocalRagWorkerClient` auf; dieser startete den festen cp314-Worker, indexierte ausschließlich zwei synthetische Dateien in einem temporären `.work`-Projekt und schloss eine versteckte `.env` aus. Gemessen: 2 Chunks, 2 inkludierte Dateien, 1 versteckter Pfad ausgeschlossen, `CPUExecutionProvider`, 2 Treffer; oberster Beleg `docs/retrieval.md:1-3`. Das Nutzerprojekt wurde nicht gescannt.
- Neue Prozessbeobachtung: erfolgreiche Workerantworten speichern CPU-Zeit, Peak-Working-Set und Peak-Commit oder explizite `unknown`-Felder. Die synthetische Evaluation schreibt diese Werte unter `worker_index.resource_observation` und je Suche unter `latency.search_samples[].resource_observation`; sie misst keine GPU- oder Systemlast.
- Gezielte App-/RAG-Prüfung: `pytest -p no:cacheprovider ... tests\test_local_rag_service.py tests\test_local_rag.py tests\test_chat_context_service.py tests\test_ui_smoke.py -k "rag or local_rag or chat_context"` → **35 bestanden, 20 abgewählt** in 2,16 s. Enthalten sind Checkout-/Bundle-Pfade, Prozessmessung und Unknown-Fallback sowie RAG-Consent, Einmal-Kontext-Chat, Projektbindung und Quellpfadvalidierung.
- Breiter Lauf unter der Haupt-`.venv`: **297 bestanden, 176 Warnungen** in 10,60 s; `tests/test_speech*.py` wurde vollständig ausgeschlossen, kein Speech-/Mikrofontest lief. Die Warnungen sind die vorhandenen LangChain-Deprecations zu `asyncio.iscoroutinefunction`.
- Ruff auf allen sechs geänderten RAG-/UI-/Testdateien: **All checks passed**.
- Repo-weites `ruff check --no-cache src tests` meldet zusätzlich **einen Import-Sortierungsbefund** (`I001`) in der bereits vorher geänderten, von diesem Arbeitsschritt nicht angefassten `tests/test_conversation_store.py`. Diese fremde Änderung wurde nicht verändert.
- `pip check` in Haupt-`.venv` und `.work\venv314`: **No broken requirements found**.
- Erste eingefrorene synthetische Evaluation (vor Prozessmessung): **PASS**, 18 Fragen, 24 Quelldateien/33 Chunks, alle 23 Gates; Recall@1/3/5 `0,833/1,000/1,000`, MRR@5 `0,917`, nDCG@5 `0,938`; Such-p50/p95 `1.991/2.052 ms`, Einzelindexierung `2.695 ms`. Englisch-Recall@1 `0,500` liegt genau auf dem Mindestwert. Rohdaten: `.work\rag-evaluation\run-ff3tfn68\results.json`.
- Erneute eingefrorene synthetische Evaluation mit Ressourcenbeobachtung: **PASS**, weiterhin 23/23 Gates; Such-p50/p95 `1.962/2.162 ms`, Einzelindexierung `3.657 ms`. Alle 19 Workerprozesse wurden gemessen: CPU-Zeit Median `1.938 s`, Peak-Working-Set Median/Max `878,2/878,5 MiB`, Peak-Commit Median/Max `1.388,0/1.388,3 MiB`. Das sind nur Worker-Prozesspeaks, keine App-/Systemgesamtwerte. [Evaluationsbericht mit Grenzen](DBZSCC2611_OFFLINE_RAG_EVALUATION.md); Rohdaten: `.work\rag-evaluation-resource\run-c1xh4cvc\results.json`.
- Keine Speech-/Mikrofon-Testmodule, physische Mikrofonaufnahme oder Nutzerprojekt-Dateien wurden in diesem Arbeitsschritt ausgeführt beziehungsweise gescannt.

## Geänderte Projektdateien

Neu hinzugefügt wurden `src/dbzscc/rag/__init__.py`, `workspace.py`, `embeddings.py`, `index.py`, der feste JSON-Worker `worker.py`, der App-Bridge-Dienst `src/dbzscc/app/local_rag_service.py`, `tests/test_local_rag.py`, `tests/test_local_rag_service.py`, dieses Dokument sowie das Modellmanifest und die beiden gepinnten Artefakte unter `models/rag/multilingual-e5-small/`. Ergänzt wurden `src/dbzscc/app/chat_context_service.py`, `src/dbzscc/ui/main_window.py` und `tests/test_ui_smoke.py`. Die wiederverwendbaren Smoke-Testdateien und Quellenarbeitsnotizen liegen unter `.work/rag-cpu-baseline/`.

Es wurden keine vorhandenen Änderungen überschrieben, keine globalen oder Systempakete installiert und weder Commit noch Push ausgeführt.
