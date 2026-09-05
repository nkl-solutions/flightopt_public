"""Saved search profiles used by autonomous daily scans."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from flightopt.domain.models import Cabin, LegSpec, Pax, SearchSpec, StayRange
from flightopt.jobs.runner import specs_to_dict


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
