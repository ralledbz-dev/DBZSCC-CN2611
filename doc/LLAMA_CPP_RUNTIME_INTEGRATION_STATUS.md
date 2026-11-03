# llama.cpp-Windows-Runtimes – Integrationsstatus

**Stand:** 3. November 2026 · **Ergebnis:** drei lokale Runtime-Varianten additiv in den Checkout integriert und getestet. Die Binärdateien stammen aus dem vom Nutzer benannten lokalen Bestand, nicht aus einem in diesem Schritt verifizierten Upstream-Archiv.

## Was im Projekt liegt

Unter [`runtimes/llama.cpp`](../runtimes/llama.cpp/README.md) liegen jetzt drei getrennte Windows-x64-Builds: `cpu-x64` (42 Dateien, 48.208.440 Byte), `cuda-13.1-x64` (43 Runtime-Dateien plus 3 CUDA-DLLs, insgesamt 46 Dateien/724.670.856 Byte) und `vulkan-x64` (43 Dateien, 110.175.288 Byte). Gesamtumfang der kopierten Runtime-Dateien: 883.054.584 Byte. CUDA-Abhängigkeiten `cudart64_13.dll`, `cublas64_13.dll` und `cublasLt64_13.dll` wurden aus dem separaten lokalen CUDA-Abhängigkeitspfad neben die CUDA-Binaries kopiert, sodass sie aus dem App-Pfad geladen werden können.

Quelle waren ausschließlich die lokalen Nutzerordner `D:\win_runtimes\llama\llama-b8842-bin-win-cpu-x64`, `...-cuda-13.1-x64`, `...-vulkan-x64` und `cudart-llama-bin-win-cuda-13.1-x64`. Die Quellordner wurden nicht verändert. Die Ziele waren vor dem Kopieren nicht vorhanden; vorhandene, bereits geänderte Projektdateien wurden nicht überschrieben. `D:` hatte unmittelbar davor 218.635.239.424 Byte frei (203,62 GiB).

Alle drei kopierten `llama-cli.exe` melden `version: 8842 (455d8e4be)`, Clang 19.1.5, Windows x86_64. Die drei `llama-cli.exe` haben denselben lokalen SHA-256 `B98B6096E39340414EA3009E3688CAE1E8F402917B7B246F40D57A241F130FEB`. Die llama.cpp-Programme und Backend-DLLs sind nicht Authenticode-signiert. Die drei mitgelieferten CUDA-DLLs und `libomp140.x86_64.dll` meldeten bei Windows eine gültige Authenticode-Signatur; die Datei-Hashes stehen im lokalen [`MANIFEST.csv`](../runtimes/llama.cpp/MANIFEST.csv). Diese lokalen Hashes dienen der Erkennung späterer Änderungen; sie authentifizieren die Builds nicht gegen Upstream.

Das Manifest enthält 138 Dateien mit insgesamt 883.075.217 Byte; die Manifestdatei selbst ist ausgenommen. Alle 131 kopierten Runtime-Dateien wurden zusätzlich per SHA-256 mit ihren freigegebenen lokalen Quellordnern verglichen; sie stimmten alle überein. Das belegt die Kopie, nicht die Upstream-Herkunft.

## Upstream-Herkunft und erwartete Archiv-Digests

