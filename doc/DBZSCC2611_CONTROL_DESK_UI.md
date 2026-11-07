# DBZSCC 2611 — Control Desk und Run/Test/Fix-Pipeline

**Stand:** 4. November 2026
**Status:** Implementiert; fokussiert und vollständig getestet. Eine manuelle visuelle Abnahme der gestarteten Desktopoberfläche wurde in diesem Arbeitsschritt nicht ausgeführt.

## Produktentscheidung

Der Control Desk ist der ruhige, operative Einstiegspunkt der App: zuerst aktives Projekt und echter Run-Zustand, dann der nächste sinnvolle Schritt, danach Modell-/Runtime-, MIRO- und Ressourcenbelege sowie letzte Ausführungsereignisse. Die bestehende Chat-Seite bleibt der primäre Arbeitsbereich; das Cockpit ersetzt sie nicht und erfindet keinen separaten Projektzustand.

Die Umsetzung folgt der freigegebenen Richtung **Control Desk**: klare produktive Struktur, zurückhaltende Karten und ein funktionales Dunkelthema bei verfügbarer Light-Alternative. Die bereits bestehende Navigation umfasst acht Bereiche: Control Desk, Chat, Projekt, Workflows, Diff & Änderungen, MIRO · Modell-Vault, Runtime und Einstellungen.

## Sichtbares Verhalten

- **Aktives Projekt:** zeigt die ausdrücklich ausgewählte Projektwurzel. Branch, HEAD oder Git-Sauberkeit werden daraus nicht abgeleitet und im Control Desk nicht behauptet.
- **Run / Workflow:** zeigt Zustand und Zusammenfassung des aktuellen bzw. zuletzt gespeicherten Runs. Ohne Run steht ausdrücklich „Noch kein Run gestartet“.
- **Nächster Schritt:** führt abhängig von ausgewähltem Projekt, MIRO-Befund, Modell-/Runtimezustand und Reviewstatus in den relevanten Bereich. Ein fehlender oder veralteter Beleg wird als solcher dargestellt, nicht als Erfolg.
- **Modell / Runtime:** unterscheidet Auswahl, manuelle Runtime-Prüfung und tatsächlichen Status. Modellmetadaten oder ein Dateiname belegen keine Inferenzfähigkeit.
- **Ressourcen:** zeigt ausschließlich Werte eines tatsächlich vorliegenden Ressourcenplans. Der Zustand ist standardmäßig „Nicht gemessen“; eine Planfreigabe wird nicht als Live-Monitor oder gemessene Auslastung ausgegeben. Reduzierte Zulassung, Blockierung und Unsicherheit sind voneinander unterscheidbar.
- **MIRO:** zeigt den aktuellen Katalog-/Scanbefund und erhält den Unterschied zwischen gefunden, geparst, Laufzeit-getestet und qualifiziert. Wenn der Startup-Refresh in den Einstellungen aktiviert ist (Standard), aktualisiert die App konfigurierte Modellroots beim Start im Hintergrund; ohne konfigurierte Roots meldet sie den Refresh als nicht verfügbar. Runtime-Roots und Runtime-Probes werden dadurch nicht automatisch gestartet.
- **Ausführungsaktivität:** basiert auf gespeicherten Ereignissen der aktuellen Unterhaltung; Run-IDs werden im Cockpit gekürzt, während die vollständige ID im Workflow-Kopf und in der Konsole sichtbar bleibt.

## Run/Test/Fix und Eventbindung

Die Workflows-Seite visualisiert den Ablauf als **Plan → Code → Test → Analyse**. Pipeline-Status, Run-ID, strukturierte Konsole und Abschlusszusammenfassung werden aus dem vorhandenen lokalen `ExecutionEventStore` und `ExecutionRunRecord` abgeleitet. Ereignisse und Ausgaben werden auf Unterhaltung und `run_id` gefiltert; stdout, stderr und Exitcode bleiben unterscheidbar. Es wird kein paralleler UI-Statusspeicher als Quelle der Wahrheit angelegt.

Der sichtbare Zustandsweg umfasst unter anderem laufenden Run, Diagnose, Patchvorschlag, wartendes Diff-Review, angewendeten Patch, Retest sowie erfolgreiche, fehlgeschlagene oder abgebrochene Endzustände. Bei Abbruch wird die Anforderung angezeigt, ohne einen erfolgreichen Abschluss vorzutäuschen. Ein abgeschlossener Run kann nicht versehentlich mit derselben Run-ID erneut gestartet werden.

