# MIRO – lokale Artefaktinventarisierung und GGUF-Metadaten

**Status:** Read-only Discovery-/GGUF-Kernservices und lokale SQLite-Registry implementiert und getestet; noch nicht in eine GUI oder App verdrahtet.
**Capability-Evidenz:** Ein konservativer Metadaten-Slice ist implementiert und getestet; echte Runtime-Fähigkeiten und multimodale Nutzung bleiben unverifiziert.

**Plan, Entscheidungen, Quellen und Testnachweis:** [MIRO Model Registry — Gesamtplan und Implementierungsnachweis](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md).

## Lokale Artefakte entdecken

`src/dbzscc/miro/local_inventory.py` stellt `LocalInventoryScanner` bereit. Der Aufrufer übergibt die zu prüfenden Modell- und Runtime-Wurzelordner explizit. Der rekursive Scanner liefert:

- GGUF-Dateikandidaten aus Modellordnern, erkannt ausschließlich an der Dateiendung `.gguf`;
- llama.cpp-Toolkandidaten aus Runtime-Ordnern, erkannt am Namensmuster `llama-*` mit leerer Dateiendung oder `.exe`;
- absolute Pfade, Herkunfts-Root, Dateigröße, Änderungszeit in UTC, Erkennungsevidenz und stabile, pfadbezogene Artefakt-IDs;
- nicht-fatale Scanprobleme strukturiert im Ergebnis.

Der Inventarservice liest nur Datei- und Verzeichnismetadaten. Er öffnet keine Artefaktinhalte, führt keine Programme aus, schreibt keine Daten und greift nicht auf Netzwerkdienste zu. Symlinks und Windows-Reparse-Points werden übersprungen. Überlappende Roots erzeugen keine doppelten Artefakte; der zuerst konfigurierte Root bleibt als Herkunft erhalten.

## GGUF-Metadaten lesen

`src/dbzscc/miro/gguf_metadata.py` stellt `GGUFMetadataReader.read(path)` bereit. Der Reader unterstützt die Little-Endian-Struktur von GGUF v2 und v3 und gibt entweder ein `GGUFMetadataDocument` oder einen strukturierten `GGUFParseIssue` zurück. Fehler enthalten einen stabilen Code sowie – soweit bekannt – den Datei-Offset.

Der Reader endet exakt hinter den Metadaten-Key/Value-Paaren. Er liest **keine Tensorbeschreibungen oder Tensorgewichte** und behauptet daher nicht, eine vollständige GGUF-Datei oder ein ausführbares Modell validiert zu haben. Datei, Größe, UTC-Änderungszeit, SHA-256 des gelesenen Präfixes sowie Offset, Schlüssel, Typ und Wert jedes gespeicherten Feldes bilden die nachvollziehbare Herkunft.

Die Standardgrenzen sind bewusst fest und beim Erzeugen des Readers konfigurierbar:

| Grenze | Standard |
|---|---:|
| gelesener Metadaten-Präfix | 16 MiB |
| einzelner String | 1 MiB |
| Metadatenschlüssel | 65.535 Bytes, ASCII |
| Metadatenfelder | 100.000 |
| Elemente in einem Array | 1.000.000 |
| Verschachtelungstiefe | 8 |
| insgesamt gespeicherte Array-Elemente je Datei | 4.096 |

Arrays oberhalb des gemeinsamen Speicherbudgets werden weiterhin strukturell gelesen; ihre Einzelwerte werden nicht im Ergebnis abgelegt. Boolesche Arraywerte werden auch dann auf die gültigen Bytes `0` und `1` geprüft. Ungültige UTF-8-Strings, doppelte Schlüssel, abgeschnittene Daten, unbekannte Typen und überschrittene Limits werden als konkrete Parseprobleme zurückgegeben statt als Parser-Ausnahme durchgereicht.

