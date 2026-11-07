# DBZSCC 2611 – UI-Bereitschaft und Ansichtsgrößen

## Befunde und Änderungen

Die drei gemeldeten UI-Fehler wurden behoben. **Chat → Senden** bleibt gesperrt, bis Nachricht, aktueller Katalogeintrag und eine erreichbare, verifizierte `/v1`-Runtime mit passender Lauf-, Modell- und Backendbindung vorhanden sind. Der Hilfetext nennt die fehlenden Voraussetzungen statt einen Bereitschaftszustand vorzutäuschen. Das Leeren des Editors beim Streamstart deaktiviert nicht mehr die aktive **Stoppen**-Aktion; verspätete Stream-Tokens werden nach einem Abbruch verworfen. Die zugelassene Runtime-Provenienz kann einschließlich Build-Hash in der Unterhaltung gespeichert werden.

Deaktivierte Workflow-, Run- und DevServer-Schaltflächen sind in beiden Themes sichtbar gedämpft; die Regressionstests prüfen einen Kontrast von mindestens 4,5:1 und die weiterhin aktiven Interaktionen. Die Seitenleiste verwendet jetzt explizite Zeilenumbrüche für „LOKALE / ENTWICKLUNGS- / UMGEBUNG“. Die Live-Prüfung zeigte zusätzlich eine zu hohe Fenster-Mindestgröße, durch die Inhalte bei Maximierung hinter die Taskleiste gerieten. Alle Seiten liegen nun in nativen Scrollbereichen; das Fenster kann mindestens auf 960 × 650 verkleinert werden. Der Test bestätigt echte Fenstergrößen von 1024 × 700 und 1440 × 900.

Das Theme wird beim Start weiterhin zum Rendern gelesen, aber nicht mehr zurückgeschrieben. Persistiert wird es nur nach einer bewussten Auswahl durch den Nutzer. Für diese Arbeit wurden weder bestehende Registry-/Profilwerte gelesen oder geändert noch zurückgesetzt.

Die Implementierung befindet sich in [main_window.py](../src/dbzscc/ui/main_window.py) und [conversation_store.py](../src/dbzscc/app/conversation_store.py); die Regressionen liegen in [test_ui_smoke.py](../tests/test_ui_smoke.py) und [test_conversation_store.py](../tests/test_conversation_store.py).

## Verifikation

- `.venv` verwendet Python 3.15.2.
- Offscreen-UI-Suite: **23 bestanden**.
- Gesamte Testsuite: **280 bestanden**, 176 Deprecation-Warnungen aus dem bestehenden LangChain-Workflow-Testpfad.
- Ruff auf allen vier von dieser Änderung berührten Python-Dateien: **bestanden**. Der vollständige `src`-/`tests`-Baum besteht alle anderen Ruff-Regeln; der unveränderte Bestand enthält weiterhin **22 I001-Importsortierbefunde**. Diese nicht zugehörigen Imports wurden nicht umsortiert.
- `git diff --check` für die vier berührten Code-/Testdateien: **bestanden**.

## Live-GUI und Screenshots

Die echte Anwendung lief im Vordergrund mit einem frisch angelegten QSettings-INI-Profil und einem separaten leeren App-Datenordner unter `.work/ui-readiness-validation`. Nach QApplication- und QSettings-Initialisierung wurde der Startrefresh abgeschaltet; Projekt-, Modell- und Runtime-Roots waren leer, der frische MIRO-Katalog enthielt 0 Einträge. Es wurde kein Modellserver gestartet und kein Mikrofon- oder Gerätetest ausgeführt. Alle drei Screenshots wurden im Vordergrund aufgenommen; der eigene Inspektionsprozess ist beendet.

Die Screenshots liegen lokal unter `outputs/ui-readiness-validation/` und sind generierte Prüfartefakte; sie werden zusammen mit anderen Outputs nicht in den Repository-Push aufgenommen.

Der Checkout enthielt bereits weitere Änderungen und unversionierte Dateien. Sie wurden beibehalten; es gab keinen Commit und keinen Push. Das temporäre Prüfprofil, die Logs und das Capture-Skript bleiben für Folgerevisionen unter `.work/ui-readiness-validation` erhalten.
