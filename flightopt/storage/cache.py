"""TTL cache and the append-only price history, both on the same SQLite file.

The cache key deliberately includes every parameter that can change a price.
Two searches that share a leg-date share the fetch; that dedup is what keeps
request counts inside polite limits.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from flightopt.domain.models import Money
from flightopt.storage import db

# How long a price of each kind stays usable. Flight fares change a few times a
# day, so a calendar estimate is fine for a day while a live quote is not.
TTL_CALENDAR = timedelta(hours=24)
TTL_LEG = timedelta(hours=6)
TTL_VERIFY = timedelta(minutes=30)


def cache_key(
    source: str,
    kind: str,
    origin: str,
    destination: str,
    day: date,
    *,
    until: date | None = None,
    pax: int = 1,
    cabin: str = "economy",
    currency: str = "EUR",
    max_stops: int | None = None,
) -> str:
    """Every parameter that can change the answer, hashed into one key.

    `max_stops` is appended only when a source was actually asked for it: a
    calendar for one stop is a different calendar than one for two, but an
    airline that has no such knob would otherwise lose every cached row for a
    parameter it never saw.

    `until` ist das Fensterende und gehoert nur zum Kalender: es aendert nicht
    den Preis eines Tages, wohl aber den Umfang der Antwort. Ohne es traf eine
    Suche ueber zwei Monate den Eintrag einer Suche ueber zwei Wochen, sah eine
    nicht-leere Antwort und fragte gar nicht erst nach. Das Gitter bekam zwei
    Wochen, der Bericht meldete null Abrufe und null Fehler, und die duenne
    Abdeckung sah aus wie Angebotsmangel.
    """
    raw = f"{source}|{kind}|{origin}|{destination}|{day.isoformat()}|{pax}|{cabin}|{currency}"
    if until is not None:
        raw += f"|bis{until.isoformat()}"
    if max_stops is not None:
        raw += f"|stops{max_stops}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class SqliteCache:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    async def get(self, key: str, *, max_age: timedelta | None = None,
                  now: datetime | None = None) -> Any | None:
        """Ein Eintrag, wenn er noch gilt - und wenn er jung genug ist.

        Zwei verschiedene Fragen, die vorher eine waren:

        * Die **TTL** sagt, wie lange eine Antwort fuer *andere* gilt. Fuer
          eine Suche ist der Kalender von heute Morgen weiterhin gut genug.
        * `max_age` sagt, wie alt eine Antwort fuer *diesen* Aufrufer sein
          darf. Die Jagd auf Fehltarife braucht frische Preise; eine
          Kalenderantwort von vor zwoelf Stunden beantwortet ihre Frage nicht.

        Ohne diese Trennung waere die Jagd sinnlos: bei einer TTL von
        vierundzwanzig Stunden und einem Takt von zwanzig Minuten fragte sie
        nie die Quelle, sondern immer nur ihre eigene Antwort von vorhin.
        Umgekehrt die TTL zu verkuerzen haette jedem Suchlauf die Ersparnis
        genommen, obwohl er sie gebrauchen kann.

        `now` ist die Uhr des Aufrufers. Sie steht hier aus demselben Grund
        wie an `run_watchlist` und `refresh_baselines`: ohne sie liesse sich
        die Beziehung zwischen Takt und Frischegrenze nur in Echtzeit pruefen,
        also gar nicht.
        """
        row = self.conn.execute(
            "SELECT payload, expires_at, fetched_at FROM price_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        moment = now or datetime.now()
        if db.parse_dt(row["expires_at"]) <= moment:
            return None
        if max_age is not None:
            try:
                fetched = db.parse_dt(row["fetched_at"])
            except (TypeError, ValueError):
                # Ein unlesbarer Zeitstempel heisst "Alter unbekannt", und
                # unbekannt ist fuer einen Aufrufer mit Frischeanspruch dasselbe
                # wie zu alt.
                return None
            if moment - fetched > max_age:
                return None
        return json.loads(row["payload"])

    async def put(self, key: str, value: Any, ttl: timedelta, *, source: str = "",
                  now: datetime | None = None) -> None:
        moment = now or datetime.now()
        self.conn.execute(
            "INSERT INTO price_cache(cache_key, source, payload, fetched_at, expires_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at",
            (
                key,
                source,
                json.dumps(value, default=str),
                moment.isoformat(timespec="seconds"),
                (moment + ttl).isoformat(timespec="seconds"),
            ),
        )

    async def purge_expired(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM price_cache WHERE expires_at <= ?", (db.now(),)
        )
        return cur.rowcount


def last_observation_by_source(conn: sqlite3.Connection) -> dict[str, str]:
    """Wann jede Quelle zuletzt eine Beobachtung geschrieben hat.

    Eine Zeile je Quelle, aus der Historie selbst. Der Katalog weiss nur, wer
    mitspielen darf; ob eine Quelle noch etwas liefert, steht ausschliesslich
    hier. Genau diese Frage konnte vorher niemand stellen: eine Quelle, die
    seit Tagen an einem 403 haengt, sah im Betrieb aus wie eine, nach der
    einfach niemand gefragt hat.
    """
    return {
        str(row["source"]): str(row["last"])
        for row in conn.execute(
            "SELECT source, MAX(observed_at) AS last FROM price_observation "
            "GROUP BY source"
        )
        if row["last"]
    }


class SqliteHistory:
    """Append-only. Every fetch writes a row; nothing is ever overwritten."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    async def record(
        self,
        *,
        source: str,
        entity_type: str,
        entity_key: str,
        travel_date: date,
        price: Money,
        observed_at: datetime | None = None,
        return_or_nights: str | None = None,
        party_size: int = 1,
        is_estimate: bool = False,
        is_indicative: bool = False,
        raw_hash: str | None = None,
    ) -> None:
        """`is_indicative` merkt sich, was der Katalog beim Schreiben wusste.

        Der Aufrufer hat die Quelle in der Hand und kann ihr Kennzeichen
        einfach durchreichen. Spaeter aus dem Quellennamen zurueckzuschliessen
        hiesse raten, welche Quellen damals als Richtwert galten.
        """
        self.conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, return_or_nights,"
            "party_size, currency, price_total_minor, is_estimate, is_indicative, raw_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (observed_at or datetime.now()).isoformat(timespec="seconds"),
                source,
                entity_type,
                entity_key,
                travel_date.isoformat(),
                return_or_nights,
                party_size,
                price.currency,
                price.minor,
                int(is_estimate),
                int(is_indicative),
                raw_hash,
            ),
        )

    async def record_many(self, rows: list[dict[str, Any]]) -> int:
        for row in rows:
            await self.record(**row)
        return len(rows)

    async def series(
        self, entity_type: str, entity_key: str, travel_date: date | None = None
    ) -> list[tuple[datetime, Money]]:
        if travel_date is None:
            cur = self.conn.execute(
                "SELECT observed_at, price_total_minor, currency FROM price_observation "
                "WHERE entity_type=? AND entity_key=? ORDER BY observed_at",
                (entity_type, entity_key),
            )
        else:
            cur = self.conn.execute(
                "SELECT observed_at, price_total_minor, currency FROM price_observation "
                "WHERE entity_type=? AND entity_key=? AND travel_date=? ORDER BY observed_at",
                (entity_type, entity_key, travel_date.isoformat()),
            )
        return [
            (db.parse_dt(r["observed_at"]), Money(r["price_total_minor"], r["currency"]))
            for r in cur.fetchall()
        ]
