"""Price baselines and simple anomaly signals."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from statistics import median
from typing import Any


def leadtime_bucket(days: int) -> str:
    if days < 7:
        return "0-6"
    if days < 14:
        return "7-13"
    if days < 30:
        return "14-29"
    if days < 60:
        return "30-59"
    if days < 120:
        return "60-119"
    return "120+"


def _baseline_key(travel_date: date, observed_at: datetime) -> tuple[int, str]:
    leadtime = max(0, (travel_date - observed_at.date()).days)
    return travel_date.weekday(), leadtime_bucket(leadtime)


def refresh_baselines(conn: sqlite3.Connection, *, now: datetime | None = None,
                      entity_type: str = "flight", min_samples: int = 5) -> int:
    computed_at = (now or datetime.now()).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT observed_at, entity_type, entity_key, travel_date, currency, "
        "price_total_minor FROM price_observation WHERE entity_type=?",
        (entity_type,),
    ).fetchall()
    grouped: dict[tuple[str, str, int, str, str], list[int]] = {}
    for row in rows:
        observed_at = datetime.fromisoformat(row["observed_at"])
        travel_date = date.fromisoformat(row["travel_date"])
        weekday, bucket = _baseline_key(travel_date, observed_at)
        key = (row["entity_type"], row["entity_key"], weekday, bucket, row["currency"])
        grouped.setdefault(key, []).append(int(row["price_total_minor"]))

    written = 0
    for (etype, entity_key, weekday, bucket, currency), prices in grouped.items():
        if len(prices) < min_samples:
            continue
        med = int(median(prices))
        deviations = [abs(p - med) for p in prices]
        mad = int(median(deviations))
        conn.execute(
            "INSERT INTO price_baseline("
            "entity_type, entity_key, weekday, leadtime_bucket, currency, "
            "median_minor, mad_minor, n, computed_at"
            ") VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_type, entity_key, weekday, leadtime_bucket, currency) "
            "DO UPDATE SET median_minor=excluded.median_minor, "
            "mad_minor=excluded.mad_minor, n=excluded.n, computed_at=excluded.computed_at",
            (etype, entity_key, weekday, bucket, currency, med, mad, len(prices), computed_at),
        )
        written += 1
    return written


def detect_price_signal(conn: sqlite3.Connection, entity_key: str, travel_date: date,
                        price_minor: int, *, observed_at: datetime | None = None,
                        entity_type: str = "flight", currency: str = "EUR") -> dict[str, Any]:
    observed = observed_at or datetime.now()
    weekday, bucket = _baseline_key(travel_date, observed)
    row = conn.execute(
        "SELECT median_minor, mad_minor, n FROM price_baseline "
        "WHERE entity_type=? AND entity_key=? AND weekday=? "
        "AND leadtime_bucket=? AND currency=?",
        (entity_type, entity_key, weekday, bucket, currency),
    ).fetchone()
    if row is None:
        return {"status": "unknown", "price_minor": price_minor}
    median_minor = int(row["median_minor"])
    mad_minor = int(row["mad_minor"])
    band = max(1500, mad_minor * 3)
    if price_minor <= median_minor - band:
        status = "cheap"
    elif price_minor >= median_minor + band:
        status = "expensive"
    else:
        status = "normal"
    return {
        "status": status,
        "price_minor": price_minor,
        "median_minor": median_minor,
        "mad_minor": mad_minor,
        "n": int(row["n"]),
    }