Die offiziellen Sollwerte stammen aus der [llama.cpp-Veröffentlichung b8842](https://github.com/ggml-org/llama.cpp/releases/tag/b8842) und ihrer [GitHub-Release-API](https://api.github.com/repos/ggml-org/llama.cpp/releases/310856260). Die b8842-Bezeichnung im Ordner und der vom Programm ausgegebene Versionsstring passen zusammen, sind aber kein kryptografischer Nachweis, dass die lokalen Dateien aus diesen Archiven stammen. Die Versuche, die offiziellen ZIPs direkt auf RAZOR zu laden, scheiterten vor Empfang eines Archivs; deshalb konnten weder Archiv- noch Inhaltshashes gegen die Upstream-Sollwerte verglichen werden.

| Offizielles Asset | Größe laut GitHub | Erwarteter SHA-256 (nicht lokal verifiziert) |
|---|---:|---|
| [`llama-b8842-bin-win-cpu-x64.zip`](https://github.com/ggml-org/llama.cpp/releases/download/b8842/llama-b8842-bin-win-cpu-x64.zip) | 15.889.810 Byte | `f4ab0f472e5731eae424251e9aaa41dc989c5bc1acbf8a2451b61474e41672cd` |
| [`llama-b8842-bin-win-cuda-13.1-x64.zip`](https://github.com/ggml-org/llama.cpp/releases/download/b8842/llama-b8842-bin-win-cuda-13.1-x64.zip) | 147.707.991 Byte | `8a151b48c654becd5a61ee06986da5f9bb76874153a17fc17e2b1753dca6cccd` |
| [`llama-b8842-bin-win-vulkan-x64.zip`](https://github.com/ggml-org/llama.cpp/releases/download/b8842/llama-b8842-bin-win-vulkan-x64.zip) | 34.033.494 Byte | `53c4948d46416396300fa2ac462d1ec71c6ed2192d68f5b8ac7dd42fbcc5f386` |
| [`cudart-llama-bin-win-cuda-13.1-x64.zip`](https://github.com/ggml-org/llama.cpp/releases/download/b8842/cudart-llama-bin-win-cuda-13.1-x64.zip) | 402.582.216 Byte | `f96935e7e385e3b2d0189239077c10fe8fd7e95690fea4afec455b1b6c7e3f18` |

## Lizenzstand

Der lokale Paketbestand enthielt weder `LICENSE`, `NOTICE`, `EULA`, `COPYING` noch `THIRD-PARTY`-Dateien. Die vollständige llama.cpp-MIT-Lizenz, geladen von [`ggml-org/llama.cpp` am Tag b8842](https://raw.githubusercontent.com/ggml-org/llama.cpp/b8842/LICENSE), liegt unter `runtimes/llama.cpp/licenses/LLAMA-CPP-MIT.txt`. Für `libomp140.x86_64.dll` sind Apache 2.0 und der LLVM-Exception-Text beigefügt; die vollständige maßgebliche [`LLVM/OpenMP-LICENSE.TXT`](https://raw.githubusercontent.com/llvm/llvm-project/main/openmp/LICENSE.TXT) kann weitere Notices enthalten und ist verlinkt. Die NVIDIA-CUDA-13.1.1-EULA ist nicht vollständig im Bundle enthalten; die [offizielle EULA](https://docs.nvidia.com/cuda/archive/13.1.1/eula/index.html) und die einschlägigen Windows-`cudart`-/cuBLAS-Attachment-A-Einträge sind in einer Referenznotiz vermerkt. Die Runtime-Ablage ist ausschließlich für persönliche lokale Nutzung vorgesehen. Die jeweils anwendbaren Lizenz- und EULA-Bedingungen bleiben maßgeblich und sind zu beachten; die Notizen sind keine vollständige EULA und heben keine Bedingungen auf.

## Gerätesichtbarkeit und Hardwaregrenzen

Die Nutzerangaben lauten: Displays seien an der AMD-iGPU angeschlossen; ihr seien 4 GB zugeteilt, typischerweise seien etwa 300 MB belegt. Die NVIDIA-GPU habe 4 GB VRAM und keine angeschlossenen Monitore. Diese Zuordnung und der typische iGPU-Verbrauch sind als Nutzerbericht festgehalten, nicht als unabhängig bestätigte Live-Messung. `Hardware.XML` nennt Ryzen 5 5600G und GTX 1650 SUPER. `nvidia-smi` bestätigte eine GTX 1650 SUPER mit 4.096 MiB.

Der Vulkan-Build listete `Vulkan0: AMD Radeon(TM) Graphics (18.119 MiB, 17.213 MiB frei)` und `Vulkan1: NVIDIA GeForce GTX 1650 SUPER (4.144 MiB, 3.554 MiB frei)`. Die großen AMD-Zahlen sind Runtime-/Vulkan-Geräteverfügbarkeit und **kein** Nachweis für dedizierten iGPU-VRAM; sie dürfen nicht als VRAM-Budget verwendet werden. Die Windows-AMD-Dedicated-/Shared-Budgetwerte ließen sich in diesem Lauf nicht live zuverlässig auslesen. Das sichtbare AMD/NVIDIA-Geräteinventar belegt Gerätepräsenz, aber weder einen gemeinsamen VRAM-Pool noch allgemeine geräteübergreifende Scheduling-Fähigkeit.

`RuntimeController` und Runtime-Seite integrieren nun den explizit vom Nutzer gestarteten App-eigenen Server: Build, GGUF, Backend, Kontext, Threads und Port werden ausgewählt; vor dem Start erfolgt ein frischer Ressourcen-Preflight; Health und Modellkennung werden lokal geprüft; beendet wird nur der eigene Prozess. Es gibt weiterhin keinen Autostart oder automatische Build-Auswahl. Der echte verwaltete End-to-End-Lauf war CPU-only; CUDA und Vulkan bleiben getrennte Builds/Prozesse, und ein Backend-Cross-Pool wird nicht behauptet. Der frühere begrenzte Vulkan-CLI-Test belegt Modellpuffer auf beiden Vulkan-Geräten, aber keinen Managed-GPU-Lifecycle.

## Tatsächliche Prüfungen

Die zuvor dokumentierten CPU/CUDA/Vulkan-CLI-Smokes nutzten ausschließlich das vorhandene `Qwen2.5-0.5B-Instruct-Q8_0.gguf` aus `D:\Models`, nacheinander in separaten Prozessen, mit kleinem Kontext 128, 2 Threads und höchstens 4 Ausgabetokens. Der Nutzer hatte den Modellstart ausdrücklich freigegeben. Kein Prozess blieb zurück. Der spätere Managed-Server-Workflow-Smoke ist getrennt im letzten Abschnitt dokumentiert.

| Variante | Ergebnis und Beleg |
|---|---|
| CPU/x64 | `--version` und `--list-devices` auf dem kopierten App-Build: Exit 0; CPU-Backend geladen. Begrenzte Qwen-Probe antwortete `OK` ohne GPU-Offload und endete normal. |
| CUDA 13.1 | Kopierter App-Build fand CUDA-Gerät 0: GTX 1650 SUPER, Compute Capability 7.5, 4.095 MiB vom Runtime-Build gemeldet. `--version`/`--list-devices` funktionierten nach dem Kopieren auch ohne PATH-Erweiterung, weil die drei CUDA-DLLs neben den Executables liegen. Begrenzte Qwen-Probe antwortete `OK`; die Ressourcenwache löste nicht aus. NVIDIA meldete nach Ende 3.930 MiB frei von 4.096 MiB. Der Build meldete fehlende Tensor-Cores als Performancehinweis; es ist keine Beschleunigungsgarantie. |
| Vulkan multi-device + CPU | Preflight unmittelbar vor Start: 11.164 MiB RAM verfügbar, 60 % Speicherauslastung, 5,7 % CPU, 3.930 MiB NVIDIA-Speicher frei. Start mit `--device Vulkan0,Vulkan1 --split-mode layer --tensor-split 1,1 --n-gpu-layers 2`, Kontext 128, Batch 32, 2 Threads und max. 4 Tokens. Antwort `OK`, Exit 0, Laufzeit 3,56 s. Die Runtime meldete währenddessen 21 MiB selbst/model/compute auf AMD `Vulkan0`, 156 MiB auf NVIDIA `Vulkan1` und 503 MiB Host (500 MiB Modell, 2 MiB Kontext); die CPU- und Vulkan-Backends waren geladen. Beim einmaligen Live-Sample nach 1,7 s: 10.893 MiB RAM verfügbar, 61 % Belegung, 25,4 % CPU und 3.770 MiB NVIDIA-Speicher frei. Danach: 11.157 MiB RAM verfügbar und 3.930 MiB NVIDIA-Speicher frei. Das belegt nur diese Zwei-GPU-Layer-Konfiguration mit CPU-Rest und diesem kleinen Modell, keinen gemeinsamen VRAM-Pool. |
| Nicht-inferenzierende App-/Runtime-Smokes | Nach dem Kopieren meldeten alle drei `llama-cli.exe --version`- und `--list-devices`-Aufrufe Exit 0. Vulkan listete AMD und NVIDIA erneut. Die App-UI-Smoke-Tests liefen offscreen und bestätigten, dass beim Start kein Runtime-Probe/Modellscan erfolgt; sie prüfen nicht den sichtbaren UI-Start/Stopp eines Servers. Der Prozess-PATH war nach den Runtime-Smokes unverändert. |

Direkt nach dem Vulkan-Test waren 11.157 MiB RAM verfügbar, die RAM-Auslastung lag bei 60 %, die CPU bei 7,4 %, und `nvidia-smi` meldete 3.930 MiB frei; es liefen keine `llama-cli`-/`llama-server`-Prozesse. Für den vorherigen `llama-server`-PID 16712 lag eine separate Nutzerfreigabe zum Beenden vor; bei den Modelltests wurde kein anderer Prozess beendet.

Aktuelle Vollsuite: Python 3.14.4 im `.work\venv314` → **208 passed, 139 DeprecationWarnings**; Python 3.15.2 im App-`.venv` → **199 passed, 9 skipped** (die optionalen Workflowfälle sind im 3.15.2-Gate deaktiviert). Ruff im 3.14.4-venv: **All checks passed!** Der Live-Server-Smoke ist separat dokumentiert und wird nicht durch die Unit-/Offscreen-Tests vorgetäuscht. Es wurden keine globalen Python-Pakete, Treiber, Firewallregeln oder System-PATH-Änderungen vorgenommen; kein Commit/Push erstellt.

## Nutzung und offene Grenzen

Auf der Runtime-Seite werden Serverbuild, Modell, Backendparameter und Loopback-Port ausdrücklich gewählt. **Planen** zeigt den frischen Ressourcenentscheid; **Starten** startet den app-eigenen Server; Health und `/v1/models` werden geprüft; **Stoppen** beendet nur diesen eigenen Prozess. Es gibt keinen Autostart, keinen stillen Backendwechsel und keine automatische Runtime-Ordnerauswahl. Die Runtime-Bundles sind noch nicht in einen Installer/Python-Wheel integriert. Dauerhafte Ressourcenreservierung, Idle-Unload, Fallback/Rollback und ein Managed-GPU-Start sind nicht verifiziert. AMD-zugewiesener Speicher, tatsächlich freie dedizierte iGPU-MiB, laufzeitweite Systemlast und Speicher-Headroom bleiben vor einer produktiven Inferenz zu erfassen; die Vulkan-Summe darf nicht als dediziertes VRAM interpretiert werden.

`src/dbzscc/app/resource_orchestrator.py` ist inzwischen an `RuntimeController` angebunden. Er plant aus einem frischen lokalen Snapshot pro Workload ein Backend-/Geräteziel und lehnt unzureichende oder unbekannte Ressourcendaten fail-closed ab. Er reserviert keine Ressourcen atomar über konkurrierende Workloads, splittet keine einzelne Workload über CUDA/Vulkan und bietet keinen allgemeinen Cross-Backend-Scheduler.


Bei der Abschlusskontrolle lagen außerdem `src/dbzscc/app/toolchain_runtime.py` und `tests/test_toolchain_runtime.py` ungetrackt im Arbeitsbaum. Das Modul enthält explizite Runtime-/Toolchain-Registrierung und typisierte Pläne, aber keine konkrete Prozess-Runner-Implementierung; die Aufrufe werden an injizierte Ports delegiert. Eine gezielte Suche fand keine Import- oder Aufrufstelle in anderen aktiven App-Modulen. Diese Dateien wurden von der Runtime-Ablage-Integration nicht verändert oder aufgerufen.


## Verwalteter Runtime- und Workflow-Smoke — 3. November 2026

Der exakte MIRO-Pfad `D:\Models\ModelVault\models\llm\qwen\qwen2-5-0-5b-instruct-gguf\weights\Qwen2.5-0.5B-Instruct-Q8_0.gguf` war vorhanden (531.068.224 Byte). Der lokale Parser bestätigte GGUF v3, 290 Tensoren, 34 Metadatenfelder, `qwen2`, 24 Blöcke, deklarierte Kontextlänge 32.768 und Embedding-Dimension 896. Der frische CPU-Ressourcen-Preflight ließ den Start zu.

Der app-eigene `RuntimeController` startete `llama-server` auf einem freien Loopback-Port. Eine instrumentierte Probe gegen denselben lokalen `GET /health`-Pfad beobachtete beim Laden HTTP 503 (`Loading model`) und anschließend HTTP 200 (`{"status":"ok"}`); `/v1/models` lieferte die echte Modellkennung. Danach lief im Python-3.14.4-venv ein echter LangChain-`RunnableSequence`-Request. Pydantic validierte einen Plan mit `inspect_project` und `run_tests`; die Modellbegründung paraphrasierte den Auftrag, und eine Qualitätsbewertung wurde nicht vorgenommen. Der injizierte Diagnose-Callback machte den Healthstatus sichtbar; der Standard-Healthprobe-Pfad wurde in diesem Live-Smoke nicht separat statt des Callbacks aufgerufen.

Der Controller stoppte ausschließlich seinen eigenen Prozess und meldete `stop() == True`; Windows berichtete beim absichtlichen Terminate Exitcode 1. Das ist der rohe Prozessstatus, kein behaupteter normaler Exitcode 0. Es wurde kein fremder Prozess beendet. Strukturierte Status-/Tool-/stdout-/stderr-Ereignisse wurden in einer neuen SQLite-Datei unter `.work/integration_verify_20261103` gespeichert; der Diagnosemitschnitt war auf 160 Ereignisse/24.000 Zeichen begrenzt.

Die zunächst eingeschränkte Loopback-Probe scheiterte mit Windows-Socketfehler 10013; ein separater lokaler Listener reproduzierte den Fehler. Beim erfolgreichen Diagnoseprozess war ausschließlich `127.0.0.1` freigegeben. Es wurden keine Firewall-/Portregeln, globalen Python-Pakete, Treiber oder System-PATH geändert. Der Befund belegt die lokale Runtime im Diagnosekontext, nicht automatisch jede spätere App-Ausführung unter einer anderen Netzwerkpolicy.

Tests am damaligen Zwischenstand: Python 3.14.4 → **208 passed, 139 Dependency-DeprecationWarnings**; Python 3.15.2 → **199 passed, 9 Workflowtests übersprungen**; Ruff → **All checks passed**. Inzwischen ist der manifestgebundene Projekt-/Debug-Consumer für PySide6/Uvicorn implementiert und ein echter UI-SSE-Chat über den verwalteten CPU-Endpunkt geprüft. Weiter offen sind allgemeine Projektprozesse außerhalb der festen Allowlist, Live-GPU-Managed-Start, echter Bild-/Audio-/Projektor-Lauf, der separate Datei-Diff-/Apply-Livepfad und die durchgängige Test-/Evidence-Verknüpfung. Keine Commits oder Pushes.

## Spätere Runtime- und Projekt-Debug-Abnahme — 3. November 2026

Der app-eigene `RuntimeController` startete den lokalen CPU-Build `llama-server 8842 (455d8e4be)` mit dem vorhandenen Qwen2.5-0.5B-Instruct-Q8_0-Modell über den Standardport `8080`. Ressourcenplan, Standard-Healthcheck und `/v1/models`-Zuordnung bestanden; die echte UI-SSE-Chatantwort war `OK` und wurde zusammen mit den Lauf-/Ressourcenmetadaten gespeichert. Der app-eigene Prozess wurde beendet, und Port 8080 war danach geschlossen. Das ist ein einfacher Textfunktionsnachweis, keine Qualitäts- oder Performancebewertung.

Der Workflows-Bereich führte nach separater Bestätigung die Manifestaktion `DEBUG_PROJECT` für `pyside6-module = dbzscc` mit dem festen Debug-`argv` `python -B -X dev -X faulthandler -m dbzscc` aus. Der frische Ressourcenplan für 1.536 MiB Hostbedarf und zwei CPU-Threads wurde zugelassen; Prozess-Liveness und Eventpersistierung funktionierten. Der UI-Stop beendete den app-eigenen Prozessbaum, danach war `has_owned_process == False` und Port 18080 geschlossen. Der Kindprozess lief im Smoke-Test mit Qt-Offscreen und isoliertem App-Datenordner.

Die Gesamtprüfung lief mit Python 3.14.4: **229 passed**, Ruff **All checks passed**. 139 DeprecationWarnings stammen aus dem installierten LangChain-/LangSmith-Stack. Es wurden keine Firewallregeln, globalen Pakete oder System-PATH-Einträge verändert. Verwalteter GPU-Start, multimodale Inferenz und der Datei-Diff-/Apply-Pfad bleiben nicht live end-to-end geprüft.


## GPU-CLI-Follow-up und Abschlusskontrolle — 3. November 2026

Vor den separaten, begrenzten Modellstarts wurde das exakte Qwen2.5-0.5B-Instruct-Q8_0-Modell (531.068.224 Byte) geprüft. Die RAM-/Commit-Messung kam aus der lesenden Windows-API `GlobalMemoryStatusEx`, CPU aus `GetSystemTimes`, GPU-Speicher/Last aus `nvidia-smi`; WMI war für das Ausführungskonto nicht lesbar. Vor beiden Starts waren die Prozesse `llama-cli`/`llama-server` sowie die Listener auf 8080 und 18080 abwesend.

| Backend | Unmittelbarer Preflight | Begrenzter Aufruf | Ergebnis |
|---|---|---|---|
| CUDA 13.1 / GTX 1650 SUPER | 12.189 MiB RAM frei, 16.853 MiB Commit frei, 4,3 % CPU, 3.930 MiB VRAM frei, 0 % GPU-Last | `llama-cli`, exakt ausgewähltes Qwen-GGUF, 2 GPU-Layer, Kontext 128, Batch 32, 2 Threads, höchstens 4 Tokens | Nach 90 Sekunden ohne bestätigten Abschluss beendet; ausschließlich der für diesen Smoke gestartete CLI-Prozess wurde abgebrochen. Kein erfolgreicher Modelllauf oder Antworttext belegt. |
| Vulkan / explizites `Vulkan1` | 12.431 MiB RAM frei, 17.094 MiB Commit frei, 7,9 % CPU, 3.930 MiB NVIDIA-VRAM frei, 0 % GPU-Last | Separater `llama-cli`-Prozess mit demselben GGUF, 2 Vulkan-Layer, Kontext 128, Batch 32, 2 Threads, höchstens 4 Tokens | Nach 90 Sekunden ohne bestätigten Abschluss beendet; ausschließlich der für diesen Smoke gestartete CLI-Prozess wurde abgebrochen. Kein erfolgreicher Modelllauf oder Antworttext belegt. |

Ein nicht-inferenzierender CLI-Metadaten-/Hilferuf (`--version`/`--list-devices`/`--help`) beendete sich ebenfalls nicht innerhalb des 60-Sekunden-Fensters und wurde ohne Modellpfad beendet. Die GPU-Smokes wurden nicht wiederholt. Die tatsächlichen CLI-Ausgaben der beiden Zeitüberschreitungen enthalten keinen nachweisbaren erfolgreichen Exit oder eine Antwort; die GPU-Modellstarts sind damit **nicht verifiziert**. Der zuvor bestätigte app-eigene CPU-Server- und UI-SSE-Lauf bleibt davon unberührt.

Nach beiden Abbrüchen: 12.752 MiB RAM und 17.445 MiB Commit verfügbar, 3.930 MiB NVIDIA-Speicher frei, 0 `llama-cli`-/`llama-server`-Prozesse und keine Listener auf 8080 oder 18080. Kein fremder Prozess wurde beendet.

Die Pairing-Tests verwenden synthetische GGUF-Metadaten. Sie weisen ausdrücklich `runtime_verified == False` aus; es gab keinen begrenzten echten Bild-/Audio-Lauf mit einem GGUF/mmproj-Paar. Multimodale Paarungen bleiben Metadatenkandidaten und dürfen nicht als nutzbar behauptet werden. Keine Treiber-, PATH-, Firewall-, globalen Python- oder Git-Konfigurationen wurden geändert.