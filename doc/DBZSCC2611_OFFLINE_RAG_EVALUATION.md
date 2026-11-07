# DBZSCC 2611: Offline-RAG-Evaluation

## Zweck und Grenze der Aussage

Diese Evaluation ist ein reproduzierbarer Regressionstest für den vorhandenen lokalen RAG-Pfad, kein repräsentativer Test echter Nutzerprojekte. Sie verwendet ausschließlich ein eigens geschriebenes, synthetisches Korpus. Kein ausgewähltes Nutzerprojekt und keine persönlichen Dokumente werden gelesen, indexiert oder in die Messung einbezogen.

Das eingefrorene Fixture `tests/fixtures/rag_eval/evaluation.json` enthält 24 kurze Quelldateien: 18 Dateien mit je einer festgelegten relevanten Antwort und sechs Ablenkungsdateien. Die 18 Abfragen sind gleichmäßig auf Deutsch, Englisch und code-orientierte Fragen verteilt (je sechs). Jede Abfrage hat genau einen relevanten Quellpfad. Die Ground Truth bewertet Quelldateien, nicht einzelne überlappende Chunks.

Der Fixture-SHA-256 lautet `2cb2764db01e41d37e78464739b4729d45fd23488e184f330cfd9e1384d76fd9`. `tests/fixtures/rag_eval/targets.json` bindet diesen Hash und markiert die Ziele ausdrücklich als vor der Messung eingefroren. Der Runner stoppt mit Fehler, wenn der Fixture-Hash oder das festgelegte Protokoll nicht passt. Eine spätere Änderung am Korpus benötigt eine neue Suite-Version und neue, vor der nächsten Messung dokumentierte Ziele; bestehende Grenzwerte werden nicht nachträglich angepasst.

## Vorab festgelegte Abnahmeziele

Retrievalmetriken werden pro Frage binär über eindeutige Quelldateien berechnet und anschließend als Makromittel zusammengefasst. Die Gesamtwerte und alle drei Sprach-/Codegruppen müssen ihre eigenen Grenzwerte erreichen.

| Metrik | Gesamtziel | Mindestziel je Gruppe |
|---|---:|---:|
| Recall@1 | ≥ 0,67 | ≥ 0,50 |
| Recall@3 | ≥ 0,89 | ≥ 0,83 |
| Recall@5 | ≥ 0,95 | ≥ 0,83 |
| MRR@5 | ≥ 0,83 | ≥ 0,75 |
| nDCG@5 | ≥ 0,90 | ≥ 0,80 |

`Recall@k` ist der Anteil relevanter Quelldateien in den ersten k eindeutigen Treffern. `MRR@5` ist der Kehrwert des Rangs des ersten relevanten Treffers (0, wenn keiner in den ersten fünf liegt). `nDCG@5` verwendet binäre Relevanz und den üblichen logarithmischen Rangrabatt; da es pro Frage genau einen relevanten Pfad gibt, beträgt der ideale DCG 1. Es wird kein Treffer-Rang aus mehrfachen Chunks derselben Datei künstlich vervielfacht.

Latenzgrenzen für den vollständigen lokalen App-Aufruf:

- Suchaufrufe: p50 ≤ 5.000 ms und p95 ≤ 15.000 ms über 18 Aufrufe (ein Aufruf pro Abfrage).
- Indexierung: der einmalige vollständige Index-Aufruf ≤ 60.000 ms. Für eine Einzelmessung werden keine p50-/p95-Werte behauptet.
- Perzentile verwenden lineare Interpolation an Position `(n - 1) * p` in den sortierten Messwerten.

Die Zeitmessung beginnt unmittelbar vor `LocalRagWorkerClient.index_workspace` beziehungsweise `search_workspace` und endet nach Rückkehr und Validierung der Antwort. Der Client startet für jeden Auftrag den fest verdrahteten JSON-Worker als eigenen Prozess. Daher umfasst die Suchzeit Prozessstart, lokale Modell-/Tokenizer-Hashprüfung, ONNX-Session-Aufbau, Query-Encoding, Retrieval und Antwortvalidierung. Der feste Worker liefert keine internen Phasenzeiten; Embedding-, FTS5-, Dichte-Suche- und RRF-Teillatenzen werden folglich weder geschätzt noch behauptet. Die Messwerte sind Windows-/Hardware- und Cachezustands-spezifisch.

Wenn Worker, Modell oder Ressourcen die Durchführung verhindern, ist das Resultat **inconclusive**. Verfehlte Qualitäts- oder Zeitgrenzen bleiben **fail**; kein Ziel wird nach Sichtung der Resultate abgesenkt. Auch ein vollständiger Pass belegt nur dieses kleine synthetische Fixture und keine allgemeine Retrievalqualität.

## Umsetzung und Wiederholung

