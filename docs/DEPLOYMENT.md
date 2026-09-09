# Deployment

Ziel fuer den ersten VPS-Schritt: flightopt laeuft als einzelner Container
auf dem Portainer-VPS, speichert SQLite in einem Volume und ist per Basic Auth
geschuetzt. Das ist gut genug fuer private Handy-Nutzung, solange HTTPS davor
liegt.

## Portainer

Der Stack baut aus dem oeffentlichen Repo
`https://github.com/nkl-solutions/flightopt_public.git`, nicht aus der privaten
Arbeitsfassung. Was dort nicht ankommt, existiert auf dem VPS nicht;
uebertragen wird mit `scripts/sync_public.py`.

1. In Portainer einen neuen Stack aus `docker-compose.portainer.yml` anlegen.
2. Unter Environment variables setzen:
   - `FLIGHTOPT_BASIC_USER`
   - `FLIGHTOPT_BASIC_PASSWORD`
   - `OPENAI_API_KEY` optional leer lassen, bis der KI-Modus echte
     Modellaufrufe nutzt.
   - `SERPAPI_KEY` optional. Ohne Wert bleibt SerpApi aus dem Quellenkatalog
     und die Suche laeuft ohne diese Verify-Schicht.
   - `SERPAPI_MONTHLY_CAP` optional, Standard 200. Deckelt die bezahlten
     Aufrufe je Kalendermonat; der Free Tier liegt bei 250.
3. Stack starten.
4. Einen HTTPS-Reverse-Proxy davor setzen, z.B. Nginx Proxy Manager, Caddy oder
   Traefik.

Die Namen stehen ohne Werte in `deploy/portainer.env.example`. Schluessel
gehoeren ausschliesslich in die Stack-Variablen von Portainer, nie ins Repo.

## Neu ausrollen

1. Im privaten Repo `uv run python scripts/sync_public.py` laufen lassen und
   den Secret-Scan gruen sehen.
2. Im oeffentlichen Repo committen und pushen. Push nur nach Freigabe.
3. In Portainer beim Stack `flightopt` "Pull and redeploy" ausloesen. Portainer
   holt den neuen Stand aus dem Repo und baut das Image neu.
4. Danach `/api/health` pruefen. Der Endpunkt bleibt absichtlich ohne Basic
   Auth erreichbar.

Geaenderte Environment-Variablen wirken erst nach einem Redeploy.

Die Compose-Datei haengt den Container an das vorhandene Docker-Netz
`proxy-network`. Damit kann Nginx Proxy Manager den Dienst intern erreichen,
ohne einen Host-Port oeffentlich freizugeben.

Sobald eine Domain existiert, eignet sich in Nginx Proxy Manager:

- Domain: `flightopt.nkl-solutions.de`
- Scheme: `http`
- Forward Hostname / IP: `flightopt`
- Forward Port: `8000`
- SSL: Let's Encrypt, Force SSL aktivieren

Stand 2026-09-06: Portainer-Stack und Nginx Proxy Manager sind eingerichtet.
NPM zeigt `flightopt.nkl-solutions.de -> http://flightopt:8000` als online,
mit Let's Encrypt, Force SSL und HTTP/2. Der direkte Test gegen die VPS-IP
liefert erwartungsgemaess Basic Auth. Nach DNS-Propagation zeigt die normale
Domain oeffentlich auf `159.195.64.156`; HTTP leitet auf HTTPS weiter, HTTPS
liefert Basic Auth.

Falls die normale Domain noch nicht erreichbar ist, aber dieser Test klappt,
ist NPM korrekt und nur DNS noch nicht durch:

```bash
curl --resolve flightopt.nkl-solutions.de:443:159.195.64.156 -I https://flightopt.nkl-solutions.de/
```

Langfristig kann Flightopt spaeter unter einer Deal-Seite als `/flights` oder
eigener Subdomain laufen. Fuer den ersten privaten Handy-Zugriff ist
`flightopt.nkl-solutions.de` am klarsten.

## Sicherung