Die Patchfreigabe bleibt menschlich kontrolliert. Der Button zum Öffnen eines wartenden Diffs wird nur aktiviert, wenn tatsächlich ein Diff geladen ist und Unterhaltung, aktiver Reparaturlauf sowie Run-ID übereinstimmen. Nach explizitem Apply bleibt der Retest an denselben Run gebunden. Die UI setzt kein willkürliches Drei-Versuche-Limit und übergeht weder die Freigabe pro Diff noch das Stop-/Fehlerergebnis.

## Datenwahrheit und Sicherheitsgrenzen

Die Cockpitansicht macht Befunde sichtbarer, weitet aber keine Berechtigungen aus. Sie startet weder Runtime noch Devserver, Tests oder Projektprozesse selbst. Der konfigurierte MIRO-Modellroot-Refresh ist ein separater, standardmäßig aktivierter Hintergrundpfad und lässt sich in den Einstellungen deaktivieren; Runtime-Scans und Runtime-Probes bleiben ausdrücklich angefordert. Ein nicht vorhandener aktueller Plan bleibt unbekannt. Temporäre UI-Tests verwenden isolierte SQLite-, QSettings- und Projektfixtures; sie ändern keine Projektdateien und sprechen weder Runtime noch Hardware an.

Die Dashboard-Aktivitätsliste zeigt nur kurze Ereigniszusammenfassungen. Die Konsole zeigt gespeicherte, begrenzte und redigierte Ereignisse; rohe Modell-Chain-of-Thought wird nicht sichtbar gemacht. Die vollständige Projektidentität (Branch/HEAD/Remote), dauerhafte Ressourcenhistorie, Hardware-Live-Telemetrie und eine manuelle Sichtprüfung des Themes sind keine Abnahmebehauptungen dieses UI-Slices.

## Geänderte Dateien dieses UI-Slices

- `src/dbzscc/ui/main_window.py` — Control-Desk-/Pipeline-Anzeige, echte Run-/Eventbindung, Zustandsformatierung, Run-spezifische Diff-Review-Aktivierung und Light-/Dark-Kartenstil.
- `tests/test_ui_smoke.py` — Regressionen für aktuelles Projekt und leeren Zustand, unbekannte/teilweise/zugelassene Ressourcenpläne sowie persistierte Fortschritts-, Abbruch-, Review-, Apply- und Retestereignisse.

Die Arbeitskopie enthielt bereits zahlreiche uncommittete Arbeiten aus dem MIRO-/Workflow-Arbeitszyklus; diese blieben unangetastet. Es wurde kein Commit, Push oder Merge ausgeführt.

## Verifikation

Auf RAZOR/Windows im Projekt-venv, aktuellem verändertem Arbeitsbaum:

- `& 'D:\Dev\DBZSCC2611\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q 'D:\Dev\DBZSCC2611\tests\test_ui_smoke.py'` (aus `C:\Users\ralle`) → **Exitcode 0; 19 passed**.
- `& '.\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q` (Arbeitsverzeichnis `D:\Dev\DBZSCC2611`) → **Exitcode 0; 265 passed in 8,32 s**. Zusätzlich **176 DeprecationWarnings** aus dem installierten LangChain-Core-Aufruf von `asyncio.iscoroutinefunction`; keine Testfehler.
- `& '.\.venv\Scripts\ruff.exe' check src tests` (Arbeitsverzeichnis `D:\Dev\DBZSCC2611`) → **Exitcode 0; All checks passed**.
- `git -c safe.directory='D:/Dev/DBZSCC2611' diff --check` (Arbeitsverzeichnis `D:\Dev\DBZSCC2611`) → **Exitcode 0; keine Whitespacefehler**. Git gab LF→CRLF-Hinweise für geänderte Arbeitskopiedateien aus.

Die vollständige Suite schließt die neuen Offscreen-Qt-Regressionen ein. Sie belegt keine manuelle Sicht-/Bedienabnahme, keinen neuen Runtime-/Modellstart, keinen Scan, keine GPU-Qualifikation und keine Live-Telemetrie.

## Nächster sinnvoller Schritt

Die Oberfläche bei der nächsten vom Nutzer gewünschten Desktopprüfung manuell ansehen und insbesondere Kartenumbruch, lange Projektpfade, Pipeline-/Konsolenhöhe und Auswahl auf gängigen Fenstergrößen kontrollieren. Danach nur konkrete Bedien- oder Layoutbefunde nachbessern; keine Runtime- oder MIRO-Liveprüfung ohne ausdrücklichen Anlass starten.
