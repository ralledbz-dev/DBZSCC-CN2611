# DBZSCC 2611 — Lokale Toolchain-Runtimes und typed invocation

**Stand:** 3. November 2026<br>
**Status:** Additiver, Qt-freier und testbarer Runtime-Grundbaustein; **nicht end-to-end in App, UI oder echte Prozessausführung eingebunden**.<br>
**Checkout:** `D:\Dev\DBZSCC2611`, Branch `master`, HEAD `95cf221382eea5bbaec45acab4e16f4631ed98fb`; Änderungen uncommittet.

## Ergebnis in einem Satz

DBZSCC hat nun eine reine Python-Domainbasis für explizite Runtime-Registry/Discovery, Manifestanforderungen, standortgebundene Paketauflösung, einen sieben Stack umfassenden Toolchain-Katalog, getrennte einmalige und langlebige Invocation-Pläne sowie einen Adapter zum bestehenden Ressourcenplaner. Sie sucht keine Programme über `PATH`, installiert nichts und startet selbst keinen Prozess; deshalb ist sie noch kein nutzbarer Werkzeuge- oder LSP-Lifecycle in der Desktop-App.

## Einordnung in die vorhandene Vision

Das Produkt ist laut [README](../README.md) eine local-first Einzelplatzanwendung. Die [MIRO-Vision](../DBZSCC2410_MIRO_MODEL_INTELLIGENCE_VISION.md) verlangt eine tatsächliche, buildabhängige Toolchain-Inventur und warnt davor, Executables als vorhanden vorauszusetzen. Die [UI-Vision](../DBZSCC2410_UI_VISION.md) sieht Runtime-Dienste, Jobs und Werkzeuge als getrennte Bedienbereiche vor. Die [Architecture Guardrails](ARCHITECTURE_GUARDRAILS.md) verlangen einen Qt-freien Servicekern und trennen UI, Businesslogik, Worker und Ausführung.

Der neue Kern erfüllt davon **nur den deklarativen und planenden Unterbau**. Er ersetzt weder die vorhandene MIRO-Modell-/Runtime-Discovery noch den getesteten Ressourcenplaner, und er behauptet nicht, dass die Vision schon in der UI verfügbar sei. Das passt zum weiterhin geltenden [Produktreifebericht](DBZSCC2611_PRODUCTION_READINESS.md): Gesamtreife ist nicht durch einen grünen Testlauf belegt. Die bisherigen Aussagen in README und Projektstatus, dass kontrollierte Tool-/Terminalausführung und verwaltete Runtime-Prozesse noch fehlen, bleiben zutreffend.

## Implementiert

### 1. Explizite Registry statt unkontrollierter Systemsuche

`src/dbzscc/app/toolchain_runtime.py` ist ein reiner Service ohne Qt-Abhängigkeit. `RuntimeRegistry.discover_explicit()` akzeptiert ausschließlich vom Aufrufer gelieferte `RuntimeCandidateSpec`-Datensätze mit **exaktem absolutem Executable-Pfad**. Geprüft wird der konkrete letzte Pfadbestandteil; fehlende oder nicht lesbare Dateien, Verzeichnisse statt regulärer Executables, Symlinks und Windows-Reparse-Points werden als strukturierte Issues abgewiesen. Es gibt kein `PATH`-Lookup und kein Directory-Walk. Die Registry ist zurzeit nur im Speicher.

Paketstandorte haben eine separate Funktion `inspect_explicit_package_locations()`: Sie prüft nur die ausdrücklich mitgegebenen Datei- oder Verzeichnispfade und markiert erfolgreiche Kandidaten als `PRESENT`. Das Statusfeld ist im normalen Konstruktor nicht setzbar; nur die explizite Pfadinspektion erzeugt einen als beobachtet markierten Kandidaten. Ein deklarierter Kandidat wird nicht schon deshalb als installiert aufgelöst, weil er im Manifest steht. Der Resolver wählt nur beobachtete Kandidaten und meldet fehlende, inkompatible, mehrdeutige, zyklische oder unvereinbare Anforderungen strukturiert. Bevorzugte Scopes sind App-Bundle, projektlokal, dann explizit deklarierter Systempfad. „System“ heißt hier nicht automatische Systeminventur.

Das Vorhandensein wird anhand des angegebenen Pfads geprüft, nicht durch Ausführen von `--version`. Version und `VersionEvidence` sind Aufruferdaten; der Kern verifiziert deren Herkunft nicht selbst. Die Pfadprüfung erfasst außerdem nicht die Sicherheit jeder übergeordneten Verzeichnis-Komponente. Eine künftige Discovery-/Execution-Grenze muss gewählte Wurzeln, Reparse-Points in Elternpfaden, Versionsbeleg und Pfadwechsel nochmals kontrollieren.

