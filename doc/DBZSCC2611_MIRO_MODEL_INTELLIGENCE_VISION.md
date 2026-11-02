# DBZSCC2611 – Model Intelligence \& Resource Orchestrator (MIRO)

## Vision und Architekturleitbild

## 1\. Zweck und Geltungsbereich

MIRO ist die intelligente Modell-, Capability-, Hardware- und Ressourcenverwaltung von DBZSCC2611.

**DBZSCC2611 ist ausschließlich für einen Benutzer und dessen eigenen PC konzipiert.** MIRO ist daher local-first und auf die konkret vorhandene Workstation optimiert. Es gibt keine Multi-Tenant-, Cluster- oder Enterprise-Skalierungsanforderungen.

Externe Quellen und Dienste wie Hugging Face, GitHub und Brave Search sind optionale Recherche- und Downloadquellen. Sie ändern nichts am lokalen Betriebsmodell.

Kernidee: **Nicht nur einzelne Modelle laden, sondern passende, getestete Modell- und Rollenbesetzungen als ausführbare Use-Case-Profile verwalten.**

## 2\. Architekturziele

* Lokale Modelle und lokale Runtime als Standard.
* Hardwarebewusste Auswahl und Ressourcenplanung.
* Modellfähigkeiten technisch erfassen und praktisch validieren.
* Modelle einzeln und in Kombination benchmarken.
* Rollen und Modelle zu realistischen Einsatzprofilen kombinieren.
* Profile vor App-Start laden und während des Betriebs kontrolliert wechseln.
* COT, ToT, Multi-Agent, RAG und Chat über dieselbe Modellintelligenz versorgen.
* Herkunft, Lizenz, Version, Konfiguration und Benchmark-Provenienz nachvollziehbar halten.

## 3\. MIRO – Funktionsbereiche

### 3.1 Model Discovery

Quellen:

* Lokaler Modellbestand, insbesondere konfigurierbare Pfade wie `D:\\\\Models`.
* Lokale Runtime- und Tool-Verzeichnisse, insbesondere `D:\\\\win\_runtimes\\\\llama`.
* Hugging Face Model Hub.
* GitHub-Repositories und Releases.
* Dokumentation und technische Veröffentlichungen, optional über Brave Web Search.

Discovery muss inkrementell arbeiten, Duplikate erkennen und kanonische Modellidentitäten von lokalen Dateipfaden und Varianten trennen.

### 3.2 Feature Extraction und Capability Analysis

Metadaten und Merkmale erfassen:

|Kategorie|Beispiele|
|-|-|
|Identität|Modellname, Hersteller, Architektur, Version|
|Größe|Parameterzahl, Dateigröße, Sharding|
|Quantisierung|GGUF-Quantisierung und weitere Formate|
|Kontext|Context Window, RoPE, Tokenizer|
|Modalität|Text, Bild, Audio, Video|
|Fähigkeiten|Chat, Coding, Reasoning, Tool Calling, JSON|
|Spezialrollen|Embeddings, Reranking, Draft, Vision|
|Runtime|llama.cpp, GGUF, Backend und Build|
|Ressourcen|RAM, VRAM, KV-Cache, Offloading|
|Herkunft|lokaler Pfad, HF-Repo, GitHub, Lizenz|

Capability-Erkennung erfolgt in mehreren Stufen:

1. Deklarierte Fähigkeiten aus Model Card und Metadaten.
2. Architektur- und Runtime-Prüfung.
3. Template- und Tokenizer-Kompatibilität.
4. Praktische Funktionstests.
5. Ergebnis mit Evidenz, Zeitstempel und Vertrauensgrad speichern.

Eine Model Card allein gilt nicht als Beweis, dass eine Fähigkeit in der konkreten Runtime-Konfiguration zuverlässig funktioniert.

### 3.3 llama.cpp und Toolchain Inspection

Die vorhandenen llama.cpp-Werkzeuge und Builds werden inventarisiert. Nicht nur `llama-server.exe`, sondern die tatsächlich verfügbaren Executables, DLLs, Backends und Build-Varianten sollen erfasst werden.

Beispiele aus der lokalen Toolchain können sein:

* `llama-cli`
* `llama-server`
* `llama-bench`
* `llama-quantize`
* `llama-embedding`
* `llama-parallel`
* weitere tatsächlich vorhandene Tools und Tests

Python- und Rust-Tools ergänzen die Extraktion, Validierung und Benchmark-Automatisierung. Die Tool-Auswahl muss dynamisch anhand des installierten Builds erfolgen; keine ausführbare Datei darf als vorhanden vorausgesetzt werden.

