# Preishistorie

Stand 2026-09-08. Wer an Baselines, Preislage oder Preisfehlern arbeitet,
schaut zuerst hier nach. Der Code liegt in `flightopt/storage/cache.py`
(Schreiben), `flightopt/storage/baseline.py` (Rechnen) und
`flightopt/hotels/signals.py` (Stufen).

Die Kurzfassung: eine einzige Beobachtungstabelle fuer Fluege und Hotels, drei
abgeleitete Baseline-Tabellen darueber, und die feste Regel, dass ein Preis nur
gegen Preise derselben Art gehalten wird. Fehlt die passende Vergleichsgruppe,
kommt `unknown` heraus und keine Zahl.

---

## 1. `price_observation`: was hineingeschrieben wird

Die Tabelle wird nur angefuegt, nie ueberschrieben und nie geloescht. Sie liegt
im Schema in `flightopt/storage/db.py`.

| Spalte | Bedeutung |
|---|---|
| `observed_at` | Zeitpunkt der Beobachtung, `isoformat(timespec="seconds")` |
| `source` | Name des Adapters, der den Preis geliefert hat |
| `entity_type` | `flight` oder `hotel` |
| `entity_key` | siehe Abschnitt 2 |
| `travel_date` | Reisetag, bei Hotels der Anreisetag |
| `return_or_nights` | bei Hotels die Naechte, bei Fluegen leer |
| `party_size` | Belegung, bei Fluegen die Zahl der Reisenden |
| `currency` | Waehrung des gespeicherten Betrags |
| `price_total_minor` | Preis in Cent, nie als Kommazahl |
| `is_estimate` | 1 = Kalender- oder Richtwertpreis, 0 = live geprueft |
| `is_indicative` | 1 = die Quelle liefert grundsaetzlich nur Richtwerte |
| `raw_hash` | optionaler Abzug der Rohantwort |

Index: `ix_obs_entity` ueber `(entity_type, entity_key, travel_date, observed_at)`.

Geschrieben wird ausschliesslich ueber `SqliteHistory.record()` in
`flightopt/storage/cache.py`. Es gibt genau drei Aufrufer:

| Aufrufer | Datei | `is_estimate` | `is_indicative` |
|---|---|---|---|
| Kalenderlauf `build_grid` | `flightopt/search/grid.py` | 1 | aus dem Quellenkatalog |
| Live-Pruefung `verify` | `flightopt/search/verify.py` | 0 | aus der Quelle des Siegerangebots |
| Hotellauf `record_offers` | `flightopt/hotels/store.py` | Quellenflag der Hotelquelle | bleibt 0 |

Dass `verify` mitschreibt, ist neu seit dem 2026-09-08. Ohne das waere die
gepruefte Grundgesamtheit dauerhaft leer geblieben und ein gepruefter Preis
haette nie eine Vergleichsgruppe seiner eigenen Art gehabt.

Hotelpreise werden immer in Euro abgelegt; findet sich kein Kurs, faellt die
Zeile weg, statt eine fremde Waehrung als Euro zu speichern.

---

## 2. `entity_key`

**Fluege: zweiteilig, `ORIGIN|DESTINATION`.** Ohne Quelle. Die Baseline soll
sagen, was diese Strecke ueblich kostet, nicht was sie bei einer Quelle kostet;
die Quelle steht ohnehin in `source`. Gebildet in `search/grid.py` und
`search/verify.py`, gelesen ueber `leg_entity_key` in `jobs/runner.py` und in
`jobs/daily.py`.

**Hotels: `<cc>|<property_key>`**, gebildet in `flightopt/hotels/models.py`.
`cc` ist der Laendercode aus einer Namenstabelle, Rueckfall `XX`.
`property_key` ist `<quelle>:<id>`, also zum Beispiel
`trivago:1d6fec31a3cf`. Damit rechnet die Baseline je Objekt und nicht je Stadt.
Zerlegt wird ueber `split_entity_key` in `storage/baseline.py`.

Wer den Schluessel anfasst, aendert die Bedeutung jeder bestehenden Zeile. Genau
das war der Fehler, den `scripts/migrate_entity_keys.py` aufraeumt (Abschnitt 7).

---

## 3. `is_indicative` und `is_estimate`

