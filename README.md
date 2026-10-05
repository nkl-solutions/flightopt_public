# flightopt

![flightopt logo](docs/assets/flightopt-logo.png)

flightopt ist ein lokaler Multi-Stopp-Flugoptimierer. Die App sucht nicht jede
Reise einzeln ab, sondern holt Kalenderpreise pro Teilstrecke und kombiniert
daraus die günstigsten Datumsketten.

Aktueller Funktionsstand, Prioritäten und Abschlusskriterien:
[Roadmap](ROADMAP.md).

Das Projekt ist bewusst als Portfolio-Version aufbereitet: verständlicher Code,
Tests, Fixtures und klare Grenzen. Keine Secrets, keine privaten Suchprofile,
keine lokale Datenbank.

## Was enthalten ist

- FastAPI-Backend mit Server-Sent Events für Fortschritt
- SQLite für Cache, Jobs, Preisbeobachtungen, Profile und Baselines
- dynamischer Programmieralgorithmus für Multi-Stopp-Routen
- responsive Web-UI in HTML, CSS und JavaScript ohne Build-Schritt,
  mit selbst gehosteten Schriften
- klare Bereiche für Flugsuche, Hotels, gespeicherte Suchen und Preisradar;
  kompakte Formulare und Ergebnisse mit aufklappbaren Details
- Airline-Adapter mit aufgezeichneten Test-Fixtures
- weltweite Flughafensuche mit Metro- und Ländergruppen wie `TYO`, `DE` und
  `DE-OST`, deutschen Aliasnamen und Tab-Vervollständigung
- gespeicherte Suchprofile als Basis für tägliche Scans
- Profilverwaltung mit Status, Pausieren, Name-/Rhythmusänderung und Einzelstart;
  abgelaufene Reisezeiträume werden nicht mehr abgefragt
- Preisbaseline aus Median und MAD, getrennt nach Kalenderpreisen,
  geprüften Angeboten und Richtwerten
- transparente Aufgabegepäck-Annahmen je Airline
- Portainer-/VPS-Start mit optionaler Basic Auth
- schnelle Satz-Eingabe als Vorstufe für den KI-Modus
- Spracheingabe für das KI-Feld im Browser
- parallele Kalender- und Live-Prüfungen für schnellere Suchen
- getrennte Ergebnisgruppen für geprüfte Routen und Kandidaten;
  Favorit nur aus geprüften Preisen
- Quellenstatus, Abbruch und schrittweise Ergebnisse während der Suche
- Währungsumrechnung mit EZB-Referenzkursen und sichtbarem Originalpreis
- Beobachtungsliste mit rollenden Fenstern, täglichen Scans und optionalem
  20-Minuten-Takt
- Fehltarif-Jagd mit Preisverläufen, Entdopplung und optionalem Discord-Versand
- Hotelsuche über Trivago MCP und optional Booking mit Playwright
- gespeicherte Hotelbeobachtungen und optionale Gesamtreise aus Flug und Hotel
- eindeutige Hotel-Watch-Historie, ohne alte Daten fremden Zielen zuzurechnen
- Hotelüberwachung in der Oberfläche: speichern, pausieren und fällige Suchen prüfen
- sichtbare Buchungslinks je Flug-Teilstrecke und Hotelangebot
- getrennte Flug-/Hotel-Scheduler-Bahnen mit Schutz vor überlappenden Sammelläufen

## Warum das spannend ist

Normale Portale erwarten feste Daten. flightopt dreht die Suche um:

```text
viele mögliche Reisen
-> wenige Teilstrecken-Kalender
-> lokale Optimierung
-> Live-Prüfung der besten Kandidaten
```

Dadurch kann eine flexible Route über Wochen gescannt werden, ohne jede
Datumskombination einzeln anzufragen.

## Start

```bash
uv sync
uv run pytest tests/ -q
uv run uvicorn flightopt.api.main:app --port 8000
```

Danach:

```text
http://127.0.0.1:8000
```

Unter Windows funktionieren bei blockierten Script-Wrappern die Modulaufrufe
`uv run python -m pytest tests/ -q` und
`uv run python -m uvicorn flightopt.api.main:app --port 8000`.

## Eingabe und Preise

Die Satz-Eingabe liest einfache Routen lokal und füllt die normale Suche vor.
Spracheingabe verwendet die Browser-Spracherkennung, zum Beispiel in Chrome
oder Edge über HTTPS oder localhost. Die Route wird vor dem Suchen bestätigt.
Ein OpenAI-Modellaufruf für komplexe Reisewünsche ist noch nicht angeschlossen.

Kalenderpreise bilden den Suchraum. Danach werden Kandidaten bei den Quellen
nachgeprüft, soweit diese eine Tagessuche anbieten. Nur vollständig geprüfte
Routen erscheinen in der geprüften Gruppe. Richtwerte bleiben als solche
sichtbar. Die Preislage beantwortet eine andere Frage: ob ein Preis gegenüber
vergleichbaren Beobachtungen günstig oder teuer ist.

Hotelpreise dienen als Richtwerte; fehlt ein Aufenthaltspreis, bleibt die
Gesamtsumme offen. Booking benötigt zusätzlich
`uv sync --group hotels` und `uv run python -m playwright install chromium`.

## VPS

Der erste Container-Start für Portainer liegt in `docker-compose.portainer.yml`.
Details stehen in `docs/DEPLOYMENT.md`.

Der Standardbauweg `lean` enthält keinen Browser. `FLIGHTOPT_VARIANT=hotels`
baut Chromium mit; dafür zunächst `FLIGHTOPT_MEM_LIMIT=1g` verwenden und den
tatsächlichen Verbrauch messen. Optionaler Discord-Versand wird mit
`FLIGHTOPT_DISCORD_WEBHOOK` eingerichtet. Beispielvariablen stehen in
`deploy/portainer.env.example`, persönliche Werte bleiben in der Umgebung.

CLI:

```bash
uv run python -m flightopt.cli search BER FCO ATH BER --window 2026-10-05:2026-11-30 --stay 3-10
```

## Projektstruktur

```text
flightopt/
  api/       FastAPI-Endpunkte
  domain/    Modelle, Airports, Airlines
  jobs/      Jobs, Scheduler, gespeicherte Profile
  search/    Kalender-Matrix, Optimierung, Verifikation
  sources/   Airline-Quellen
  hotels/    Hotelquellen, Scans und Preisbewertung
  trip/      Aufenthalte und Hotelanteil der Gesamtreise
  hunt/      Preisalarme, Quellenbudgets und Discord
  storage/   SQLite, Cache, Baselines
  web/       lokale Oberfläche
tests/       Unit-Tests und aufgezeichnete Fixtures
scripts/     Datenaufbereitung, Migration und Datenbank-Backup
```

## Projektgrenzen

Dieses Repo enthält nur den sauberen App-Code, Tests und Beispiel-Fixtures.
Lokale Datenbanken, Suchprofile, Cookies und API-Keys bleiben bewusst draußen.

Die Quellen werden vorsichtig und transparent genutzt. Vergleichsdaten werden
nicht als echte Endpreise verkauft, sondern als Richtwert markiert.

## Lizenz

Apache-2.0.

Projektname und Logo sind Teil der Projektidentität und nicht als Airline-,
Portal- oder Drittmarke zu verstehen.
