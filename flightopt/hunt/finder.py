"""Aus frisch geschriebenen Beobachtungen Funde machen.

Zwischen dem Abruf und der Meldung steht genau dieser Schritt, und er hat
zwei Aufgaben, die beide leicht falsch zu machen sind.

**Welche Zeilen sind neu?** Nicht die mit einem Zeitstempel nach dem Start des
Durchgangs: `SqliteHistory.record` schreibt `datetime.now()`, waehrend der
Durchgang mit einem uebergebenen `now` rechnet, und in einem Test liegen die
beiden Jahre auseinander. Gefragt wird deshalb nach der Zeilennummer -
`price_observation` ist append-only und hat einen aufsteigenden
Primaerschluessel. Wer vor dem Abruf den hoechsten merkt, kennt danach genau
die neuen Zeilen, unabhaengig von jeder Uhr.

**Welcher Preis eines Tages zaehlt?** Der guenstigste **nicht indikative**.
Ein Tag hat so viele Zeilen wie Quellen, die geantwortet haben. Ueber alle zu
melden hiesse, denselben Tag mehrfach zu melden; den guenstigsten ueber alle
zu nehmen hiesse, einen Richtwert gegen Tarife antreten zu lassen - und Kiwi
findet Ein-Stopp-Verbindungen unter jedem Direkttarif, waere also fast immer
der Sieger und fast nie ein Fund.

Dazu eine Obergrenze je Strecke und Durchgang. Eine Strecke mit einem
systematischen Fehler traegt ihn ueber das ganze Fenster; ohne Deckel waeren
das sechzig Meldungen aus einer Ursache. Gemeldet werden die
`MAX_FINDS_PER_ROUTE` guenstigsten, der Rest steht im Log und in der
Historie - gefunden ist er ja, nur nicht ausgesprochen.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from flightopt.hunt import alerts
from flightopt.hunt.alerts import Find
from flightopt.storage.baseline import FLIGHT, detect_price_signal

logger = logging.getLogger(__name__)

MAX_FINDS_PER_ROUTE = 3
"""Wie viele Tage einer Strecke ein Durchgang hoechstens meldet.

Drei, weil ein echter Fehltarif meistens ein einzelner Tag ist und ein
Nachbartag dazugehoeren kann. Wer vier Meldungen aus einer Strecke bekommt,
liest sie nicht mehr als vier Funde, sondern als Stoerung.
"""

ERROR = "error"


def last_observation_id(conn: sqlite3.Connection) -> int:
    """Die hoechste vergebene Zeilennummer. Null bei leerer Tabelle."""
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS last FROM price_observation").fetchone()
    return int(row["last"] or 0)


def _cheapest_per_day(conn: sqlite3.Connection, entity_key: str,
                      since_id: int) -> dict[date, sqlite3.Row]:
    """Je Reisetag die guenstigste neue Zeile aus einer nicht indikativen Quelle."""
    best: dict[date, sqlite3.Row] = {}
    for row in conn.execute(
        "SELECT id, observed_at, source, travel_date, currency, price_total_minor, "
        "party_size, is_estimate FROM price_observation "
        "WHERE id > ? AND entity_type=? AND entity_key=? AND is_indicative=0",
        (since_id, FLIGHT, entity_key),
    ):
        try:
            day = date.fromisoformat(str(row["travel_date"]))
        except ValueError:
            continue
        current = best.get(day)
        if current is None or int(row["price_total_minor"]) < int(
            current["price_total_minor"]
        ):
            best[day] = row
    return best


def find_error_fares(conn: sqlite3.Connection, entity_key: str, *,
                     since_id: int,
                     limit: int = MAX_FINDS_PER_ROUTE) -> list[Find]:
    """Die Fehltarife unter den neuen Beobachtungen einer Strecke.

    Gerechnet wird gegen die Baselines, wie sie **jetzt** stehen. Der Aufrufer
    laesst sie vorher nachrechnen; ohne das bliebe die Preislage leer, obwohl
    die Zeilen da sind.

    Der eben geschriebene Preis steckt dann selbst in seiner Baseline. Das ist
    hingenommen und kein Versehen: bei den zehn Vergleichspreisen, die die
    statistischen Bedingungen ohnehin verlangen, verschiebt ein einzelner
    Punkt den Median um hoechstens einen halben Schritt. Ihn auszunehmen hiesse,
    zwei verschiedene Baselines zu fuehren - eine zum Melden und eine zum
    Anzeigen -, und die beiden liefen auseinander.
    """
    found: list[Find] = []
    for day, row in _cheapest_per_day(conn, entity_key, since_id).items():
        price_minor = int(row["price_total_minor"])
        try:
            observed_at = datetime.fromisoformat(str(row["observed_at"]))
        except ValueError:
            observed_at = datetime.now()
        signal = detect_price_signal(
            conn,
            entity_key,
            day,
            price_minor,
            observed_at=observed_at,
            currency=str(row["currency"]),
            party_size=int(row["party_size"] or 1),
            is_estimate=bool(row["is_estimate"]),
            is_indicative=False,
        )
        if signal.get("tier") != ERROR:
            continue
        found.append(
            Find(
                entity_key=entity_key,
                travel_date=day,
                tier=ERROR,
                price_minor=price_minor,
                currency=str(row["currency"]),
                source=str(row["source"]),
                median_minor=signal.get("median_minor"),
                n=int(signal.get("n") or 0),
                population=str(signal.get("population") or "estimate"),
                reason=str(signal.get("reason") or ""),
                distance_km=signal.get("distance_km"),
                thin=bool(signal.get("thin")),
                detail={"observation_id": int(row["id"])},
            )
        )

    found.sort(key=lambda find: find.price_minor)
    if len(found) > limit:
        logger.info(
            "%s: %d Fehltarife gefunden, gemeldet werden die %d guenstigsten",
            entity_key, len(found), limit,
        )
    return found[:limit]


async def report_finds(conn: sqlite3.Connection, finds: list[Find], *,
                       env: Mapping[str, str] | None = None,
                       post: Any = None,
                       now: datetime | None = None) -> list[int]:
    """Funde melden. Ein Fehlschlag im Kanal bricht nichts ab.

    Zurueck kommen die Kennungen der geschriebenen Ereignisse, auch die der
    unterdrueckten und der gescheiterten: `alert_event` ist die Chronik der
    Jagd und nicht das Versandprotokoll.
    """
    written: list[int] = []
    for find in finds:
        try:
            event_id = await alerts.deliver(conn, find, env=env, post=post, now=now)
        except Exception:  # noqa: BLE001 - die Aufzeichnung ist wichtiger
            logger.exception("Meldung fuer %s gescheitert", find.route)
            continue
        if event_id is not None:
            written.append(event_id)
    return written
