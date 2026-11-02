# DBZSCC – Modell-, Backend- und Hardware-Kompatibilitätsmatrix

**Recherchezeitpunkt:** 31.10.2026  
**Versionsgrenze:** llama.cpp `b8842` / Commit `455d8e4be85aaae75195435a513a2a5dbef0136c`  
**Arbeitsstand:** DBZS HEAD `bcdf5d28ae5686b2e4cc48f7ceca7b278623315c` (dirty; nicht verändert)  
**Status:** Finaler Synthesebericht aus den Befunden A und B. Alle Memory-Angaben sind Planungsdaten, keine Messwerte, Fit-Zusagen oder Performancegarantien.

Diese Matrix ergänzt die [Managemententscheidung](DBZSCC_TRIPLE_BACKEND_RESEARCH.md), die [Ressourcenadmission](DBZSCC_RESOURCE_SCHEDULING_DESIGN.md) und die [CUE-Integration](DBZSCC_CUE_IMPLEMENTATION_RECOMMENDATIONS.md).

## 1. Verbindliche Lesart: fünf Evidenzkategorien

| Kategorie | Bedeutung | Zulässige Schlussfolgerung | Nicht zulässige Schlussfolgerung |
|---|---|---|---|
| **N – nachweislich unterstützt** | b8842-Quellcode, b8842-Dokumentation oder klar abgegrenzte Auftraggeber-Evidenz trägt die Aussage. | Die konkrete Schnittstelle, Architektur oder Teilausführung existiert. | Jede Modell-/Treiber-/Parameter-/Leistungskombination funktioniert E2E. |
| **G – grundsätzlich unterstützt, aber nicht getestet** | Backend- und Architekturbefund sieht den Pfad vor, aber kein lokaler End-to-End-Nachweis existiert. | Späterer kontrollierter Test ist fachlich begründet. | Das konkrete Gerät oder Artefakt ist abgenommen. |
| **E – eingeschränkt unterstützt** | Eine konkrete Build-, Artefakt-, Parameter- oder Funktionsgrenze ist bekannt. | Nur innerhalb der benannten Einschränkung planen. | Eine allgemeine Featurezusage ableiten. |
| **X – nicht unterstützt** | Der vorliegende Binärsatz oder die Gerätezuordnung schließt die Kombination aus. | Kombination nicht in der Planung verwenden. | Durch zwei getrennte Prozesse dieselbe native Funktion behaupten. |
| **U – unbekannt** | Die vorhandenen Befunde liefern keine belastbare Grundlage. | Offen dokumentieren und bei Bedarf gezielt abnehmen. | Aus Dateiname, Modellgröße oder aktueller Upstream-Dokumentation schließen. |

Die Kategorien bewerten nicht Modellqualität. Eine generische Embedding-Route beispielsweise kann N sein, während die semantische Retrievalqualität eines Chat- oder VLM-Modells U bleibt.

## 2. Fixe Hardware- und Buildgrenzen

| Logischer Targetschlüssel | Gerät / Speicherklasse | Build | Schlussfolgerung |
|---|---|---|---|
| `cuda:0` | GTX 1650 SUPER, 4 GiB dediziertes NVIDIA-Device-Memory | CUDA 13.1 | CUDA-Pfad; tatsächlicher Treiber, Runtime-DLL-Auflösung und sm_75-Fatbin sind offen. [1] |
| `vulkan:0` | AMD Vega 7, UMA / Shared-System-Memory | Vulkan | Einziger AMD-Vulkan-Target; Shared Memory ist nicht additiv zum Host-RAM oder NVIDIA-VRAM. [2] |
| `vulkan:1` | GTX 1650 SUPER, dieselbe Karte wie `cuda:0` | Vulkan | NVIDIA-Vulkan-Pfad, **keine** zweite AMD-Karte oder zweite 4-GiB-Reserve. [2] |
| `cpu` | Ryzen 5 5600G / 32 GiB System-RAM | CPU | CPU-Build ohne GPU-Backend. `-ngl 0` in GPU-Builds ist nicht gleichbedeutend mit `--device none`. [3] |

