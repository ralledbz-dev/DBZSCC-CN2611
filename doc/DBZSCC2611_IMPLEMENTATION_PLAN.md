# DBZSCC 2611 – Implementierungsplan aus den vier Architekturpapieren

**Stand:** 3. November 2026
**Zweck:** Einzelaufgaben, Funktionen, Abhängigkeiten und Abnahmekriterien aus den Modell-/Backend-, Ressourcen-, Triple-Backend- und RAG-Unterlagen extrahieren und in eine umsetzbare Roadmap ordnen.

## Leitentscheidungen

1. CPU, CUDA und Vulkan sind getrennte Ausführungspfade. Es gibt keinen gemeinsamen/additiven VRAM-Pool: `cuda:0` und `vulkan:1` bezeichnen dieselbe NVIDIA-GPU; `vulkan:0` ist die AMD-iGPU mit UMA/Shared Memory, das gegen Host-RAM/Commit mitgezählt wird.
2. Modelldatei, Metadaten, Fähigkeit, Startbarkeit und Qualitätsnachweis sind verschiedene Evidenzzustände. Ein Dateiname, GGUF-Metadatum oder erfolgreicher Ladevorgang allein beweist keine vollständige Nutzbarkeit.
3. Ein deterministischer App-Dienst entscheidet über Ressourcen, Prozesse, Endpunkte und Freigaben. Ein LLM/LangChain-Workflow darf nur typisierte Vorschläge aus einer festen Aktionsliste liefern.
4. Jede Modell-/Backend-/Parameterkombination wird separat qualifiziert. RAG-Embeddings und Reranker brauchen eigene Artefakte und Qualitätstests; generische llama.cpp-APIs belegen keine Retrievalqualität.
5. Phase 1 bleibt lokal und offline. Keine Treiber-, Firewall-, globalen PATH- oder Systemänderungen. Keine automatisch beschafften Modelle. Speculative Decoding, Qdrant und ein großer Reranker bleiben zurückgestellt, bis Bedarf und Evidenz vorliegen.

## Arbeitsauftrag in Teilaufgaben

### A. Zentrale Modellidentität und Evidenz-Registry

**Ziel:** Ein persistenter, gemeinsamer System-of-Record für Modellartefakte und deren Evidenz; kein eigener Scan-/Datenbestand pro Consumer.

- Rekursiver Scan konfigurierter Wurzeln mit Zählung, Resume/Incremental-Scan, Root-Containment, Reparse-Point-Schutz und sichtbaren Zugriffsausnahmen.
- Für GGUF: Dateipfad, Größe, SHA-256, begrenztes Metadaten-Parsing, Architektur, Quantisierung, Tensor-/Metadateninfos, Kontextangaben und Parser-Version persistieren.
- Nicht-GGUF-Artefakte als Artefakte/Kandidaten erfassen, aber `.safetensors`, `.bin`, `.pt`, `.pth` oder `.onnx` nicht ohne Gewichts-/Rollenprüfung als eigenständige startbare Modelle ausgeben.
- Evidenzzustände explizit trennen: `discovered`, `parsed`, `capability_evidence_only`, `runtime_candidate`, `runtime_tested`, `qualified`, `failed` sowie `unknown`; jede Qualifikation an Modellhash, Runtime-/Buildhash, Backend, Gerät und getestete Parameter binden.
- N/G/E/X/U-Kategorien erhalten: nachweislich unterstützt, grundsätzlich aber ungetestet, eingeschränkt, nicht unterstützt, unbekannt. Quelle, Zeitpunkt, Probe, Ergebnis und Grenze jeder Aussage speichern.
- Vorhandene CPU-E2E-Evidenz für Qwen2.5-0.5B-Instruct sowie fehlgeschlagene CUDA-/Vulkan-Smokes als getrennte Beobachtungen persistieren. Fehler nicht als Modellunverträglichkeit ausgeben, solange nur ein Timeout belegt ist.
- Zentrale typed API für Discovery, Picker, Chat, Workflow-Planer, Ressourcenplaner und Runtime-Service; Consumer filtern nach ihrem Bedarf, erfinden oder überschreiben aber keine Evidenz.

