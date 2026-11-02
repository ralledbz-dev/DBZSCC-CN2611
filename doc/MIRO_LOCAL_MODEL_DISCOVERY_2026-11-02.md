# MIRO – lokale Modellinventur (2. November 2026)

## Ergebnis in Kürze

Der vorhandene `run_local_discovery`-Ablauf wurde einmal auf `D:\Models` ausgeführt. Er fand **260 GGUF-Kandidaten**. Bei **259** ließ sich der begrenzte GGUF-Metadatenpräfix lesen; **ein** Kandidat überschritt das konfigurierte 16-MiB-Limit. Die Registry meldete 242 aufgelöste, 18 nicht aufgelöste und keine partiellen Modellidentitäten.

Die SQLite-Registry lag in einer temporären Datei außerhalb von Projekt- und Modellordner und wurde nach dem Lauf entfernt. Es wurden weder Modelle gestartet noch Inferenz, Downloads oder Installationen ausgeführt. Es gab keine Änderungen am bestehenden Code und keine Commits oder Pushes.

## Verzeichniszugriff und Parserproblem

Der aktuelle Scan meldete genau einen `directory_read_failed`-Fehler:

- `D:\Models\ModelVault\Claude\.pytest_cache` — **Zugriff verweigert**.

Es gab keine weiteren Scannerfehler. Die beiden verfügbaren MIRO-Evidenzdokumente nennen die drei Pfade aus dem früheren Lauf nicht; deshalb lässt sich nicht verlässlich sagen, welcher frühere Pfad diesem aktuellen Fehler entspricht oder die früheren drei Pfade einzeln vergleichen. Der aktuelle Lauf zeigt aber, dass die restliche sichtbare Verzeichnisstruktur ohne gemeldeten Verzeichnislesefehler durchlaufen wurde. Es wurde kein anderer Scanner als Umgehung verwendet.

Ein GGUF konnte nicht bis zum Ende seiner Metadaten gelesen werden:

- `D:\Models\ModelVault\models\audio\kokoro-gguf\weights\Kokoro_no_espeak_Q4.gguf` — `header_size_limit_exceeded`; der Metadatenpräfix überschreitet das konfigurierte Limit.

## Kandidaten für spätere Prüfung

Die folgende Auswahl priorisiert **nur grob nach kleinerer Dateigröße und lesbarer Metadatenidentität**. Sie ist eine Shortlist für mögliche Folgetests, keine Aussage, dass die Dateien praktisch funktionieren, für eine bestimmte Aufgabe taugen oder in 4 GB VRAM passen.

| Artefakt | Größe | Lesbare Metadatenhinweise | Einordnung |
|---|---:|---|---|
| `D:\Models\ModelVault\models\llm\misc\tiny-llm-tinystories-pytorch-q4-0\weights\tiny-llm-tinystories-pytorch-q4_0.gguf` | 11,105,248 Byte (10.6 MiB) | Name „Tiny LLM TinyStories PyTorch“, Größenlabel `13M`, Architektur `llama`, deklarierter Kontext 1.024, 1 Block, Embedding-Dimension 192 | Niedrigste klar benannte kleine Textmodell-Spur in dieser Auswahl; Qualität und tatsächlicher Einsatzzweck bleiben ungeprüft. |
| `D:\Models\ModelVault\models\llm\misc\axl-translate-q4-k-m-v2\weights\axl_translate-q4_k_m_v2.gguf` | 17,934,656 Byte (17.1 MiB) | Metadatenname `multiscale_transformer`, Architektur `llama`, 12 Blöcke, deklarierter Kontext 256, Embedding-Dimension 256; kein Parameter- oder Größenlabel | Kleine Spur, aber Identität und Einsatzrolle bleiben unklar. |
| `D:\Models\ModelVault\models\llm\misc\smollm2-prompt-enhance-gguf\weights\SmolLM2-Prompt-Enhance.Q4_K_M.gguf` | 105,455,136 Byte (100.6 MiB) | Metadatenname „SmolLM2 135M Instruct“, Größenlabel `135M`, deklarierter Kontext 8.192, 30 Blöcke, Embedding-Dimension 576; Chat-Template ist deklariert | Plausibler Kandidat für einen späteren allgemeinen Text-/Dialogtest; Chat-Template und Modellname belegen keine Dialogqualität oder Coding-Fähigkeit. |
| `D:\Models\ModelVault\models\multimodal\gemma-gguf\base\simllama-1-instruct-i-quality.gguf` | 134,025,856 Byte (127.8 MiB) | Metadatenname nur `Model`, Größenlabel `162M`, Architektur `llama`, deklarierter Kontext 2.048; Chat-Template ist deklariert | Wegen des generischen Namens nur nachrangig; die Metadaten reichen nicht für eine belastbare Profilzuordnung. |
| `D:\Models\ModelVault\models\llm\lfm\lfm2-5-230m-gguf\weights\LFM2.5-230M-Q4_0.gguf` | 149,080,928 Byte (142.2 MiB) | Größenlabel `230M`, Architektur `lfm2`, deklarierter Kontext 128.000, 14 Blöcke, Embedding-Dimension 1.024; für `lfm2.attention.head_count_kv` wurde kein positiver Integer extrahiert | Prüfenswert, aber das Kontextlimit ist nur deklariert; ein Metadatenproblem bleibt offen. |

Zwei weitere kleine Artefakte wirken anhand ihrer Metadaten eher wie mögliche Retrieval-Bausteine als wie Antwortmodelle: `nomic-embed-text-v1.5` (Architektur `nomic-bert`, deklarierter Kontext 2.048) und `Jina Bert Implementation` (Architektur `jina-bert-v2`, Größenlabel `33M`). Das belegt weder passende Embedding-/Rankingqualität noch einen funktionierenden RAG-Aufbau.

Ein sehr kleiner `qwen3-embed-ref.gguf`-Eintrag ist nur 4,320 Byte groß, hat zwei Metadatenfelder und keine aufgelöste Identität. Er wird deshalb nicht als verwendbares generatives Qwen-Modell priorisiert.

## Grenzen der Evidenz

Der Reader verarbeitet GGUF v2/v3 und stoppt nach dem Metadatenbereich. Er liest keine Tensorbeschreibungen oder Gewichte; sein SHA-256 ist nur der Hash des gelesenen Metadatenpräfixes, nicht der vollständigen Datei. Dateiname, Größe, Architektur, Parameter-/Größenlabel, Kontextlänge und Chat-Template sind keine Funktions- oder Qualitätsnachweise. Die Vorschlagsrollen aus dem Profilkatalog sind hier lediglich Konzeptkontext und keine verbindlichen Anforderungen oder bestätigten Fähigkeiten.

Es wurde keine llama.cpp-Runtime gestartet, keine Inferenz oder Benchmark ausgeführt, kein Hardware-Fit geprüft und keine Bild-/Audio-Ende-zu-Ende-Funktion verifiziert. Die Referenz `Ryzen 5 5600G / 32 GB RAM / GTX 1650 Super mit 4 GB VRAM` ist die bereitgestellte Hardwareangabe, keine Messung aus diesem Scan. Insbesondere belegt ein kleines GGUF-Datei- oder Metadatenlabel nicht, dass ein Modell in den verfügbaren RAM/VRAM passt.

Weitere technische Grenzen und der bestehende Vertrag stehen in [MIRO – lokale Artefaktinventarisierung und GGUF-Metadaten](MIRO_LOCAL_DISCOVERY.md) und [MIRO Model Registry — Gesamtplan und Implementierungsnachweis](MIRO_MODEL_REGISTRY_PLAN_AND_EVIDENCE.md).