Die vorhandenen Windows-pakete sind backendgetrennt: CUDA enthält keine Vulkan-DLL, Vulkan keine CUDA-DLL, CPU keine GPU-DLL. Ein CUDA0-plus-Vulkan0-Modellsplit innerhalb eines dieser Pakete ist daher X. Mehrere Server können parallel existieren, teilen aber RAM, CPU und bei CUDA0/Vulkan1 dasselbe NVIDIA-Device-Memory. [3]

## 3. Funktionsmatrix: b8842-Implementierung und konkrete Grenze

| Funktion | CPU | CUDA-13.1 | Vulkan | Quelle / Befund | Schlussfolgerung |
|---|---:|---:|---:|---|---|
| Qwen3-VL-Architektur `qwen3vl` | N | N | N | Registry erkennt `qwen3vl`; b8842-MTMD und `--mmproj` sind dokumentiert. [21] [4] | Architektur-/Schnittstellenbeleg, kein lokaler Bild-E2E; die konkrete Qwen3-VL-Modellzeile bleibt deshalb U. |
| Qwen35 / Gated DeltaNet | N | N | N | Qwen35-Hybrid-Lader und CPU-/CUDA-/Vulkan-GDN-Pfade vorhanden. [21] [22] [23] [5] | Agents-A1-artige Hybridoperatoren sind implementiert; Stabilität je Modell/Parameter offen. |
| GPU-Layer-Offload | X | N | N | `-ngl` ist für GPU-Builds dokumentiert. [4] | CPU-Build besitzt keinen GPU-Offload. |
| CPU/GPU-Hybrid | X | N | N | Nicht-offgeladene Schichten verbleiben im Hostpfad. [4] | Plausibler Teiloffload, ohne konkrete VRAM-Prognose. |
| Flash-Attention-Steuerung | G | E | G | `--flash-attn` existiert; CUDA-Instanzen sind cachetyp-/compileabhängig. [4] [6] | Gewählte Kombination muss später getestet werden. |
| K-/V-Cache-Typen und KV-Offload | G | G | G | b8842 listet F16/BF16/Q8-/Q4-/Q5-/IQ4-Optionen. [4] | API vorhanden, effektiver Kernel-/Speicherpfad je Build offen. |
| Generische Embedding-API | N | N | N | Server stellt Embedding-Routen bereit. [4] | Keine Aussage zur Embedding-Qualität eines generativen Modells. |
| Vision-Projector-Offload | G | G | G | `--mmproj`, MTMD und Standard-Offload sind dokumentiert. [4] [7] | Projektor- und Gesamtfit müssen modellbezogen belegt werden. |
| CUDA0 plus Vulkan0 in einem vorhandenen Paket | X | X | X | Lokale DLL-Trennung, keine Cross-Backend-Dokumentation. [3] | Nicht als Modellsplit oder Draft-Plan einsetzen. |
| Native Model-Draft-Speculation | G | G | G | `-md` und Draftoptionen in b8842 vorhanden. [8] | Ohne gültiges Paar, Gerätebindung und Messung bleibt DBZS-Status DEFER. |

## 4. Konkrete Modell-/Hardware-/Speichermatrix

**Methodik.** Gewichte sind veröffentlichte Artefaktgrößen. F16-KV-Untergrenzen beziehen sich – soweit die Architektur vorliegt – auf einen Slot bei 4K und erfassen weder Batch-/UBatch-Workspace, Allocator, OS, Vision-Tokens noch Teiloffload. Planungsrahmen rechnen Dateibytes zuerst in GiB um und addieren den genannten F16-KV-Wert plus 0,8–1,8 GiB angenommenen Laufzeitpuffer; sie sind nicht gemessen. GB=10^9 Bytes, GiB=2^30 Bytes. Der Rahmen ist ein alternatives vollständiges CPU- oder GPU-Platzierungsszenario, nicht gleichzeitig RAM und VRAM und kein Hybridbudget. Gemappte GGUF-Seiten, Uploadduplikate und Ladepeaks benötigen ein separates Hostprofil. Architekturabhängige GDN-/Visionpeaks bleiben zusätzlich offen. Alle GPU-Zuordnungen erfordern die jeweilige Targettelemetrie und die gemeinsame Startpolicy.