**Abnahme:** Wiederholter Scan liefert stabile Identität/Hash; Metadaten/Beobachtungen bleiben nach Neustart erhalten; veränderte Datei, Runtime oder Backend invalidiert alte Qualifikation. Zugriff verweigert, Reparse-Point oder Parsefehler sind explizit ausgewiesen. Kein Consumer führt einen eigenen alternativen Modellbestand.

### B. Multimodales Modell- und Projektor-Pairing

**Ziel:** Passende Sprach-GGUF- und `mmproj`-GGUF-Paare über Ordnergrenzen hinweg finden, aber Kandidaten nicht mit funktionsfähigen Paaren verwechseln.

- Beide Artefakte unabhängig hashen und Metadaten lesen; Modellfamilie, Architektur und explizite Kompatibilitätsfelder vergleichen.
- Cross-folder Paarung unterstützen. Dateiname oder pauschale Gleichheit eines Projektortyps reichen nicht als Beleg.
- `image`, `audio` und gegebenenfalls Video als getrennte Fähigkeitsdimensionen speichern.
- Pairing-Zustand mindestens `candidate`, `metadata_compatible`, `runtime_pair_tested`, `qualified`/`failed`; nur vollständiger Test mit passendem Projector und echter Probe darf als nutzbar erscheinen.
- Vision-Abnahme: korrekt gebundenes GGUF+`mmproj` laden, Testbild mit bekanntem Inhalt einspeisen, Antwortabschluss und Speicherpeaks prüfen. Audio entsprechend mit realer Audioeingabe/Audioausgabe und getrenntem Profil testen.

**Abnahme:** Positive und negative Cross-folder Tests, keine falschen Family-Matches, keine Startfreigabe für bloße Kandidaten. Image-/Audio-Evidenz bleibt unabhängig. Modell- und Projectorhash sind Teil des Runtime- und Ressourcen-Fingerprints.

### C. Hardware-, Build- und Target-Inventar

**Ziel:** Backend-/Gerätezuordnung aus echten Build- und Adapterdaten statt UI-Reihenfolge oder Namen ableiten.

- Für jedes Runtime-Binary Manifest aus llama.cpp-Version/Commit, Dateipfad, SHA-256, Lizenz, Buildtyp, verfügbaren Flags/Version und DLL-Abhängigkeiten erstellen.
- Targets getrennt erfassen: CPU, CUDA-NVIDIA, Vulkan-AMD, Vulkan-NVIDIA. Laufzeitordinale über verifizierte stabile Adapteridentität/LUID binden; `vulkan:1` nicht als zusätzliche NVIDIA-Speicherreserve zählen.
- GPU-Layer-Offload, Flash Attention, KV-Typ, Embedding-, Reranking- und Multimodaloptionen als build-/versionsspezifische Fähigkeiten aufnehmen.
- Erst nach Hardware-/Treiber-/Runtime-Validierung erlauben; fehlende oder veraltete Telemetrie ergibt `DEFER_TELEMETRY`, nicht „freie Kapazität“.

**Abnahme:** Jede Startkonfiguration benennt Runtime, Backend, Gerät, Modellhash und Parameter. CUDA0/Vulkan1 teilen dieselbe NVIDIA-Identität. Shared Memory bleibt Host-backed und wird nicht additiv dargestellt.

### D. Ressourcenplaner und Runtime-Lifecycle

**Ziel:** Ein einziger deterministischer Admission-Punkt vor jedem Modell-/Devserver-Prozess.

- Frische Snapshots mit Zeit, Quelle, Qualität und Fehlergrund für Host-RAM/Commit, CPU-Last/Threads, dediziertes NVIDIA-Memory, AMD-Shared-/Local-Budget, Prozess-/Portzustand.
- Atomare Admission mit Zuständen `ADMIT`, `QUEUE`, `REJECT_CONFIG`, `REJECT_CAPACITY`, `DEFER_TELEMETRY`; Modell- und Ressourcenwahl bleiben deterministisch, nicht LLM-gesteuert.
- Reservierungsledger und Leases `LOAD`, `RUNTIME`, `REQUEST`; keine Doppelzählung von AMD-Shared-RAM und keine Doppelbelegung CUDA0/Vulkan1. Queue begrenzen, FIFO/Fairness/Deadline definieren, aktive Jobs nie durch Scheduler killen.
- Ein einziger Prozess-Owner startet/stoppt nur app-eigene Prozesse. Health/Identität über `/health` und, wenn Build vorhanden, `/props`/`/v1/models`, PID, Run-ID, Port und erwartetes Modell abgleichen.
- Readiness unterscheidet Laden/Health/Fehler. Fehlgeschlagener Healthcheck hält Ressourcen bis bestätigtem Prozessende reserviert.
- Ereignisse enthalten Korrelation, Status, Ressourcenclaims/Fingerprints und Zeit, nicht Secrets, komplette Prompts oder Antworten.

