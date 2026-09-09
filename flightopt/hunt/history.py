"""Der Preisverlauf einer Strecke, verdichtet auf eine Kurve.

`price_observation` ist append-only und fein: sechzig Reisetage mal zehn
Quellen mal einen Abruf am Tag sind sechshundert Zeilen taeglich je Strecke.
Eine Kurve daraus zeichnet niemand. Verdichtet wird deshalb hier und nicht in
der Oberflaeche - was gezeigt wird, soll dieselbe Antwort sein, egal wer
fragt.

Zwei Blickrichtungen, weil es zwei Fragen sind und beide berechtigt:

* `by="observed"` gruppiert nach **Beobachtungstag**. Das beantwortet "wird
  diese Strecke gerade teurer oder billiger" und ist die Kurve, die ein
  Beobachter sehen will.
* `by="travel"` gruppiert nach **Reisetag**. Das beantwortet "wann sollte ich
  fliegen" und ist der uebliche Preiskalender.

Richtwert-Zeilen (`is_indicative = 1`) bleiben in beiden Faellen draussen,
und zwar aus demselben Grund, aus dem sie nicht in die Baseline duerfen: sie
preisen ein anderes Produkt. Eine Linie, die zwischen Direkttarif und
Ein-Stopp-Verbindung hin und her springt, zeigt nicht den Preis der Strecke,
sondern welche Quelle an diesem Tag geantwortet hat.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from statistics import median
from typing import Any

BY_OBSERVED = "observed"
BY_TRAVEL = "travel"
GROUPINGS: tuple[str, ...] = (BY_OBSERVED, BY_TRAVEL)

DEFAULT_DAYS = 30
MAX_DAYS = 365


def route_series(conn: sqlite3.Connection, entity_key: str, *,
                 days: int = DEFAULT_DAYS, by: str = BY_OBSERVED,
                 currency: str = "EUR",
                 now: datetime | None = None) -> list[dict[str, Any]]:
    """Je Gruppierungstag ein Punkt: guenstigster, mittlerer, teuerster Preis.

    Der Median steht neben dem Minimum, weil beide etwas anderes sagen. Das
    Minimum ist das Angebot, der Median ist die Lage. Eine Kurve nur aus
    Minima sieht jeden Ausreisser als Trend.

    `days` begrenzt in **beiden** Blickrichtungen den Beobachtungszeitraum und
    nie den Reisezeitraum. Bei `by="travel"` heisst das: "was kostet jeder
    Reisetag nach dem, was wir in den letzten `days` Tagen gesehen haben".
    Andersherum waere es ein Preiskalender aus Beobachtungen beliebigen
    Alters, und der zeigte dann Preise von vor drei Monaten als heutige.
    """
    if by not in GROUPINGS:
        raise ValueError(
            f"Unbekannte Gruppierung: {by!r}. Erlaubt sind {', '.join(GROUPINGS)}."
        )
    window = max(1, min(int(days), MAX_DAYS))
    moment = now or datetime.now()
    since = (moment - timedelta(days=window)).date().isoformat()
    column = "observed_at" if by == BY_OBSERVED else "travel_date"

    grouped: dict[str, list[int]] = {}
    for row in conn.execute(
        f"SELECT substr({column}, 1, 10) AS day, price_total_minor AS price "
        f"FROM price_observation WHERE entity_type='flight' AND entity_key=? "
        f"AND currency=? AND is_indicative=0 AND substr(observed_at, 1, 10) >= ? "
        f"ORDER BY day",
        (entity_key, currency, since),
    ):
        grouped.setdefault(str(row["day"]), []).append(int(row["price"]))

    points: list[dict[str, Any]] = []
    for day in sorted(grouped):
        prices = grouped[day]
        points.append(
            {
                "day": day,
                "min": min(prices) / 100,
                "median": int(median(prices)) / 100,
                "max": max(prices) / 100,
                "n": len(prices),
            }
        )
    return points
