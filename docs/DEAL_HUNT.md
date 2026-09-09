# Die Jagd auf Fehltarife

Stand 2026-09-09. Wer am Takt, an der Erkennung oder am Meldekanal arbeitet,
schaut zuerst hier nach. Was ein Preis gegenueber seiner Historie taugt, steht
dagegen in `docs/PRICE_HISTORY.md`; die vierte Stufe `error` ist dort
beschrieben und hier nur noch benutzt.

Der Code liegt in `flightopt/hunt/` (Takt, Budget, Erkennung, Meldung),
`flightopt/jobs/hunt.py` (der Durchgang) und `flightopt/api/main.py` (die
Endpunkte).

Die Kurzfassung: die Beobachtungsliste bekommt einen zweiten Takt. Eine als
**heiss** markierte Strecke wird alle zwanzig Minuten gefragt statt einmal am
Tag, ein Fund landet in `alert_event` und, wenn ein Webhook gesetzt ist, auf
Discord. Alles daran ist so gebaut, dass es im Zweifel zu langsam ist statt zu
schnell - die Quellen sind dieselben, die auch die Suche benutzt.

---

## 1. Der Takt und woraus er folgt

### Gemessen: was ein Durchgang kostet

Ein Durchgang holt fuer eine Strecke den Preiskalender ueber sechzig Tage.
Was das an HTTP-Abrufen kostet, entscheidet der Endpunkt. Gemessen an einem
Fenster, das mitten im Monat beginnt und damit drei Kalendermonate beruehrt
(`tests/test_hunt_cadence.py` haelt die Messung fest):

| Quelle | Abrufe | Grund |
|---|---|---|
| ryanair, aegean, britishairways, jetblue | 3 | Endpunkt nimmt einen Monat |
| wizz, condor | 2 | Zeitraum, aber begrenzte Fensterlaenge |
| eurowings, icelandair, airbaltic, kiwi | 1 | ein Abruf deckt das Fenster |

Schlechtester Fall: **drei Abrufe je Quelle und Durchgang**. Verbucht wird
dieser Wert fuer jede Quelle, auch fuer die, die mit einem auskommt. Zu teuer
verbuchen kostet Takt, zu billig verbuchen kostet die Quelle.

### Abgeleitet: zwanzig Minuten

Der Massstab ist etwas, das ohnehin passiert und das keine Quelle je
beanstandet hat: eine gewoehnliche Suche. Drei Teilstrecken ueber dasselbe
Fenster kosten je Quelle drei mal drei, also neun Abrufe. Der Takt ist so
gewaehlt, dass eine heisse Strecke je Quelle und Stunde genau so viel kostet:

```
3 Abrufe je Durchgang  x  3 Durchgaenge je Stunde  =  9 Abrufe je Stunde
```

Drei Durchgaenge je Stunde sind **1200 Sekunden** Abstand. Dazu bis zu 120
Sekunden Streuung, und die wirkt **nur nach hinten** - nach vorn wuerde sie
die Rechnung aufweichen, aus der die Obergrenze folgt.

### Die harte Obergrenze: 45 je Quelle und Stunde

Der Takt allein haelt nichts, denn zwanzig heisse Strecken waeren zwanzig mal
neun. Darueber liegt eine Grenze je Quelle, und sie haengt an der
**langsamsten** Kalenderquelle, weil ein Durchgang immer alle fragt: Aegean,
British Airways und Eurowings takten sich selbst auf fuenfzehn Abrufe je
Minute. Als vertretbarer Dauerbetrieb gilt ein Zwanzigstel davon:

```
15 je Minute  x  60 Minuten  x  0,05  =  45 Abrufe je Quelle und Stunde
```

Das sind fuenfzehn Durchgaenge je Stunde und damit **fuenf heisse Strecken**.
Wer mehr heiss schaltet, bekommt sie nicht schneller, sondern langsamer: das
Budget **verschiebt** Strecken, die am laengsten wartende zuerst. Es lehnt
keine Eingabe ab, und es reisst die Grenze nicht.

Verrechnet wird gegen eine **rollende** Stunde (`hunt_call`). Eine
Kalenderstunde erlaubt das volle Budget um 11:59 und noch einmal um 12:00,
und genau diesen Doppelschlag sieht die Quelle.

Verbucht wird **vor** dem Abruf. Ein Abruf, der in einem Zeitablauf endet, hat
die Quelle genauso erreicht wie einer, der antwortet; wer erst nach der
Antwort verbucht, verbucht ausgerechnet die Abrufe nicht, die schiefgehen.

