# DBZSCC2611 -- UI Vision

## Zweck und Leitbild

**DBZSCC. ist eine dedizierte lokale Entwicklungs- und KI-Arbeitsumgebung ausschließlich für den eigenen PC und einen Benutzer.** Die GUI soll zweckmäßig, professionell, ruhig und effizient sein. Dekoration, unnötige Animationen und überladene Dashboard-Elemente werden vermieden.

Leitlinie: **Industrial Developer mit Clean-Enterprise-Elementen.** Dark ist das bevorzugte Standardtheme; Light ist eine gleichwertige Alternative.

## 1\. Produktprinzipien

* Chat-zentrierte Entwicklungsumgebung: Der Chat ist das Herzstück, kein nachrangiges Modul.
* Bestehende Funktionen und Schnittstellen erhalten; Vereinfachung darf keine Funktionalität entfernen.
* Klare Informationshierarchie, kompakte Navigation, konsistente Abstände und Bedienelemente.
* Tabellen, strukturierte Listen und Arbeitsbereiche bevorzugen statt überdimensionierter Kacheln.
* Technische Zustände sichtbar und verständlich machen.
* Keine Cloud-first-, Multi-Tenant-, Cluster- oder Enterprise-Abstraktionen.
* Für die konkrete lokale Hardware und vorhandene Runtime optimieren.

## 2\. Visuelle Richtung

### Industrial Developer

* Graphit-/Anthrazit-Oberflächen im Dark Theme.
* Dezente, funktionale blaue Akzente.
* Helles Theme mit neutralen Grau- und Weißtönen.
* Klare Typografie, zurückhaltende Rahmen und Statusfarben.
* Keine unnötigen Leuchteffekte, Animationen oder dekorativen Elemente.

## 3\. Hauptnavigation

Die Navigation soll kompakt und dauerhaft erreichbar sein:

1. Dashboard
2. Chat
3. Projekte
4. Modelle
5. Agenten
6. Runtime
7. Jobs
8. Werkzeuge
9. Memory
10. Einstellungen

## 4\. Chat Workspace – zentraler Arbeitsbereich

Der Chat ist der primäre Arbeitsplatz und soll in der Oberfläche den größten Raum erhalten.

### Layout

* Links: Projekt-Explorer mit Projektbaum, Suche und geöffneten Dateien.
* Mitte: Chat- und Arbeitsbereich.
* Rechts: kontextabhängige Informationen zu aktivem Modell, Ressourcen, Agenten und Runtime.
* Unten: Nachrichteneingabe mit Datei-Anhang, Modellauswahl und Senden.
* Oben im Chatbereich: Arbeitsmodi wie Chat, Code, Planner/ToT, Agenten, Konsens, Memory, Logs und Terminal.

### Chat-Funktionen

Der Chat muss mehr als Textnachrichten darstellen können:

* Codeblöcke mit Syntax-Highlighting, Kopieren und Diff-Ansicht.
* Tool-Aufrufe und deren Ergebnisse.
* Datei- und Projektbezüge.
* Agentenaktivitäten und Planungsstatus.
* Ausführungs- und Testergebnisse.
* Logs und Runtime-Ausgaben.
* Alternativen, Entscheidungserklärungen und Belege.
* Freigabe- und Bestätigungsschritte vor relevanten Datei- oder Systemänderungen.

### Kontextbereiche

Die Seitenleiste rechts zeigt nur zum aktuellen Vorgang passende Informationen, beispielsweise:

* aktives Modell und dessen Status,
* ausgewähltes Einsatzprofil,
* CPU, RAM, GPU und VRAM,
* aktive Runtime-Dienste,
* aktive Agenten und deren Rollen,
* Kontext- und Memory-Informationen.

## 5\. Weitere GUI-Bereiche

### Dashboard

Kompakte Systemübersicht mit Runtime-Zustand, aktivem Modell, Hardwareauslastung, Jobs und letzten Aktivitäten. Schnellaktionen nur dort, wo sie einen echten Arbeitsvorteil bieten.

### Projektverwaltung

