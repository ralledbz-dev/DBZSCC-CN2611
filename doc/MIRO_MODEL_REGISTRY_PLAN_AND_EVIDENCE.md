# MIRO Model Registry — Gesamtplan und Implementierungsnachweis

**Projekt:** DBZSCC2611 · **Stand:** 3. November 2026
**Aktueller Status:** Persistenter MIRO-Katalog und zentrale App-Consumer sind verdrahtet und regressionstestsicher. Der echte `D:\Models`-Scan ist gespeichert, wegen einer Zugriffsausnahme als Teilresultat markiert. Es wurde kein Modell gestartet; Runtime-Qualifikationen bleiben **0**. Kein Commit/Push.

## Kurzüberblick

MIRO erfasst ausdrücklich ausgewählte lokale Modellbestände in einer zentralen, persistenten SQLite-Registry. UI-Listen/Picker, Chat, Workflowplanung, Ressourcen-Admission, Projektlauf und Development-Server verwenden jetzt typisierte `ModelCatalogEntry`-Daten statt jeweils unabhängiger Modellwahrheiten. Metadaten und Dateinamen bleiben von Fähigkeit, Laufzeit-Kompatibilität und Qualität getrennt.

Der echte Scan erfasste 6.005 Verzeichnisse und 53.392 Einträge: 260 GGUF-Kandidaten, 178 Nicht-GGUF-Kandidaten, 243 aufgelöste GGUF-Identitäten und 17 ungelöste. Es gab 0 Parserfehler und 1 verweigertes Verzeichnis (`D:\Models\ModelVault\Claude\.pytest_cache`); der Lauf wurde vollständig traversiert, bleibt aber korrekt `partial` und hat keinen Fortsetzungscursor.

`metadata_prefix_sha256` ist ausschließlich der SHA-256 des gelesenen GGUF-Metadatenpräfixes. Ein vollständiger Datei-SHA-256 wird nicht bei jedem Scan gerechnet, sondern für explizit ausgewählte Modelle/Paare bei Bedarf gestreamt und vor einer Qualifikation erneut geprüft. Der Scan hat weder Runtime-Builds geprüft noch Modelle gestartet oder heruntergeladen.

## Verbraucher-Matrix

Die Registry ist die gemeinsame Quelle für Modellidentität und Beobachtungs-Provenienz. Verbraucher erhalten typisierte Katalogeinträge; ihre jeweils eigenen Aufgabenregeln bleiben davon getrennt.

Der Producer-Pfad ist ebenfalls angebunden: Der App-Start-Hintergrundscan ist standardmäßig eingeschaltet, verwendet konfigurierte Roots und persistiert in dieselbe Registry, aus der die Verbraucher lesen.

| Consumer | Zentrale Registry-Eingabe | Status und Testnachweis |
|---|---|---|
| MIRO-Liste/Detailzeile | Run-spezifisches `query_catalog(CatalogQuery(...))`; Tabelle zeigt Artefakt, Identität, Rollen, Pairings und Qualification-Status. | Angeschlossen; siehe `main_window.py` und `test_miro_app_service.py`. |
| Runtime-Modellpicker | Rekursiver Katalog-Discovery-Output; UI übernimmt nur echte `ModelCatalogEntry`-Objekte mit aktueller, geparster GGUF-Observation. | Angeschlossen; Picker-Test prüft gespeicherten Katalogpfad. |
| Chat | Eindeutige Zuordnung des ausgewählten Server-Modellnamens zu einem aktuellen Katalogrecord; `CatalogChatModel` trägt den Entry in den Streaming-Client. | Angeschlossen; Chat-Client- und UI-Streamingtests prüfen Typ, Beobachtung und gespeicherte Modell-IDs. |
| LangChain-Workflowplanner | Der aktuelle `ModelCatalogEntry` geht in die echte Runnable-Kette; der Chat-Client erhält eine Registry-gebundene `CatalogChatModel`. Der Prompt erhält nur beschränkte Identitäts-/Provenienzfelder. | Angeschlossen; Test prüft, dass Client und Planner dasselbe Entry-Objekt erhalten. Keine Fähigkeitsinferenz aus Metadaten. |
| Runtime-Preflight und Start | Der ausgewählte Pfad wird gegen aktuellen Katalogpfad, Observation und Snapshot geprüft; der ausgewählte GGUF wird on-demand voll gehasht und vor Prozessstart erneut gebunden. | Angeschlossen; Runtime-Test prüft typed ResourceRequest und Abbruch bei Hashänderung nach Admission. Kein echter Start in dieser Arbeit. |
| ResourcePlanner/Admission | `WorkloadRequest.model_catalog_entry` ist ein validierter Evidence-Anhang. Größen-, Kontext-, Speicher- und Workload-Filter bleiben getrennte Planungsinputs und überschreiben keine Registry-Evidenz. | Angeschlossen; Runtime-Admission-Regression prüft den unveränderten typed Entry im Request. |
| Projektlauf/Debug und Development-Server | Validierter Workflowplan und Ausführungs-/Serverplan führen denselben aktuellen `ModelCatalogEntry`; fehlende oder veraltete Records werden fail-closed zurückgewiesen. | Angeschlossen; ProjectExecution- und Devserver-Tests prüfen die Weitergabe. Jeder Prozessstart bleibt separat freigabegesteuert. |
| Künftiger RAG-Consumer | Noch nicht implementiert. Ein späteres RAG muss Inhalte/Quellen getrennt halten und Modellidentität aus `CatalogQuery` beziehen. | Geplant; derzeit keine RAG-Registry-Anbindung behauptet. |