| Modell / konkrete Rolle | Artefakt- und Architekturbeleg | 4K-KV-Untergrenze / statisch | CPU | CUDA0 GTX 4 GB | Vulkan0 AMD | Evidenz, Quelle und Schlussfolgerung |
|---|---|---|---|---|---|---|
| **Qwen3-VL-4B-Instruct Q4_K_M** `VISION_INSPECTOR` | Sprach-GGUF 2.497.281.664 B; Q8-`mmproj` 453.974.304 B oder F16-Projector; 36 Schichten, 8 KV-Köpfe, Head-Dim 128. | ca. 0,563 GiB F16-KV; rund 2,95 GB statische Gewichte mit Q8-Projector; Planungsrahmen ca. 4,1–5,2 GiB. | U | U | U | Quelle: Qwen-GGUF und Konfiguration [9]. Schlussfolgerung: VLM-Kandidat nur als Paar; b8842 registriert die Architektur, die damalige explizite Visionliste führt Qwen3-VL jedoch nicht. Ohne b8842-`mmproj`-/Bild-E2E ist der konkrete Modell-/Backendpfad unbekannt, keine 4-GB-Fitgarantie. |
| **Agents-A1-4B Q4_K_M** `SUBTASK_WORKER` | Textgewicht 2,708 GB; separater Projector 672 MB; 24 Linear-/8 Full-Attention-Schichten, GDN-Hybrid. | ca. 0,125 GiB F16-KV nur für Full-Attention; mindestens ca. 3,38 GB statische Gewichte inkl. Projector, GDN-State/Vision/Scratch offen. | G | G | **N** nur Text-Teilpfad | Quelle: Agents-GGUF und Konfiguration [10]. Schlussfolgerung: Vulkan0 laden/API/GDN ist als Auftraggebernachweis belegt, aber kein vollständiger Antwort- oder Visionnachweis. |
| **Qwen2.5-Coder-1.5B Q4_K_M** `PROMPT_ENHANCER`/kleiner Coding-Worker | 1,12 GB; 28 Schichten, 2 KV-Köpfe. | ca. 0,109 GiB; Planungsrahmen ca. 2,0–3,0 GiB. | G | G | G | Quelle: offizielle GGUF-Karte [11]. Schlussfolgerung: robustester kleine Coder-Kandidat unter Vorbehalt eines b8842-Gerätetests; nicht Review-/Orchestrator. |
| **Qwen2.5-Coder-3B Q4_K_M** option. Worker | 2,105 GB; 36 Schichten, 2 KV-Köpfe. | ca. 0,141 GiB; Planungsrahmen ca. 2,9–3,9 GiB. | G | E | G | Quelle: offizielle GGUF-Karte [12]. Schlussfolgerung: 4-GB-Grenzzone; Full-GPU nicht zusagen. Lizenzmarkierung `qwen-research` vor Freigabe prüfen. |
| **Qwen2.5-Coder-7B Q4_K_M** `CODING_ORCHESTRATOR` | 4,68 GB; 28 Schichten, 4 KV-Köpfe. | ca. 0,219 GiB; Planungsrahmen ca. 5,4–6,4 GiB. | G | E (Teiloffload) | E (Teiloffload) | Quelle: offizielle GGUF-Karte [13]. Schlussfolgerung: kein Full-GPU; CPU/Hybrid und serialisiert. |
| **Qwen2.5-Coder-14B Q4_K_M** komplexer Code/Review on demand | 8,99 GB; 48 Schichten, 8 KV-Köpfe. | ca. 0,750 GiB bei 4K; Planungsrahmen ca. 9,9–10,9 GiB. | G | E (wenige Layer) | E (wenige Layer) | Quelle: offizielle GGUF-Karte [14]. Schlussfolgerung: ausschließlich CPU/Hybrid on demand; nie parallel zur Vision-Residenz planen. |
| **Qwen3-4B Q4_K_M** späterer `PRIMARY_CHAT` | 2,50 GB; 36 Schichten, 8 KV-Köpfe. | ca. 0,563 GiB; Planungsrahmen ca. 3,7–4,7 GiB. | N für Textarchitektur | G/E | G/E | Quelle: Qwen-GGUF und Qwen llama.cpp-Hinweis [15] [16]. Schlussfolgerung: b8842 liegt nach genanntem Mindeststand b5092, aber kein Full-GPU-Orchestrator; Thinking-Switch nicht direkt exponiert. |
| **Qwen3-8B Q4_K_M** allgemeiner Text on demand | 5,03 GB. | ca. 0,563 GiB; Planungsrahmen ca. 6,0–7,1 GiB. | G | E | E | Quelle: Qwen-GGUF [17]. Schlussfolgerung: CPU/Hybrid; hinter Coder-7B für Code einordnen. |
| **Gemma 3 4B Q4_K_M** Vision-Alternative | 2,49 GB Sprachmodell + 0,851 GB F16-Projector; Gemma Terms. | mind. ca. 3,34 GB statisch; KV offen. | G | E | E | Quelle: b8842-Multimodal- und Gemma-Artefaktliste [7] [18]. Schlussfolgerung: b8842-direkt gelistete Visionfamilie, aber auf 4 GB vor KV im roten Bereich und Lizenzprüfung nötig. |
| **SmolVLM2-2.2B Q4_K_M** leichte Vision-Alternative | 1,11 GB; Apache-2.0, explizit in b8842-Visionliste. | Gesamt inklusive Bild-/KV-Buffer offen. | G | G | G | Quelle: SmolVLM2-Karten und b8842-Multimodal [7] [19]. Schlussfolgerung: spätere schlanke Option für einfache Bild/OCR/UI-Beschreibung, keine Qualitätsgleichsetzung mit Qwen3-VL. |
| **LLaMA 2 7B Chat Q4_K_M** Legacy-Fallback | Lokale Provenienz/Größe offen; Textmodell. | Keine seriöse konkrete Rechnung. | G | G | G | Quelle: Meta-Modellkarte [20]. Schlussfolgerung: nur Vergleich/Fallback; nicht als neue Rollenbasis. |

