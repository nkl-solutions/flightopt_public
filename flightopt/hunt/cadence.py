"""Wie oft eine heisse Strecke gefragt werden darf, und woraus das folgt.

Ein Fehltarif lebt Minuten bis Stunden. Einmal am Tag findet ihn nie. Der
Takt der Jagd ist deshalb ein anderer als der der Beobachtung - und genau
das ist die gefaehrlichste Stelle des ganzen Werkzeugs, weil es dieselben
Quellen sind, die auch die Suche benutzt. Eine Sperre traefe beide.

Deshalb steht hier keine gerundete Zahl, sondern eine Ableitung.

## 1. Was ein Durchgang kostet (gemessen)

Ein Durchgang holt fuer eine Strecke den Preiskalender ueber das
Beobachtungsfenster von sechzig Tagen. Was das je Quelle an HTTP-Abrufen
kostet, haengt am Endpunkt und nicht an uns. Gemessen an einem Fenster, das
mitten im Monat beginnt und damit drei Kalendermonate beruehrt:

| Quelle          | Abrufe | Grund                                    |
|-----------------|--------|------------------------------------------|
| ryanair         | 3      | `cheapestPerDay` nimmt einen Monat       |
| aegean          | 3      | Endpunkt nimmt `YYYY-MM`                 |
| britishairways  | 3      | Endpunkt nimmt `month_year:(YYYYMM)`     |
| jetblue         | 3      | Endpunkt nimmt "NOVEMBER 2026"           |
| wizz            | 2      | Zeitraum, aber begrenzte Fensterlaenge   |
| condor          | 2      | Zeitraum in 31-Tage-Kacheln              |
| eurowings       | 1      | ein Abruf deckt fuenfzehn Monate         |
| icelandair      | 1      | Zeitraum bis zur eigenen Obergrenze      |
| airbaltic       | 1      | ein Abruf deckt ein Jahr                 |
| kiwi            | 1      | Chunk von neunzig Tagen                  |

Der schlechteste Fall ist also **drei Abrufe je Quelle und Durchgang**, und
verrechnet wird dieser und nicht der Einzelwert: wer zu wenig verbucht,
schuetzt nichts. `tests/test_hunt_cadence.py` haelt die Messung fest.

## 2. Woraus der Takt folgt

Der Vergleichsmassstab ist etwas, das ohnehin passiert und das keine Quelle
je beanstandet hat: **eine gewoehnliche Suche**. Eine Suche ueber drei
Teilstrecken und dasselbe Sechzig-Tage-Fenster kostet je Quelle drei mal
drei, also neun Abrufe.

Der Standardtakt ist deshalb so gewaehlt, dass **eine heisse Strecke je
Quelle und Stunde genau so viel kostet wie eine einzige solche Suche**:

    3 Abrufe je Durchgang  x  3 Durchgaenge je Stunde  =  9 Abrufe je Stunde

Drei Durchgaenge je Stunde sind **zwanzig Minuten** Abstand. Das ist kein
runder Wunschwert, sondern das, was der Vergleich hergibt.

## 3. Die harte Obergrenze

Der Takt allein haelt nichts: zwanzig heiss geschaltete Strecken waeren
zwanzig mal neun Abrufe. Darueber liegt deshalb eine Grenze je Quelle und
Stunde, und sie haengt an der **langsamsten** Kalenderquelle, weil ein
Durchgang immer alle fragt. Aegean, British Airways und Eurowings takten
sich selbst auf fuenfzehn Abrufe je Minute. Eine ganze Stunde in diesem Takt
waeren neunhundert Abrufe - eine Zahl, die kein Mensch am Rechner erzeugt.

Als vertretbarer Dauerbetrieb gilt hier **ein Zwanzigstel davon**, also drei
Minuten Eigen-Takt der Quelle, verteilt ueber eine Stunde:

    15 je Minute  x  60 Minuten  x  0,05  =  45 Abrufe je Quelle und Stunde

Bei drei Abrufen je Durchgang sind das fuenfzehn Durchgaenge je Stunde, und
bei drei Durchgaengen je Strecke und Stunde damit **fuenf heisse Strecken**.
Wer eine sechste heiss schaltet, bekommt sie nicht schneller, sondern nur
spaeter: das Budget verschiebt Strecken, es reisst die Grenze nicht.

## 4. Der Cache

`price_cache` haelt eine Kalenderantwort vierundzwanzig Stunden. Ein Takt von
zwanzig Minuten wuerde damit gar nicht die Quelle fragen, sondern die eigene
Antwort von heute Morgen erneut lesen: der ganze Aufwand waere umsonst und
das Budget wuerde etwas verbuchen, das nie stattgefunden hat.

Die Aufloesung trennt zwei Fragen, die vorher eine waren:

* **TTL** sagt, wie lange eine Antwort fuer *andere* gilt. Sie bleibt bei
  vierundzwanzig Stunden, denn fuer eine Suche ist der Kalender von heute
  Morgen weiterhin gut genug.
* **`max_age`** sagt, wie alt eine Antwort fuer *diesen* Aufrufer sein darf.
  Die Jagd verlangt hoechstens `MAX_CACHE_AGE`.

`MAX_CACHE_AGE` ist die halbe Taktzeit. Damit fragt ein heisser Durchgang
immer wirklich die Quelle, auch wenn ihn die Streuung ein paar Minuten zu
frueh dran nimmt - und ein Suchlauf, der zwischen zwei Durchgaengen kommt,
bekommt trotzdem die frische Antwort geschenkt, statt sie noch einmal zu
holen.
"""