**Abnahme:** Konkurrenztest lässt nur eine sichere Reservation zu; Live-PID wird nicht freigegeben; Timeout/Abbruch ist kein erfolgreicher Start; Port, PID, Modell, Backend und Ressourcenplan sind an `run_id` gebunden. Serialer `parallel=1`-Betrieb bleibt Standard, bis Slot-/KV-Peaks belegt sind.

### E. Modellrollen und Triple-Backend-E2E

**Ziel:** Rollen auf vorhandene qualifizierte Artefakte abbilden und jede Kombination mit vollständigem Output belegen.

- Erst vorhandenes kleines Textmodell/CPU-Baseline; danach NVIDIA-CUDA für kleine Modelle oder gezielte Vision nur nach Ressourcenfreigabe; AMD/Vulkan zunächst experimenteller Worker mit begrenzter Last.
- Rollenbeispiele aus den Papieren: `PRIMARY_CHAT`, `CODING_ORCHESTRATOR`, `CODE_REVIEWER`, `SUBTASK_WORKER`, `PROMPT_ENHANCER`, `VISION_INSPECTOR`, `RAG_EMBEDDING`, `RAG_RERANKER`.
- Modellpläne begrenzen Kontext, Batch/UBatch, Threads, GPU-Layer, Tokenlimit, Parallelität und Stopbedingungen. Rollenwahl darf weder Toolrechte noch Sicherheitsregeln verändern.
- E2E bedeutet vollständige abgeschlossene Antworten, korrekten Stop-Grund, persistierte Modell-/Backend-/Parameter-/Ressourcenmetadaten und kontrolliertes Ende des eigenen Prozesses. Ein Lade- oder HTTP-Health-Erfolg allein reicht nicht.
- Speculative Decoding bleibt `DEFER`: kompatibles Target/Draft-Tokenizerpaar, gleiche Profilparameter, Draft-Gerätebindung, vollständiger Output, Peakmessung und A/B-Vorteil sind Voraussetzungen für spätere Freigabe.

**Abnahme:** Zwei unabhängige vollständige Antworten pro freizugebender Kombination; Peaks bei Load, Prefill, Generation und Vision. Keine allgemeine Leistungszusage aus Einzeltest ableiten.

### F. Lokale RAG-Baseline

**Ziel:** Offline-Retrieval als eigenständige CPU-Baseline, ohne neue Parallelarchitektur oder unnötige GPU-Konkurrenz.

1. Multilingual-e5-small als CPU-ONNX-Embedding-Baseline; `query: ` und `passage: ` exakt verwenden, dokumentiertes Pooling/L2-Normalisierung/Trunkierung. CPU-Threads und Host-RAM gehen in dieselbe Ressourcenpolicy ein.
2. SQLite FTS5/BM25 als lexikale Suche; eingebettete Vektorsuche nur mit freigegebener, hashgeprüfter und ABI-kompatibler SQLite-Extension. Ohne Extension nur expliziter, messbarer blockweiser CPU-Cosine-Pfad für kleine Korpora.
3. Getrennte lexikale/dichte Kandidatenlisten mit Reciprocal Rank Fusion (RRF); Scores diagnostisch trennen, keine willkürliche BM25-Skalierung als Reranker ausgeben.
4. Projektbezogener Datei-Allowlist-Scanner; `.git`, `.venv`, versteckte Dateien, Build-/Cache-/Backupverzeichnisse sowie `.env`, Tokens, Schlüssel, Zertifikate und Credentials ausschließen. Keine Inhalte, Hashes oder Chunks ausgeschlossener Dateien loggen/indexieren.
5. Chunking: Python über AST/Symbole; Markdown entlang Überschriften; andere Sprachen nur mit tatsächlichem Parser bzw. klar bezeichnetem Fallback. Jeder Chunk mit Projekt-ID, kanonischem Relativpfad, Symbol/Zeilen, Sprache/Format und Inhalts-Hash.
6. Unveränderliche Indexgenerationen mit Embedding-/Tokenizer-/ONNX-/Parser-/Chunker-Fingerprint. Rebuild separat erzeugen und validieren; aktiven Zeiger erst nach Erfolg atomar umschalten. Unterbrochener Rebuild lässt alte Generation intakt.
7. Quellenversehener Kontext mit Pfad/Symbol/Zeilen. Optionaler Cross-Encoder nur nach Testset-/Qualitäts- und Ressourcenabnahme, Top-K begrenzt, RRF-Fallback bei Timeout.
8. Qdrant erst nach belegtem Skalierungs-, Mehrprozess- oder HNSW-Bedarf; kein zusätzlicher Dienst als Phase-1-Standard.

