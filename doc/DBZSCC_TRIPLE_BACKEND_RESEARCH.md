# DBZSCC – Triple-Backend-Research und Managemententscheidung

**Recherchezeitpunkt:** 31.10.2026  
**Referenz:** llama.cpp `b8842`, Release vom 19.04.2026 / Commit `455d8e4be85aaae75195435a513a2a5dbef0136c`  
**DBZS-Arbeitsstand:** `bcdf5d28ae5686b2e4cc48f7ceca7b278623315c` (dirty; nicht verändert)  
**Status:** Finaler Fachbericht aus den vorhandenen Befunden A, B und E. Es wurden für diesen Bericht keine Runtimes, Modelle, Tests, Downloads, Installationen oder Systemkonfigurationen gestartet bzw. verändert.

## Management-Entscheidung in einem Satz

DBZS soll die vorhandenen CPU-, CUDA- und Vulkan-Runtimes **deterministisch koordinieren**, aber nicht als drei addierbare Beschleuniger behandeln: NVIDIA-Vision läuft nur bedarfsweise mit bestätigtem `mmproj`, AMD/Vulkan0 bleibt ein experimenteller, begrenzter Worker, große Coding-Modelle laufen CPU-/Hybrid-gebunden on demand; **Speculative Decoding bleibt in Phase 1 DEFER**.

Der Nutzen einer koordinierten Architektur liegt in reproduzierbarer Rollenwahl, klarer Ressourcenadmission, sicheren Fallbacks und weniger Fehlstarts – nicht in einer Garantie paralleler Tokenraten oder einer automatischen Beschleunigung.

Weiterführende Entwürfe: [Modell- und Hardwarematrix](DBZSCC_MODEL_COMPATIBILITY_MATRIX.md), [Ressourcen-Scheduling](DBZSCC_RESOURCE_SCHEDULING_DESIGN.md), [RAG-Empfehlung](DBZSCC_RAG_BACKEND_RECOMMENDATION.md), [Fehler- und Sicherheitsanalyse](DBZSCC_FAILURE_SECURITY_ANALYSIS.md) sowie [CUE-Implementierungsfahrplan](DBZSCC_CUE_IMPLEMENTATION_RECOMMENDATIONS.md).

---

## 1. Entscheiderübersicht

| Entscheidungsfeld | Entscheidung | Begründung | Technische Konsequenz |
|---|---|---|---|
| Runtime-Basis | Genau drei getrennte b8842-Builds als eigene Backendpfade behandeln | Die lesende lokale Dateiinventur im Fachbefund A belegt getrennte CPU-, CUDA-13.1- und Vulkan-Verzeichnisse; CUDA enthält kein `ggml-vulkan.dll`, Vulkan kein `ggml-cuda.dll`. | Kein einzelnes Modell über CUDA0 plus Vulkan0 splitten; unterschiedliche Server sind getrennte Prozesse mit eigenen Gewichten, KV-Caches und Buffern. [1] |
| NVIDIA GTX 1650 SUPER, 4 GB | Nicht als dauerhafter Universal-Orchestrator einsetzen | Q4-Gewichte allein erfassen weder KV-Cache, Compute-/Allocator-Buffer noch Visionprojektor. CUDA0 und Vulkan1 adressieren dieselbe 4-GB-Karte. | NVIDIA nur gezielt und mit Admission; keine Addition von CUDA0- und Vulkan1-Speicher. [1] |
| AMD Vega 7 / Vulkan0 | Experimenteller `SUBTASK_WORKER`, nicht Kapazitätsreserve | Vega-7-Speicher ist UMA/Shared RAM. Ein Auftraggeberlauf belegt nur einen begrenzten Agents-A1-Textpfad. | Vulkan0 immer explizit AMD; Shared RAM gleichzeitig gegen Host-Commit und DXGI-Shared-Budget prüfen, niemals als dediziertes VRAM ausgeben. [1] |
| CPU / Ryzen 5 5600G | Träger für große Coder und Fallbacks, on demand | 7B/14B-Coder passen nicht vollständig in 4 GB dediziertes VRAM; CPU-/Hybrid-Betrieb ist der konservative Pfad. | Keine dauerhafte 14B-Residenz; Coder-Aufträge serialisieren und mit Vision/Worker konkurrieren lassen. [2] |
| Orchestrierung | Deterministisch in der Anwendung, nicht durch ein LLM | Modellselbstauswahl kann Rollen, Geräte, Ports, Tool-Freigaben oder Ressourcengrenzen nicht vertrauenswürdig erzwingen. | Rollen-, Target-, Kontext-, Zeit- und Werkzeugpolicy werden im DBZS-Code entschieden; ein LLM liefert höchstens Plantext. [2] |
| Speculative Decoding | **DEFER in Phase 1** | Zwar existiert in b8842 ein nativer `-md`-Pfad, aber es gibt kein verifiziert kompatibles Paar, keine explizite Draft-Geräteführung im DBZS-Startweg und keinen A/B-Nachweis. | Keine Produktiv- oder Versuchsschaltung aufgrund von README-Claims; erst ein getrennt freigegebenes A/B-Protokoll. [3] |