Zwei Kennzeichen, zwei verschiedene Fragen. Beide stehen an der Zeile und nicht
an der Auswertung, weil spaeter niemand mehr weiss, wie eine Quelle damals
eingestuft war.

**`is_indicative`: taugt diese Quelle ueberhaupt als Vergleichsmass?**
Das Kennzeichen entsteht beim Schreiben aus dem Quellenkatalog, aus dem Attribut
`indicative` am Adapter (`flightopt/sources/base.py`, Sammelabfrage
`indicative_source_names` in `flightopt/sources/registry.py`). Bei den Fluegen
traegt es heute genau eine Quelle: **Kiwi**. Solche Zeilen fallen schon in der
Abfrage heraus und beruehren weder Median noch Streuung noch `n`.

Der Grund ist nicht nur der dokumentierte Aufschlag von rund 13 Prozent
gegenueber demselben Flug. Kiwi preist ein anderes Produkt: jede Airline, bis zu
einem Umstieg. **Kontraintuitiv, aber gemessen: Kiwi zog die Mediane nicht nach
oben, sondern nach unten**, weil Ein-Stopp-Verbindungen fremder Airlines unter
jedem Direkttarif liegen. Die alte gemischte Baseline lag also zu tief und liess
echte Tarife zu teuer aussehen. Wer sich das falsch herum merkt, baut den Fehler
wieder ein.

Hotelzeilen bleiben bei `is_indicative = 0`. Ihre Naeherung steckt in
`is_estimate`; ein Kennzeichen hier wuerde ihnen jede Vergleichsgruppe nehmen,
weil beide Hotelquellen Richtwerte liefern.

**`is_estimate`: welcher Grundgesamtheit gehoert die Zeile an?**
Der Kalender nennt den Tagesbestpreis irgendeines Flugs, die Live-Pruefung den
Preis eines bestimmten, mit anderen Gepaeck- und Zuschlagsanteilen. Das sind
zwei Verteilungen, keine eine. Deshalb steht `is_estimate` im Schluessel von
`flight_baseline`, und ein Leg wird gegen die Baseline seiner eigenen Art
gehalten. Fehlt sie, bleibt es bei `unknown`.

---

## 4. Die drei Baseline-Tabellen

Alle drei sind abgeleitet und jederzeit neu berechenbar. Sie halten Median, MAD,
`n` und `computed_at` und werden per Upsert geschrieben.

| Tabelle | Schluessel | Wofuer |
|---|---|---|
| `flight_baseline` | `entity_key, weekday, leadtime_bucket, currency, is_estimate` | die einzige Quelle fuer Flug-Preisaussagen |
| `hotel_baseline` | `scope, group_key, weekday, leadtime_bucket, stay_key, currency` | Hotels, auf zwei Ebenen: Eigenhistorie und Peer-Gruppe |
| `price_baseline` | `entity_type, entity_key, weekday, leadtime_bucket, currency` | Altbestand, traegt nur noch Hotelzeilen und wird von nichts gelesen |

**`flight_baseline`** liegt im Schema in `storage/db.py`, geschrieben von
`refresh_flight_baselines`, gelesen von `_flight_signal`. Eigene Tabelle statt
einer Spalte in `price_baseline`, weil SQLite einen zusammengesetzten
Primaerschluessel nicht per `ALTER TABLE` erweitert und eine bestehende Datei
unter einem laufenden Prozess nicht die Form wechseln soll.

**`hotel_baseline`** liegt als `HOTEL_BASELINE_SCHEMA` in `storage/baseline.py`
und wird beim ersten Zugriff angelegt. `scope` ist `own` oder `peer`;
`group_key` ist bei `own` der `entity_key`, bei `peer`
`<cc>|<stadt>|<sterne>`. `stay_key` ist `p<belegung>n<naechte>`, sonst laege ein
Familienzimmer fuer drei Naechte neben einem Einzelzimmer fuer eine.

**`price_baseline`** ist der Altbestand. Seit dem 2026-09-08 stehen die
Flugzeilen nicht mehr darin; geschrieben wird sie nur noch auf dem Hotelpfad,
und kein Lesepfad im Produktivcode fasst sie an. Wer sie erweitert, erweitert
eine Zahl, die niemand liest.

---

## 5. Wie eine Baseline entsteht