Daneben steht ein zweiter Deckel, `MAX_ROUTES_PER_RUN` = 5. Das Stundenbudget
sichert die **Summe** einer Stunde, nicht ihre Verteilung: bei zwanzig heissen
Strecken haette ein einziger Aufruf fuenfzehn Durchgaenge hintereinander
gefahren und danach eine Stunde geschwiegen. Salve, Pause, Salve ist genau das
Muster, das einer Quelle auffaellt.

### Die Sicherung

Geht bei einer Quelle der `CircuitBreaker` zu, faellt **die ganze Jagd** auf
den Tagestakt zurueck, bis die Abkuehlung um ist (`hunt_pause`, dieselben 1800
Sekunden wie im Breaker). Nicht nur diese Quelle: ein Block ist der Hinweis,
dass unser Fussabdruck auffaellt, und die anderen weiter dreimal die Stunde zu
fragen holt den naechsten. Ein Fehltarif, der waehrend einer halben Stunde
durchrutscht, ist billiger als eine Quelle, die auch der Suche fehlt.

Der Breaker lebt im Quellenobjekt und damit nur so lange wie ein Durchgang -
`build_sources` legt den Katalog je Lauf neu an. Sein Befund wandert deshalb
nach jedem Durchgang in `hunt_pause`, sonst faengt der naechste wieder bei
null an und lernt nichts.

Sichtbar in `/api/health/detail` unter `hunt.paused`. Die **Aufzeichnung**
haelt dabei nie an: die Strecken bleiben faellig und laufen ueber
`run_watchlist` im Tagestakt weiter.

### Der Cache

`price_cache` haelt eine Kalenderantwort vierundzwanzig Stunden. Ein Takt von
zwanzig Minuten wuerde damit gar nicht die Quelle fragen, sondern die eigene
Antwort von heute Morgen erneut lesen - der ganze Aufwand waere umsonst, und
das Budget wuerde etwas verbuchen, das nie stattgefunden hat.

Aufgeloest wird das, indem zwei Fragen getrennt werden, die vorher eine waren:

* **TTL** sagt, wie lange eine Antwort fuer *andere* gilt. Sie bleibt bei
  vierundzwanzig Stunden, denn fuer eine Suche ist der Kalender von heute
  Morgen weiterhin gut genug.
* **`max_age`** sagt, wie alt eine Antwort fuer *diesen* Aufrufer sein darf.
  Die Jagd verlangt hoechstens die **halbe Taktzeit**, also zehn Minuten.

Damit fragt ein heisser Durchgang immer wirklich die Quelle, auch wenn ihn die
Streuung frueher dran nimmt. Geschrieben wird weiter mit der normalen TTL, ein
Suchlauf zwischen zwei Durchgaengen bekommt die frische Antwort also geschenkt.

**Die Regel:** `max_age < Takt <= TTL`. Wer den Takt kuerzt, muss `max_age`
mitkuerzen, sonst laeuft die Jagd im Leerlauf.

---

## 2. Was gemeldet wird

Stufe `error` aus `docs/PRICE_HISTORY.md`, und nur die. `cheap` waere die
naheliegende zweite Wahl und ist die falsche: fuenf heisse Strecken erzeugen
jeden Tag Dutzende guenstiger Tage, und eine Meldung, die jeden Tag kommt,
liest niemand.

Je Strecke und Durchgang wird der **guenstigste nicht indikative** Preis eines
Reisetages beurteilt, hoechstens **drei** Tage werden gemeldet
(`MAX_FINDS_PER_ROUTE`). Eine Strecke mit einem systematischen Fehler traegt
ihn sonst ueber das ganze Fenster, und aus einer Ursache wuerden sechzig
Meldungen.

Welche Zeilen neu sind, entscheidet die **Zeilennummer** und nicht der
Zeitstempel: `SqliteHistory.record` schreibt `datetime.now()`, waehrend der
Durchgang mit einem uebergebenen `now` rechnet. `price_observation` ist
append-only, der Primaerschluessel steigt, also merkt sich der Durchgang den
hoechsten vor dem Abruf.

Gemeldet wird auch im **Tageslauf**. Ein Fehltarif faellt nicht nur auf
heissen Strecken vom Himmel, und die Zeilen liegen ohnehin geschrieben da.

---

## 3. Die Meldung

### Der Kanal

Ein **Discord-Webhook**: eine URL in `FLIGHTOPT_DISCORD_WEBHOOK`, kein
Bot-Konto, kein OAuth, keine Sitzung, die ablaufen kann. Der Name steht in
`deploy/portainer.env.example`. Kein Token liegt im Repo, und es wird auch
keiner erzeugt.