### 2. Manifestanforderungen und tatsächliches `pyproject.toml`

Es gibt zwei Eingangswege:

- `ToolchainManifest.from_json()` / `read_toolchain_manifest()` liest ein explizit ausgewähltes JSON-Manifest mit Schema-Version 1. Nur bekannte Felder und gültige Requirement-/Scope-Werte werden angenommen; der Pfad muss eine konkrete reguläre Datei sein.
- `read_pyproject_requirements()` liest ein explizites `pyproject.toml`: `[build-system].requires`, `project.requires-python`, `project.dependencies` und nur die optionalen Gruppen, die der Aufrufer ausdrücklich nennt. Das aktuelle Projektmanifest wird somit tatsächlich unterstützt, ohne einen Verzeichnis- oder Systemscan auszulösen.

Für PEP 508 werden in diesem Slice einfache Paketnamen und Versionsspecifiers übernommen; Paketnamen werden normalisiert. Umgebungsmakros/Marker (`; ...`) und Extras (`[extra]`) führen **fail-closed** zu einem Fehler, statt stillschweigend nicht aufgelöst zu werden. Der Resolver versteht derzeit nur numerische Vergleichsklauseln `==`, `!=`, `>=`, `<=`, `>` und `<`, kommagetrennt; etwa PEP 440 `~=`, Wildcard-/Prerelease-Regeln und Markerauswertung sind nicht implementiert. Die TOML-Lesefunktion verwendet das vorhandene `tomllib`; für Python 3.10 ist `tomli` nur verwendbar, wenn es bereits in der Umgebung vorhanden ist. Eine fehlende TOML-Bibliothek löst einen Fehler aus; es wurde nichts installiert.

Das aktuelle [`pyproject.toml`](../pyproject.toml) deklariert `requires-python >=3.10`, `PySide6>=6.7`, die Entwicklungsgruppe `pytest>=8` und `ruff>=0.4` sowie `setuptools>=68` als Build-Anforderung. Das sind **deklarierte Anforderungen, keine bestätigten installierten Versionen**. In der geprüften Projektumgebung lief tatsächlich Python 3.15.2; die Parsermodule `tomllib` waren vorhanden und `tomli` nicht.

### 3. Stack-Katalog: Abdeckung, keine Installationsbehauptung

`STACK_CATALOG` enthält versionlose Rollen-/Komponentendeklarationen für:

| Stack | Erfasste Komponentenrollen (Auswahl) |
|---|---|
| Python | Python, `venv`, pip, pytest, Ruff lint/format, Pyright/Python-LSP, debugpy, PySide6 |
| Java / Kotlin | JDK/JRE, Kotlin-Compiler, Gradle/Maven, JUnit, detekt, ktlint, jdtls/Kotlin-LSP, jdb |
| Rust | rustup, rustc, Cargo build/test, rustfmt, Clippy, rust-analyzer, rust-gdb |
| JavaScript / TypeScript / Vite / Electron | Node.js, npm/pnpm/Yarn, TypeScript/tsserver, Vite build/dev server, Vitest, ESLint, Prettier, Electron und Packaging-Werkzeuge |
| Android / SDK / NDK | JDK, Android SDK, Android NDK/Native Compiler, Gradle, adb, Kotlin-Compiler, Tests, LLDB |
| C / C++ | LLVM/Clang, CMake, Ninja, CTest, clang-tidy, clang-format, clangd, LLDB, Qt 6 als optionales Framework |
| .NET | SDK/Runtime, MSBuild, Roslyn-Compiler, VSTest, Analyzer, C#-LSP, vsdbg, ASP.NET-Core-Framework |

Die Einträge sind `StackProfile`-/`StackComponentSpec`-Deklarationen. Sie enthalten weder installierte Versionsstände noch tatsächliche Pfade; der Katalog ist **keine** Inventur und löst nichts selbst auf. Im aktuellen Checkout gibt es keine automatischen ecosystem-spezifischen Parser für `package.json`, `Cargo.toml`, Gradle/Maven, `.csproj`, CMake oder Android-SDK-Metadaten. Diese werden als konkrete nächste Adapter ergänzt.

### 4. One-shot-Werkzeuge und langlebige Prozesse sind getrennt

`RuntimeFamily` unterscheidet Compiler, Build-Systeme, Debugger, Linter, Formatter, Language Server, Test Runner, Language Runtime, Package Manager und Development Server. `ProcessModel` teilt deren Startvertrag:

| Lebenszyklus | Typisierter Vertrag | Beispiel/Protokoll | Aktueller Umfang |
|---|---|---|---|
| Einmaliger Lauf | `OneShotInvocation` → `OneShotInvocationPlan` | Check/Fix, Compile, Build, Test; einzelne immutable `argv`-Argumente, Projektwurzel, CWD, Timeout, Env-Profil-ID, Ressourcenbedarf; Fix verlangt expliziten relativen `write_scope` | Validierung und Planer; kein konkreter Prozessrunner |
| Langlebige Session | `PersistentSessionRequest` → `PersistentSessionPlan` | LSP über JSON-RPC/stdio; Debugger über Debug-Adapter/stdio; Development-/Runtime-Session über `MANAGED_STDIO` | Startvertrag und Session-Handle; kein Prozess-Supervisor oder Transport |

Debugger und LSP müssen persistent sein; Development Server werden standardmäßig persistent klassifiziert. Die Invocation-Pläne enthalten eine Argumentsequenz statt eines Shell-Strings. `TypedRuntimeInvoker` leitet sie nur an injizierte `OneShotExecutionPort`- und `PersistentSessionPort`-Protokolle weiter. In der App gibt es derzeit **keine konkreten Implementierungen dieser Ports**, keinen `subprocess`-Start aus diesem Modul, keine Prozessbaumkontrolle, Cancellation/Restart/Crash-Recovery, stdout/stderr-Begrenzung oder reale LSP-/DAP-Message-Framing- und Handshake-Implementierung.

### 5. Ressourcenplaner-Anbindung

`ResourceDemand.to_workload_request()` bildet deklarierte Host-RAM-/CPU-Claims auf den existierenden CPU-Workloadvertrag ab; `plan_resource_demand()` delegiert an `ResourceOrchestrator.plan()`. Der Ressourcenplaner unter `src/dbzscc/app/resource_orchestrator.py` und dessen Tests wurden **nicht geändert**. Ein Ressourcenplan ist hier eine Schätzung/Admission-Entscheidung, keine Reservierung: Der Adapter erzeugt weder atomare Leases noch Queue, PID-/Prozessverbrauchsabgleich oder Transaktion „Plan → Start“. GPU-/SDK-spezifische Claims sind nicht modelliert.

Bevor ein ausführender Port angeschlossen wird, muss er diesen Planer anrufen und eine echte Reservation-/Lease-Grenze implementieren. Sonst könnte zwischen Plan und Prozessstart Kapazität doppelt vergeben werden. Das folgt der Verantwortungstrennung im [Ressourcenplan-Design](DBZSCC_RESOURCE_SCHEDULING_DESIGN.md), ohne dessen laufenden Llama-/Ressourcen-Code zu ersetzen.

### 6. Installationsrezepte sind nur Daten

`PackageRecipe` und `validate_package_recipe()` prüfen Pflichtmetadaten wie numerisch gepinnte Version, HTTPS-URL, Quell-ID, SHA-256, Lizenz-ID, App-Managed-Scope und verbieten Adminrechte oder globale `PATH`-Änderungen. `PackageInstallerPort` ist nur ein Zukunftsprotokoll für Review vor Approval und Install nach Approval. Es ist kein konkreter Installer registriert.

Insbesondere überprüft diese Metadatenvalidierung nicht unabhängig, ob eine Quell-ID tatsächlich offiziell ist, validiert keine Signatur/Hashes gegen heruntergeladene Bytes, präsentiert keine Lizenz, lädt nichts herunter und schreibt nichts. Es gibt keine Paketinstallation, kein SDK-Setup und keine Lizenzfreigabe-UI. Für die persönliche lokale Nutzung bleiben App-Managed-/nicht privilegierte Installationen ein späterer, separat freizugebender Pfad; dieser Slice baut keine allgemeine, veröffentlichte Paket-/Deployment-Kette.

## Was getestet und was nicht verifiziert wurde

### Tatsächliche Belege

| Prüfung | Ergebnis |
|---|---|
| Neue Toolchain-Tests: `.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q tests\test_toolchain_runtime.py` (mit `PYTHONDONTWRITEBYTECODE=1`) | **19 passed**, zuletzt 0,16 s |
| Gesamte bestehende Testsuite: `.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q` (mit `PYTHONDONTWRITEBYTECODE=1`) | **174 passed**, zuletzt 1,95 s |
| Geänderter Umfang: `.venv\Scripts\ruff.exe check --no-cache src tests` | **All checks passed** |
| Ruff nur auf beiden neuen Python-Dateien | **All checks passed** |
| Ruff auf Checkout `.` | **Fehlgeschlagen**: `I001` Importreihenfolge in `.work/scripts/offscreen_start_smoke.py`; zusätzlich Zugriff-verweigert-Warnung beim Scan. Die nicht zum Slice gehörende `.work`-Datei wurde nicht geändert. |
| Aufrufstellen im `src/dbzscc`-Python-Code | Kein Verweis auf `toolchain_runtime` gefunden: noch nicht im App-Startup, UI oder bestehender Runtime-Anwendung verdrahtet. |