from __future__ import annotations

from datetime import timedelta

DAILY = "daily"
HOT = "hot"
CADENCES: tuple[str, ...] = (DAILY, HOT)
"""Mehr als zwei Takte gibt es nicht. Ein Zwischenwert waere eine Zahl, die
niemand mehr gegen eine Obergrenze halten kann."""

WATCH_WINDOW_DAYS = 60
"""Das Fenster, auf dem die Messung beruht (`watchlist.DEFAULT_LEAD_*`).

Steht hier und nicht als Import, weil es hier eine andere Rolle spielt: dort
ist es eine Vorgabe, die ein Nutzer aendern darf, hier die Grundlage einer
Kostenrechnung. Ein groesseres Fenster kostet mehr Abrufe je Durchgang, und
dann stimmt `CALLS_PER_PASS` nicht mehr - deshalb begrenzt
`MAX_HOT_WINDOW_DAYS` das Fenster einer heissen Strecke.
"""

MAX_HOT_WINDOW_DAYS = 62
"""Groesstes Fenster, das eine heisse Strecke haben darf.

Zweiundsechzig statt sechzig, weil ein Fenster von sechzig Tagen im
schlechtesten Fall drei Kalendermonate beruehrt und ein Fenster von
zweiundsechzig Tagen auch. Ab dem dreiundsechzigsten Tag kann es vier
werden, und dann kostet ein Durchgang vier Abrufe statt dreien - also mehr,
als das Budget verbucht.
"""

CALLS_PER_PASS = 3
"""Gemessener schlechtester Fall: HTTP-Abrufe je Quelle und Durchgang.

Verrechnet wird dieser Wert fuer jede Quelle, auch fuer die, die mit einem
Abruf auskommt. Zu teuer verbuchen kostet Takt, zu billig verbuchen kostet
die Quelle.
"""

SEARCH_CALLS_PER_SOURCE = 9
"""Was eine gewoehnliche Suche je Quelle kostet: drei Teilstrecken, drei
Abrufe je Teilstrecke. Der Massstab, an dem der Takt haengt."""

HOT_INTERVAL_SECONDS = int(3600 * CALLS_PER_PASS / SEARCH_CALLS_PER_SOURCE)
"""Zwanzig Minuten. Siehe Abschnitt 2 des Modulkopfs."""

HOT_JITTER_SECONDS = 120
"""Streuung um den Takt herum, plus/minus.

Gleichmaessige Abstaende auf die Sekunde sind selbst ein Bot-Merkmal;
dieselbe Ueberlegung wie im `RateLimiter`. Zwei Minuten sind ein Zehntel der
Taktzeit: genug, damit kein Muster entsteht, zu wenig, um die Rechnung aus
Abschnitt 2 zu verschieben.
"""

SLOWEST_SOURCE_PER_MINUTE = 15
"""Die langsamste Kalenderquelle im Katalog (Aegean, British Airways,
Eurowings). Ein Test haelt fest, dass keine langsamere dazukommt, ohne dass
die Ableitung darunter angefasst wird."""

CAP_SHARE = 0.05
"""Welcher Anteil des Eigen-Takts einer Quelle im Dauerbetrieb vertretbar ist.

Ein Zwanzigstel: drei Minuten Volllast je Stunde. Die Zahl ist ein Urteil und
keine Messung - sie laesst sich nicht messen, ohne genau das zu riskieren,
was sie verhindern soll. Sie ist bewusst so klein gewaehlt, dass die Jagd im
Zweifel zu langsam ist statt zu schnell.
"""

MAX_CALLS_PER_SOURCE_HOUR = int(SLOWEST_SOURCE_PER_MINUTE * 60 * CAP_SHARE)
"""45. Die harte Grenze je Quelle und rollender Stunde."""


def passes_per_hour(interval_seconds: int = HOT_INTERVAL_SECONDS) -> int:
    """Wie oft eine heisse Strecke in einer Stunde drankommt."""
    return max(1, 3600 // max(1, interval_seconds))


MAX_HOT_ROUTES = MAX_CALLS_PER_SOURCE_HOUR // (CALLS_PER_PASS * passes_per_hour())
"""Fuenf. Mehr heisse Strecken passen nicht gleichzeitig unter die Grenze.

Es ist keine Fehlermeldung, mehr einzutragen: die ueberzaehligen laufen
langsamer, weil das Budget sie verschiebt. Eine Grenze, die eine Eingabe
ablehnt, waere haerter als noetig - und der Nutzer haette keine Moeglichkeit,
eine Strecke fuer eine Stunde vorzuziehen.
"""

MAX_CACHE_AGE = timedelta(seconds=HOT_INTERVAL_SECONDS // 2)
"""Zehn Minuten. Aelter darf eine Cache-Antwort fuer die Jagd nicht sein.

Kuerzer als der Takt, sonst liest ein Durchgang seine eigene letzte Antwort.
Siehe Abschnitt 4 des Modulkopfs.
"""
