# Offline-Spracheingabe

## So verwendest du sie

Beim ersten Mal **Offline-STT laden / prüfen** anklicken. Die App lädt Runtime und Modell erst dann, prüft beide SHA-256-Prüfsummen und legt sie unter `models/stt/` im Projekt ab. Das Laden kann bei Bedarf abgebrochen und später fortgesetzt werden. Danach **Mikrofon halten**, sprechen und loslassen. Das erkannte Transkript erscheint als editierbarer Entwurf im Composer. Es wird **niemals automatisch gesendet**: Du prüfst den Text und klickst selbst auf **Senden**. **Abbrechen** verwirft eine laufende Aufnahme oder Erkennung.

## Was lokal bleibt

Aufnahme und Erkennung laufen auf dem Windows-Rechner. Das Mikrofon ist nur während gedrückter Taste aktiv; App-Wechsel, Abbruch oder Loslassen beenden die Aufnahme. Audio wird kurzlebig im Arbeitsspeicher aufgenommen, auf mono 16-kHz-PCM normalisiert und als WAV in `models/stt/.tmp/` zwischengespeichert. Die Datei wird nach Erfolg, Fehler oder Abbruch gelöscht; sie steht nicht im Chatverlauf. Es gibt keine Cloud-STT und beim App-Start weder Downloads noch Netzwerkanfragen. Die Speech-Oberfläche prüft im Hintergrund vorhandene lokale STT-Dateien und ihre SHA-256-Werte, verändert dabei aber keine Dateien. Internet wird ausschließlich nach Klick auf **Offline-STT laden / prüfen** für die fest gepinnten Downloads benutzt.

Die Aufnahme ist auf 30 Sekunden und 12 MiB begrenzt. Die lokale Runtime verwendet maximal vier CPU-Threads. Bei fehlendem oder verweigertem Windows-Mikrofonzugriff zeigt die App einen Status, statt eine leere Aufnahme zu behaupten. Bei einer beschädigten/unerwarteten vorhandenen Datei überschreibt die App sie nicht.

## Fest gepinnte Dateien

| Bestandteil | Festlegung | SHA-256 |
| --- | --- | --- |
| Windows-x64 CPU-Runtime | whisper.cpp Release `b5454`, `whisper-bin-x64.zip`, 8.928.640 Bytes; entpackt nur nach `models/stt/runtime/` | `6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb` |
| Mehrsprachiges Deutsch-fähiges Modell | `ggml-base-q5_1.bin`, 59.707.625 Bytes (~57 MiB), aus Commit `f281eb45af861ab5e5297d23694b7d46e090c02c` | `422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898` |

**Warum `base-q5_1`:** Es ist mehrsprachig (anders als Modelle mit `.en` im Namen) und mit knapp 57 MiB deutlich kompakter als das reguläre mehrsprachige `base`-Modell von 142 MiB. Die 5-Bit-Quantisierung tauscht etwas Genauigkeit gegenüber unquantisierten Gewichten gegen kleinere Downloads und geringeren Speicherbedarf. Die tatsächliche Erkennungsqualität und Laufzeit hängen von Stimme, Aufnahme und CPU ab.

Runtime-Archiv und Prüfsumme stammen aus dem [offiziellen whisper.cpp-Release `b5454`](https://github.com/ggml-org/whisper.cpp/releases/tag/b5454) und dessen [GitHub-Asset-Metadaten](https://api.github.com/repos/ggml-org/whisper.cpp/releases/404951781/assets?per_page=10&page=1). Das Modell samt Größe/Hash stammt aus dem offiziellen [`ggerganov/whisper.cpp`-Modell-Repository](https://huggingface.co/ggerganov/whisper.cpp/tree/main); die [whisper.cpp-Modellliste](https://github.com/ggml-org/whisper.cpp/blob/master/models/README.md) erklärt Größen und Mehrsprachigkeit. whisper.cpp ist unter der [MIT-Lizenz](https://github.com/ggml-org/whisper.cpp/blob/v1.9.5/LICENSE) lizenziert; die aus OpenAI Whisper konvertierten Gewichte basieren auf dem ebenfalls [MIT-lizenzierten OpenAI-Whisper-Projekt](https://github.com/openai/whisper/blob/main/LICENSE).

## Prüfstand auf diesem Windows-Gerät

Eine kurze, lokal mit der Windows-Stimme „Microsoft Hedda Desktop“ erzeugte deutsche Testphrase wurde mit dem Projektmodell korrekt als „Guten Morgen. Diese Erkennung läuft vollständig lokal.“ erkannt. Das prüft die echte CPU-Inferenz ohne Netzwerk; die Test-WAV wurde anschließend entfernt. Das physische Eingabegerät „Analogue 1 + 2 (Focusrite USB Audio)“ wird zwar von Qt aufgelistet, `QAudioSource.start()` scheiterte jedoch sowohl im bevorzugten 48-kHz-Stereoformat als auch in 16-kHz-Mono mit `OpenError`. Dabei wurden keine Audio-Bytes erfasst oder gespeichert. Echte Hardwareaufnahme ist auf diesem Gerät somit noch nicht bestätigt. Prüfe bei Bedarf Windows **Einstellungen → Datenschutz & Sicherheit → Mikrofon → Desktop-Apps den Zugriff erlauben** sowie den Status/die Belegung des Focusrite-Eingangs. Die App ändert diese Systemberechtigungen nicht.

## Grenzen

Die Erkennung erzwingt Deutsch und läuft CPU-only über das lokale `whisper-cli.exe`; sie benötigt keine Python-STT-Bibliothek und keine global installierten Modelle. Ein langsamer Rechner kann bei einer längeren Aufnahme einige Sekunden für die Erkennung benötigen. Das Modell wird nicht stillschweigend aktualisiert; ein Versionswechsel muss samt neuer Quelle, Größe und Prüfsumme ausdrücklich dokumentiert werden.

Die Aufnahme basiert auf [Qt `QAudioSource`](https://doc.qt.io/qt-6/qaudiosource.html) und dem Standard-Eingabegerät von [Qt `QMediaDevices`](https://doc.qt.io/qt-6/qmediadevices.html). Es wird nur das von Windows als Standard gemeldete Mikrofon verwendet; Geräteauswahl und das Ändern von Windows-Datenschutzeinstellungen gehören nicht zur App.