Git meldete beim abschließenden Status zudem eine Lesewarnung für `.tmp/pytest-initial/`. Der Arbeitsbaum hatte zahlreiche schon vorhandene Änderungen und untracked Projektdateien. Es wurden keine Commits, Pushes oder Merges erzeugt. `resource_orchestrator.py`, die vorhandene UI und die sonstigen bestehenden Dateien wurden von diesem Slice nicht editiert.

### Nicht ausgeführt / nicht behauptet

Es wurde keine Systemsuche nach Compilern oder Debuggern gemacht, kein `PATH` ausgewertet, keine System-/Projekt-Toolchain gestartet, keine LSP- oder Debugger-Session geöffnet, kein Build ausgeführt und keine Framework-/SDK-Version auf dem Rechner inventarisiert. Es gab keinen manuellen UI-Lauf und keinen App-End-to-End-Test für Start, Stop, Approval oder Jobs. Es wurde nichts heruntergeladen oder installiert. Die Tests verwenden temporäre Fixture-Executables und Fake-Ports; sie belegen Domainvalidierung und Planer-Vertragsübergabe, keinen echten Werkzeugstart.

## Entscheidungen und konkrete nächste Schritte

1. **Explizite Kandidatenpfade statt `PATH`-/Root-Suche:** schützt die lokale Workstation und verhindert, dass ein zufälliges gleichnamiges Binary als verfügbar gilt. Spätere Discovery darf nur vom Nutzer gewählte Projekt-/Bundle-Pfade enumerieren und muss Quelle, beobachtete Zeit und Version belegen.
2. **PEP-621 plus eigenes JSON als erste Manifestquellen:** das gegenwärtige `pyproject.toml` funktioniert jetzt tatsächlich; weitere Formatadapter bleiben eigenständige, fail-closed Änderungen. Zuerst sollten die im Projekt real benötigten `package.json`, Cargo-/Gradle-/Maven-/CMake-/.NET-/Android-Formate geordnet und mit fixtures getestet werden.
3. **Explizite Lifecycles vor Executors:** als Nächstes die typed one-shot- und persistent-Ports als kontrollierte Worker-/Supervisor-Services mit Prozessbaum-Abbruch, Timeout, Ausgabe-/Loglimits, redigierter Run-Evidence und stabilen IDs realisieren. LSP/DAP erhalten Sessionhandshake, Messagegrenzen, Fehlerzustände und sauberes Beenden.
4. **Ressourcenplanung vor Start:** Plan, atomare Reservation, Start, Beobachtung und Release/Reconciliation müssen einen konsistenten Flow bilden. Der neue Adapter allein ist dafür bewusst nicht ausreichend.
5. **Dann Runtime-/Werkzeuge-UI und Approval-Pfad:** Inventar und Verfügbarkeit als „deklariert / am Pfad vorhanden / Version beobachtet / geprüft / ausführbar“ getrennte Zustände darstellen; niemals „installiert“ aus einer Katalogdeklaration ableiten. Änderungen an Projektdateien (Formatter/Fixer) verlangen eine sichtbare Scopeprüfung und Freigabe.
6. **Erst danach Installations-Flow:** konkrete offizielle Quellen für jeden Baustein belegen, Paketmetadaten/Signaturen/Hashes, Größe, Lizenz und Speicherplatz validieren, Review und Nutzerapproval vor Download, ausschließlich appverwaltet und ohne Admin-/globalen `PATH`-Eingriff.
7. **Akzeptanz:** kontrollierte Fake-Executable-/Timeout-/Abbruchtests, LSP/DAP-Protokollfixtures, Ressourcenreservation-Integration, UI-Wiring-Smoke und abschließend ein ausdrücklich freigegebener lokaler End-to-End-Lauf. Erst reale, reproduzierbare Toolergebnisse belegen „ausführbar“.

Nachweise und Hintergrund: [README](../README.md), [Implementierungs- und Evidenzbericht](DBZSCC2611_APP_IMPLEMENTATION.md), [Produktreife und Gates](DBZSCC2611_PRODUCTION_READINESS.md), [MIRO-Toolchain-Vision](../DBZSCC2410_MIRO_MODEL_INTELLIGENCE_VISION.md), [UI-Vision](../DBZSCC2410_UI_VISION.md), [Architecture Guardrails](ARCHITECTURE_GUARDRAILS.md), [Ressourcenplan-Design](DBZSCC_RESOURCE_SCHEDULING_DESIGN.md), [`toolchain_runtime.py`](../src/dbzscc/app/toolchain_runtime.py), [`test_toolchain_runtime.py`](../tests/test_toolchain_runtime.py).