### Harte Gerätezuordnung

| Logischer Schlüssel | Physisches Ziel | Zulässiger Build | Einordnung |
|---|---|---|---|
| `cuda:0` | NVIDIA GTX 1650 SUPER | CUDA-13.1-Build | dediziertes NVIDIA-Device-Memory |
| `vulkan:0` | AMD Vega 7 | Vulkan-Build | UMA/Shared-System-Memory |
| `vulkan:1` | NVIDIA GTX 1650 SUPER | Vulkan-Build | dieselbe physische NVIDIA-Karte wie `cuda:0` |
| `cpu` | Ryzen 5 5600G / System-RAM | CPU-Build | kein GPU-Speicherbudget |

`Vulkan1` ist **nicht** AMD. CUDA0 und Vulkan1 sind zwei APIs für dieselbe NVIDIA-Karte; ihre 4 GB dürfen nicht doppelt budgetiert werden. Windows trennt außerdem dedizierten Video-, dedizierten System- und Shared-System-Speicher. [7] Die Vega-7-UMA ist deshalb weder ein zweiter VRAM-Pool noch zu den 4 GB der GTX addierbar. [1]

---

## 2. Belastbare Backendbefunde und Grenzen

### 2.1 CPU, CUDA und Vulkan sind Implementierungspfade – keine Gleichwertigkeitszusage

Der b8842-Quellstand registriert `qwen3vl` und `qwen35`. Für Qwen35 existieren Loader- und Rechenpfade für die Hybridarchitektur einschließlich Gated DeltaNet auf CPU, CUDA und Vulkan. [9] [10] [11] Die Vulkanquellen enthalten dedizierte Gated-DeltaNet-Pipelines; CPU und CUDA dispatchen die Operatoren ebenfalls. [10] [11] [12] Das belegt Architektur- und Backendcode, aber keinen konkreten lokalen E2E-Lauf bei beliebigem Treiber, Kontext, Batch oder Cachetyp. [1]

Für CUDA ordnet NVIDIA die GTX-16-SUPER-Serie Turing / Compute Capability 7.5 zu. Die b8842-CMake-Logik enthält einen Turing-Pfad. CUDA 13.x verlangt mindestens Treiberreihe 580 [8], doch der installierte Treiber, die DLL-Auflösung und der konkrete sm_75-/PTX-Inhalt der vorhandenen DLL wurden nicht geprüft. Daraus folgt: CUDA ist ein plausibler Kandidat, aber kein abgenommener Produktionspfad. [1]

### 2.2 Getrennte Server sind Koordination, kein Modell-Speicherpool

Ein CUDA- und ein Vulkan-Server können unterschiedliche Modelle über getrennte Ports anbieten. Jeder Prozess hält jedoch eigene Gewichte, KV-Caches, Compute-Buffer und Prozessoverhead. Gemeinsame Ressourcen bleiben CPU, DDR4, SSD-I/O und – bei CUDA0/Vulkan1 – dieselbe NVIDIA-Karte. Ein Mehrserverbetrieb kann sinnvoll sein, erzeugt aber keinen gemeinsamen VRAM und garantiert keinen parallelen Speedup. [1]