`tools/rag_eval.py` materialisiert ausschließlich die JSON-Fixturedateien unter einem neu angelegten `.work/rag-evaluation/run-*/synthetic-corpus` und übergibt genau diesen absoluten Ordner an `LocalRagWorkerClient`. Pro Lauf bleiben synthetischer Workspace, SQLite-Index, rohe Suchlatenzen, zurückgegebene Pfade und `results.json` im Run-Unterordner erhalten. Es gibt weder Download noch Netzwerkzugriff; die Worker-Bindung muss `intfloat/multilingual-e5-small` und `CPUExecutionProvider` bestätigen.

Vorab-Prüfungen, ohne Modellstart:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_rag_eval.py
.venv\Scripts\ruff.exe check tools\rag_eval.py tests\test_rag_eval.py
```

Echte lokale CPU-Messung über den App-Client und dessen festen lokalen Python-3.14-JSON-Worker (im Checkout `.work/venv314`; in einem eingefrorenen Paket der gebündelte Sidecar):

```powershell
.venv\Scripts\python.exe tools\rag_eval.py
```

Die Ausgabe nennt den jeweiligen `results.json`-Pfad. Ein Exitcode 0 bedeutet, dass alle eingefrorenen Ziele bestanden wurden; 2 bedeutet mindestens ein gemessenes Ziel wurde verfehlt; 3 bedeutet, dass die Messung nicht vollständig durchgeführt werden konnte.

## Quellen und Entscheidungen

- `doc/DBZSCC2611_IMPLEMENTATION_PLAN.md`, Abschnitt „F. Lokale RAG-Baseline“, verlangt ein eingefrorenes Deutsch-/Englisch-/Code-Eval mit Recall@K, MRR/nDCG sowie Latenz p50/p95 und fordert ausdrücklich, Zielwerte vor der Messung festzulegen.
- `doc/DBZSCC2611_OFFLINE_RAG_CPU_BASELINE.md` beschreibt den bestätigten lokalen E5-ONNX-/FTS5-/RRF-Pfad, den fest verdrahteten JSON-Worker und die Grenze bisheriger synthetischer Smoke-Tests. Dort ist auch die offizielle Modellkarte von `intfloat/multilingual-e5-small` verlinkt.
- Die geprüften Implementierungsverträge liegen in `src/dbzscc/app/local_rag_service.py`, `src/dbzscc/rag/worker.py`, `src/dbzscc/rag/index.py`, `src/dbzscc/rag/embeddings.py` und `src/dbzscc/rag/workspace.py`. Sie begründen den End-to-End-Aufruf, die Fünf-Treffer-Grenze, E5-Präfixe, Projektbindung, CPU-Ausführung sowie den Quellpfad-/Chunkvertrag.
- Das Testfixture ist selbstgeschrieben und enthält keine externe oder reale Projektdatenquelle. Die Grenzwerte sind vor dem ersten echten Worker-Aufruf in `targets.json` festgehalten; sie sind Abnahmekriterien für diese Regressionsevaluation, keine aus externen Benchmarks abgeleiteten Produktversprechen.

## Vorab-Prüfungen

Vor dem echten Worker-Lauf bestanden die vier neuen Fixture-/Metriktests (`tests/test_rag_eval.py`) mit **4 passed**. Ruff meldete für `tools/rag_eval.py` und `tests/test_rag_eval.py` **All checks passed**. Diese Prüfungen starten kein Modell und enthalten keine Mikrofon-/Speech-Tests.

## Messergebnis

Der eingefrorene CPU-Worker-Lauf ist abgeschlossen und hat alle vorab gesetzten Ziele bestanden. Messwerte und Aussagegrenzen folgen unmittelbar darunter.


## Tatsächliches Messergebnis — 2026-11-05

**Gesamtergebnis: PASS.** Alle 23 vorab eingefrorenen Retrieval- und Latenzprüfungen bestanden; die Grenzwerte wurden nicht verändert. Der reale App-Client startete den festen lokalen JSON-Worker und bestätigte `intfloat/multilingual-e5-small` mit `CPUExecutionProvider`. Der ausschließlich synthetische Workspace enthielt 24 Dateien und ergab 33 indexierte Chunks; es wurden keine versteckten oder ausgeschlossenen Pfade beobachtet.

| Gruppe | Recall@1 | Recall@3 | Recall@5 | MRR@5 | nDCG@5 | Gruppen-Gate |
|---|---:|---:|---:|---:|---:|---|
| Deutsch (6 Fragen) | 1,000 | 1,000 | 1,000 | 1,000 | 1,000 | bestanden |
| Englisch (6 Fragen) | 0,500 | 1,000 | 1,000 | 0,750 | 0,815 | bestanden, jeweils Mindestwert |
| Code (6 Fragen) | 1,000 | 1,000 | 1,000 | 1,000 | 1,000 | bestanden |
| **Gesamt (18 Fragen)** | **0,833** | **1,000** | **1,000** | **0,917** | **0,938** | **bestanden** |

Drei der sechs englischen Fragen fanden ihre relevante Datei auf Rang 2; die übrigen drei auf Rang 1. Damit liegt Englisch bei Recall@1 und MRR genau auf dem vorab gesetzten Mindestwert. Das ist ein nützlicher Hinweis für spätere natürliche Korpora, aber keine ausreichende Basis für eine Qualitätsoptimierung oder eine breite Sprachleistungsbehauptung.

Die gemessene Indexierung dauerte **2.695,3 ms** (Einzelmessung; Ziel ≤ 60.000 ms). Für die 18 vollständigen Such-Worker-Aufrufe ergaben sich **p50 1.990,7 ms** und **p95 2.052,4 ms** (Zielwerte ≤ 5.000 bzw. ≤ 15.000 ms; Messbereich 1.934,6–2.070,4 ms). Je Gruppe: Deutsch p50/p95 **1.941,2/1.997,4 ms**, Englisch **1.991,9/2.064,1 ms**, Code **2.008,0/2.040,7 ms**. Diese Zeiten umfassen pro Suche Prozessstart, lokale Modell-/Tokenizer-Prüfung, ONNX-Session-Aufbau und Antwortverarbeitung. Interne Teillatenzen wurden nicht erhoben; **in diesem ersten Lauf** wurden CPU-Zeit und Prozessspeicher-Spitzen nicht gemessen.

Die Messung lief auf Windows 11 mit Runner Python 3.15.2 und 12 gemeldeten logischen Prozessoren; der Client verwendete den festen Worker unter `.work\venv314\Scripts\python.exe`. Die vollständigen per-Abfrage-Ranglisten, Rohzeiten, Ziele und Zielprüfungen stehen im gespeicherten [JSON-Ergebnis](../.work/rag-evaluation/run-ff3tfn68/results.json); Korpus und Index liegen im selben `.work`-Run-Ordner. Wiederholung: `.venv\Scripts\python.exe tools\rag_eval.py`.

Dieser PASS gilt ausschließlich für das kleine, selbst erstellte, hashgebundene Fixture. Eine Frage pro Query, sechs Fälle pro Gruppe und ein einzelner Suchlauf pro Frage reichen nicht aus, um p95 auf anderen Maschinen oder Retrievalqualität in echten Nutzer-Workspaces zu verallgemeinern. Kein Nutzerprojekt wurde für diese Messung gescannt.

## Wiederholung mit Prozessressourcen — 2026-11-05

Nach Ergänzung der Windows-Prozessbeobachtung lief dieselbe unveränderte, hashgebundene Evaluation erneut ausschließlich auf dem synthetischen Fixture: **PASS, 23 von 23 Gates**. Die Retrievalwerte blieben gleich. In diesem Lauf dauerte die Einzelindexierung **3.657,2 ms**; Such-p50/p95 lagen bei **1.962,3/2.162,0 ms**. Die Abweichung zur ersten Latenzmessung ist Lauf-zu-Lauf-Variation, kein neu gesetzter Grenzwert.

Für alle **19 von 19** Workerprozessen (eine Indexierung, 18 Suchen) lieferten `GetProcessTimes` und `GetProcessMemoryInfo` Messwerte. Über diese einzelnen Prozesslebensläufe lagen CPU-Zeit min/Median/max bei **1,891/1,938/3,719 s**, Peak-Working-Set bei **878,0/878,2/878,5 MiB** und Peak-Commit bei **1.387,7/1.388,0/1.388,3 MiB**. Der Indexierungsprozess allein meldete 3,719 s CPU-Zeit, 878,0 MiB Peak-Working-Set und 1.388,3 MiB Peak-Commit. Das sind echte Windows-Prozesszähler, aber keine CPU-Auslastung in Prozent und keine Gesamtwerte der App oder des Systems; die Spitzen umfassen auch Interpreter- und Modellaufbau. Eine Ressourcen-Zulassungsentscheidung nutzt diese Werte noch nicht. Wenn die API nicht verfügbar ist, bleiben die Zahlen ausdrücklich `null`/`unknown`.

Der neue Rohdatensatz liegt in [results.json](../.work/rag-evaluation-resource/run-c1xh4cvc/results.json); er enthält jede Index-/Suchlatenz zusammen mit den Prozesswerten. Das Nutzerprojekt wurde nicht gescannt. Die Messung belegt weiterhin keinen Installer: das Bundle-Layout ist synthetisch getestet, aber kein eigenständiges Paket wurde gebaut und Ende-zu-Ende ausgeführt.