Der Code liegt in zwei Git-Repos und ist jederzeit wiederherstellbar. Die
Preisbeobachtungen liegen einmal lokal in `data/flightopt.db` und ein zweites,
davon voellig getrenntes Mal im Docker-Volume `flightopt-data` auf dem VPS.
Beide Bestaende fliessen nie zusammen, und keiner von beiden laesst sich
nachtraeglich herstellen: eine verlorene Beobachtung ist ein Tag, den niemand
nachholt. Also beide sichern.

### Lokal

```bash
uv run python scripts/backup_db.py
```

Laeuft ohne Argumente und ohne den Dienst anzuhalten. Das Skript legt
`data/backups/flightopt-JJJJMMTT-HHMMSS.db` an, prueft die Kopie danach mit
`integrity_check` und zaehlt die Beobachtungen darin. Rueckgabewert 0 heisst
gesichert und geprueft, 1 fehlgeschlagen, 2 Bedienfehler; damit erkennt eine
Aufgabenplanung den Fehlschlag.

Es kopiert nicht die Datei, sondern nutzt die Backup-Schnittstelle von SQLite.
Ein `cp` oder `tar` ueber eine laufende Datenbank im WAL-Modus liefert eine
Kopie, die sich oeffnen laesst und trotzdem nicht stimmt, weil die zuletzt
geschriebenen Zeilen im Write-Ahead-Log daneben stehen.

Aufbewahrt werden die letzten 14 Sicherungen plus je der juengste Stand der
letzten 6 Kalendermonate. Alles andere wird geloescht, fremde Dateien im
Ordner bleiben unangetastet.

`data/backups` liegt auf derselben Platte wie die Datenbank und schuetzt damit
gegen ein Versehen und gegen eine kaputte Datenbank, nicht gegen einen
Plattenausfall. Fuer das zweite Exemplar auf ein anderes Laufwerk zeigen:

```bash
uv run python scripts/backup_db.py --target-dir D:/flightopt-backups
```

Taeglicher Lauf unter Windows, einmal einrichten:

```powershell
schtasks /Create /TN flightopt-backup /SC DAILY /ST 03:00 /TR "cmd /c cd /d C:\Users\klink\Flightopt\flightopt_private && uv run python scripts/backup_db.py"
```

### VPS-Volume

Das Image kopiert nur `flightopt/` hinein, `scripts/` ist dort nicht vorhanden.
Auf dem VPS laeuft dieselbe Backup-Schnittstelle deshalb als Einzeiler im
laufenden Container. Das ist ein Handgriff des Nutzers, kein Code im Projekt:

```bash
docker exec -i flightopt python - <<'PY'
import sqlite3, datetime, pathlib
target = pathlib.Path("/app/data/backups")
target.mkdir(parents=True, exist_ok=True)
path = target / f"flightopt-{datetime.datetime.now():%Y%m%d-%H%M%S}.db"
source = sqlite3.connect("file:/app/data/flightopt.db?mode=ro", uri=True)
copy = sqlite3.connect(path)
source.backup(copy)
source.close()
print(path, copy.execute("SELECT count(*) FROM price_observation").fetchone()[0])
copy.close()
PY
```

Danach die Sicherungen vom VPS herunterholen, sonst liegen sie im selben
Volume wie das Original:

```bash
docker cp flightopt:/app/data/backups ./flightopt-vps-backups
```

Wer zusaetzlich das ganze Volume wegschreiben will, nimmt einen
Wegwerf-Container. Das ersetzt den Schritt oben nicht, es sichert ihn ab:

```bash
docker run --rm -v flightopt-data:/data:ro -v "$PWD:/out" alpine \
  tar czf /out/flightopt-data-$(date +%Y%m%d).tar.gz -C /data .
```

---

## Tests im CI

`.github/workflows/tests.yml` faehrt die Suite bei jedem Push und bei jedem
Pull Request: Ubuntu, Python 3.13, `uv sync --frozen`, dann
`uv run python -m pytest -q`. Ein Lauf, eine Python-Version, kein
Matrix-Aufbau, Abhaengigkeiten gecacht an `uv.lock`. Fuer das oeffentliche Repo
sind die Minuten frei, fuer das private zaehlen sie gegen ein Kontingent.