### 3.4 Multimodale Modelle und mmproj

Multimodale Modellkonfigurationen werden als zusammengehörige, validierte Einheiten verwaltet.

* Basismodell und kompatiblen Projektor (z. B. `mmproj`) erkennen.
* Architektur- und Runtime-Kompatibilität prüfen.
* Modell-/Projektor-Paar als Ladeeinheit registrieren.
* Speicherbedarf gemeinsam kalkulieren.
* Vision-Funktionstest durchführen.
* Konfiguration und Testergebnis versionieren.

Ziel ist zu prüfen, ob ein einziges multimodales Modell Chat, Coding und Vision sinnvoll abdecken kann oder ob getrennte Spezialmodelle besser sind.

## 4\. Hardware Intelligence

Die tatsächliche lokale Hardware ist die Referenz. MIRO ermittelt mindestens:

* CPU-Modell, Kerne und relevante Instruktionsmerkmale.
* Gesamten und verfügbaren RAM.
* GPU(s), Architektur und verfügbare Backends.
* dedizierten VRAM und Shared Memory.
* Treiber- und Runtime-Versionen.
* llama.cpp-Builds und unterstützte Beschleunigung.
* Speicherbudgets und gleichzeitige Ressourcenbelegung.

### Hardwarebewusste Zuordnung

Beispielhafte Rollenverteilung, keine fest verdrahtete Regel:

|Ressource|Mögliche Rollen|
|-|-|
|iGPU / Shared Memory|kleine Embedding-, Reranking-, Translation-, Prompt-Enhancement- und Subtask-Modelle|
|CPU / Hybrid|Orchestrierung, Architect, UI Inspector, Draft-Modell und Hintergrundaufgaben|
|dedizierte GPU|Haupt-Chat, Coding, Vision und Reviewer|

Die konkrete Zuordnung muss Backend-Unterstützung, Speicherart, Bandbreite, Offloading-Möglichkeiten, Parallelität und gemessene Performance berücksichtigen.

Shared Memory darf nicht als zusätzlicher kostenloser VRAM behandelt werden. Bei Layer-Offloading auf mehrere Geräte muss die tatsächliche Speicher- und Performancewirkung gemessen werden.

## 5\. Resource Estimation

Vor dem Laden muss MIRO den erwarteten Ressourcenbedarf abschätzen:

* Modellgewichte und Quantisierung.
* Kontextlänge und KV-Cache.
* Batch- und Parallelitätsparameter.
* multimodale Projektoren und zusätzliche Speicherpuffer.
* Runtime-Overhead.
* bereits belegte Ressourcen.
* Sicherheitsreserve für Betriebssystem und andere Anwendungen.

Die Schätzung muss Unsicherheit und Sicherheitsmarge ausweisen. Nach dem Laden werden Prognose und tatsächlicher Verbrauch verglichen und zur Verbesserung der Schätzungen gespeichert.

## 6\. Benchmark Lab

### 6.1 Einzelmodell-Benchmarks

* Ladezeit und Time to First Token.
* Prefill- und Decode-Tokens pro Sekunde.
* Latenz und Durchsatz.
* RAM-/VRAM-Verbrauch.
* Verhalten bei unterschiedlichen Kontextlängen.
* Coding- und Reasoning-Aufgaben.
* Tool Calling und strukturierte Ausgabe.
* Vision- und Multimodaltests.
* Stabilität, Fehlerquote und Abbrüche.

### 6.2 Vergleichs- und Kombinationstests

Modelle sollen gegeneinander und miteinander getestet werden:

* Modell A allein gegen Modell B allein.
* A und B parallel.
* Planner A mit Coder B.
* Coder A mit Reviewer B.
* kleines Draft-Modell mit Target-Modell.
* Multi-Agent Consensus mit unterschiedlichen Modellbesetzungen.
* Embedding- und Reranker-Kombinationen.

Jeder Test muss dieselben Aufgaben, vergleichbare Bedingungen und dokumentierte Konfigurationen verwenden.

### 6.3 Spezielle Kompatibilitätsregeln

Speculative Decoding ist eine spezielle Modellpaarung. Draft- und Target-Modell müssen die erforderlichen Tokenizer- und Kompatibilitätsbedingungen erfüllen. Beliebige Modelle dürfen nicht automatisch als kompatibles Paar angeboten werden.

### 6.4 Bewertungsdimensionen

* Aufgabenqualität und Testerfüllung.
* Geschwindigkeit und Latenz.
* Ressourcenverbrauch.
* Effizienz im Verhältnis zur Qualität.
* Stabilität und Fehlerrate.
* Parallelisierungsverhalten.
* Gesamtnutzen des Modellverbunds.