**Abnahme:** Secret-/Exclude-Audit; Embedding-Vertrag; Offline-Kaltstart ohne Download; Index-Fingerprint/Invalidierung; Atomizität des Rebuilds; eingefrorenes deutsch/englisch/Code-Eval mit Recall@K, MRR/nDCG sowie p50/p95 nach Stufe. Zielwerte vor Messung festlegen.

## Priorisierte Phasen und Aufgabenpakete

| Phase | Einzelaufgaben | Abhängigkeiten | Ergebnis / Gate |
|---|---|---|---|
| **0 – Belegbarer Bestand** | Modelldatei-/Runtime-Manifest; vollständiger rekursiver Scanner; GGUF-Parsergrenzen; Registry-Migration und zentrale API; N/G/E/X/U-Evidenzschema | Keine; Bestand ist Datenbasis | Wiederholbarer Scan und persistente Evidenz; keine unbelegten Fähigkeiten |
| **1 – Paarung und Qualifikation** | multimodale Cross-folder Paarung; per-Backend Startproben; Hash-/Template-/Kontext-/Parameterfingerprints; Fail-/Timeout-Evidence | Phase 0 | Modellpicker zeigt „gefunden“, „geparst“, „Kandidat“, „qualifiziert“, „Pair getestet“ getrennt |
| **2 – Sichere Runtime-Admisssion** | Targetbindung; frische Telemetrie; atomare Admission/Reservations/Queue/Leases; Runtime lifecycle/health/stop; Eventkorrelation | Phasen 0–1 | Jeder Start kontrolliert und korrelierbar; keine Doppelzählung oder fremde Prozessübernahme |
| **3 – Verbraucher-Verdrahtung** | Chat, Model Picker, LangChain-Workflow, Projekt-Run/Debug, MIRO und (später) RAG lesen zentralen Bestand; typed capability queries; UI Status/Erklärungen | Phasen 0–2 | Kein Consumer verwendet einen privaten/parallel gepflegten Modellbestand |
| **4 – RAG CPU-Baseline** | E5-ONNX, Tokenizer/Manifest, FTS5, Chunking, Vektoren, RRF, atomare Indexgeneration, Quellenanzeige | Phasen 0–3; lokale Artefakte freigegeben | Offline-Retrieval ohne GPU-VRAM-Konkurrenz, evaluierbar und invalidierbar |
| **5 – Qualität/Optimierung nur bei Bedarf** | Reranker-A/B; CUDA-/Vulkan-Profile; Mehrserver-Leases; spätere Spekulation; optionale Skalierung/Qdrant | Stabiler Phase-4-/Runtime-Basispfad, konkrete Qualitäts-/Skalierungsbegründung | Nur gemessene, vollständige Profile erhalten „GO“; Rest bleibt DEFER/UNKNOWN |

## Aktueller DBZSCC-2611-Stand und unmittelbare nächste Schritte