Kein Test ist abgewaehlt. Die Suite arbeitet gegen aufgezeichnete Antworten
unter `tests/fixtures/`; die einzige echte Anfrage geht an die
EZB-Referenzkurse, weil neun Tests im Job-Runner eine frische Datenbank anlegen
und deshalb keinen Kurs im Cache finden. Der Lauf leitet `www.ecb.europa.eu`
per `/etc/hosts` ins Leere, statt diese neun Tests auszuschliessen: sie decken
genau den Runner ab, in dem die vier stillen Fehler steckten, und die
Kursabfrage faellt ohnehin weich auf den mitgelieferten Snapshot zurueck.
Die Sperre sitzt auf Hostebene, weil `curl_cffi` an Pythons `socket`-Modul
vorbei aufloest.

Ebenfalls gesetzt: `FLIGHTOPT_DAILY_SCANS=0`. Ohne das startet der Tagesplaner
beim ersten `TestClient` und stiesse eine echte Suche an.

Der Ablauf faellt unter die Whitelist von `scripts/sync_public.py` und landet
damit auch im oeffentlichen Repo, aus dem der Portainer-Stack baut. Genau dort
gehoert er hin: was auf dem VPS landet, soll vorher gruen gewesen sein.

---

## Ressourcen

Der Stack setzt:

- `mem_limit: 512m`
- `cpus: "1.0"`
- `MALLOC_ARENA_MAX=2`

Python hat keine portable Heap-Grenze wie `NODE_OPTIONS` oder `GOMEMLIMIT`.
Die harte Grenze ist deshalb das Docker-/cgroup-Limit; ein einzelner Uvicorn-
Prozess bleibt bewusst schlicht.

## Hotelsuche auf dem VPS

Trivago laeuft im bestehenden Stack ohne Zutun: dieselbe HTTP-Schicht wie die
Flugquellen, kein Schluessel, kein Browser. Seite und Endpunkte (`/hotels`,
`/api/hotels/*`) liegen hinter derselben Basic Auth wie alles andere.

Booking braucht einen Browser, und deshalb gibt es zwei Bauwege.

### Zwei Bauwege, ein Image

Das Dockerfile hat zwei Abschnitte, ausgewaehlt ueber die Stack-Variable
`FLIGHTOPT_VARIANT`:

| Wert | Was drin ist | Booking |
|------|--------------|---------|
| `lean` (Standard) | Python, Kernabhaengigkeiten, Anwendung | aus |
| `hotels` | dazu Playwright und Chromium samt Systembibliotheken | an |

Ein Image mit zwei Abschnitten und nicht zwei getrennte Images: die Anwendung
ist ein Prozess und eine SQLite-Datei. Zwei Container gegen dasselbe Volume
waeren zwei Schreiber auf derselben Datenbank. Ein zweites Image mit eigener
Kopie der Anwendung waere ausserdem eine zweite Wahrheit, die auseinanderlaeuft.

Der `hotels`-Abschnitt setzt `FLIGHTOPT_HOTELS_BOOKING=1` selbst - ein Image
mit Browser und ohne Schalter waere ein Image ohne Zweck. Die Stack-Variable
gleichen Namens bleibt daneben als Notausschalter stehen.

Umstellen heisst: `FLIGHTOPT_VARIANT=hotels` **und** `FLIGHTOPT_MEM_LIMIT`
hochsetzen, danach "Pull and redeploy". Wer nur das erste tut, bekommt einen
Container, den der Kernel beim ersten Chromium abraeumt.

Ein Vorbehalt zum Bauen: BuildKit baut nur die Abschnitte, die der gewaehlte
Weg wirklich braucht - beim schlanken Weg wird der Browser also gar nicht
erst geholt. Der alte Builder baut dagegen alle Abschnitte. Sollte der erste
schlanke Build ploetzlich mehrere hundert Megabyte ziehen, ist BuildKit aus;
`DOCKER_BUILDKIT=1` in der Umgebung des Docker-Daemons schaltet es ein.

### Speicher: was gemessen ist und was nicht