Consensus darf nicht nur auf Mehrheitszählung beruhen. Testresultate, Belege, Widersprüche und Reviewer-Bewertungen müssen in die Entscheidung einfließen.

## 7\. Use-Case Model Profiles

Ein Profil beschreibt eine vollständige, ausführbare Modellbesetzung für einen konkreten Einsatzzweck.

Ein Profil enthält mindestens:

* eindeutige ID und Version.
* Use-Case und Beschreibung.
* Modellreferenzen und lokale Artefaktpfade.
* Rollen und Aufgabenverteilung.
* Hardware-/Backend-Zuordnung.
* Ressourcenbudgets und Sicherheitsreserven.
* Kontext- und Parallelitätslimits.
* Ladeprioritäten und Warm-up.
* Fallback-Modelle und Ausfallregeln.
* Timeouts und Retry-Verhalten.
* optionale mmproj- und Draft-/Target-Paarungen.
* Benchmark- und Validierungsnachweise.
* Kompatibilitätsbedingungen.

### Beispiel: Development Swarm

|Rolle|Modellklasse|mögliche Ressource|
|-|-|-|
|Main Coder|Coding LLM|GPU|
|Chat Assistant|General LLM|GPU / Hybrid|
|Planner|Reasoning LLM|CPU / GPU|
|Architect|General / MoE|CPU / GPU|
|Reviewer|Coding LLM|GPU / CPU|
|Draft Worker|kleines kompatibles LLM|CPU|
|Embeddings|Embedding Model|iGPU / CPU|
|Reranker|Cross-Encoder|iGPU / CPU|
|Vision Inspector|multimodales LLM|GPU / CPU|
|Prompt Enhancer|kleines LLM|CPU / iGPU|

### Weitere mögliche Profile

* Chat \& Coding.
* Research Swarm.
* Low Resource.
* Multimodal Studio.
* Maximum Throughput.
* frei konfigurierbares Custom Profile.

Diese Namen sind Use-Case-Vorschläge, keine bereits bestätigten oder benchmarkten Konfigurationen.

## 8\. Profile Lifecycle

Profile müssen sowohl vor dem App-Start als auch im laufenden Betrieb verfügbar sein.

### 8.1 Pre-Startup

1. Hardwareinventar laden.
2. ausgewähltes Profil und Version einlesen.
3. Modell- und Projektor-Artefakte auflösen.
4. Kompatibilität prüfen.
5. Ressourcenbudget gegen aktuelle Verfügbarkeit validieren.
6. Runtime-Konfiguration vorbereiten.
7. erforderliche Modelle laden und Warm-up durchführen.
8. Startbereitschaft melden oder verständlichen Fehler ausgeben.

### 8.2 Runtime

* Modelle dynamisch laden und entladen.
* Rollen und Agenten neu zuweisen.
* Profile kontrolliert wechseln.
* Ressourcen überwachen.
* Fallbacks aktivieren.
* laufende Jobs berücksichtigen.
* Änderungen und Zustandsübergänge protokollieren.

Profilwechsel erfolgen transaktional: erst Zielprofil und Ressourcen prüfen, dann kontrolliert vorbereiten und umschalten. Laufende Jobs oder Modellinstanzen dürfen nicht unkontrolliert zerstört werden. Rollback auf den letzten gültigen Zustand muss möglich sein.

### 8.3 Shutdown und Persistence

* aktives Profil und relevanten Zustand sichern.
* Ressourcen kontrolliert freigeben.
* Modell- und Profilinformationen persistent halten.
* beim nächsten Start den letzten gültigen Zustand wiederherstellen können.

## 9\. Profile-Portabilität

Profile können konkrete Hardwarereferenzen und abstrakte Anforderungen kombinieren.

Beispielhafte Labels:

* `AMD\_LOCAL\_16GB`
* `LOW\_RESOURCE`
* `GPU\_ACCELERATED`
* `MULTIMODAL\_WORKSTATION`

Diese Labels sind lokale Profil- und Kompatibilitätsklassen, keine Anforderungen an Cluster oder fremde Rechner.

Bei geänderter Hardware wird ein Profil neu validiert und gegebenenfalls angepasst. Modellzuordnungen und Speicherbudgets dürfen nicht blind übernommen werden.

## 10\. Integration mit COT, ToT und Multi-Agent

MIRO liefert dem Cognitive Orchestrator:

* verfügbare Modelle und Fähigkeiten.
* Rollen-Eignung und Capability-Evidenz.
* Ressourcenverfügbarkeit.
* Benchmark-basierte Leistungsdaten.
* aktive Profile und Fallbacks.
* Lade- und Ausführungszustände.