Ein Geräte-/Tensor-Split ist nur ein Prozessfeature des Server-Builds und darf nicht mit zwei HTTP-Servern verwechselt werden. Für die vorhandenen getrennten Binary-Pakete ist ein CUDA0-plus-Vulkan0-Split eines einzelnen Modells **nicht unterstützt**. Ein künftig selbst gebautes Kombinationspaket wäre ein neuer, separat zu belegender Sachverhalt.

### 2.3 Einziger vorhandener Leistungsbefund: eng abgrenzen

Der Auftraggeber berichtet für `Agents-A1-4B Q4_K_M` auf **Vulkan0 = AMD Vega 7** einen Lauf mit `ngl=8`, Kontext 2048, Batch 128, UBatch 64, vier Threads, `parallel=1` und Port 8083. Modellladung, AMD-Compute-Buffer, Gated DeltaNet und eine API-Anfrage wurden berichtet; die angegebenen Raten lauten **50,57 Prompt-Token/s** und **8,89 Generation-Token/s**.

Dies ist ausschließlich Auftraggeber-Evidenz für genau diesen Teilpfad. Der Reasoning-Output erreichte das Tokenlimit ohne Antwortabschluss; Speculative Decoding war nicht aktiv. Der Befund ist deshalb weder ein Benchmark für CUDA, CPU, Vision, RAG oder andere Parameter noch ein Nachweis eines korrekt abgeschlossenen Worker-Ergebnisses.

---

## 3. Rollen, erste Modellzuordnung und Zurückstellungen

### 3.1 Rollenmodell

| Rolle | Erstentscheidung | Begründung | Technische Konsequenz |
|---|---|---|---|
| `PRIMARY_CHAT` | Nicht als GPU-Dauerprozess erzwingen; später Qwen3-4B CPU/Hybrid bewerten | Qwen3-4B Q4_K_M plus 4K-F16-KV und Annahmepuffer liegt im Planungsrahmen oberhalb belastbarer 4-GB-Full-GPU-Planung. | On-demand starten; Thinking-/Non-Thinking-Verhalten in llama.cpp nicht als Hard-Switch voraussetzen. [2] |
| `CODING_ORCHESTRATOR` | Qwen2.5-Coder-7B serialisiert; 14B nur CPU/Hybrid on demand | Q4_K_M des 7B-Modells wiegt 4,68 GB, das 14B-Modell 8,99 GB; beide sind keine Full-GPU-Fälle. | Keine parallele Vision- oder Worker-Residenz; 14B nur bei anspruchsvoller Architektur/Refactoring. [2] |
| `CODE_REVIEWER` | Eigenständiger, begrenzter 7B-/14B-Durchlauf | Generierung ist keine Freigabe. | Getrennte Budget- und Ergebnisvalidierung, keine automatische Selbstabnahme. |
| `SUBTASK_WORKER` | Vorhandenes Agents-A1-4B, begrenzt und schemafixiert | Die Architektur ist auf b8842 nachweisbar; Vulkan0-Teilpfad ist berichtet. Der Antwortabschluss blieb jedoch offen. | Kurze Aufgaben, harte `max_tokens`, Stop-/Schema-Prüfung und keine zentrale Steuerrolle. [1] [2] |
| `PROMPT_ENHANCER` | Qwen2.5-Coder-1.5B oder Llama 3.2 1B später prüfen | Kleine, klar begrenzte Textrolle. | Darf weder Gerätewahl, Rollenwahl noch Toolfreigaben verändern. [2] |
| `VISION_INSPECTOR` | Qwen3-VL-4B nur on demand und nur als GGUF-`mmproj`-Paar; Alternativen Gemma 3 4B / SmolVLM2 | Qwen bietet Sprachmodell und separaten Projector; b8842-Architektur-/MTMD-Support ist belegt, Qwen3-VL steht aber nicht in der damaligen b8842-Visionliste. | Ohne hashgeprüften, passenden Projector kein Visionstart; GPU-Projektoroffload ist kein 4-GB-Fitnachweis. [1] [2] |
| `SPECULATIVE_DRAFT` | **Leer / deaktiviert** | Kleinere Parameterzahl oder Dateiname sind kein Kompatibilitätsnachweis. | Erst nach Tokenizer-/Metadatenprüfung und positivem, gemessenem A/B-Nachweis belegen. [3] |
| `RAG_EMBEDDING`, `RAG_RERANKER` | Separate Artefakte, nicht aus Chat-/VLM-Modellen ableiten | Generische Embedding-API beweist keine semantische Retrievalqualität. | Siehe [RAG-Empfehlung](DBZSCC_RAG_BACKEND_RECOMMENDATION.md). [1] |