**Einziger Ausführungsnachweis in der Matrix.** Für Agents-A1 auf Vulkan0 gelten die Kategorien nur für Text/GDN-Load/API-Verarbeitung bei `ngl=8`, Kontext 2048, Batch 128, UBatch 64, vier Threads und `parallel=1`. Die Auftraggeberwerte von 50,57 Prompt-tok/s und 8,89 Generation-tok/s endeten ohne Antwortabschluss; Speculation war aus. Daraus wird weder eine Kapazitäts- noch Qualitäts- noch Durchsatzzusage abgeleitet.

### 4.1 Parameter, Kontext, Lizenz und Rollenqualität

Herstellerkontext ist die Modellobergrenze, kein DBZS-Default. Für die Erstabnahme gelten ein Slot, 2K für den berichteten AMD-Worker, ansonsten 4K; 8K erst nach Peakabnahme. Größere native Fenster erfordern keinen automatischen Download und werden nicht als verfügbare lokale Kapazität beworben.

| Konkretes Modell | Parameter / nativer Modellkontext | DBZS-Startkontext | Lizenz und Stärken | Grenze / technische Folge |
|---|---|---|---|---|
| Qwen3-VL-4B-Instruct | 4B / 256K, Erweiterung separat | 2K–4K inkl. Bildtokens | Apache-2.0; OCR, UI, multimodale Analyse. [9] [24] | Kein Bild-E2E auf diesem Rechner; Projector und Bildauflösung budgetieren. |
| Agents-A1-4B | 4B / 262144 Positionsplätze | 2048 wie Auftraggeberlauf | Apache-2.0; strukturierte Agent-/Toolaufgaben, auch Visionarchitektur. [10] [25] | Antwortabschluss offen, kein autonomer Leiter; GDN-State separat. |
| Qwen2.5-Coder-1.5B-Instruct | 1,54B / 32K GGUF | 4K | Apache-2.0; kleine Codefixes/Extraktion. [11] | Kleine Qualitätsklasse, kein schwieriger Review. |
| Qwen2.5-Coder-3B-Instruct | 3,09B / 32K GGUF | 4K | Coding-Worker; konkrete `qwen-research`-Lizenzdatei prüfen. [12] | GPU-Rahmen ist ohne 1-GiB-Reserve; daher Full-GPU nicht versprechen. |
| Qwen2.5-Coder-7B-Instruct | 7,61B / 32K GGUF | 4K, 8K gated | Apache-2.0; Codegenerierung, Fixes, Code-Reasoning. [13] | CPU/Hybrid, Tokenlatenz ungemessen. |
| Qwen2.5-Coder-14B-Instruct | 14,7B / 32K GGUF | 4K, 8K gated | Apache-2.0; anspruchsvollere Code-/Reviewrolle. [14] | CPU on demand, nicht Hintergrunddauerlast. |
| Qwen3-4B / Qwen3-8B | 4,0B / 8,2B; 32K nativ, ca. 131K YaRN separat | 4K | Apache-2.0; allgemeiner Chat/Reasoning. [15] [17] | Reasoningbudget und Template testen; für Code hinter Coder priorisieren. |
| Gemma 3 4B-it | 4B / 128K | 2K–4K | Gemma Terms, nicht Apache; multimodaler Alternativpfad. [18] [26] | Projector/Puffer; keine 4-GB-Fitgarantie. Gemma 3 1B ist text-only, kein kleiner Visionersatz. |
| SmolVLM2-2.2B-Instruct | 2,2B / exakter GGUF-Kontext hier offen | 2K–4K nach Metadatenprüfung | Apache-2.0; kompakte Bild-/Videoanalyse. [19] | Primär Englisch; Gesamtprojector-/KV-Peak offen. |
| LLaMA 2 7B Chat | 7B / 4096 | 2K–4K | Llama-2-Community-Lizenz; vorhandener Vergleichspfad. [20] | Älteres, primär englisches Chatmodell; hoher KV ohne GQA. |