Gemessen auf dem Entwicklungsrechner, mit `spike/probe_browser_memory.py`
(sechzig Ladevorgaenge der aufgezeichneten Ergebnisseite ueber einen lokalen
Server, kein fremder Verkehr) und `spike/probe_booking_live.py` (zwei echte
Ergebnisseiten):

| Messpunkt | Wert |
|-----------|------|
| Chromium im Leerlauf nach dem Start | rund 140 MB |
| nach 60 gespeicherten Seiten | 143,1 MB, also rund 55 kB Zuwachs je Seite |
| mit echten Booking-Seiten samt WAF-Challenge | 151,3 MB, nach der zweiten Seite 153,1 MB |
| nach dem Schliessen | 0 MB, es bleibt nichts stehen |
| Startdauer eines Chromium | rund 0,11 s |
| eine echte Ergebnisseite Ende bis Ende | 4,3 bis 5,2 s |

**Nicht gemessen** ist die Groesse des fertigen Images und der
Speicherbedarf des laufenden Containers. Auf diesem Rechner laeuft kein
Docker: WSL2 meldet "wird von Ihrer aktuellen Computerkonfiguration nicht
unterstuetzt", die optionale Komponente "Plattform fuer virtuelle Computer"
fehlt. Das zu aendern braucht Administratorrechte und einen Neustart und ist
deshalb nichts, was ein Werkzeug nebenbei tut.

Beides steht mit drei Befehlen auf jedem Docker-Wirt fest. Der erste baut
beide Wege, der zweite nennt die Groessen, der dritte den Speicherbedarf unter
Last:

```bash
docker build --build-arg FLIGHTOPT_VARIANT=lean   -t flightopt:lean   .
docker build --build-arg FLIGHTOPT_VARIANT=hotels -t flightopt:hotels .
docker images flightopt --format '{{.Tag}}	{{.Size}}'
```

```bash
docker run --rm -d --name flightopt-mess -m 1g \
  -e FLIGHTOPT_BASIC_USER=mess \
  -e FLIGHTOPT_BASIC_PASSWORD=mess \
  -e FLIGHTOPT_DAILY_SCANS=0 \
  flightopt:hotels
docker exec flightopt-mess python -c "print(open('/sys/fs/cgroup/memory.current').read())"
# eine echte Suche anstossen, danach:
docker stats --no-stream flightopt-mess
docker exec flightopt-mess python -c "print(open('/sys/fs/cgroup/memory.peak').read())"
docker rm -f flightopt-mess
```

Bis diese Zahlen vorliegen, gilt als Ausgangspunkt: `mem_limit: 1g` fuer den
`hotels`-Weg. Der schlanke Container lief bisher in 512m; ein Chromium mit zwei
Kontexten kommt mit gemessenen rund 150 MB dazu, plus Neustart-Spitze und
Reserve. Wer den Wert danach senken will, senkt ihn gegen `memory.peak` und
nicht gegen ein Gefuehl.

Die Grenze ist Pflicht und nicht Kosmetik. Python hat keine portable
Heap-Obergrenze und Chromium erst recht nicht; was den Wirt schuetzt, ist
dieses cgroup-Limit. Reisst der Container es, raeumt der Kernel **im
Container** auf - der Dienst stirbt, der VPS nicht. Ohne Grenze nimmt ein
davonlaufender Chromium den ganzen Wirt mit, und dann steht auch die
Flugsuche.

Ein zweiter Deckel liegt im Browser selbst. Das Image setzt

```
FLIGHTOPT_HOTELS_BROWSER_ARGS="--no-sandbox,--disable-dev-shm-usage,--disable-gpu,--js-flags=--max-old-space-size=256"
```

`--js-flags=--max-old-space-size=256` ist das Gegenstueck zu `NODE_OPTIONS`:
es deckelt den Renderer, bevor `mem_limit` den ganzen Container abraeumt.
`--disable-dev-shm-usage` haelt Chromium von `/dev/shm` fern, das in Docker
standardmaessig 64 MB gross ist und sonst mitten in einer Seite ausgeht (die
Alternative waere `shm_size: 256m` am Dienst). `--no-sandbox` ist noetig, weil
im Container alles als root laeuft und Chromium so seine eigene Sandbox nicht
startet; die Grenze ist dort der Container. Auf dem Entwicklungsrechner ist
die Variable leer, dort behaelt Chromium seine Sandbox.