**Ohne URL laeuft alles trocken.** Der Fund wird erkannt, als `dry_run` in
`alert_event` geschrieben und protokolliert, was gesendet **wuerde**. Das ist
der vorgesehene erste Betriebszustand: so laesst sich beobachten, was das
System melden will, bevor es reden darf. Ein Detektor, den man erst nach dem
Einrichten eines Kanals beurteilen kann, wird nie beurteilt.

Discord bremst mit HTTP 429 und nennt in `retry_after` (Rumpf und Kopf), wie
lange. Gewartet wird, hoechstens dreissig Sekunden und hoechstens dreimal - bei
einer globalen Sperre darf der Durchgang nicht stehen bleiben. Ein 404
(geloeschter oder falsch abgetippter Webhook) wird nicht wiederholt: derselbe
Fehler beim zweiten Versuch ist derselbe Fehler.

**Ein Fehlschlag im Kanal bricht nie einen Durchgang ab.** Die Aufzeichnung
ist unwiederbringlich, die Meldung nicht.

### Entdopplung

Schluessel: **Strecke, Reisetag, Stufe**. Der Preis gehoert bewusst nicht
hinein - dieselbe Verbindung fuer 39,00 statt 39,50 Euro ist derselbe Fund.

Ruhezeit: **sechs Stunden**. Die Zahl kommt aus dem Takt. Bei zwanzig Minuten
Abstand meldete ein Fund, der einen Vormittag lang buchbar bleibt, ohne
Ruhezeit achtzehnmal, bevor die erste Stunde um ist. Sechs Stunden sind lang
genug, dass daraus eine Meldung wird, und kurz genug, dass ein Tarif, der am
Abend noch steht, noch einmal erinnert: hoechstens vier Meldungen je Strecke,
Tag und Stufe an einem Tag.

Ausnahme: faellt der Preis um mindestens **zwanzig Prozent** unter den zuletzt
gemeldeten, ist das ein neuer Fund. Von 39 auf 25 Euro will man wissen, von 39
auf 38,50 nicht.

Gezaehlt wird ab der letzten **gemeldeten** Zeile (`sent` oder `dry_run`),
nicht ab der letzten geschriebenen. Sonst schoebe jede unterdrueckte Meldung
die Ruhezeit vor sich her, und dieselbe Zeile waere nie wieder zu hoeren.

### Die Nachricht

Deutsch, fuenf Zeilen, immer dieselben fuenf. Keine Pfeile, keine Em- oder
En-Striche. Auf 2000 Zeichen gekuerzt, und die Kuerzung ist sichtbar.

```
Fehltarif BER-BKK am Fr, 20.11.2026
39,00 Euro (ryanair, Kalenderpreis)
Warum: unter 25 Prozent des Medians von 500,00 Euro (n=12)
Basis: Median 500,00 Euro aus 12 Vergleichspreisen (Kalenderpreis)
Strecke: 8622 km
Buchen: https://www.ryanair.com/gb/en/trip/flights/select?adults=1&...
```

Der Buchungslink zeigt bei Ryanair und Wizz auf die Tagesliste der Airline und
sonst auf eine Google-Flights-Suche. Weiter geht es nicht: ein Kalenderpreis
nennt keinen Flug, sondern den guenstigsten Preis eines Tages, und ein Link
auf einen bestimmten Flug waere geraten.

### Regeln

`alert_rule` sagt, wofuer gemeldet wird. Ohne eine **einzige** Zeile gilt die
eingebaute Vorgabe: jede Strecke, Stufe `error`. Eine Regel ist Feineinstellung
und keine Voraussetzung - waere sie eine, liefe ein frisch aufgesetzter Dienst
still und niemand wuesste warum. Sobald eine Regel eingetragen ist, gilt nur
noch, was dort steht; auch eine abgeschaltete Regel ist eine Meinung.

---

## 4. Endpunkte

| Endpunkt | Was er tut |
|---|---|
| `GET /api/hunt/finds?limit=&open_only=&tier=` | Funde und Kanalstand |
| `PATCH /api/hunt/finds/{id}` `{"acknowledged": bool}` | Fund abhaken oder aufmachen |
| `GET /api/hunt/history/{von}/{nach}?days=&by=&currency=` | Preisverlauf einer Strecke |
| `POST /api/hunt/run-once` | einen Durchgang jetzt |
| `PATCH /api/watchlist/{id}` `{"cadence": "hot"}` | Takt einer Strecke stellen |
| `GET /api/health/detail` | `hunt`-Block mit Takt, Budget, Sperren, Kanal |

