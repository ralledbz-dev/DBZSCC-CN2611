# MIRO – lokale Artefaktinventarisierung und GGUF-Metadaten

**Status:** Nicht-ausführende Discovery-/GGUF-Kernservices, persistente zentrale Registry und die in der [Consumer-Matrix](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md#verbraucher-matrix) aufgeführten UI-/App-Verbraucher sind implementiert und getestet. Der reale `D:\Models`-Scan ist persistent, vollständig traversiert, aber wegen einer Zugriffsausnahme korrekt als Teilresultat markiert. Runtime-Qualifikationen: **0**.

Der App-Start-Hintergrundscan ist standardmäßig aktiviert. Er verwendet ausdrücklich konfigurierte Roots und speichert Beobachtungen in derselben MIRO-Registry, die Picker, Chat und Planner abfragen.

**Plan, Datenfluss, Hash-/Qualification-Gates und Testnachweis:** [MIRO Model Registry — Gesamtplan und Implementierungsnachweis](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md). **Zähler und Scanprovenienz:** [Abschlussbericht zum rekursiven Modellscan](MIRO_RECURSIVE_SCAN_VALIDATION.md).

## Lokale Artefakte entdecken

`src/dbzscc/miro/local_inventory.py` stellt `LocalInventoryScanner` bereit. Der Aufrufer muss Modell- und Runtime-Roots ausdrücklich angeben. Die rekursive Inventur erfasst GGUF-Dateikandidaten, llama.cpp-Toolkandidaten, absolute Pfade, Herkunft, Größe, UTC-Änderungszeit, stabile Artefakt-IDs und nicht-fatale Scanprobleme. Symlinks und Windows-Reparse-Points werden übersprungen; überlappende Roots erzeugen keine doppelten Artefakte.

Der Scanservice selbst öffnet keine beliebigen Dateien außer den ausdrücklich geprüften Metadaten, startet keine Programme und greift nicht auf Netzwerkdienste zu. Er führt weder Modell-Downloads noch Runtime- oder Hardwaretests aus.

## GGUF-Metadaten lesen

`src/dbzscc/miro/gguf_metadata.py` unterstützt Little-Endian-GGUF v2/v3 und liefert ein `GGUFMetadataDocument` oder ein strukturiertes `GGUFParseIssue` mit stabilem Fehlercode und – sofern bekannt – Datei-Offset. Der Reader endet hinter den Metadaten-Key/Value-Paaren; Tensorbeschreibungen und Gewichte werden nicht gelesen. Limits: 16 MiB Metadatenpräfix, 1 MiB Einzelstring, 100.000 Felder, Tiefe 8 und 4.096 persistierte Arrayelemente pro Datei. Big-Endian wird als nicht unterstützt gemeldet.

Die Formatnorm ist die [GGUF-Spezifikation im ggml-Repository](https://github.com/ggml-org/ggml/blob/master/docs/gguf.md).

## Registry, Identität und Hashes

`src/dbzscc/miro/model_registry.py` stellt `LocalModelRegistry` bereit. Modellidentität (`model_id`), pfadbezogenes lokales Artefakt (`artifact_id`) und konkreter beobachteter Snapshot (`observation_id`) bleiben getrennt. Architektur plus normalisierter Name sind die bevorzugte lokale Identität; ein schwächerer Basename-Fallback ist ausdrücklich als `partial` gekennzeichnet. Ein Metadatenfingerprint beweist keine externe Herkunft oder Laufzeitfähigkeit.

`metadata_prefix_sha256` ist ausschließlich der SHA-256 des gelesenen Metadatenpräfixes, **kein** Hash der vollständigen Modelldatei. Der aktuelle Quellcode ergänzt dafür Migration v5 mit separater, additiver Speicherung eines gestreamten Vollhashes pro Datei und Observation. Vollhashes werden nur für ausdrücklich ausgewählte Modelle/Paare gerechnet, nicht pauschal bei jedem Scan. Der echte lokale Datenbankbestand wurde bei diesem Arbeitsschritt nicht geöffnet oder migriert; der bestehende Scan bleibt unverändert und Migration v5 läuft beim nächsten normalen Registry-Öffnen.

## Fähigkeiten und Runtime-Belege

`src/dbzscc/miro/capability_evidence.py` extrahiert nur eng allowlistete technische/deklarierte Metadatenhinweise, darunter Architekturparameter und Template-Präsenz, -Länge und -Hash. Ein Metadatenhinweis belegt weder Tool-Calling, Qualität, Coding/Reasoning noch eine tatsächlich funktionierende Modalität.

Der Scan hat zwei Bild-Paarungskandidaten gespeichert; beide sind `not_tested`, keine Qualification. Für eine spätere multimodale Qualification müssen Modell und Projektor als exakt ausgewähltes Paar jeweils aktuell voll gehasht und gemeinsam in einer frischen Runtime-Prüfung getestet werden. Runtime-Qualification bindet zusätzlich den exakten Build-SHA-256, Backend, physische Geräte-ID und Runtime-Parameter und verlangt mindestens zwei vollständige, separat gespeicherte Antworten mit unterschiedlichen Lauf- und Message-IDs.

## App-Verbraucher und offene Punkte

Runtime-Modellpicker, Chat, Workflowplanner, ResourcePlanner/Admission, ProjectExecution und Development-Server konsumieren nun typisierte aktuelle `ModelCatalogEntry`-Records. Ressourcen-/Workload-Filter bleiben separate Planungsinputs und verändern keine Registry-Evidenz. Eine künftige RAG-Anbindung ist **nicht implementiert** und wird nicht als verbunden behauptet.

Im echten Scan war kein Runtime-Root konfiguriert; es wurde daher kein Runtime-Binary geprüft oder gestartet. Kein Modell wurde heruntergeladen oder gestartet. Echte Tensor-/Gewichtsprüfung, Runtime-/GPU-Messung, multimodale End-to-End-Tests, Benchmarking und Modellqualifikation bleiben offen. Die konkrete Scanbilanz und die einzige Zugriffsabweichung stehen im [Scan-Abschlussbericht](MIRO_RECURSIVE_SCAN_VALIDATION.md).

## Verifikation

Auf RAZOR mit Python 3.15.2: vollständige Testsuite **254 passed, 0 skipped**; fokussierte Consumer-Regressionen **105 passed**. Es gab 176 LangChain-Deprecation-Warnungen, keine Testfehler. **Ruff 0.17.3:** alle Prüfungen bestanden. UI-/App-Datenfluss wurde durch Tests abgedeckt; echte llama.cpp-Inferenz und GPU-Erkennung wurden nicht ausgeführt. Es wurde nicht committet oder gepusht.