## Hash-Integrität und Qualification-Gate

Der Discovery-Hash bleibt klar als `metadata_prefix_sha256` beschriftet. Schema-Migration v5 ergänzt eine separate Tabelle für dateigroße, gestreamte SHA-256-Werte je Artefakt und Observation. Gehasht wird nur bei expliziter Auswahl eines Modells oder eines Paars; bestehende Inventurdateien werden nicht bei jedem Scan erneut vollständig gelesen. Ein geänderter Snapshot oder abweichender Vollhash bricht die Startfreigabe ab.

Eine Runtime-Qualification ist nur mit aktuellem Vollhash des GGUF, exaktem llama.cpp-Build-SHA-256, Backend, physischer Geräte-ID, Einstellungen und Runtime-Parametern zulässig. Multimodale Qualification bindet zusätzlich den ausgewählten Projektor und dessen eigenen aktuellen Vollhash. Der Registry-Aggregator leitet `qualified` erst aus mindestens zwei separaten vollständigen, persistierten Antworten mit unterschiedlichen Lauf- und Message-IDs sowie identischer Hardware-/Build-/Hash-Bindung ab. Der Chat prüft die vollständige Antwort per SQLite-Read-back, bevor er diesen Beleg schreibt.

Der vorhandene CPU-Qwen-Eintrag zählt nicht automatisch als aktueller Nachweis: Ohne belegte aktuelle Datei-, Build-, Geräte- und Parameterbindung bleibt er historische Information, keine Qualification.

## Übergreifender MIRO-Plan

Die folgende Reihenfolge fasst die im MIRO-Visionpapier beschlossenen Entwicklungsbereiche zusammen. Sie ist ein Plan, keine Behauptung, dass spätere Bereiche bereits implementiert seien.

1. **Lokale Discovery:** ausdrücklich angegebene Modell- und Runtime-Roots rekursiv inventarisieren; Kandidaten, lokale Pfade und Scanprobleme strukturiert zurückgeben. **Implementiert und durch Unit-Tests abgedeckt.**
2. **Begrenzte GGUF-Metadatenextraktion:** Little-Endian v2/v3-Präfix lesen, Limits erzwingen, Big-Endian ablehnen und Parserfehler strukturiert melden. Tensoren und Gewichte bleiben unangetastet. **Implementiert und mit synthetischen Dateien getestet.**
3. **Model-/Artifact-Registry:** ausgewählte Metadaten normalisieren, Modellidentität von lokalen Artefakten trennen, Provenienz und Beobachtungen lokal in SQLite persistieren. **Dieser Slice ist implementiert und getestet.**
4. **Capability- und Kompatibilitätsanalyse:** technische Merkmale, Template/Tokenizer und Runtime-Nachweise mit klaren Evidenzstufen verknüpfen. **Erster konservativer Metadaten-Slice implementiert und getestet; Modalitätsabnahme, Lizenzprüfung und Runtime-Nachweise bleiben offen.**
5. **Hardware- und Ressourcenprofil:** das bestehende ResourceOrchestrator-Admission nimmt nun den typisierten Katalogeintrag als Evidenz mit; Workload-Schätzung und Hardwarefilter bleiben separate Inputs. **Adapter implementiert und getestet; der echte Modellbestand wurde nicht gemessen/admitted.**
6. **Benchmark-Labor und Modellprofile:** reproduzierbare Einzel-/Kombinationstests sowie versionierte, validierte Use-Case-Profile ergänzen. **Geplant; kein Benchmark oder Profil wurde ausgeführt.**
7. **Runtime-, Workflow- und UI-Integration:** Picker, Chat, Planner, Runtime-Preflight, Projektlauf und Devserver greifen auf dieselbe Registry-Observation zu. **Adapter implementiert und getestet; kein echter Runtime-Start und keine reale Modellqualifikation. RAG bleibt geplant.**