Gerechnet wird mit **Median und MAD**, nie mit dem Mittelwert und nie mit MAD
als Nenner: ein Modified-Z-Score waere bei `mad == 0` undefiniert.

Gruppiert wird nach:

- **Fluege:** Strecke (`entity_key`), Wochentag des Reisetags, Vorlauf-Fenster,
  Waehrung, `is_estimate`. Zeilen mit `is_indicative = 1` fallen vorher heraus.
- **Hotels, `own`:** Objekt, Wochentag, Vorlauf-Fenster, `stay_key`, Waehrung.
- **Hotels, `peer`:** Laendercode, Stadt und Sternekategorie statt des Objekts.
  Namen, die nach Schlafsaal, Tageszimmer, Campingplatz oder Boot aussehen,
  gehen gar nicht erst in die Peer-Verteilung.

Die Vorlauf-Fenster (`leadtime_bucket` in `storage/baseline.py`), gerechnet als
Reisetag minus Beobachtungstag:

```
0-6   7-13   14-29   30-59   60-119   120+
```

**Mindestzahl: fuenf Beobachtungen je Gruppe.** Das ist der Standardwert
`min_samples = 5` an allen drei `refresh_*`-Funktionen; kein Aufrufer setzt ihn
anders. Unter fuenf entsteht schlicht keine Zeile.

Neu gerechnet wird einmal je Suchlauf, bevor die erste Beobachtung dieses Laufs
geschrieben wird (`jobs/runner.py`), bei Hotels zusaetzlich alle zehn
Fortschrittsschritte (`BASELINE_EVERY` in `jobs/hotel_runner.py`).

---

## 6. Wie die Stufen zustande kommen

Einstieg ist `detect_price_signal(conn, entity_key, travel_date, price_minor, ...)`
in `storage/baseline.py`. Alles ausser `entity_type="hotel"` geht auf den
Flugpfad.

### Fluege: drei Stufen plus `unknown`

`band_status` in `flightopt/hotels/signals.py`:

```
band = max(BAND_FLOOR_MINOR, mad_minor * 3)      # BAND_FLOOR_MINOR = 1500, also 15 Euro
preis <= median - band   ->  cheap
preis >= median + band   ->  expensive
sonst                    ->  normal
keine Baseline           ->  unknown
```

Die Schranke ist **absolut in Cent**, keine Prozentabweichung. Der
zurueckgegebene Eintrag traegt `status`, `tier`, `reason`, `basis`, `n`,
`population` und `thin`; bei Fluegen sind `status` und `tier` immer gleich.
`population` ist `estimate` oder `verified` und sagt, gegen welche
Grundgesamtheit gemessen wurde. Eine vierte Stufe kennt der Flugpfad nicht.

Ohne passende Baseline kommt `{"tier": "unknown", "reason": "keine Baseline",
"n": 0}` zurueck, ohne Median und ohne Abweichung. Es wird nichts ersatzweise
gerechnet.

In der Ergebnistabelle steht das als eigene Spalte **Preislage** neben dem
Status: `guenstig`, `normal`, `teuer` oder `keine Basis`. Der Status sagt, wie
sicher ein Preis ist (`geprueft`, `Richtwert`, `Schaetzung`), die Preislage sagt,
ob er gut ist. Zwei Felder, zwei Fragen, nie eins statt des anderen.

### Hotels: vier Stufen plus `encoding_suspect` und `unknown`

`classify` in `flightopt/hotels/signals.py` behaelt den Drei-Stufen-Wert in
`status` und setzt zusaetzlich `tier`. Die vierte Stufe `error` greift, wenn
mindestens eines zutrifft:

1. `preis <= median - 6 * mad` bei `n >= 10` (`MAD_FACTOR`, `MAD_MIN_N`).
2. `preis <= 0,30 * median` bei `n >= 5` (`RATIO`, `RATIO_MIN_N`). Das ist die
   einzige prozentuale Schranke im ganzen System.
3. Preis pro Nacht unter der Plausibilitaetsschranke der Sternekategorie:
   15 / 20 / 30 / 45 / 70 Euro fuer 1 bis 5 Sterne, 15 Euro ohne Sterneangabe
   (`DEFAULT_LIMITS`).

