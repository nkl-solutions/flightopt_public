"""SQLite schema and connection handling.

One file, WAL mode, one writer. Append-only for observations: a price we
recorded is never updated, so the history table doubles as the audit trail.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("data/flightopt.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_cache (
    cache_key   TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cache_expiry ON price_cache(expires_at);

-- Append-only. Flights and hotels share this table; entity_key shape differs.
CREATE TABLE IF NOT EXISTS price_observation (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at        TEXT NOT NULL,
    source             TEXT NOT NULL,
    entity_type        TEXT NOT NULL,      -- 'flight' | 'hotel'
    entity_key         TEXT NOT NULL,      -- flight: 'BER|ATH|FR'   hotel: 'ATH|12345'
    travel_date        TEXT NOT NULL,
    return_or_nights   TEXT,
    party_size         INTEGER NOT NULL DEFAULT 1,
    currency           TEXT NOT NULL,
    price_total_minor  INTEGER NOT NULL,
    is_estimate        INTEGER NOT NULL DEFAULT 0,
    raw_hash           TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_entity
    ON price_observation(entity_type, entity_key, travel_date, observed_at);

CREATE TABLE IF NOT EXISTS search_job (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    spec           TEXT NOT NULL,
    status         TEXT NOT NULL,          -- pending|running|done|failed|cancelled
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    progress_done  INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS search_task (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES search_job(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,              -- calendar|leg|verify
    source     TEXT NOT NULL,
    cache_key  TEXT NOT NULL,
    params     TEXT NOT NULL,
    status     TEXT NOT NULL,              -- pending|running|done|failed|skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_task_job ON search_task(job_id, status);

CREATE TABLE IF NOT EXISTS itinerary_result (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL REFERENCES search_job(id) ON DELETE CASCADE,
    rank              INTEGER NOT NULL,
    dates             TEXT NOT NULL,
    price_total_minor INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    stops             INTEGER,
    air_minutes       INTEGER,
    is_estimate       INTEGER NOT NULL DEFAULT 1,
    detail            TEXT
);
CREATE INDEX IF NOT EXISTS ix_result_job ON itinerary_result(job_id, rank);

CREATE TABLE IF NOT EXISTS search_profile (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    spec           TEXT NOT NULL,
    airlines       TEXT NOT NULL DEFAULT '[]',
    cadence_days   INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    last_run_at    TEXT,
    next_run_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_profile_due ON search_profile(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS price_baseline (
    entity_type        TEXT NOT NULL,
    entity_key         TEXT NOT NULL,
    weekday            INTEGER NOT NULL,
    leadtime_bucket    TEXT NOT NULL,
    currency           TEXT NOT NULL,
    median_minor       INTEGER NOT NULL,
    mad_minor          INTEGER NOT NULL,
    n                  INTEGER NOT NULL,
    computed_at        TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_key, weekday, leadtime_bucket, currency)
);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def iso(value: datetime | date) -> str:
    return value.isoformat()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def expires(ttl: timedelta) -> str:
    return (datetime.now() + ttl).isoformat(timespec="seconds")


def fetchone(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row