### 3.2 Modell- und Speicheraussagen korrekt lesen

Qwen3-VL-4B Q4_K_M (rund 2,497 GB) benötigt zusätzlich einen separaten Q8-Projector (rund 454 MB) oder F16-Projector, KV-Cache und Laufzeitbuffer. Für 4K ist aus der Textarchitektur ein F16-KV-Untergrenzenwert von rund 0,563 GiB ableitbar. Ein daraus abgeleiteter 4,1–5,2-GiB-Planungsrahmen ist eine konservative **Planungsannahme**, nicht gemessener RAM/VRAM und keine Startgarantie. [2]

Agents-A1-4B hat 24 Linear-Attention- und acht Full-Attention-Schichten. Das offiziell ausgewiesene Q4_K_M-Textgewicht (rund 2,71 GB) plus separater Projector (rund 672 MB) und ein F16-KV-Untergrenzenwert für die acht Full-Attention-Schichten erklären noch nicht DeltaNet-Rekurrenzzustand, Visionbuffer oder Scratch-Allocation. K/V-Quantisierung bewertet daher nicht automatisch den separaten Gated-DeltaNet-State. [1] [2]

Diese Grenzen sind die Gründe für on-demand Rollen: Die Speichergröße einer GGUF-Datei ist keine Kapazitätszusage und darf nicht mit Vega-Shared-Memory „repariert“ werden.

---

## 4. Speculative Decoding: eindeutige Defer-Entscheidung

### 4.1 Was b8842 tatsächlich kann

b8842 implementiert nativen Model-Draft-Speculation: Ein Target wird mit `-m`, ein lokales Draftmodell im **gleichen Serverprozess** mit `-md/--model-draft` geladen. Getrennte Draftparameter umfassen unter anderem `--threads-draft`, `--draft-max`, `--ctx-size-draft`, `--device-draft`, `--gpu-layers-draft` sowie Draft-KV-Typen. Zwei getrennte HTTP-Server sind dadurch nicht automatisch ein native Target-/Draft-System. [3]

Für den schnellen Kompatibilitätspfad prüft b8842 Vokabulartyp, BOS/EOS-Regeln, Vokabulargröße und Tokeninhalte. Bei Abweichung kann retokenisiert oder ersetzt werden; das ist keine gleichwertige Grundlage für einen ersten Performancevergleich. Ein erstes Paar benötigt daher denselben direkten Tokenizerpfad ohne Übersetzungswarnung. [3]

### 4.2 Warum Phase 1 trotzdem DEFER bleibt

1. Keines der genannten lokalen Modelle ist anhand von GGUF-Metadaten und Spezialtokenregeln als kompatibles Target/Draft-Paar belegt.
2. `speculative_engine.py` reicht `-md` weiter, wählt Drafts aber heuristisch nach Namen/Größe; `llama_client.py` setzt weder Draftgerät noch Draftkontext, Layer, Cache oder Akzeptanzmetriken.
3. Target CUDA0 plus Draft CPU ist allenfalls ein späterer Kandidat. Target CUDA0 plus Draft Vulkan0 ist mit den getrennten Paketen nicht nachgewiesen und wird ausgeschlossen.
4. Target und Draft benötigen getrennte Modell- und Kontextresourcen. Q4_K_M ist kein Gesamtbudget.
5. Der AMD-Lauf hatte keine aktive Speculation und endete am Tokenlimit. Die in README-/Codekommentaren genannten 1,8×–2,5× sind kein Nachweis für dieses System.

### 4.3 Bedingter späterer Go-Mechanismus

Ein später freizugebender Test verwendet je Bedingung einen nativen b8842-Serverprozess: einmal Target-only, einmal gleiches Target plus lokaler `-md`-Draft. Beide Bedingungen müssen Build-/Model-/Tokenizer-Hashes, Geräte, Kontext, Batch/UBatch, KV-Typen, Threads, `parallel=1`, Sampling und Outputbudget identisch protokollieren. Erforderlich sind mindestens fünf warme Wiederholungen pro versioniertem Prompt.