### Der lange Browser

Der Chromium wird nicht mehr je Suchlauf gestartet und weggeworfen. Ein Pool
je Prozess haelt hoechstens einen und schliesst ihn nach drei Regeln:

| Grenze | Voreinstellung | Variable |
|--------|----------------|----------|
| Seiten je Prozess | 60 | `FLIGHTOPT_HOTELS_BROWSER_PAGES` |
| Hoechstdauer | 1800 s | `FLIGHTOPT_HOTELS_BROWSER_MAX_AGE` |
| Leerlauffrist | 120 s | `FLIGHTOPT_HOTELS_BROWSER_IDLE` |

Die Leerlauffrist ist die wichtigste der drei: zwischen zwei Durchgaengen
liegen Minuten, und in denen braucht der Container so wenig Speicher wie ohne
Booking. Dazu kommt eine Frist von 120 Sekunden fuer einen ganzen
Ladevorgang - Playwright misst je Schritt, und ein Schritt, der nie
zurueckkehrt, haelt sonst seinen Platz fuer immer. Faellt ein Ladevorgang mit
einem Fehler aus, wird der Browser weggeworfen statt weiterbenutzt.

### Dauerbetrieb: gespeicherte Suchen

Ein Hotellauf war bisher etwas, das ein Mensch startet. So findet er nie einen
Preisfehler, denn die Erkennung braucht Historie und Historie entsteht nur
durch Wiederholung. `hotel_watch` traegt deshalb gespeicherte Suchen mit
rollendem Vorlauf-Fenster, zugeschnitten wie die Beobachtungsliste der Fluege:

* faellig ist ein **Kalendertag**, kein Stundenabstand - so driftet "einmal am
  Tag" nicht, und ein Neustart des Dienstes loest keinen zweiten Abruf aus,
* **gelaufen heisst gelaufen**, auch ohne Treffer: sonst laeuft dieselbe
  kaputte Suche im Takt des Planers immer wieder gegen dieselbe Wand,
* hoechstens **drei Beobachtungen je Durchgang**. Ein Hotelfenster kostet eine
  Anfrage je Anreisetag und Quelle; ein Flugkalender liefert sechzig Tage in
  einer einzigen.

Geschrieben wird ueber denselben Weg wie im Handbetrieb (`run_scan` ->
`store.record_offers` -> `price_observation`). Zwei Schreibwege in dieselbe
Tabelle waeren zwei Wahrheiten.

Gemeldet wird hier nichts. `hotel_findings` liefert die auffaelligen Zeilen
fertig gerechnet, im selben Zuschnitt wie `collect_deals` bei den Fluegen,
damit beide denselben Melder fuellen koennen.

### Preisfehler bei Hotels: was wann greift

Vier Stufen gibt es (`error`, `cheap`, `normal`, `expensive`, dazu
`encoding_suspect` und `unknown`). Was davon wann ueberhaupt etwas sagen kann:

| Bedingung | Braucht | Greift ab |
|-----------|---------|-----------|
| Plausibilitaetsschranke je Sternekategorie | nichts | dem ersten Tag |
| Preis unter 30 Prozent des Medians | 5 Beobachtungen in der Gruppe | siehe Anlaufzeit |
| 6-fache Streuung unter dem Median | 10 Beobachtungen in der Gruppe | spaeter |
| Median-Band (`cheap` / `normal` / `teuer`) | 5 Beobachtungen in der Gruppe | siehe Anlaufzeit |

"Gruppe" heisst: dasselbe Haus, derselbe Wochentag, dieselbe Vorlauf-Stufe,
dieselbe Belegung, dieselbe Naechtezahl, dieselbe Waehrung. Daraus folgt die
Anlaufzeit, und sie ist unbequemer als sie aussieht:

* **Ein Fenster von einem Anreisetag braucht fuenf Wochen.** Je Durchgang
  faellt genau eine Beobachtung in genau eine Gruppe, und die naechste in
  dieselbe Gruppe erst eine Woche spaeter - der Wochentag wandert mit.
* **Vierzehn Anreisetage in derselben Vorlauf-Stufe brauchen drei Tage.**
  Jeder Wochentag ist zweimal dabei, die Gruppe hat nach drei Durchgaengen
  sechs Punkte.
* Die Voreinstellung (Vorlauf 14 bis 20) liegt vollstaendig in der Stufe
  "14-29" und deckt jeden Wochentag einmal ab: eine Woche Anlauf bei sieben
  Anfragen je Quelle und Durchgang.

Bis dahin traegt ausschliesslich die Plausibilitaetsschranke, und die findet
nur, was in keinem europaeischen Markt ein Angebot ist. Damit die Oberflaeche
das nicht verschweigt, steht es in der Begruendung der Zeile selbst:

* ohne Historie: `"unter der Schranke von 45 Euro je Nacht, ohne
  Vergleichspreise"`,
* mit duenner Historie: `", nur 6 Vergleichspreise"`.

Einen dritten Zusatz gab es bis zum 2026-09-09: `", Vergleichsgruppe enthaelt
auch Richtwerte"`. Er stand an jedem Urteil ueber einen Haendlerpreis, weil
`hotel_baseline` Richtwerte und Haendlerpreise in derselben Verteilung fuehrte.
Der Zusatz war ehrlich, aber ein Zusatz ist keine Trennung: der Median lag
trotzdem daneben.

Die Tabelle traegt jetzt `population` im Primaerschluessel, genau wie
`flight_baseline` sein `is_estimate`, und der Zusatz faellt damit weg. Der
Preis dafuer steht in `docs/PRICE_HISTORY.md`, Abschnitt 8: jede
Grundgesamtheit braucht ihre eigenen fuenf Beobachtungen je Gruppe, und bis
die zusammen sind, steht dort `keine Basis`. Eine Zahl, die die falsche
Verteilung misst, ist schlechter als keine.

Der Bestand wird von `scripts/migrate_hotel_baseline_populations.py`
nachgezogen. Ohne diesen Lauf legt der Dienst die Tabelle beim ersten
Hotelsignal selbst neu an und rechnet sie beim naechsten Durchgang wieder
voll; das Skript macht dasselbe, aber mit einem Bericht darueber, was sich
verschoben hat.

## Healthcheck

`/api/health` bleibt ohne Basic Auth erreichbar, damit Docker den Container
einfach pruefen kann. Die Oberflaeche und alle Such-Endpunkte sind geschuetzt,
sobald User und Passwort gesetzt sind.

Weil der Endpunkt offen ist, steht in seiner Antwort auch nur das Urteil:
`{"ok": true}` mit 200, oder `{"ok": false}` mit 503. Keine Quellennamen, keine
Pfade, keine Zaehlerstaende. Rot wird er, wenn die Datenbank nicht erreichbar
ist oder wenn Basic Auth halb konfiguriert wurde (nur User oder nur Passwort) -
dann kommt durch den Container ohnehin kein einziger Aufruf durch, und das soll
Docker sehen.

Die ausfuehrliche Auskunft steht unter `/api/health/detail` und will die
Zugangsdaten sehen. Sie nennt

- die Quellen, die wirklich im Katalog stehen, samt Airline-Codes je Adapter
  (SerpApi taucht auf, sobald `SERPAPI_KEY` gesetzt ist; Hotelquellen mit dem
  Grund, warum eine fehlt),
- wann jede Quelle zuletzt eine Beobachtung geschrieben hat, und seit wie
  vielen Tagen sie still ist,
- ob die Datenbank erreichbar ist und ob der Tagesplaner laeuft.

"Still" heisst nicht "kaputt": eine Quelle, nach der niemand gefragt hat,
schreibt auch nichts. Die Tageszahl daneben ist der Anhaltspunkt, der Schluss
gehoert einem Menschen. Live-Anfragen an fremde Server macht der Healthcheck
nicht - er laeuft alle 30 Sekunden.