## Was jetzt implementiert ist

`src/dbzscc/miro/model_registry.py` stellt `LocalModelRegistry` und `normalize_gguf_identity` bereit. `synchronize(model_roots=..., runtime_roots=...)` verwendet den bestehenden `LocalInventoryScanner` und den bestehenden `GGUFMetadataReader`; es enthält keinen zweiten Dateisystemscanner oder GGUF-Parser. Die Roots werden ausschließlich vom Aufrufer angegeben. Der Scanner und die GGUF-Lektüre finden vor der SQLite-Schreibtransaktion statt; Fehler beim Persistieren rollen den gesamten Registry-Write zurück.

Eine normalisierte Modellidentität entsteht nur, wenn `general.architecture` und ein Name aus `general.name` vorhanden und gültig sind. Als ausdrücklich schwächerer Fallback ist `general.basename` erlaubt; solche Identitäten erhalten den Status `partial`. Der Normalisierer wendet Unicode-NFKC, Whitespace-Kollaps und Casefolding an. `general.organization` wird – falls vorhanden – als zusätzlicher Identitätsbestandteil verwendet. Quantisierung und Dateipfad sind **keine** Modellidentität: verschiedene lokale Dateien oder Quantisierungen können dieselbe Modell-ID teilen, während jedes Artefakt seine eigene pfadbezogene ID behält. Eine Metadaten-ID ist weiterhin kein Beweis einer extern verifizierten Modellquelle.

Die Auswahl der zusätzlich persistierten Rohmetadaten ist bewusst eng: `general.architecture`, `general.name`, `general.basename`, `general.organization`, `general.file_type`, `general.quantization_version`, `general.parameter_count` und `general.size_label`. `general.file_type` bleibt als rohe numerische Kennung erhalten; dieser Slice übersetzt sie nicht in eine Quantisierungsbezeichnung.

## SQLite-Vertrag und Evidenz

Das lokale Schema beginnt mit `schema_migrations` (Version 1) sowie den Tabellen `models`, `artifacts` und `artifact_observations`. Die Migrationen sind versioniert; eine unbekannte neuere Version wird fail-closed abgelehnt. Die Registry ist ausschließlich MIRO-Modell-/Artefaktzustand und übernimmt keine Projekt-, Workspace-, Task- oder UI-Daten.

Schema-Migration v5 ergänzt die optionale Vollhash-Tabelle additiv. Die bestehende lokale App-Datenbank und ihr realer Scan wurden in diesem Implementierungsschritt nicht geöffnet, migriert, neu gescannt oder überschrieben; der bestehende Datenbestand bleibt erhalten. Die Migration läuft beim nächsten normalen Öffnen der Registry.

Drei Identitäten bleiben getrennt:

- **`model_id`:** stabiler, versionierter SHA-256-Fingerprint der normalisierten Metadatenfelder Architektur, Name und optional Organisation. Er belegt nur die lokale Metadatenübereinstimmung.
- **`artifact_id`:** pfadbezogene ID aus dem vorhandenen Inventurdienst. Zwei Kopien derselben Modellvariante bleiben zwei lokale Artefakte.
- **`observation_id`:** Fingerprint eines beobachteten Artefaktzustands; identische Wiederholungen werden aktualisiert statt als Duplikate angelegt. Geänderte Dateisignatur oder Metadatenpräfix erzeugt eine neue Beobachtung.