Ein Go verlangt zugleich: direkten Vokabularpfad, nicht-null Draftakzeptanz, keinen Tokenlimit-/OOM-/Treiberabbruch, dokumentierte Peakressourcen und eine bessere mediane End-to-End-Generationszeit ohne unzulässige TTFT- oder p95-Regression. Akzeptanzrate allein genügt nicht. [3]

---

## 5. Gemeinsame konservative Startpolicy

Die Policy ist eine veränderbare Sicherheitsannahme, keine Hardwaremessung oder Leistungszusage. Sie gilt systemweit konsistent für die verlinkten Entwürfe:

- CPU-Threadbudget `8`, UI-/Service-Reserve `4`;
- `os_ui_available_guard_mib=6144`, `commit_safety_margin_mib=4096`, `min_available_physical_mib=4096`;
- NVIDIA-Schutzreserve `1024 MiB` zusätzlich zu profilbezogenen Reserven;
- AMD-Shared-Schutzreserve `2048 MiB`, ausschließlich als DXGI-Shared-Budgetprüfung und nie nochmals als Host-RAM-Abzug;
- globale Queue `32`, höchstens `8` pro Rolle; Telemetriecache `1 s`, maximaler Admission-Snapshot `2 s`, Healthcache `2 s`;
- Load-Lease `45 s`, Busy-Heartbeat `5 s`, Idle-TTL `300 s`, Pressure-Cooldown `30 s` und drei frische Clear-Samples;
- `parallel=1`, bis die konkrete b8842-Slot-/KV-Semantik separat geprüft ist.

Die Detaillogik einschließlich atomarer Admission und nicht doppelt gezähltem UMA-Speicher steht im [Scheduling-Design](DBZSCC_RESOURCE_SCHEDULING_DESIGN.md).

---

## 6. Höchster Nutzen: drei bis fünf nächste Schritte

1. **Build- und Target-Snapshot manuell erfassen.** Pro CPU-, CUDA- und Vulkan-Binary b8842-Identität, unterstützte Gerätewahl, tatsächliche Geräteliste und die Zuordnung CUDA0/Vulkan0/Vulkan1 festhalten. Das verhindert falsche Flags und Fehlrouting.
2. **Explizite Target- und Rollenverträge implementieren.** `backend`, `device_key`, Speicherklasse, Build- und Artefaktfingerprint müssen vor dem Start deterministisch aus DBZS-Konfiguration kommen – nicht aus UI-Reihenfolge oder Modelloutput.
3. **Zentrale Ressourcenadmission vor Mehrserverbetrieb einführen.** Host-Commit, dediziertes NVIDIA-Memory, AMD-Shared-Budget, Threads, Port und Leases atomar reservieren. Das verhindert Doppelverkauf desselben Headrooms.
4. **Vision- und Worker-Abnahme kontrolliert nachziehen.** Qwen3-VL erst mit korrektem `mmproj` und Bild-E2E, Agents-A1 auf Vulkan0 erst mit vollständig terminierten Antworten und sichtbarem Stop-Grund abnehmen.
5. **Speculation konsequent zurückstellen.** Erst nach diesen Basisarbeiten ein offlinees Target-only-vs.-`-md`-A/B mit validiertem Paar entscheiden.

---

## 7. Phasierte Roadmap

| Phase | Umfang | Eintrittskriterium | Ergebnis |
|---|---|---|---|
| 0 – Inventar und Schutz | Manuelle Build-/Artefakt-/Targeterfassung, Manifest, feste Rollen | Freigabe für lokale, offlinee Bestandsprüfung | Keine neue Inferenzbehauptung; reproduzierbare Konfigurationsbasis |
| 1 – Kontrollierte Runtime | CUE-P0, Targettelemetrie, Admission, Lifecycle-Fassade | b8842-Capability-Snapshot je Build | Deterministische Starts, `parallel=1`, keine zweite Ownership |
| 2 – Rollen-E2E | Textabschluss Agents, CPU/CUDA-Baseline, Vision-`mmproj`-Paar | P0-Abnahme, manuelle Bedienfreigabe | Rollen als belegt, eingeschränkt oder abgelehnt dokumentiert |
| 3 – RAG und Beobachtbarkeit | E5/FTS5/RRF, Manifest, Indexgeneration, optionale Rerank-Evaluation | Offline-Artefakte und Workspace-Allowlist | Lokales Retrieval ohne GPU-Konkurrenz als Baseline |
| 4 – Optimierung | Optionaler Draft-A/B, Cache-/Flash-Varianten, spätere Skalierung | Stabiler Basispfad und definierte Messsuite | Nur gemessene Einzelprofile erhalten ein Go |