**Llama-Alternativen konkret:** Llama 3.2 1B-Instruct (1,23B) und 3B-Instruct (3,21B) sind Textmodelle mit Deutschunterstützung, 128K Herstellerkontext und Community-Lizenz; Metas separat quantisierte Varianten nennen 8K, das ist nicht automatisch der Kontext jeder Community-GGUF. Community-Q4_K_M-Artefakte wiegen ungefähr 0,808 GB bzw. 2,02 GB. [27] [28] [29] Bei 4K ergibt ein grober Rahmen aus Gewichten, **angenommenen, hier nicht primärverifizierten** KV-Werten (0,125 bzw. 0,438 GiB) und Puffer etwa **1,7–2,7 GiB** bzw. **3,1–4,1 GiB**. CPU/CUDA0/Vulkan0: G, keine lokalen Tests. 1B eignet sich eher für Klassifikation/Promptpolitur, 3B für kleinen Chat; weder ersetzt einen 7B-/14B-Coder. Unter 1 GiB NVIDIA-Schutzreserve ist 3B Full-GPU nicht zugesagt. Llama-3.2-Vision beginnt bei 11B und wird hier nicht priorisiert. Ein stärkerer Reviewanspruch braucht ein stärkeres Modell, nicht einen neuen Rollenname.

### 4.2 LLaMA-2-Speicher: Architekturschätzung trotz offener lokaler Datei

