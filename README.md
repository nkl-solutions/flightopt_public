# flightopt

![flightopt logo](docs/assets/flightopt-logo.png)

flightopt ist ein lokaler Multi-Stopp-Flugoptimierer. Die App sucht nicht jede
Reise einzeln ab, sondern holt Kalenderpreise pro Teilstrecke und kombiniert
daraus die günstigsten Datumsketten.

Das Projekt ist bewusst als Portfolio-Version aufbereitet: verständlicher Code,
Tests, Fixtures und klare Grenzen. Keine Secrets, keine privaten Suchprofile,
keine lokale Datenbank.

## Was enthalten ist

- FastAPI-Backend mit Server-Sent Events für Fortschritt
- SQLite für Cache, Jobs, Preisbeobachtungen, Profile und Baselines
- dynamischer Programmieralgorithmus für Multi-Stopp-Routen
- Single-File-Web-UI ohne Build-Schritt
- Airline-Adapter mit aufgezeichneten Test-Fixtures
- Flughafen-Gruppen wie `DE` und `DE-OST`
- gespeicherte Suchprofile als Basis für tägliche Scans
- Preisbaseline mit einfachem Ausreißer-Signal
- transparente Aufgabegepäck-Annahmen je Airline

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
  storage/   SQLite, Cache, Baselines
  web/       lokale Oberfläche
tests/       Unit-Tests und aufgezeichnete Fixtures
```

## Regeln

- keine Credentials im Repo
- keine lokalen Datenbanken im Repo
- keine CAPTCHA-Umgehung
- keine Login- oder Paywall-Umgehung
- nur lokale Nutzung und transparente Tests
- Vergleichsquellen werden als Richtwert markiert

## Lizenz

Apache-2.0.

Projektname und Logo sind Teil der Projektidentität und nicht als Airline-,
Portal- oder Drittmarke zu verstehen.