Die serielle Target-/Lifecycle-Basis ist ein früher Meilenstein. Für den vollständigen, hier empfohlenen Scope einschließlich atomarer Admission, Offline-/Toolschutz, RAG und Altpfadbereinigung ergibt der [CUE-Fahrplan](DBZSCC_CUE_IMPLEMENTATION_RECOMMENDATIONS.md) eine Schätzung von 33–52 Personentagen, bei einer Person grob 7–11 Arbeitswochen plus Bediener-/Artefaktwartezeit. Keine Zusage; keine Beschaffung, Treiberänderung oder Benchmarkausführung enthalten.

---

## 8. Direkte Antworten auf die sieben Managementfragen des Auftrags

1. **Ist Triple-Backend sinnvoll?** Ja als koordinierte, offlinefähige Rollenarchitektur. Nein als addierter VRAM-Pool oder als Pflicht zu drei gleichzeitig residenten großen Modellen.
2. **Welche Aufgaben pro Backend?** CUDA für abgenommene kleine Textmodelle oder bedarfsweise Vision/Teiloffload; AMD Vulkan0 zunächst begrenzte experimentelle Worker; CPU für deterministische Steuerung, RAG-Baseline und größere Coder on demand. CPU-RAG konkurriert ebenfalls um Threads und DDR4.
3. **Welche Modelle zuerst?** Zuerst vorhandenes Agents-A1 auf Vulkan0 auf vollständige Textantworten prüfen und vorhandenes Qwen3-VL mit passendem mmproj auf Bild-E2E abnehmen. LLaMA2 nur Legacyvergleich. Erst bei später manuell freigegebenen neuen Artefakten Qwen2.5-Coder-1.5B (kleiner Worker) und 7B (Hauptcoder CPU/Hybrid); 14B später bedarfsweise, keine Auto-Beschaffung.
4. **Welche Komponenten zuerst?** Target-/Profilvertrag und wirksamer Rollenparametermerge, ein LlamaServer-Lifecycle-Owner mit bestehender Registry, zielbezogene Telemetrie, danach atomare Admission/Leases/Queue. Offline- und Toolschutz sind Freigabegates, nicht zuletzt nachgereichte Extras.
5. **Was zurückstellen?** Speculative Decoding, permanentes Dreifachladen, automatischer CUDA/Vulkan-Wechsel, großer BGE-Reranker, DirectML-RAG und Qdrant ohne belegten Bedarf.
6. **Größte Risiken?** Unvollständiger Workeroutput, falsches Vulkan1-Routing, doppelt gezähltes RAM/VRAM, fehlender Visionprojector, Treiber-/Kernel-/GDN-Parameterfehler, ungesicherte Spooler-Toolpfade und blindes Vertrauen in DXGI-CurrentUsage. Für UMA ist WDDM-Budget kein globaler Verbrauchswert.
7. **Welche 3–5 Schritte bringen den größten Nutzen?** Build-/Artefaktmanifest; wirksamer Target-/Rollenvertrag; Lifecycle-/Toolgrenzen; atomare zielbezogene Admission; vollständig protokollierte Worker-/Visionabnahme. Erst anschließend RAG und messbasierte Optimierung.

### Bekannte Stabilitätsberichte, keine bestätigten Fehler dieses Rechners