Der Orchestrator kann damit Planner, Architect, Coder, Reviewer, RAG-Komponenten und weitere Worker passend zur Aufgabe auswählen.

Die GUI zeigt nachvollziehbare Planungszusammenfassungen, Alternativen, Bewertungen, Agentenbeiträge und Belege – keine ungefilterten privaten Chain-of-Thought-Inhalte.

## 11\. Datenhaltung und Nachvollziehbarkeit

Die Datenhaltung sollte logisch getrennte, verknüpfte Datensätze vorsehen:

* Model Registry.
* Artifact Registry und lokale Pfade.
* Capability Results.
* Hardware Inventory.
* Runtime/Backend Inventory.
* Benchmark Runs.
* Profile Registry und Versionen.
* Compatibility Checks.
* Load/Unload Events.
* Profile Activation History.

Jeder Benchmark und jede Profilentscheidung soll auf Modellversion, Runtime-Build, Hardwarezustand und Testkonfiguration zurückführbar sein.

## 12\. Vorgeschlagene Komponentenstruktur

```text
MIRO
├── discovery
│   ├── local\_scanner
│   ├── huggingface\_provider
│   ├── github\_provider
│   └── web\_research
├── extraction
│   ├── gguf\_metadata
│   ├── architecture
│   ├── tokenizer\_template
│   ├── capability\_analyzer
│   └── mmproj\_resolver
├── hardware
│   ├── inventory
│   ├── backend\_detection
│   └── resource\_estimator
├── benchmark
│   ├── single\_model
│   ├── pairing
│   ├── multi\_agent
│   └── evaluator
├── profiles
│   ├── registry
│   ├── validator
│   ├── planner
│   ├── loader
│   └── hot\_swap
└── integration
    ├── runtime
    ├── cognitive\_orchestrator
    └── gui
```

Die Struktur ist ein Architekturvorschlag und muss vor Implementierung mit dem bestehenden DBZSCC2611-Repository abgeglichen werden.

## 13\. Umsetzung in sinnvollen Slices

### Slice 1 – Inventarisierung

Lokale Modelle, Pfade, llama.cpp-Tools, Hardware und Backends zuverlässig erfassen.

### Slice 2 – Model Registry

Kanonische Modellidentitäten, Artefakte, Metadaten und Herkunft speichern.

### Slice 3 – Capability Extraction

Metadaten- und Dokumentationsanalyse mit praktischen Funktionstests kombinieren.

### Slice 4 – Hardware Profiler

Speicher- und Backend-Erkennung sowie konservative Ressourcenabschätzung.

### Slice 5 – Benchmark Lab

Reproduzierbare Einzelmodell- und Kombinationstests implementieren.

### Slice 6 – Profile Manager

Use-Case-Profile definieren, validieren, versionieren und persistent speichern.

### Slice 7 – Runtime Integration

Pre-Startup-Laden, kontrolliertes Hot-Swap, Fallback und Rollback integrieren.

### Slice 8 – Cognitive Integration

COT, ToT, Multi-Agent und RAG mit den verfügbaren Profilen verbinden.

## 14\. Nicht-Ziele

* Kein Multi-Tenant-System.
* Keine allgemeine Cloud-Plattform.
* Keine Cluster-Orchestrierung.
* Keine künstliche Unterstützung beliebiger Hardware ohne tatsächlichen Nutzen.
* Keine pauschale Modellzuordnung allein anhand von Modellnamen.
* Keine automatische globale Übernahme ungeprüfter Fähigkeiten oder Benchmark-Ergebnisse.

## 15\. Erfolgskriterien

MIRO ist erfolgreich, wenn DBZSCC2611:

1. den lokalen Modellbestand und die Toolchain zuverlässig erkennt.
2. technische Merkmale und Fähigkeiten mit nachvollziehbarer Evidenz erfasst.
3. realistische Ressourcenanforderungen für die eigene Hardware bestimmt.
4. Modelle einzeln und als Kombination reproduzierbar benchmarken kann.
5. passende Use-Case-Profile erstellt und validiert.
6. Profile vor App-Start laden kann.
7. Profile während des Betriebs kontrolliert wechseln kann.
8. COT, ToT, Agenten und RAG mit passenden Profilen versorgen kann.

**Zielbild:** DBZSCC2611 stellt für den konkreten PC aus den verfügbaren lokalen und optional externen Modellen eine geeignete, getestete und ressourcenbewusste KI-Arbeitsumgebung zusammen.

