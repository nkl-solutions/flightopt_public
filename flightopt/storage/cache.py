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
    pax: int = 1,
    cabin: str = "economy",
    currency: str = "EUR",
) -> str:
    raw = f"{source}|{kind}|{origin}|{destination}|{day.isoformat()}|{pax}|{cabin}|{currency}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class SqliteCache:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    async def get(self, key: str) -> Any | None:
        row = self.conn.execute(
            "SELECT payload, expires_at FROM price_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if db.parse_dt(row["expires_at"]) <= datetime.now():
            return None
        return json.loads(row["payload"])

    async def put(self, key: str, value: Any, ttl: timedelta, *, source: str = "") -> None:
        self.conn.execute(
            "INSERT INTO price_cache(cache_key, source, payload, fetched_at, expires_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at",
            (key, source, json.dumps(value, default=str), db.now(), db.expires(ttl)),
        )

    async def purge_expired(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM price_cache WHERE expires_at <= ?", (db.now(),)
        )
        return cur.rowcount


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
        raw_hash: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, return_or_nights,"
            "party_size, currency, price_total_minor, is_estimate, raw_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