Die lokale GGUF-Größe und Provenienz bleiben unbekannt. Unter der expliziten Annahme der üblichen 7B-Architektur (32 Layer, 32 KV-Köpfe, Head-Dimension128, F16 K/V) ergeben sich **2 GiB KV bei 4096 Tokens**, also erheblich mehr als bei GQA-Codern. Der Gewichtsuntergrenzenwert bei idealisierten 4 Bit beträgt etwa 3,3 GiB; Q4_K_M enthält zusätzliche Metadaten und höherpräzise Anteile. Gesamt-RAM ist deshalb nur als `lokale GGUF-GiB + 2 GiB KV + Laufzeitpuffer` seriös anzugeben. Full-GPU unter 4 GiB wird nicht empfohlen; CPU/Teiloffload bleibt G. Das ist keine Messung und kein Nachweis der lokalen Konvertierung. [20]

## 5. Vision, Embeddings, KV und Flash Attention: wichtige Nichtgleichsetzungen

### Vision

Ein Visionmodell ist erst nachgewiesen, wenn Sprach-GGUF und exakt passender Projektor gemeinsam geladen werden und ein lokales Testbild mit verifizierbarem Inhalt korrekt beantwortet wird. `--mmproj` und Standard-GPU-Offload sind b8842-Features; sie ersetzen nicht die Prüfung von Hash, Paarung, Speicherpeak und Bild-E2E. Für Agents-A1 ist die lokale Projektorpaarung U. [4] [7] [10]

### Embeddings und Reranking

B8842 kann Embedding- und Rerank-Endpunkte bereitstellen. Daraus folgt nicht, dass Qwen3-VL, Agents-A1 oder ein Coder semantisch brauchbare Retrieval-Embeddings erzeugt. Dafür sind dedizierte, evaluierte Artefakte nötig; die Baseline lautet multilingual-e5-small-ONNX auf CPU. Siehe [RAG-Empfehlung](DBZSCC_RAG_BACKEND_RECOMMENDATION.md). [4]

### KV-Quantisierung und GDN

K/V-Cache-Optionen betreffen bei Agents-A1 die acht Full-Attention-Schichten. Die 24 rekurrenten Gated-DeltaNet-Schichten besitzen separate FP32-Rekurrenzzustände. Eine K/V-Quantisierung darf daher nicht als pauschale Aussage über Gesamtstate, Langkontextspeicher oder Stabilität behandelt werden. [5] [10]

### Flash Attention

`--flash-attn on|off|auto` existiert in b8842. Für CUDA hängen einige Instanzen vom Compile-Set ab; aus Ordnernamen lässt sich nicht ableiten, welche Cachekombination die lokale DLL trägt. Jede produktive Kombination aus Modell, K-/V-Typ, Kontext und UBatch bleibt E, bis sie getrennt abgenommen ist. [4] [6]

## 6. Reproduzierbare Abnahmekriterien

1. **Identität:** Binarypfad, b8842-Version, Build, Backend, Gerät, Modell-/Projectorhash, Chattemplate und Profilparameter werden pro Lauf erfasst.
2. **Geräte:** CUDA0=NVIDIA, Vulkan0=AMD, Vulkan1=NVIDIA muss je Build bestätigt sein; keine Ordinalannahme zwischen APIs.
3. **Abschluss:** Eine erfolgreiche Ladephase reicht nicht. Mindestens zwei unabhängige Antworten müssen vor bewusst gesetztem Ausgabelimit vollständig enden; Limit/Trunkierung gilt als unvollständig.
4. **Vision:** Nur korrekt gepaartes GGUF+`mmproj` plus Bild-E2E besteht.
5. **Speicher:** Peaks in Load, Prefill, Generation und Vision werden gegen die Policyreserve gemessen; Shared Memory wird nicht als dediziertes VRAM gezählt.
6. **Hybrid/Cache:** Tatsächlich offgeladene Layer, K/V-Typ, Flash-Attention und Backendfehler müssen dokumentiert werden.
7. **Mehrserver:** Jeder Prozess benötigt ein eindeutiges Target, Port, Ressourcenplan und vollständige Antworten ohne Überbuchung.
8. **Speculation:** Erst mit kompatiblem Tokenizerpaar, nativer `-md`-Instanz, Akzeptanzdaten und positivem A/B darf ein Draftprofil aktiviert werden.

## 7. Offene Punkte