Upstream-Issues berichten AMD-Vulkan `DeviceLost` in Abhängigkeit von UBatch/Kontext sowie Qwen35-/DeltaNet-Ausgabeprobleme bei bestimmten Batchkonfigurationen. [13] [14] Das ist relevante Testmotivation, aber keine Aussage, dass dieser konkrete b8842-Build oder Vega7 denselben Fehler hat. Konsequenz: den berichteten ngl8/ctx2048/b128/ub64-Startpunkt beibehalten, dann jeweils nur einen Parameter ändern; vollständige Antworten, Backendfehler und Peaks festhalten. Treiberänderungen bleiben gesondert freigegebene Systemarbeit.

## 9. Offene technische Freigabefragen

1. **CUDA-Betriebsfähigkeit:** Welche NVIDIA-Treiberversion, CUDA-Runtime-DLL-Auflösung und welcher konkrete sm_75-/PTX-Inhalt liegen für den CUDA-13.1-Build tatsächlich vor?
2. **Stabile Targetbindung:** Bestätigt ein manueller Build-Snapshot eindeutig und dauerhaft CUDA0=NVIDIA, Vulkan0=AMD und Vulkan1=NVIDIA – einschließlich der b8842-Geräteauswahlsyntax?
3. **Artefaktprovenienz:** Welche Hashes, GGUF-Metadaten, Konverterstände, Chattemplates und gültigen `mmproj`-Paare haben die lokalen Qwen3-VL- und Agents-A1-Artefakte?
4. **Vollständiger Workerabschluss:** Welches Token-/Stoplimit verursachte das unvollständige Agents-A1-Reasoning, und kann Vulkan0 zwei unabhängige, vollständig abgeschlossene Antworten mit protokolliertem Stop-Grund liefern?
5. **Peakressourcen:** Welche getrennten Peaks entstehen bei Load, Prefill, Generation, Vision-Encoding und Stop für die tatsächlich freizugebenden Modell-/Backend-/Profilkombinationen?
6. **Rollen- und Sicherheitspriorität:** Welche Rollen benötigen welche Deadline, maximale Ausgabe, Queuepriorität und menschenfreigegebene Werkzeugaktionen; wann ist ein kontrollierter Modellwechsel zulässig?
7. **Optimierungsfreigabe:** Soll nach stabiler Baseline ein text-only, nativer Target/Draft-A/B überhaupt verfolgt werden, oder überwiegt auf dieser Hardware der Wert robuster on-demand Rollen?

---

## Quellen

[1]: https://github.com/ggml-org/llama.cpp/releases/tag/b8842 "llama.cpp Release b8842"  
[2]: https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct-GGUF "Qwen2.5-Coder-14B-Instruct GGUF"  
[3]: https://github.com/ggml-org/llama.cpp/blob/455d8e4be/docs/speculative.md "llama.cpp b8842 Speculative Decoding documentation"  
[4]: https://raw.githubusercontent.com/ggml-org/llama.cpp/455d8e4be85aaae75195435a513a2a5dbef0136c/tools/server/README.md "llama.cpp b8842 server documentation"  
[5]: https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF "Qwen3-VL-4B-Instruct GGUF Model Card"  
[6]: https://huggingface.co/InternScience/Agents-A1-4B-Q4_K_M-GGUF "InternScience Agents-A1-4B Q4_K_M GGUF"  
[7]: https://learn.microsoft.com/en-us/windows-hardware/drivers/display/retrieving-graphics-memory-numbers "Retrieving Graphics Memory Numbers"  
[8]: https://docs.nvidia.com/cuda/archive/13.1.1/cuda-toolkit-release-notes/index.html "CUDA Toolkit 13.1 Release Notes"
[9]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/src/llama-arch.cpp "llama.cpp b8842 Architekturregistry"
[10]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/ggml/src/ggml-cuda/ggml-cuda.cu "llama.cpp b8842 CUDA Operatoren"
[11]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/ggml/src/ggml-vulkan/ggml-vulkan.cpp "llama.cpp b8842 Vulkan Operatoren"
[12]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/ggml/src/ggml-cpu/ggml-cpu.c "llama.cpp b8842 CPU Operatoren"
[13]: https://github.com/ggml-org/llama.cpp/issues/20515 "AMD Vulkan DeviceLost und Parameterabhängigkeit"
[14]: https://github.com/ggml-org/llama.cpp/issues/27237 "Vulkan Qwen35 DeltaNet Ausgabe bei Batchkonfiguration"