Vorgeschaltet: verglichen wird nur bei gleicher Belegung und gleicher
Naechtezahl, und unter zehn eigenen Beobachtungen (`THIN_HISTORY_N`) rechnet die
Peer-Baseline statt der Eigenhistorie. `basis` sagt, welche es war: `own`,
`peer` oder `none`.

Zuletzt kommt `encoding_suspect` als eigene Stufe. Sie ueberstimmt `error` und
`expensive`, wenn `preis / median` nahe 0,01 oder 100 liegt oder in dem Band, in
dem die gefaehrlichen Tageskurse liegen (0,85 bis 1,7 fuer USD, CHF, GBP). Das
ist dann ein Dezimal- oder Waehrungsfehler in den Daten und kein Preisfehler im
Angebot. Eigene Stufe statt stillem Aussortieren, damit man sieht, dass etwas
fehlt.

---

## 7. Reparatur des Bestands

Zwei Skripte, in dieser Reihenfolge. Beide sind idempotent, beide nehmen `--db`
und arbeiten sonst auf `data/flightopt.db`.

**`scripts/migrate_entity_keys.py`** kuerzt dreiteilige Flugschluessel
`ORIGIN|DEST|quelle` auf `ORIGIN|DEST`. Ein zweiter Lauf findet nichts mehr, weil
die Abfrage auf `LIKE '%|%|%'` danach nicht mehr trifft.

**`scripts/migrate_baseline_quality.py`** traegt die Spalte `is_indicative` nach
(ueber `ADDED_COLUMNS` in `storage/db.py`, mit `PRAGMA table_info` abgesichert),
kennzeichnet Bestandszeilen indikativer Quellen aus dem Katalog, rechnet
`flight_baseline` von Grund auf neu und raeumt die alten Flugzeilen aus
`price_baseline`. Ein zweiter Lauf kennzeichnet nichts mehr, loescht nichts mehr
und rechnet dieselben Baselines noch einmal. Das Vorher-Bild liest es auf einer
eigenen Nur-Lese-Verbindung, damit die DDL-Nebenwirkungen von `db.connect` es
nicht faerben.

---

## 8. Ab wann eine Aussage traegt

- **Unter fuenf Beobachtungen je Gruppe gibt es keine Baseline**, also `unknown`
  und in der Oberflaeche `keine Basis`. Eine Gruppe ist Strecke mal Wochentag
  mal Vorlauf-Fenster mal Waehrung mal Grundgesamtheit; das ist feiner, als es
  auf den ersten Blick aussieht.
- **Gepruefte Legs bleiben vorerst ohne Basis.** Die Grundgesamtheit
  `is_estimate = 0` entsteht erst, seit `verify` mitschreibt. Bis dort fuenf
  Live-Preise je Strecke, Wochentag und Vorlauf-Fenster zusammengekommen sind,
  steht bei geprueften Legs `keine Basis`. Das ist richtig so: die Alternative
  waere, sie gegen Kalenderschaetzungen zu messen, und genau das war der Fehler.
- **Unter zehn Punkten ist die Basis duenn.** Die Oberflaeche nennt Zahl und Art
  der Vergleichspreise und schreibt "duenne Basis" dazu (`thin`). Bei Hotels
  schaltet dieselbe Schwelle auf die Peer-Baseline um.
- **Die ersten Wochen sind schwach.** Preisfehler-Erkennung und Preislage werden
  erst mit taeglichen Beobachtungen belastbar. Bis dahin tragen bei Hotels nur
  Peer-Baseline und Plausibilitaetsschranke, bei Fluegen nur die
  Kalender-Grundgesamtheit.

---

## 9. Fallstricke

- Schluessel schreiben und Schluessel lesen muessen dieselbe Form haben. Der
  Fehler von 2026-09-08 war unsichtbar: keine Ausnahme, kein Log, nur eine leere
  Baseline-Tabelle und ueberall `unknown`.
- Eine neue Quelle mit Richtwertpreisen braucht `indicative = True` am Adapter,
  sonst wandert sie stumm in die Baseline und verzieht sie.
- Wer eine Spalte in den Schluessel einer Baseline-Tabelle aufnimmt, legt eine
  neue Tabelle an, statt `ALTER TABLE` zu versuchen.
- `price_baseline` sieht nach dem Haupttisch aus und ist keiner. Fuer Fluege ist
  `flight_baseline` zustaendig, fuer Hotels `hotel_baseline`.