Projektbaum, Suche, geöffnete Dateien, Kontextmenüs und Schnellzugriff. Versteckte bzw. auszuschließende Verzeichnisse müssen über eine nachvollziehbare Konfiguration steuerbar sein.

### Modellverwaltung

Modellbestand, technische Eigenschaften, Fähigkeiten, Quellen, Ladezustände und Einsatzprofile. Details siehe separates Dokument **DBZSCC2611 – Model Intelligence \& Resource Orchestrator Vision**.

### Agenten

Übersicht über Rollen, Aufgaben, Modellzuordnung, Ausführungsstatus und Ergebnisse. Keine künstlichen Agentenanzeigen: nur tatsächlich aktive oder verfügbare Worker darstellen.

### Runtime und Jobs

Runtime-Dienste, Start/Stopp/Reload, Job-Spooler, Warteschlange, Fortschritt, Logs, Fehler und Verlauf.

### Werkzeuge

Zentraler Zugriff auf Terminal, Datei-Manager, Artefakt-Explorer, Log-Viewer und Umgebungswerkzeuge.

### Memory

Projektbezogenes und globales Wissen sichtbar und kontrollierbar machen. Übernahme in globales Memory nur nach definierten Qualitäts- und Verifikationsregeln.

### Einstellungen

Klar nach Funktionsbereichen gruppiert: Allgemein, Darstellung, Modelle, Profile, Runtime, Pfade, Hardware, Memory und Erweitert.

## 6\. COT, ToT und Schwarmintelligenz in der GUI

Der Chat-Workspace soll die agentische Verarbeitung verständlich sichtbar machen:

1. Aufgabenanalyse und Anforderungserfassung (COT-gestützte Analyse).
2. Mehrere Lösungswege im Tree-of-Thoughts-Planer entwickeln.
3. Machbarkeit, Ressourcenbedarf, Risiken und erwartete Qualität bewerten.
4. Spezialisierte Agenten parallel oder sequenziell einsetzen.
5. Ergebnisse durch Consensus- und Review-Verfahren vergleichen.
6. Implementierung, Tests und Diffs präsentieren.
7. Verifizierte Erkenntnisse für Memory vorschlagen.

Interne private Chain-of-Thought-Inhalte werden nicht ungefiltert offengelegt oder gespeichert. Die GUI zeigt stattdessen nachvollziehbare Zusammenfassungen, Pläne, Alternativen, Kriterien, Bewertungen und Belege.

## 7\. Bedien- und Sicherheitsprinzipien

* Nutzer bleibt bei relevanten Änderungen entscheidungsbefugt (Human-in-the-Loop).
* Vor Dateiänderungen, Refactorings und Systemeingriffen klare Freigaben anbieten.
* Änderungen als Diff zeigen und nachvollziehbar protokollieren.
* Lange Vorgänge asynchron ausführen und ihren Status sichtbar machen.
* Laufende Jobs und Modellinstanzen bei Profilwechseln kontrolliert behandeln.
* Statusanzeigen müssen reale Zustände widerspiegeln.

## 8\. Umsetzungsstrategie

1. Bestehende GUI, Komponenten und Schnittstellen inventarisieren.
2. Chat als primären Workspace und Layout-Anker definieren.
3. Navigation und Designsystem vereinheitlichen.
4. Dashboard und übrige Bereiche auf Informationsnutzen reduzieren.
5. COT/ToT-, Agenten- und Memory-Ansichten integrieren.
6. Dark/Light vollständig und konsistent umsetzen.
7. Bedienbarkeit, Responsiveness und Regressionen testen.

## 9\. Abgrenzung

Dieses Dokument beschreibt ausschließlich die **GUI-Vision**. Die technische Modellverwaltung, Hardwareanalyse, Benchmarking- und Profilarchitektur ist separat dokumentiert.

**Zielbild:** DBZSCC2611 soll sich wie eine ausgereifte lokale Entwickler-IDE mit einem leistungsfähigen, agentischen KI-Arbeitsplatz anfühlen – nicht wie ein dekoratives Dashboard mit angehängtem Chat.