- Vorhanden sind MIRO-Inventur/Metadaten/SQLite-Registry, konservative Capability-Evidenz, Resource-Orchestrator-Grundlagen, lokale Runtime-/Chat-Schnittstellen, manifestgebundene Python-/PySide6-Prozessläufe, LangChain-Workflow-Grundlage und die abgesicherte Diff-/Approval-Schleife.
- Nachgewiesen sind der lokale Qwen-CPU-Chat im UI-SSE-Pfad und ein manifestgebundener PySide6-Debugprozess. Die getrennten CUDA-/Vulkan-Smokes liefen jeweils in ein 90-Sekunden-Timeout und sind **nicht** als erfolgreiche Inferenz zu werten.
- Die zentrale rekursive MIRO-Registry und die Consumer-Verdrahtung sind implementiert. Schema v4 persistiert kumulative Scanmetriken einschließlich Nicht-GGUF-, Reparse- und Zugriffsfehlerzählern; GUI und Laufstatus lesen dieselbe typisierte Registry.
- Die lokale RAG-CPU-Baseline (E5/ONNX, FTS5/RRF, atomare projektgebundene Indexgeneration) ist implementiert und über einen festen lokalen Worker mit manuell bestätigter Chat-Nutzung verdrahtet. Das eingefrorene synthetische Deutsch-/Englisch-/Code-Regressionseval mit 18 Fragen bestand alle vorab gesetzten Retrieval- und Latenzgates; die erste Messung hatte Gesamt-Recall@1/3/5 `0,833/1,000/1,000`, MRR@5 `0,917`, nDCG@5 `0,938`, Such-p50/p95 `1.991/2.052 ms`. Eine erneute Messung erfasste zusätzlich echte Worker-Prozess-CPU-Zeit, Peak-Working-Set und Peak-Commit; der Bundle-Pfad ist per synthetischem Layout-Test abgedeckt. Details, Grenzen und Rohdaten stehen in `DBZSCC2611_OFFLINE_RAG_EVALUATION.md` und `DBZSCC2611_OFFLINE_RAG_CPU_BASELINE.md`. Diese Tests belegen keine allgemeine Qualität für Nutzerprojekte. Die Ressourcenwerte fließen noch nicht in eine RAG-Zulassungs-/Lastbegrenzung ein, und ein eigenständiger Installer wurde nicht gebaut oder Ende-zu-Ende getestet. Reranker und echtes multimodales Bild-/Audio-E2E bleiben offen.
- Der echte Scan am 03.11.2026 erfasste 6.005 Verzeichnisse und 438 Kandidaten; Details und Einschränkungen stehen in `MIRO_RECURSIVE_SCAN_VALIDATION.md`. Ein Zugriffsausnahmefall markiert den Lauf als Teilresultat, obwohl kein Fortsetzungscursor offen ist.
- Nächste Schritte: (1) die gemessene RAG-CPU-/RAM-Last in die Ressourcenpolicy einordnen und ein Bundle mit cp314-Sidecar, Worker und Modell bauen und Ende-zu-Ende testen; (2) über vollständige SHA-256 großer GGUF-Gewichte sowie Runtime-Roots und echte Modell-/Projektor-Qualifikation entscheiden; (3) echtes Bild-/Audio-E2E erst nach separaten lokalen Modellen und Tests angehen.

## Quellenumfang

Dieser Plan extrahiert Entscheidungen und Abnahmekriterien aus:

- `DBZSCC_MODEL_COMPATIBILITY_MATRIX.md` – Evidenzklassen, Backend-/Hardwarezuordnung, Modellrollen, multimodale und Runtime-Abnahme.
- `DBZSCC_RESOURCE_SCHEDULING_DESIGN.md` – Admission, Telemetrie, Speicherbilanz, Queue, Leases, Zustandsautomat und Runtime-Events.
- `DBZSCC_TRIPLE_BACKEND_RESEARCH.md` – Managemententscheidung, Rollen-/Backendstrategie, Speculative-Decoding-Defer und Phasenroadmap.
- `DBZSCC_RAG_BACKEND_RECOMMENDATION.md` – CPU-ONNX-E5, FTS5, SQLite-Vektorindex, RRF, sichere Workspace-Indexierung, Atomizität, Evaluation und Deferred-Komponenten.

Die vier Papiere sind Architektur-/Rechercheunterlagen (Stand 31.10.2026), keine Leistungs- oder Implementierungsnachweise. Dieser Plan trennt deren Empfehlungen von den aktuell beschriebenen lokalen Tests.
