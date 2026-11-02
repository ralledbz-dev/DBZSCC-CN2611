# MIRO Model Registry — Gesamtplan und Implementierungsnachweis

**Projekt:** DBZSCC2611 · **Stand:** 2. November 2026  
**Status dieses Slices:** Capability-Evidenzextraktion implementiert und lokal getestet; keine GUI-/Runtime-Abnahme.

## Kurzüberblick

MIRO soll den lokalen Modellbestand nachvollziehbar erfassen, ohne Dateinamen mit Fähigkeiten oder Modellidentität zu verwechseln. Der bisherige Kern kann ausdrücklich angegebene Modell- und Runtime-Roots inventarisieren und begrenzte GGUF-v2/v3-Metadaten lesen. Auf der Registry baut jetzt eine kleine, reine Capability-Evidenzextraktion auf; sie hält technische und deklarierte Metadaten getrennt von echten Runtime-Tests.

Der Stand ist bewusst enger als eine produktive Modellverwaltung: Die Registry liest vorhandene Metadaten, führt keine Modelle aus und lädt nichts herunter. Der SHA-256-Wert gilt nur für den vom GGUF-Reader gelesenen Metadatenpräfix, nicht für die vollständige Modell-Datei.

## Übergreifender MIRO-Plan

Die folgende Reihenfolge fasst die im MIRO-Visionpapier beschlossenen Entwicklungsbereiche zusammen. Sie ist ein Plan, keine Behauptung, dass spätere Bereiche bereits implementiert seien.

1. **Lokale Discovery:** ausdrücklich angegebene Modell- und Runtime-Roots rekursiv inventarisieren; Kandidaten, lokale Pfade und Scanprobleme strukturiert zurückgeben. **Implementiert und durch Unit-Tests abgedeckt.**
2. **Begrenzte GGUF-Metadatenextraktion:** Little-Endian v2/v3-Präfix lesen, Limits erzwingen, Big-Endian ablehnen und Parserfehler strukturiert melden. Tensoren und Gewichte bleiben unangetastet. **Implementiert und mit synthetischen Dateien getestet.**
3. **Model-/Artifact-Registry:** ausgewählte Metadaten normalisieren, Modellidentität von lokalen Artefakten trennen, Provenienz und Beobachtungen lokal in SQLite persistieren. **Dieser Slice ist implementiert und getestet.**
4. **Capability- und Kompatibilitätsanalyse:** technische Merkmale, Template/Tokenizer und Runtime-Nachweise mit klaren Evidenzstufen verknüpfen. **Erster konservativer Metadaten-Slice implementiert und getestet; Modalitätsabnahme, Lizenzprüfung und Runtime-Nachweise bleiben offen.**
5. **Hardware- und Ressourcenprofil:** tatsächliche Telemetrie, Speicherbudgets, Unsicherheit und konservative Admission nach dem vorhandenen Scheduling-Entwurf abbilden. **Geplant; keine Messung oder Admission wurde hier vorgenommen.**
6. **Benchmark-Labor und Modellprofile:** reproduzierbare Einzel-/Kombinationstests sowie versionierte, validierte Use-Case-Profile ergänzen. **Geplant; kein Benchmark oder Profil wurde ausgeführt.**
7. **Runtime-, Cognitive- und UI-Integration:** Start/Lifecycle, Profile, Orchestrierung und UI erst hinter den getesteten Verträgen anbinden. **Geplant; dieser Slice verdrahtet nichts in die Anwendung.**

## Was jetzt implementiert ist

`src/dbzscc/miro/model_registry.py` stellt `LocalModelRegistry` und `normalize_gguf_identity` bereit. `synchronize(model_roots=..., runtime_roots=...)` verwendet den bestehenden `LocalInventoryScanner` und den bestehenden `GGUFMetadataReader`; es enthält keinen zweiten Dateisystemscanner oder GGUF-Parser. Die Roots werden ausschließlich vom Aufrufer angegeben. Der Scanner und die GGUF-Lektüre finden vor der SQLite-Schreibtransaktion statt; Fehler beim Persistieren rollen den gesamten Registry-Write zurück.

Eine normalisierte Modellidentität entsteht nur, wenn `general.architecture` und ein Name aus `general.name` vorhanden und gültig sind. Als ausdrücklich schwächerer Fallback ist `general.basename` erlaubt; solche Identitäten erhalten den Status `partial`. Der Normalisierer wendet Unicode-NFKC, Whitespace-Kollaps und Casefolding an. `general.organization` wird – falls vorhanden – als zusätzlicher Identitätsbestandteil verwendet. Quantisierung und Dateipfad sind **keine** Modellidentität: verschiedene lokale Dateien oder Quantisierungen können dieselbe Modell-ID teilen, während jedes Artefakt seine eigene pfadbezogene ID behält. Eine Metadaten-ID ist weiterhin kein Beweis einer extern verifizierten Modellquelle.

Die Auswahl der zusätzlich persistierten Rohmetadaten ist bewusst eng: `general.architecture`, `general.name`, `general.basename`, `general.organization`, `general.file_type`, `general.quantization_version`, `general.parameter_count` und `general.size_label`. `general.file_type` bleibt als rohe numerische Kennung erhalten; dieser Slice übersetzt sie nicht in eine Quantisierungsbezeichnung.

## SQLite-Vertrag und Evidenz