`metadata_prefix_sha256` bezeichnet exakt den bereits vom GGUF-Reader berechneten SHA-256-Hash des Metadatenpräfixes. Er ist **kein** Voll-Datei-Hash und kein Tensor-/Gewichts-Fingerprint. Größen- und UTC-Änderungszeit stammen aus der Discovery; zusätzlich werden Größe und Zeit der vom Reader geöffneten Datei gespeichert. Weichen beide Snapshots voneinander ab, wird die Modellidentität nicht angehängt und die Beobachtung als `artifact_changed_during_scan` mit Unsicherheit festgehalten.

Artefaktstatus (`metadata_read`, `metadata_parse_failed`, `artifact_changed_during_scan`, `runtime_candidate`) und Identitätsstatus (`resolved`, `partial`, `unresolved`, `not_applicable`) sind getrennte Felder. Parserfehler bleiben damit als lokale Artefaktbeobachtung sichtbar, ohne eine Modellidentität zu erfinden. First-/last-seen-Zeitstempel werden getrennt von Datei-Änderungszeiten gespeichert; UTC-Zeitstempel sind ISO-8601-Strings.

Beispiel für die bewusst explizite Verwendung:

```python
from dbzscc.miro import LocalModelRegistry

registry = LocalModelRegistry(r"D:\DBZSCC2611-data\miro-models.sqlite")
result = registry.synchronize(
    model_roots=[r"D:\Models"],
    runtime_roots=[r"D:\win_runtimes\llama"],
)
```

Der Aufrufer entscheidet über den Datenbankpfad und die zu scannenden Roots. Die Registry erstellt keine DB im Projekt-Checkout, falls sie nicht ausdrücklich dorthin gerichtet wird.

## Capability-Evidenz aus GGUF-Metadaten

`src/dbzscc/miro/capability_evidence.py` stellt `extract_gguf_capability_evidence(document)` bereit. Es verwendet den vorhandenen `normalize_gguf_identity`; Architektur-spezifische Werte werden nur über exakt passende, allowlistete Schlüssel extrahiert: `<architecture>.context_length`, `.embedding_length`, `.block_count`, `.attention.head_count` und `.attention.head_count_kv`. Zusätzlich werden `tokenizer.ggml.model` und das Vorhandensein von `tokenizer.chat_template` erfasst. Der Template-Inhalt wird nicht ausgegeben; der Nachweis enthält nur Zeichenanzahl, SHA-256, Feldtyp und Offset.

Jeder Wert trägt eine Evidenzklasse (`technical_metadata` oder `declared_metadata`), den ursprünglichen Metadatenschlüssel und den Datei-Offset. Falsche Typen oder nichtpositive Architekturwerte werden als Extraktionsprobleme zurückgegeben, nicht als Eigenschaften. Modellname und Dateiname erzeugen keine Fähigkeitssignale. Insbesondere folgt aus Kontextmetadaten keine tatsächlich konfigurierte Runtime-Kontextgröße und aus einem Chat-Template weder Tool-Calling-Kompetenz noch Antwortqualität oder Coding-/Reasoning-Qualität.

Der Profilstatus ist ausdrücklich `runtime_test_status="not_tested"` mit leerer Liste `runtime_tested_capabilities`. `multimodal_status` bleibt `not_verified`; ein GGUF-Metadatenhinweis genügt nicht. Die separate Pairing-Komponente kann Kandidaten speichern, aber erst ein gemeinsamer Bild-Ende-zu-Ende-Test darf multimodale Nutzung bestätigen. Dieser Metadaten-Evidenzextraktor selbst führt weder Projektor- noch Runtime-Tests aus.

## Zusammengesetzter Ein-Pass-Discovery-Ablauf

`src/dbzscc/miro/discovery_pipeline.py` ergänzt `run_local_discovery(database_path, model_roots=..., runtime_roots=...)`. Die Implementierung nutzt die bestehenden injizierbaren Scanner-/Reader-Slots von `LocalModelRegistry`: deren `synchronize` bleibt alleiniger Besitzer des einen Scans, der GGUF-Lektüre und des atomaren SQLite-Upserts. Kleine interne Aufzeichnungsadapter reichen die exakten Ergebnisse desselben Laufs weiter, damit Capability-Evidenz nicht durch einen zweiten, potenziell abweichenden Scan oder Parse entsteht. Vorhandene Registry- und SQLite-Verträge bleiben unverändert.