`GET /api/hunt/finds` antwortet mit `{"finds": [...], "summary": {...}}`. Eine
Zeile in `finds`:

```json
{
  "id": 12, "created_at": "2026-09-09T12:00:00",
  "route": "BER-BKK", "entity_key": "BER|BKK", "travel_date": "2026-11-20",
  "tier": "error", "source": "ryanair", "currency": "EUR",
  "price": 39.0, "price_minor": 3900, "median": 500.0, "n": 12,
  "population": "estimate", "reason": "unter 25 Prozent des Medians ...",
  "delivery": "dry_run", "delivered_at": null, "error": null,
  "acknowledged_at": null, "booking_url": "https://...",
  "distance_km": 8622.0, "thin": false
}
```

`summary` traegt `events`, `sent`, `dry_run`, `suppressed`, `failed`, `open`,
`last_find_at`, `quiet_hours` und `channel_configured`. Die letzte Zahl ist
die wichtigste: ohne sie sieht ein Trockenlauf genauso aus wie ein kaputter
Webhook.

`GET /api/hunt/history/{von}/{nach}` nimmt Ortsnamen wie das Suchformular und
antwortet mit `route`, `entity_key`, `by`, `days`, `currency`, `points`,
`cadence`, `hot`, `stats` und `finds`. Ein Punkt:

```json
{"day": "2026-09-09", "min": 90.0, "median": 105.0, "max": 120.0, "n": 2}
```

Betraege in ganzen Waehrungseinheiten, weil eine Kurve sie so zeichnet.
`by=observed` (Vorgabe) gruppiert nach Beobachtungstag und beantwortet "wird
diese Strecke gerade teurer", `by=travel` nach Reisetag und beantwortet "wann
sollte ich fliegen". Richtwerte bleiben in beiden Faellen draussen, sonst
springt die Linie zwischen Direkttarif und Ein-Stopp-Verbindung hin und her.
`cadence` und `stats` sind `null`, wenn die Strecke gar nicht beobachtet wird;
eine Historie hat sie trotzdem, denn auch gewoehnliche Suchen schreiben.

`hunt` in `/api/health/detail` traegt `interval_seconds`,
`effective_interval_seconds`, `jitter_seconds`, `calls_per_pass`,
`cap_per_source_hour`, `max_hot_routes`, `max_cache_age_seconds`,
`calendar_ttl_seconds`, `hot_routes`, `hot_due`, `budget` (je Quelle `used`,
`remaining`, `cap`), `paused`, `alerts` und `channel`.

`effective_interval_seconds` ist die Zahl, die zaehlt: die Jagd laeuft nur so
oft, wie der Tagesplaner sie aufruft. Steht `FLIGHTOPT_SCAN_INTERVAL_SECONDS`
hoeher als 1200, ist der heisse Takt eine Absichtserklaerung und keine
Tatsache.

---

## 5. Fallstricke

- **Ein Takt kuerzer als `max_age` fragt sich selbst.** Wer an
  `HOT_INTERVAL_SECONDS` dreht, dreht an `MAX_CACHE_AGE` mit.
- **Ein Fenster ueber 62 Tage darf nicht heiss laufen.** Es beruehrt vier
  Kalendermonate und kostet damit vier Abrufe statt der drei, die das Budget
  verbucht. `set_cadence` lehnt es ab, und `add_route` schaltet eine heisse
  Strecke auf den Tagestakt zurueck, wenn jemand ihr Fenster aufzieht.
- **Eine neue Kalenderquelle mit teurerem Endpunkt kippt die Rechnung.**
  `tests/test_hunt_cadence.py` faellt dann aus, und das ist der Zweck: entweder
  steigt `CALLS_PER_PASS` oder die Quelle lernt groessere Fenster.
- **Eine langsamere Quelle kippt die Obergrenze.** Derselbe Test prueft, dass
  `SLOWEST_SOURCE_PER_MINUTE` noch stimmt.
- **Ein heisser Durchgang zaehlt als Tageslauf.** Er setzt beide Zeitstempel.
  Wer das trennt, laesst eine heisse Strecke ihren Tageslauf ein zweites Mal
  bezahlen.
- **`alert_event` ist die Chronik und nicht das Versandprotokoll.** Auch
  unterdrueckte und gescheiterte Funde stehen darin. Wer nach "was ging
  hinaus" fragt, filtert auf `delivery = 'sent'`.