Das lokale Schema beginnt mit `schema_migrations` (Version 1) sowie den Tabellen `models`, `artifacts` und `artifact_observations`. Die Migrationen sind versioniert; eine unbekannte neuere Version wird fail-closed abgelehnt. Die Registry ist ausschließlich MIRO-Modell-/Artefaktzustand und übernimmt keine Projekt-, Workspace-, Task- oder UI-Daten.

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

Der Profilstatus ist ausdrücklich `runtime_test_status="not_tested"` mit leerer Liste `runtime_tested_capabilities`. `multimodal_status` bleibt `not_verified`; ein GGUF-Metadatenhinweis genügt nicht. Eine künftige multimodale Verifikation muss das passende Modell-/Projektor-Paar gemeinsam verwenden und einen Bild-Ende-zu-Ende-Test bestehen. Dieser Slice erkennt oder paart keine Projektoren, startet keine Runtime, lädt nichts herunter, ändert keine SQLite-Migration und verdrahtet weder UI noch Runtime.

## Zusammengesetzter Ein-Pass-Discovery-Ablauf

`src/dbzscc/miro/discovery_pipeline.py` ergänzt `run_local_discovery(database_path, model_roots=..., runtime_roots=...)`. Die Implementierung nutzt die bestehenden injizierbaren Scanner-/Reader-Slots von `LocalModelRegistry`: deren `synchronize` bleibt alleiniger Besitzer des einen Scans, der GGUF-Lektüre und des atomaren SQLite-Upserts. Kleine interne Aufzeichnungsadapter reichen die exakten Ergebnisse desselben Laufs weiter, damit Capability-Evidenz nicht durch einen zweiten, potenziell abweichenden Scan oder Parse entsteht. Vorhandene Registry- und SQLite-Verträge bleiben unverändert.

Das Rückgabeobjekt trennt je Kandidat den `LocalArtifact`, den aktuellen `ArtifactRecord`, das exakte `GGUFParseResult` und – falls zulässig – das `CapabilityProfile`. Runtime-Kandidaten haben kein Parsergebnis und werden nicht geöffnet oder ausgeführt. Parserfehler bleiben zusammen mit ihren strukturierten Issues sichtbar; Scannerprobleme und erfolgreiche Teilergebnisse laufen weiter. Ein Profil wird nur bei erfolgreichem Parsing und Registry-Status `metadata_read` erzeugt. Bei `artifact_changed_during_scan` bleiben die gelesenen Metadaten als Parsebeleg erkennbar, aber Identität und Capability-Hinweise werden unterdrückt. `has_partial_results` markiert Scan-/Parse-/Identitäts-/Evidenzprobleme; SQLite-Fehler propagieren unverändert und führen zum Rollback.

Nur der vom Aufrufer explizit angegebene persistente SQLite-Pfad wird beschrieben. Es gibt keine impliziten Roots, Schemaänderung, Netzsuche, Downloads, Runtime-Starts, Hardwareprüfung oder UI-Integration. Die Ausgabe folgt der stabil sortierten Inventur; ein vom Aufrufer gesetztes `observed_at` macht Zeitstempel reproduzierbar, während `run_id` absichtlich je Synchronisation neu ist. Die Entscheidung stützt sich ausschließlich auf die bereits dokumentierten lokalen Projektverträge und die bestehenden vier MIRO-Services; in diesem Schritt wurden keine zusätzlichen externen Quellen oder alten Codevorlagen verwendet.

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

Es wurden keine Commits, Pushes oder Merges erstellt. Die vorhandenen MIRO-Zieldateien wurden punktuell erweitert; nicht betroffene untracked Projektdateien wurden weder bereinigt noch überschrieben.

## Tatsächliche Testevidenz und offene Grenzen

Ausgeführt auf RAZOR unter Windows im Checkout `D:\Dev\DBZSCC2611` mit Python 3.15.2 und Ruff 0.17.3:

- `python -m pytest -p no:cacheprovider -q` → **31 passed**, Exitcode **0**.
- `ruff check src/dbzscc tests` → **All checks passed**, Exitcode **0**.

Die Tests nutzen synthetische GGUF-Dateien. Nicht ausgeführt wurden ein Scan echter Modellordner, ein echter Modell-/Voll-Datei-Hash, Tensor- oder Gewichtsprüfung, llama.cpp-Inferenz, Hardware-/GPU-Messung, Kompatibilitäts- oder Capability-Abnahme sowie GUI-/App-Integration. Der Checkout meldete `master` ohne vorhandenen Commit; alle Projektdateien waren untracked. Deshalb kann dieses Testergebnis nicht an einen Commit-SHA gebunden werden.

## Nächster sinnvoller Schritt

Als Nächstes ist eine separate multimodale Verifikation sinnvoll: passendes Modell-/Projektor-Paar gemeinsam prüfen und einen Bild-Ende-zu-Ende-Test dokumentieren. Bis dahin bleibt die multimodale Nutzung unverifiziert; Runtime- und Hardwareaussagen benötigen weiterhin separat ausgeführte, konkret dokumentierte Prüfungen.


## Nächster Schritt nach diesem Capability-Slice

Der oben dokumentierte Capability-Metadaten-Slice ist jetzt umgesetzt und getestet. Ein späterer, separater multimodaler Nachweis müsste ein passendes Modell-/Projektor-Paar gemeinsam prüfen und einen Bild-Ende-zu-Ende-Test bestehen; bis dahin bleibt `multimodal_status="not_verified"`. Das wurde in diesem Auftrag nicht ausgeführt.