Das Rückgabeobjekt trennt je Kandidat den `LocalArtifact`, den aktuellen `ArtifactRecord`, das exakte `GGUFParseResult` und – falls zulässig – das `CapabilityProfile`. Runtime-Kandidaten haben kein Parsergebnis und werden nicht geöffnet oder ausgeführt. Parserfehler bleiben zusammen mit ihren strukturierten Issues sichtbar; Scannerprobleme und erfolgreiche Teilergebnisse laufen weiter. Ein Profil wird nur bei erfolgreichem Parsing und Registry-Status `metadata_read` erzeugt. Bei `artifact_changed_during_scan` bleiben die gelesenen Metadaten als Parsebeleg erkennbar, aber Identität und Capability-Hinweise werden unterdrückt. `has_partial_results` markiert Scan-/Parse-/Identitäts-/Evidenzprobleme; SQLite-Fehler propagieren unverändert und führen zum Rollback.

Der Ein-Pass-Discovery-Ablauf bleibt auf den vom Aufrufer explizit angegebenen persistenten SQLite-Pfad und Roots beschränkt; er selbst lädt nichts herunter, führt keine Modelle aus und misst keine Hardware. Die spätere Consumer-Integration ist in der Matrix oben getrennt belegt. Die Ausgabe folgt der stabil sortierten Inventur; ein vom Aufrufer gesetztes `observed_at` macht Zeitstempel reproduzierbar, während `run_id` je Synchronisation neu ist.

## Verwendete Projektunterlagen und Quellen

Diese lokalen Projektunterlagen wurden für Zielbild, Grenzen, Dokumentationspflichten und Testmaßstab herangezogen:

- [MIRO-Vision und Slice-Reihenfolge](DBZSCC2611_MIRO_MODEL_INTELLIGENCE_VISION.md) — insbesondere Model Discovery, getrennte Model-/Artifact-Registry und Umsetzungsbereiche.
- [Modell-, Backend- und Hardware-Kompatibilitätsmatrix](DBZSCC_MODEL_COMPATIBILITY_MATRIX.md) — Evidenzkategorien und die Regel, aus Metadaten oder Dateinamen keine Laufzeit-/Fit-Zusage abzuleiten.
- [Ressourcen-Scheduling-Design](DBZSCC_RESOURCE_SCHEDULING_DESIGN.md) — eine Modell-Registry soll die bestehende Modellwahrheit respektieren und keine globale Ressourcen- oder Projektzustandsdatenbank duplizieren.
- [Architekturleitplanken](ARCHITECTURE_GUARDRAILS.md) — fachliche Services, Persistence-Grenzen, Herkunft, stabile IDs, Tests und keine unnötige Parallelhaltung.
- [Dokumentations- und Projektpapier-Policy](DOCUMENTATION_AND_PROJECT_PAPERS_POLICY.md) — Statuswahrheit, Testevidenz, Grenzen und nächste konkrete Aktion.
- [DBZSCC-Masterleitdokument](DBZSCC2611_MASTER.md) — generelle modulare Modellverwaltung und Präferenz für lokale relationale Speicherung.
- [Bisherige MIRO-Discovery-Dokumentation](MIRO_LOCAL_DISCOVERY.md) und [Development Control Center](DEVELOPMENT_CONTROL_CENTER.md) — aktueller Implementierungs- und Nachweisstand.

