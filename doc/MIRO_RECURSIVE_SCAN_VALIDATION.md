# MIRO: Abschlussbericht zum rekursiven Modellscan

**Kurzfazit:** Der konfigurierte Modellroot wurde vollständig traversiert und der Lauf in der zentralen MIRO-Registry gespeichert. Wegen einer einzelnen verweigerten Verzeichnisberechtigung ist der Lauf korrekt als **Teilresultat** markiert; es gibt aber keinen offenen Fortsetzungscursor.

## Was jetzt zentral gespeichert wird

Die rekursive Inventur ist resumierbar und begrenzt; sie protokolliert Verzeichnis-/Dateizähler, Zugriffsausnahmen und übersprungene Reparse-Points. Der Scan wurde ursprünglich unter Schema **v4** gespeichert, das persistente Reparse-Skips, Zugriffsausnahmen und Nicht-GGUF-Zähler ergänzt. Der aktuelle Quellcode enthält zusätzlich eine additive Schema-v5-Migration für optionale vollständige Datei-Hashes und exakte Qualification-Bindungen. Die reale App-Datenbank wurde in diesem Consumer-/Hash-Schritt nicht geöffnet oder migriert; der gespeicherte Scan bleibt unverändert. Siehe [resumierbare Inventur](../src/dbzscc/miro/resumable_inventory.py), [Registry und Migrationen](../src/dbzscc/miro/model_registry.py), [Discovery-Pipeline](../src/dbzscc/miro/discovery_pipeline.py) und [MIRO-Service](../src/dbzscc/app/miro_service.py).

Die GUI zeigt nun Kandidaten, Nicht-GGUF-Dateien, Parsefehler, Reparse-Skips und Zugriffsausnahmen an. Ein bloßer Nicht-GGUF-Kandidat wird nicht zum startbaren Modell. Ein fortgesetzter Scan bleibt nach erfolgreichem Abschluss nicht mehr fälschlich als aktuell „abgebrochen“ markiert; die historische Abbruchnotiz bleibt im Laufdatensatz. Siehe [MIRO-Statusanzeige](../src/dbzscc/ui/main_window.py).

## Tatsächlicher Scan

Der Lauf vom **3. November 2026** verwendete ausschließlich den konfigurierten Modellroot `D:\Models`; es waren keine Runtime-Roots konfiguriert. Lauf-ID: `9dcd5625d912409da567d73c4a5321f8`.

| Ergebnis | Wert |
|---|---:|
| Besuchte Verzeichnisse | 6.005 |
| Gesehene Einträge | 53.392 |
| Reguläre Dateien | 47.366 |
| Übersprungene Reparse-Points | 22 |
| Verweigerte Verzeichnisse | 1 |
| Modell-/Runtime-Kandidaten insgesamt | 438 |
| GGUF-Kandidaten | 260 |
| GGUF-Artefakte mit aufgelöster Identität | 243 |
| GGUF-Artefakte ohne aufgelöste Identität | 17 |
| Eindeutige Modell-IDs | 174 |
| Nicht-GGUF-Kandidaten | 178 |
| GGUF-Metadaten gelesen | 260 |
| Parsefehler | 0 |
| Laufzeitqualifikationen | 0 |
| Dauer | ca. 63 Sekunden |

Die einzige Zugriffsausnahme war `D:\Models\ModelVault\Claude\.pytest_cache` („Zugriff verweigert“). Daher lautet der gespeicherte Run-Zustand `partial`, obwohl der rekursive Scanner fertig wurde (`complete=true`, kein Fortsetzungscursor). Nach Bereinigung der Berechtigung kann ein neuer Scan den Root erneut prüfen.

Der Scan wurde mit **Schema v4** gespeichert; der aktuelle Code migriert beim nächsten normalen Registry-Öffnen additiv auf v5. Die 438 Zähler stimmen mit dem run-bezogenen Katalog überein. Diese Arbeit hat weder die reale Datenbank migriert noch den Scan neu geschrieben.

## Multimodale Paarungen und Startbarkeit

Es wurden **2 Bild-Paarungskandidaten** gespeichert, beide mit dem Status `metadata_association_candidate` und `association_only_compatibility_unestablished`; keiner ist runtime-getestet oder qualifiziert. Die Pairing-Hinweise enthalten SHA-256-Werte der GGUF-Metadatenpräfixe; das ist **kein Vollhash**. Die neue On-demand-Funktion für ausgewählte Modell-/Projektor-Paare wurde auf diesem realen Scan nicht ausgeführt.

Da `runtime_roots` leer waren, wurde kein Runtime-Binary geprüft oder gestartet und es konnte keine Runtime-Qualifikation entstehen. Die beiden Paarungen dürfen deshalb nicht als nachgewiesen nutzbare Bildmodelle ausgegeben werden.

## Offene Punkte

Der Inventurscan rechnet weiterhin ausschließlich den begrenzten Metadatenpräfix-Hash. Ein vollständiger SHA-256 wird nun bei expliziter Modellauswahl gestreamt und vor Start/Qualification erneut geprüft; er wird nicht automatisch für alle großen Bestandsdateien berechnet. Auf dem realen Scan wurde kein Vollhash persistiert. Runtime-Roots und echte Modell-/Projektor-Laufzeitproben fehlen weiterhin. Siehe [Implementierungsplan](DBZSCC2611_IMPLEMENTATION_PLAN.md) und [Consumer-/Hash-Evidenz](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md).

## Tests

Die vollständige Suite meldete **254 bestanden, 0 übersprungen**; 176 LangChain-Deprecation-Warnungen unter Python 3.15.2, keine Testfehler. Die fokussierten Consumer-Regressionen meldeten **105 bestanden**. **Ruff 0.17.3: alle Prüfungen bestanden.** Migrations-, Vollhash-, Qualification-, Stale-Start- und Consumer-Tests sind enthalten; siehe [Registrytests](../tests/test_model_registry.py), [Katalogtests](../tests/test_registry_catalog.py), [Runtime-Service](../tests/test_runtime_service.py), [UI](../tests/test_ui_smoke.py) und [Workflow](../tests/test_workflow_runtime.py).

Es wurde weder committet noch gepusht.
