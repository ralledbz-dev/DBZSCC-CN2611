# DBZSCC 2611 – Implementierter Ressourcen- und Modellzuordnungsplaner

## Ergebnis und Grenze

Der additive, Qt-freie Slice ist in `src/dbzscc/app/resource_orchestrator.py` implementiert. Er erzeugt aus einem aktuellen `ResourceSnapshot` und einem oder mehreren `WorkloadRequest`s einen typisierten `ResourcePlan` mit Backend, Runtime-Ziel, physischem Adapter, CPU-Threads, Speicherclaims, Begründungen, Unsicherheiten und Fail-closed-Gründen.

Der Planer ist kein Prozessmanager und keine UI-Integration: Er startet, überwacht oder beendet keine Prozesse und reserviert keinen Speicher im Betriebssystem. Er vergibt pro Workload genau ein Backend-/Geräte-Ziel. Modell-Layer-Splits oder ein gemeinsamer CUDA-/Vulkan-Prozess werden nicht behauptet.

## Ressourcen werden getrennt bilanziert

- **Dediziertes GPU-Memory:** Der Plan nutzt den aktuellen adapterbezogenen Gesamtwert, OS-Budget und Verbrauch und zieht standardmäßig 1.024 MiB Reserve ab. CUDA- und Vulkan-Ziele, die dieselbe stabile `physical_id` tragen, teilen ein Budget. `cuda:0` und `vulkan:1` dürfen daher nicht als zwei Grafikkarten gezählt werden.
- **Integrierte GPU / Shared-System-Memory:** Der aktuelle Shared-Budgetwert wird separat gegen den GPU-Claim geprüft. Standardmäßig bleiben 2.048 MiB Shared-Reserve; wenn der Adapter ein Display treibt oder die Displayzuordnung unbekannt ist, bleiben zusätzlich 1.024 MiB. Der Shared-Claim wird zugleich als Host-RAM-Backing berücksichtigt, aber nicht ein zweites Mal zum Hostbedarf addiert. Eine zugewiesene UMA-Größe wird nicht als dediziertes VRAM ausgegeben.
- **Host-Speicher:** Das dynamisch verfügbare physische RAM muss zusätzlich zu 6.144 MiB OS-Headroom und 4.096 MiB Mindestreserve reichen; der Commit-Headroom muss nach einer Reserve von 4.096 MiB ebenfalls reichen. Es zählt die jeweils engere Grenze. Die Werte sind konfigurierbare Policy-Defaults, keine Gerätemessungen.
- **CPU:** Der Threadrahmen wird aus logischen Kernen und aktueller CPU-Last abgeleitet; standardmäßig bleiben zwei Service-Threads frei. Bei Druck kann der Plan die Threadzahl reduzieren, sofern die Workload-Minimalzahl eingehalten wird. RAM- oder Commitdruck blockiert die CPU-Zuordnung unabhängig davon.

Die Standardwerte sind in `ResourcePolicy` konfigurierbar. Snapshots älter als zwei Sekunden sowie als `STALE`, `DEGRADED` oder `UNKNOWN` markierte globale Telemetrie werden abgelehnt. Adaptertelemetrie wird ebenfalls frisch geprüft; ein unbrauchbarer GPU-Kandidat kann nur dann auf einen anderen angeforderten Pfad ausweichen, wenn dessen eigene Daten und Budgets tragfähig sind.

## Modellbedarf bleibt eine Schätzung

Ein Aufrufer kann Gewichts-, KV-Cache-, Backend-Overhead- und Host-RAM-Bedarf explizit übergeben. Alternativ schätzt der Planer Gewichte mit `ceil(model_file_size_mib × 1,25)`, verwendet bei unbekanntem KV-/Kontextbedarf standardmäßig 1.024 MiB und bei unbekanntem Backend-/Allocator-Overhead 512 MiB. Sind Kontextlänge und ein KV-Wert pro 1.024 Token vorhanden, wird daraus eine KV-Schätzung berechnet.