Direkt wiederverwendete Implementierungsverträge: [`local_inventory.py`](../src/dbzscc/miro/local_inventory.py), [`gguf_metadata.py`](../src/dbzscc/miro/gguf_metadata.py) und deren bestehende Tests. Die normative GGUF-Formatreferenz, die bereits in der Discovery-Doku verlinkt ist, lautet: [GGUF-Spezifikation im ggml-Repository](https://github.com/ggml-org/ggml/blob/master/docs/gguf.md). In diesem Implementierungsdurchlauf wurde keine zusätzliche Webrecherche und kein externes Modell-/Runtime-Verzeichnis verwendet.

Die konzeptionellen und technischen 2410-Materialien wurden nicht als Quellcode übernommen. Die ausdrücklich ausgeschlossenen alten Source-Verzeichnisse `G:\Dev\DBZSCC2410-runtime-orchestration-v2-clean\rei`, `docs\REI-001` und `D:\Dev\DBZSCC2611\media\CodeSources` wurden für diese Umsetzung nicht als Codequelle verwendet.

## Geänderte und ergänzte Dateien

- `src/dbzscc/miro/model_registry.py` — normalisierte Identität, versioniertes SQLite-Schema, atomare Registry-Upserts und lesende Abfragen.
- `src/dbzscc/miro/__init__.py` — öffentliche Registry- und Capability-Evidenz-Exporte.
- `src/dbzscc/miro/capability_evidence.py` — allowlistete technische/deklarierte Metadatenevidenz, getrennt von Runtime-Verifikation.
- `src/dbzscc/miro/discovery_pipeline.py` — additive Ein-Pass-Orchestrierung auf Basis bestehender Registry-Verträge.
- `tests/test_capability_evidence.py` — Evidenz-Provenienz, Fehltypen, nicht inferierte Claims und unverifizierter Runtime-/Multimodalstatus.
- `tests/test_model_registry.py` — synthetische Fixtures und fokussierte Registry-Tests.
- `tests/test_discovery_pipeline.py` — Ein-Pass-Verknüpfung, Wiederholbarkeit, Parse-/Scan-Teilfehler und Snapshot-Änderung.
- `doc/MIRO_LOCAL_DISCOVERY.md` — aktuelle Funktionsgrenzen, Status und Link auf dieses Dokument.
- `doc/MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md` — dieser neue Plan-/Quellen-/Evidenznachweis.

### Consumer-Integration und Regressionen

- Services/UI: `src/dbzscc/app/miro_service.py`, `chat_client.py`, `conversation_store.py`, `workflow_runtime.py`, `resource_orchestrator.py`, `runtime_service.py`, `project_execution.py`, `development_server.py` und `src/dbzscc/ui/main_window.py`.
- Tests: `test_registry_catalog.py`, `test_catalog_pairing.py`, `test_runtime_service.py`, `test_app_chat_client.py`, `test_workflow_runtime.py`, `test_project_execution.py`, `test_development_server.py`, `test_conversation_store.py`, `test_miro_app_service.py` und `test_ui_smoke.py`.
- Schema-/Hash-Änderungen sind additiv; die echte lokale App-Datenbank und der bereits gespeicherte Scan wurden in diesem Schritt nicht geöffnet, migriert, neu gescannt oder überschrieben. Beim nächsten normalen Öffnen der Registry greift Migration v5.

Es wurden keine Commits, Pushes oder Merges erstellt. Es wurden keine Modelle heruntergeladen und kein Runtime-Prozess gestartet.

## Tatsächliche Testevidenz und offene Grenzen

Ausgeführt auf RAZOR unter Windows im Checkout `D:\Dev\DBZSCC2611` mit Python 3.15.2 und Ruff 0.17.3:

- `python -m pytest -p no:cacheprovider -q -rs` → **254 passed, 0 skipped**, Exitcode **0**.
- Fokussierte Consumer-Regressionen → **105 passed**, Exitcode **0**.
- `ruff check src/dbzscc tests` → **All checks passed**, Exitcode **0**.

Unter Python 3.15.2 meldete der Workflow-Test 176 `DeprecationWarning`s aus LangChain (`asyncio.iscoroutinefunction`); sie sind nicht fehlgeschlagen und es gab keine Skips. Ruff 0.17.3 war sauber.

Der reale Scan von `D:\Models` ist aus dem vorherigen Schritt persistent vorhanden; dieser Durchlauf hat ihn nicht erneut ausgeführt. Es wurde kein On-demand-Vollhash auf dem echten Bestand gerechnet, kein Runtime-Binary geprüft oder gestartet, keine Hardware-/GPU-Messung vorgenommen und keine Multimodalität qualifiziert. Der unveränderte Prefix-Hash ist kein Ersatz für Voll-Datei-Hashes.

## Nächster sinnvoller Schritt

Als nächstes müssten Runtime-Roots ausdrücklich konfiguriert, ein klar unterstütztes und sicheres Modell-/Projektor-Paar ausgewählt und nach frischer Ressourcen-Admission geprüft werden. Erst nach Vollhash-Prüfung beider Dateien, exakter Build-/Backend-/Geräte-/Parameterbindung und zwei vollständigen, separat gespeicherten Antworten darf daraus eine Qualification entstehen. Bis dahin sind die zwei vorhandenen Bild-Paar-Kandidaten `not_tested`; RAG bleibt nicht implementiert.


## Tatsächliche lokale E2E-Prüfung auf RAZOR (3. November 2026)

Der echte Modelllauf mit `Qwen2.5-0.5B-Instruct` wurde **nicht** gestartet. Die Registry im normalen Benutzerprofil war aus dem aktuellen Ausführungskontext wegen fehlenden Zugriffs nicht lesbar; damit ließ sich weder die aktuelle Auswahl noch die fortbestehende Existenz dieses Modells verlässlich bestätigen. Entsprechend wurden kein app-eigener llama.cpp-Prozess gestartet, kein `/health` abgefragt und keine vollständigen Modellantworten erzeugt. Es wurden kein GGUF-Vollhash, kein Runtime-Build-Hash und keine neue Runtime-Evidenz in eine Registry geschrieben. Insbesondere wurden die Zustände `runtime_tested` und `qualified` nicht verändert oder gleichgesetzt; eine zusätzliche Datenbank wurde nicht angelegt.

Für den isolierten Run/Test/Fix-Test blockierte Windows den zuvor vorgesehenen Loopback-HTTP-Testtransport mit `WinError 10013`. Es wurde kein Socketzugriff weiter versucht: Nur der Transport des deterministischen Vorschlags wurde durch einen In-Memory-Testdouble ersetzt. Der Produktionsaufruf `OpenAICompatibleClient.complete`, der strukturierte Vorschlagsparser, `ProjectExecutionService`, `apply_reviewed_proposal` und der automatische pytest-Retest liefen; es fand **keine** Modellinferenz statt. Das Ergebnis ist deshalb ausdrücklich ein deterministischer Fixture-Test und kein modellgestützter Patch-Nachweis.

Im neu angelegten `.tmp/cue-e2e-run-test-fix-20261103-233200/fixture` bestand das reale, app-eigene pytest-Startverfahren seine frische Ressourcen-Admission. Ein einzelner stabiler `run_id` (`b17e5eac874149e49d145f07f9f070da`) blieb über vier Vorschlags-/sichtbare-Diff-/Apply-/Retest-Zyklen erhalten. Jeder Patch war an die vorab erfasste Datei gebunden und durfte ausschließlich `tests/target.py` ändern; nach jedem atomaren Apply wurde der Projekt-Fingerprint aktualisiert und für den Retest neu gebunden. Die Retests 1–3 endeten erwartungsgemäß mit Exitcode 1; Iteration 4 bestand mit Exitcode 0 (`1 passed in 0.01s`). stdout, stderr und separate Exitcode-Events wurden erfasst; es gingen keine Ausgabelinien verloren. Damit wurde die Schleife tatsächlich über drei Iterationen hinaus geprüft, ohne eine gewöhnliche feste Dreiergrenze.

Ein separater abbrechbarer Fixture-Test startete und meldete `CANCEL_FIXTURE_STARTED`; die Cancellation wurde nach etwa 2,13 Sekunden als `cancelled` zurückgegeben (Prozess-Exitcode 1). Nach jedem Retest und nach diesem Abbruch meldete der Projektservice keinen aktiven app-eigenen Prozess mehr. Zusätzlich wurde `MainWindow._repair_failure_signature` mit realer, redigierter Fehlerausgabe auf Gleichheit nach Whitespace-Normalisierung geprüft. Ein künstlicher No-Progress-Zyklus mit absichtlich falschem Patch wurde nicht erzwungen, um keinen wissentlich schlechten Patch anzuwenden.

Die Fixture-Ausgabe und die vier sichtbaren Diff-Dateien liegen für die Nachprüfung im genannten `.tmp`-Arbeitsverzeichnis. Originale Projektquellen, echte Projekttests, Runtime-Binaries und Registry-Daten wurden für diesen Fixture-Lauf nicht geändert; es gab keinen Commit oder Push. Die zuvor gemeldete vollständige Suite mit 263 bestandenen Tests und sauberem Ruff-Lauf wurde in dieser konkreten E2E-Prüfung nicht erneut ausgeführt.


Der Fixture-Nachweis lief kopflos über die Produktionsservices und korrelierte Run-ID, Fingerprints und Ereignisse; eine interaktive MainWindow-/Qt-Signalsequenz oder echte Chat-UI-Bedienung wurde nicht ausgeführt. Die sichtbaren Diff-Dateien wurden im `.tmp`-Arbeitsverzeichnis abgelegt, nicht in einer UI angezeigt.