- Treiber-/CUDA-Runtime- und sm_75-Status der lokalen CUDA-DLL;
- Modellhashes, GGUF-Metadaten, Konverterstände und `mmproj`-Paarungen;
- stabile Vulkan0↔AMD-Targetauflösung je b8842-Build;
- reale Peakressourcen bei 2K/4K/8K, Vision und mehreren Servern;
- konkrete Cache-/Flash-Attention-Kombinationen;
- Lizenzdatei der gewählten Qwen2.5-Coder-3B-Revision;
- Qualitätsschwellen und Testset für Embeddings/Reranking;
- kompatibles native Target/Draft-Paar.

## Quellen

[1]: https://docs.nvidia.com/cuda/archive/13.1.1/turing-compatibility-guide/index.html "Turing Compatibility Guide for CUDA Applications"  
[2]: https://learn.microsoft.com/en-us/windows-hardware/drivers/display/retrieving-graphics-memory-numbers "Retrieving Graphics Memory Numbers"  
[3]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/docs/build.md "llama.cpp b8842 Build Documentation"  
[4]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/tools/server/README.md "llama.cpp b8842 Server Documentation"  
[5]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/ggml/src/ggml-cpu/ggml-cpu.c "GGML CPU Backend b8842"  
[6]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/ggml/src/ggml-cuda/CMakeLists.txt "GGML CUDA CMake Configuration b8842"  
[7]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/docs/multimodal.md "llama.cpp b8842 Multimodal Documentation"  
[8]: https://github.com/ggml-org/llama.cpp/blob/455d8e4be/docs/speculative.md "llama.cpp b8842 Speculative Decoding documentation"  
[9]: https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF "Qwen3-VL-4B-Instruct GGUF Model Card"  
[10]: https://huggingface.co/InternScience/Agents-A1-4B-Q4_K_M-GGUF "InternScience Agents-A1-4B Q4_K_M GGUF"  
[11]: https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF "Qwen2.5-Coder-1.5B-Instruct GGUF"  
[12]: https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF "Qwen2.5-Coder-3B-Instruct GGUF"  
[13]: https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF "Qwen2.5-Coder-7B-Instruct GGUF"  
[14]: https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct-GGUF "Qwen2.5-Coder-14B-Instruct GGUF"  
[15]: https://huggingface.co/Qwen/Qwen3-4B-GGUF "Qwen3-4B GGUF"  
[16]: https://qwen.readthedocs.io/en/v3.0/run_locally/llama.cpp.html "Qwen llama.cpp local execution guide"  
[17]: https://huggingface.co/Qwen/Qwen3-8B-GGUF "Qwen3-8B GGUF"  
[18]: https://huggingface.co/api/models/ggml-org/gemma-3-4b-it-GGUF/tree/main?recursive=false&expand=false "Gemma 3 4B GGUF file listing"  
[19]: https://huggingface.co/ggml-org/SmolVLM2-2.2B-Instruct-GGUF "SmolVLM2-2.2B Instruct GGUF"  
[20]: https://huggingface.co/meta-llama/Llama-2-7b-chat-hf "Meta Llama 2 7B Chat model card"
[21]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/src/llama-arch.cpp "llama.cpp b8842 Architekturregistry"
[22]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/ggml/src/ggml-cuda/ggml-cuda.cu "llama.cpp b8842 CUDA Operatoren"
[23]: https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/ggml/src/ggml-vulkan/ggml-vulkan.cpp "llama.cpp b8842 Vulkan Operatoren"
[24]: https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct/raw/main/config.json "Qwen3-VL-4B Konfiguration"
[25]: https://huggingface.co/InternScience/Agents-A1-4B/raw/main/config.json "Agents-A1-4B Konfiguration"
[26]: https://ai.google.dev/gemma/docs/core/model_card_3 "Gemma 3 Modellkarte"
[27]: https://huggingface.co/meta-llama/Llama-3.2-1B-Instruct "Llama 3.2 1B Instruct Modellkarte"
[28]: https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF "Llama 3.2 1B Community GGUF"
[29]: https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF "Llama 3.2 3B Community GGUF"