Dateigröße ist ausdrücklich kein exakter Runtime-Footprint. Ohne Dateigröße oder Gewichtsestimate wird fail-closed abgelehnt. Schätzungen und unbekannte Inputs erscheinen im Plan unter `uncertainties`. Für dedizierte GPU-Ziele wird bei unbekanntem Hostbedarf konservativ der vollständige geschätzte Modellbedarf zusätzlich im Host-RAM reserviert; für Shared-UMA ist derselbe Backing-Claim kein zweiter Hostposten.

## Deterministische Auswahl und Konkurrenz

`plan_many` verarbeitet höhere Priorität zuerst und löst Gleichstände stabil über die Workload-ID auf; die Ergebnisreihenfolge entspricht weiterhin der Eingabe. Akzeptierte Ansprüche werden für nachfolgende Pläne kumulativ berücksichtigt. Physische Budgets sind unabhängig von Backend-Aliasen nach `physical_id` geführt. Bei CPU-Druck darf der Plan Threads reduzieren; nicht passende Speicher- oder Mindestthread-Anforderungen werden mit konkreten Kapazitätsgründen abgelehnt, statt still bis zu einer möglichen OOM-Allokation durchzureichen.

Die Backend-Priorität wird vom Aufrufer über `backend_preferences` bestimmt. Innerhalb eines Backends werden dedizierte Adapter vor Shared-Adaptern geprüft. Wenn dieselbe Hardware in CUDA und Vulkan sichtbar ist, bleibt es bei einem ausgewählten Backend pro Workload; ein Cross-Backend-Split wird weder angenommen noch ausgeführt.

## Laufzeitvertrag für die spätere Anbindung

Die Integrationsschicht muss stabile physische IDs aus verifizierter Adapterzuordnung liefern, aktuelle Messungen beisteuern und einen akzeptierten Plan **atomar** reservieren, bevor ein Runtimeprozess startet. Sie muss laufende Prozesse, Zeitlimits, Telemetrie-Aktualisierung, Lease-/Reservation-Abgleich und Cancellation außerhalb dieses reinen Planers umsetzen. Bereits gemessener Verbrauch darf nicht erneut als vollständiger Peak abgezogen werden: Der Snapshot-/Ledger-Vertrag unterstützt dafür `observed_*`-Werte; bei fehlender Beobachtung wird der Anspruch konservativ vollständig abgezogen.

Die lokalen CPU-, CUDA- und Vulkan-Runtimes, reale Modelle, Gerätetreiber, CLI-Flags und die Zuordnung von CUDA-/Vulkan-Geräte-IDs wurden in diesem Slice nicht ausgeführt oder abgenommen. Insbesondere folgt aus getrennten llama.cpp-Builds keine Zusage, CUDA und Vulkan in einem Prozess kombinieren zu können. Die tatsächliche Build- und Geräteunterstützung muss mit den konkreten lokalen Binaries separat geprüft werden.

## Validierung

Auf RAZOR mit der Repo-venv ausgeführt:

- `python -m pytest -p no:cacheprovider tests/test_resource_orchestrator.py -q` — **18 bestanden**.
- `ruff check --no-cache src/dbzscc/app/resource_orchestrator.py tests/test_resource_orchestrator.py` — **alle Prüfungen bestanden**.

Die Tests verwenden ausschließlich deterministische Fake-Snapshots; es gab keine Hardware-/Modellausführung und keine Änderungen an UI-, Chat- oder Runtime-Dateien.

## Offizielle Referenzen

Die llama.cpp-Build-Dokumentation beschreibt CPU-, CUDA- und Vulkan-Builds sowie Build-Optionen: [llama.cpp – Build](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md). Die CLI-Dokumentation beschreibt unter anderem `--list-devices`, `--device`, `--n-gpu-layers`, `--split-mode`, `--tensor-split` und `--fit`: [llama.cpp – CLI](https://github.com/ggml-org/llama.cpp/blob/master/tools/cli/README.md). Diese Upstream-Dokumentation ersetzt keine Abnahme des konkreten vorhandenen Builds oder Treibers.
