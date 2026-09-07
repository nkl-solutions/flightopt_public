"""Saved search profiles used by autonomous daily scans."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from flightopt.domain.models import Cabin, LegSpec, Pax, SearchSpec, StayRange
from flightopt.jobs.runner import specs_to_dict
from flightopt.storage.baseline import detect_price_signal


@dataclass(frozen=True, slots=True)
class SearchProfile:
    id: int
    name: str
    specs: list[SearchSpec]
    airlines: list[str]
    cadence_days: int
    next_run_at: datetime


def _spec_from_dict(row: dict) -> SearchSpec:
    airports = row["airports"]
    legs = tuple(LegSpec(airports[i], airports[i + 1]) for i in range(len(airports) - 1))
    stays = tuple(StayRange(a, b) for a, b in row.get("stays", []))
    return SearchSpec(
        legs=legs,
        stays=stays,
        window_start=datetime.fromisoformat(row["window_start"]).date(),
        window_end=datetime.fromisoformat(row["window_end"]).date(),
        pax=Pax(adults=int(row.get("adults", 1))),
        cabin=Cabin(row.get("cabin", "economy")),
        currency=row.get("currency", "EUR"),
        checked_bags=int(row.get("checked_bags", 0)),
        max_stops=row.get("max_stops"),
    )


def _specs_from_json(value: str) -> list[SearchSpec]:
    data = json.loads(value)
    if "variants" in data:
        return [_spec_from_dict(row) for row in data["variants"]]
    return [_spec_from_dict(data)]


def save_profile(conn: sqlite3.Connection, name: str, specs: Sequence[SearchSpec], *,
                 airlines: Sequence[str] = (), cadence_days: int = 1,
                 now: datetime | None = None) -> int:
    if not specs:
        raise ValueError("a saved profile needs at least one route")
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO search_profile("
        "name, spec, airlines, cadence_days, enabled, created_at, next_run_at"
        ") VALUES(?,?,?,?,?,?,?)",
        (
            name,
            json.dumps(specs_to_dict(specs)),
            json.dumps([a.upper() for a in airlines]),
            cadence_days,
            1,
            ts,
            ts,
        ),
    )
    return int(cur.lastrowid)


def due_profiles(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[SearchProfile]:
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, name, spec, airlines, cadence_days, next_run_at "
        "FROM search_profile WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at, id",
        (ts,),
    ).fetchall()
    return [
        SearchProfile(
            id=int(row["id"]),
            name=row["name"],
            specs=_specs_from_json(row["spec"]),
            airlines=json.loads(row["airlines"]),
            cadence_days=int(row["cadence_days"]),
            next_run_at=datetime.fromisoformat(row["next_run_at"]),
        )
        for row in rows
    ]


def mark_scanned(conn: sqlite3.Connection, profile_id: int, *, now: datetime | None = None,
                 cadence_days: int | None = None) -> None:
    current = now or datetime.now()
    row = conn.execute(
        "SELECT cadence_days FROM search_profile WHERE id=?",
        (profile_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown search profile: {profile_id}")
    cadence = cadence_days if cadence_days is not None else int(row["cadence_days"])
    conn.execute(
        "UPDATE search_profile SET last_run_at=?, next_run_at=? WHERE id=?",
        (
            current.isoformat(timespec="seconds"),
            (current + timedelta(days=cadence)).isoformat(timespec="seconds"),
            profile_id,
        ),
    )


def dispatch_due_profiles(conn: sqlite3.Connection, runner, *,
                          now: datetime | None = None) -> list[dict[str, int | str]]:
    current = now or datetime.now()
    jobs: list[dict[str, int | str]] = []
    for profile in due_profiles(conn, now=current):
        job_id = runner.create(profile.specs)
        runner.start(job_id, profile.specs, airlines=profile.airlines)
        mark_scanned(
            conn,
            profile.id,
            now=current,
            cadence_days=profile.cadence_days,
        )
        jobs.append({"profile_id": profile.id, "job_id": job_id, "name": profile.name})
    return jobs


def collect_deals(conn: sqlite3.Connection, *, limit: int = 50,
                  now: datetime | None = None) -> list[dict[str, Any]]:
    """Der beste Treffer je abgeschlossenem Profil-Scan, mit Abstand zur Baseline.

    `search_job` traegt keine Profilspalte. Die Verbindung laeuft ueber die
    Spec-Zeichenkette: `save_profile` und `JobRunner.create` schreiben beide
    `json.dumps(specs_to_dict(specs))`, die Strings sind also zeichengleich.
    """
    rows = conn.execute(
        "SELECT j.id AS job_id, j.finished_at AS scanned_at, p.id AS profile_id, "
        "p.name AS profile_name, r.dates AS dates, r.price_total_minor AS price_minor, "
        "r.currency AS currency, r.is_estimate AS is_estimate, r.detail AS detail "
        "FROM itinerary_result r "
        "JOIN search_job j ON j.id = r.job_id "
        "JOIN search_profile p ON p.spec = j.spec "
        "WHERE r.rank = 1 AND j.status = 'done' "
        "ORDER BY j.finished_at DESC, j.id DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()

    observed = now or datetime.now()
    deals: list[dict[str, Any]] = []
    for row in rows:
        legs = [leg for leg in json.loads(row["detail"] or "[]") if isinstance(leg, dict)]
        dates = json.loads(row["dates"])
        if not legs or not dates:
            continue
        route = legs[0].get("route") or "-".join(
            [legs[0].get("origin", "")] + [leg.get("destination", "") for leg in legs]
        )
        # Eine Baseline gilt je Strecke, nicht je Kette. Die erste Teilstrecke ist
        # der einzige Schluessel, den beide Seiten sicher teilen.
        entity_key = f"{legs[0].get('origin', '')}|{legs[0].get('destination', '')}"
        price_minor = int(row["price_minor"])
        signal = detect_price_signal(
            conn,
            entity_key,
            date.fromisoformat(dates[0]),
            price_minor,
            observed_at=observed,
            currency=row["currency"],
        )
        median_minor = signal.get("median_minor")
        deals.append(
            {
                "job_id": int(row["job_id"]),
                "profile_id": int(row["profile_id"]),
                "profile": row["profile_name"],
                "route": route,
                "scanned_at": row["scanned_at"],
                "dates": dates,
                "price": price_minor / 100,
                "currency": row["currency"],
                "verified": not bool(row["is_estimate"]),
                "median": median_minor / 100 if median_minor else None,
                "deviation_pct": (
                    round((price_minor - median_minor) / median_minor * 100, 1)
                    if median_minor
                    else None
                ),
                "signal": signal["status"],
            }
        )
    return deals
