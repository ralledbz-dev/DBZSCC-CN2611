# Brave-Websuche: isolierter Service-Slice

## Was dieser Slice bereitstellt

`dbzscc.app.brave_search.BraveSearchClient` ist ein Qt-freier, schlanker Python-Service für ausdrücklich übergebene Suchanfragen an Braves offizielle Web Search API. Er liefert typisierte Ergebnisse mit Titel, Quell-URL, Snippet und UTC-Abrufzeit sowie Hinweise auf weitere Ergebnisse und Rate Limits.

Jede ausgeführte Suche übermittelt die sichtbare Suchanfrage an Brave. Der Schlüssel wird bei jedem `search()`-Aufruf neu aus der Umgebungsvariable `BRAVE_SEARCH_API_KEY` gelesen und ausschließlich im HTTP-Header `X-Subscription-Token` verwendet. Fehlt der Schlüssel, wird vor jedem Netzaufruf ein verständlicher Fehler ausgelöst.

## Datenfluss und Schutzgrenzen

Der Service erhält nur den Query-Text und ausdrücklich angegebene Suchoptionen (`count`, `offset`, `country`, `search_lang`, `freshness`). Er liest weder Chat-Historie noch Projekt-, Datei- oder sonstige persönliche Inhalte aus der App und fügt solche Daten auch nicht automatisch zur Anfrage hinzu. Die aufrufende UI-/Agentenintegration muss vor dem Senden die konkrete Query sichtbar machen und darf vertrauliche Informationen nicht in sie aufnehmen.

Der Transport verwendet ausschließlich HTTPS zur festen Domain `api.search.brave.com` und zum Web-Search-Endpunkt. Umgebungs-Proxys werden deaktiviert; Redirects werden abgewiesen und nicht verfolgt. Es gibt einen begrenzten Timeout, höchstens 20 Ergebnisse pro Anfrage, einen begrenzten Antwortumfang und keine automatischen Wiederholungen. Brave-Fehlerantworten werden nicht in Exceptions ausgegeben. API-Schlüssel werden weder in Ergebnissen noch in Fehlermeldungen oder Logs aufgenommen.

Die Suchergebnisse sind externe, nicht vertrauenswürdige Inhalte. Der Service öffnet Ergebnis-URLs nicht; eine spätere Integration muss Inhalte weiter als externe Daten behandeln und darf sie nicht ungeprüft als Anweisungen ausführen.

## Schnittstelle und Fehler

```python
from dbzscc.app.brave_search import BraveSearchClient

client = BraveSearchClient()
response = client.search(
    "sichtbare Suchanfrage",
    count=10,
    country="DE",
    search_lang="de",
    freshness="pw",
)
```

`count` ist auf 1–20 begrenzt. `offset` akzeptiert Braves dokumentierte Seitenpositionen 0–9; `more_results_available` zeigt an, ob eine weitere Seite verfügbar ist. Zeiträume können `pd`, `pw`, `pm`, `py` oder ein gültiger ISO-Datumsbereich sein. Country- und Sprachcodes sind optionale zweistellige Codes.

Ergebnisse sind `BraveSearchResponse` mit einer Tupel-Liste von `BraveSearchSource`-Werten. Typisierte Fehler unterscheiden ungültige Eingaben, fehlenden Schlüssel, Transport-/JSON-Probleme, HTTP-Fehler, zurückgewiesene Redirects und HTTP 429 (`RateLimitedError`). Rate-Limit-Header und – sofern angegeben – ein Retry-Zeitwert werden zurückgegeben; der Service schläft nicht und wiederholt nicht selbständig.

## Tests und Abgrenzung

`tests/test_brave_search.py` verwendet nur einen Fake-Transport und fiktive Schlüssel. Die Tests führen keine Brave-Anfragen aus und lesen keinen realen EnvVar-Schlüssel. Abgedeckt sind unter anderem Query-/Parametergrenzen, Schlüsselabruf pro Aufruf, fehlender Schlüssel ohne Netzaufruf, Header, Zeitstempel, Quell-URL-Filter, HTTP 429, Redirect-Ablehnung, Host-Allowlist, fehlerhafte/zu große Antworten und Redaction von Transportfehlern.

Dieser Slice implementiert **noch keinen Agenten-Router und keine UI-/Chat-Integration**. Er entscheidet nicht selbst, ob ein Informationsbedarf Websuche verlangt, und sendet nichts automatisch. Es wurde keine LangChain-Abhängigkeit ergänzt.

## Folgeintegration in der lokalen Roadmap

Die bestehende [Reifegrad-Roadmap](DBZSCC2611_PRODUCTION_READINESS.md) führt W1 als geplanten Brave-Search-Informationsagenten nach den ModelPort-/Runtime- und Tool-Gates. In diesem späteren Integrationsslice kann die App externe bzw. aktuelle Informationsbedarfe an diesen einen Suchanbieter routen. Dafür bleiben eine erkennbare, vor dem Senden freizugebende Query, eine explizite Berechtigungsgrenze und Quellenanzeige erforderlich. Dieser isolierte Service-Slice schließt W1 oder ein Produktreife-Gate nicht ab.

## Offizielle Quellen

- [Brave Web Search API](https://api-dashboard.search.brave.com/documentation/services/web-search) — Web-Endpunkt, Suchparameter, `count` bis 20, `offset` 0–9 und `more_results_available`.
- [Brave API Authentication](https://api-dashboard.search.brave.com/documentation/guides/authentication) — vertraulicher Schlüssel im Header `X-Subscription-Token`.
- [Brave Rate Limiting](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting) — HTTP 429 sowie `X-RateLimit-*`-Header.