Big-Endian-GGUF wird nicht geparst. Ein eindeutig als Big-Endian erscheinendes Versionsfeld wird als `unsupported_byte_order` gemeldet, nicht stillschweigend als Little-Endian interpretiert. Die normative Formatbeschreibung hält Little-Endian als Standard fest und beschreibt die Versions- und Metadatentypen: [offizielle GGUF-Spezifikation im ggml-Repository](https://github.com/ggml-org/ggml/blob/master/docs/gguf.md).

Der Parser prüft noch nicht sämtliche semantischen Konventionen für Metadatenschlüssel und verifiziert weder Tensor-Shapes/-Offsets noch Gewichte, Runtime-Kompatibilität oder Modellfähigkeiten. Die Tests verwenden synthetische Binärfixtures; ein echtes Modellartefakt wurde in diesem Schritt nicht eingelesen.

## Modellidentität und lokale SQLite-Registry

`src/dbzscc/miro/model_registry.py` stellt `normalize_gguf_identity` und `LocalModelRegistry` bereit. `synchronize(model_roots=..., runtime_roots=...)` verwendet die beiden bestehenden Discovery-/Parserdienste; es gibt keinen zweiten Scanner oder GGUF-Parser. Der Aufrufer gibt Roots und Datenbankpfad explizit an.

Die bevorzugte Modellidentität kombiniert normalisierte `general.architecture` und `general.name`; `general.basename` ist nur ein Fallback mit Status `partial`. Fehlende oder ungültige Felder ergeben `unresolved`, statt eine Identität aus dem Dateinamen zu erfinden. Organisation wird einbezogen, wenn sie vorhanden ist. Quantisierung ist Artefakt-Metadatum und kein Identitätsbestandteil. So können unterschiedliche lokale Dateien derselben Metadatenidentität als getrennte, pfadbezogene Artefakte unter demselben Modell geführt werden.

Das migrationsfähige SQLite-Schema hält `models`, `artifacts` und `artifact_observations` getrennt. Wiederholte, identische Beobachtungen werden aktualisiert; geänderte Dateisignaturen erzeugen neue Beobachtungen. Datei-Änderungszeit, First-/Last-Seen-Zeit und Status/Unsicherheit sind getrennte Werte. Parserfehler und Dateien, die sich zwischen Inventur und Parserlesen erkennbar geändert haben, werden ohne Modellzuordnung festgehalten. Ein fehlgeschlagener Registry-Write rollt die ganze Persistenztransaktion zurück.

Der gespeicherte `metadata_prefix_sha256` ist ausschließlich der Hash des vom Reader gelesenen GGUF-Metadatenpräfixes, ausdrücklich **kein** Hash der vollständigen Modell-Datei. Auch eine auf Metadaten beruhende Modell-ID beweist weder externe Herkunft noch Runtime-Kompatibilität oder Fähigkeiten. Der detaillierte Vertrag und die nächsten MIRO-Slices sind im [Plan-/Evidenzdokument](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md) beschrieben.

## Capability-Evidenz aus vorhandenen Metadaten

`src/dbzscc/miro/capability_evidence.py` stellt `extract_gguf_capability_evidence(document)` bereit. Der Auswerter verwendet `normalize_gguf_identity` und liest nur exakt passende, allowlistete Architekturfelder (`context_length`, `embedding_length`, `block_count`, `attention.head_count`, `attention.head_count_kv`) sowie `tokenizer.ggml.model` und `tokenizer.chat_template`. Für ein Template werden Präsenz, Länge, SHA-256 und Feld-Offset ausgegeben, nicht sein Inhalt. Jedes Signal enthält den Quelldatenschlüssel, GGUF-Typ, Offset und die Klasse `technical_metadata` oder `declared_metadata`; falsch typisierte Werte bleiben als Extraktionsprobleme sichtbar.

Die Liste enthält nur technische/deklarierte Metadatenhinweise, keine Aussagen über Coding-/Reasoning-Qualität, Tool-Calling-Kompetenz oder tatsächlich funktionierende Modalität. Dateiname und allgemeiner Modellname werden nicht zur Capability-Ableitung herangezogen. Der Profilstatus bleibt `runtime_test_status="not_tested"`, die runtime-getestete Capability-Liste leer und `multimodal_status="not_verified"`. Für eine spätere Bild-Verifikation sind das zusammengehörige Modell-/Projektor-Paar und ein Bild-Ende-zu-Ende-Test erforderlich; dieser Slice prüft beides nicht und ändert weder Registry-Schema noch Runtime/UI.

## Einmaliger lokaler Discovery-Ablauf

`src/dbzscc/miro/discovery_pipeline.py` stellt `run_local_discovery(database_path, model_roots=..., runtime_roots=...)` bereit. Der Aufrufer gibt die Roots und einen persistenten SQLite-Pfad ausdrücklich an. Die Funktion nutzt die vorhandene Dependency-Injection der Registry und lässt deren `synchronize` genau einen Inventur-, GGUF-Lese- und Transaktionslauf ausführen; kleine Aufzeichnungsadapter reichen genau dessen Inventur- und Parsergebnisse an die Ergebnisansicht weiter. Es gibt keinen zweiten Scan, kein zweites Parsen und keine Änderung der SQLite-Migrationen oder bestehenden Service-APIs.

Pro Kandidat bleiben Dateierkennung, Parsergebnis, persistierter Registry-Datensatz und abgeleitete Capability-Evidenz getrennte Felder. Ein Profil wird nur aus einem erfolgreichen Parsergebnis abgeleitet, wenn die Registry den Dateisnapshot als `metadata_read` akzeptiert; erkennt sie eine Änderung zwischen Inventur und Metadatenlesen, bleibt das Parsergebnis sichtbar, aber Identität und Capability-Profil werden nicht angehängt. Parserfehler und Scannerprobleme bleiben im Resultat erhalten; `has_partial_results` signalisiert Teilresultate. Ein SQLite-Fehler folgt dem Registry-Vertrag und rollt die Schreibtransaktion zurück.

Runtime-Dateien bleiben bloße Namenskandidaten und werden weder geöffnet noch gestartet. Nur die ausdrücklich übergebene Datenbank wird beschrieben; Roots werden nicht erraten, und dieser Ablauf führt weder Downloads/Netzwerkzugriffe noch Runtime-, Hardware- oder UI-Integration aus. Ein explizites `observed_at` erlaubt reproduzierbare Beobachtungszeitstempel; die Inventur ist stabil sortiert, während die Registry-Lauf-ID weiterhin pro Lauf neu erzeugt wird.

## Was ausdrücklich noch nicht verifiziert ist

Ein Dateiname ist kein Nachweis für ein gültiges Modell oder ein funktionsfähiges llama.cpp-Programm. Die Registry und der Capability-Evidenzextraktor liefern nur normalisierte Metadatenidentität, ausgewählte technische/deklarierte Eigenschaften und begrenzte lokale Provenienz; sie verifizieren weder vollständige Artefakte noch eine kanonische externe Quelle. GGUF-Tensoranalyse, Modell-/Projektor-Zuordnung, Runtime- oder GPU-Prüfung, Hardware-Profiler, echte Capability-/Benchmark-Abnahme und UI-Integration bleiben offen. Große, weit gefasste Such-Roots können beim Inventarisieren lange dauern; Roots sollten gezielt gewählt werden.

## Geänderte Dateien

- `src/dbzscc/miro/local_inventory.py` – read-only Dateiinventar des ersten MIRO-Slices.
- `src/dbzscc/miro/gguf_metadata.py` – begrenzter GGUF-Metadatenreader mit Herkunft und strukturierten Fehlern.
- `src/dbzscc/miro/model_registry.py` – normalisierte Identitäten, lokale SQLite-Registry und nachvollziehbare Artefaktbeobachtungen.
- `src/dbzscc/miro/capability_evidence.py` – enge technische/deklarierte Metadatenevidenz, ohne Runtime- oder Multimodalverifikation.
- `src/dbzscc/miro/discovery_pipeline.py` – additives Ein-Pass-Orchestrierungsmodul ohne Schemaänderung.
- `src/dbzscc/miro/__init__.py`, `src/dbzscc/__init__.py` – Paketgrenzen und öffentliche Exporte.
- `tests/test_local_inventory.py`, `tests/test_gguf_metadata.py` – Discovery, Metadaten, Arrays, Limits und Fehlerpfade.
- `tests/test_model_registry.py` – Identitätsnormalisierung, Upsert/Dubletten, Parsefehler, Transaktions-Rollback und Persistenz.
- `tests/test_discovery_pipeline.py` – Zusammenspiel, deterministische Wiederholung, Teilfehler, Runtime-Grenze und Snapshot-Änderung.
- `tests/test_capability_evidence.py` – Provenienz, Allowlist, Fehltypen, keine Namens-Inferenz und unverifizierter Runtime-/Multimodalstatus.
- `pyproject.toml` – minimale pytest-Konfiguration für `src/` und Ruff-Zielversion.
- `doc/MIRO_LOCAL_DISCOVERY.md` – dieser Status, Funktionsgrenzen und Links.
- `doc/MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md` – Gesamtplan, tatsächlich verwendete Quellen, Entscheidungen und Prüfbelege.

Die vorhandenen Master-, Vision-, Guardrail- und übrigen Spezifikationsdateien blieben unverändert. Es wurden keine Commits, Pushes oder Merges erstellt.

## Verifikation

Ausgeführt auf RAZOR unter Windows mit Python 3.15.2 und Ruff 0.17.3:

- `python -m pytest -p no:cacheprovider -q` → **31 passed**, Exitcode **0**.
- `ruff check src/dbzscc tests` → **All checks passed**, Exitcode **0**.

Nicht getestet wurden andere Betriebssysteme, reale Modellartefakte, eine llama.cpp-Runtime, Hardware-/GPU-Erkennung und eine Anwendung/UI-Integration.

## Nächster sinnvoller Slice

Als Nächstes ist eine separate multimodale Verifikation sinnvoll: das passende Modell-/Projektor-Paar gemeinsam prüfen und einen Bild-Ende-zu-Ende-Test dokumentieren. Bis dahin bleibt die multimodale Nutzung unverifiziert; Runtime- und Hardwareaussagen brauchen separat ausgeführte, konkret dokumentierte Prüfungen.


## Nächster Schritt nach diesem Capability-Slice

Die zuvor empfohlene Metadaten-Capability-Extraktion ist jetzt implementiert und getestet. Ein späterer multimodaler Nachweis muss das passende Modell-/Projektor-Paar gemeinsam prüfen und einen Bild-Ende-zu-Ende-Test bestehen; bis dahin bleibt die Nutzung unverifiziert. Dieser Nachweis gehörte nicht zu diesem Slice.
