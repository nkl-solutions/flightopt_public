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
    entity_key         TEXT NOT NULL,      -- flight: 'BER|ATH'   hotel: 'ATH|12345'
    travel_date        TEXT NOT NULL,
    return_or_nights   TEXT,
    party_size         INTEGER NOT NULL DEFAULT 1,
    currency           TEXT NOT NULL,
    price_total_minor  INTEGER NOT NULL,
    is_estimate        INTEGER NOT NULL DEFAULT 0,
    -- Richtwert statt Tarif: die Quelle hat einen bekannten systematischen
    -- Aufschlag oder preist ein anderes Produkt. `source` allein reicht dafuer
    -- nicht, denn was indikativ ist, weiss der Quellenkatalog und nicht die
    -- Datenbank - und ob eine Quelle es *damals* war, weiss danach niemand
    -- mehr. Deshalb steht die Antwort in der Zeile, nicht in einer Liste.
    is_indicative      INTEGER NOT NULL DEFAULT 0,
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

-- Die Flug-Baseline mit der Grundgesamtheit im Schluessel. Ein Kalenderpreis
-- und ein gepruefter Live-Preis beschreiben nicht dasselbe: der eine ist der
-- Tagesbestpreis irgendeines Flugs, der andere der Preis eines bestimmten
-- Flugs, und Gepaeck und Zuschlaege stecken einmal drin und einmal nicht.
-- Gegeneinander gerechnet ergibt das eine Zahl, die nichts misst.
--
-- Eigene Tabelle statt einer Spalte in `price_baseline`, aus demselben Grund,
-- aus dem `hotel_baseline` daneben steht: der Primaerschluessel ist
-- zusammengesetzt, und den erweitert SQLite nicht per `ALTER TABLE`. Eine neue
-- Tabelle legt `CREATE TABLE IF NOT EXISTS` dagegen auch in einer bestehenden
-- Datei an, ohne dass ein noch laufender Prozess auf der alten stolpert.
CREATE TABLE IF NOT EXISTS flight_baseline (
    entity_key         TEXT NOT NULL,      -- 'BER|ATH'
    weekday            INTEGER NOT NULL,
    leadtime_bucket    TEXT NOT NULL,
    currency           TEXT NOT NULL,
    is_estimate        INTEGER NOT NULL,   -- 1 = Kalenderpreis, 0 = geprueft
    median_minor       INTEGER NOT NULL,
    mad_minor          INTEGER NOT NULL,
    n                  INTEGER NOT NULL,
    computed_at        TEXT NOT NULL,
    PRIMARY KEY(entity_key, weekday, leadtime_bucket, currency, is_estimate)
);

CREATE TABLE IF NOT EXISTS fx_rate (
    currency   TEXT PRIMARY KEY,
    rate       REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Stammdaten je Unterkunft. Der Preis steht nicht hier, sondern in
-- price_observation: ein Objekt hat viele Preise, aber nur einen Namen.
CREATE TABLE IF NOT EXISTS hotel_property (
    property_key   TEXT PRIMARY KEY,   -- '<quelle>:<id>', z.B. 'trivago:1d6fec31a3cf'
    source         TEXT NOT NULL,
    name           TEXT NOT NULL,
    city           TEXT,
    country        TEXT,
    country_code   TEXT,               -- ISO-2, 'XX' wenn unbekannt
    stars          INTEGER,
    lat            REAL,
    lon            REAL,
    review_rating  REAL,
    review_count   INTEGER,
    url            TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_property_place
    ON hotel_property(country_code, city, stars);

-- Ein Zeitraum-Durchlauf. current_day traegt die Wiederaufnahme: ein Lauf, der
-- abbricht, macht dort weiter statt von vorn.
CREATE TABLE IF NOT EXISTS hotel_scan (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    destination  TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    nights       INTEGER NOT NULL DEFAULT 1,
    adults       INTEGER NOT NULL DEFAULT 2,
    children     INTEGER NOT NULL DEFAULT 0,
    rooms        INTEGER NOT NULL DEFAULT 1,
    filters      TEXT NOT NULL DEFAULT '{}',   -- Sterne, Bewertung, Waehrung
    status       TEXT NOT NULL,                -- pending|running|done|failed|cancelled
    current_day  TEXT,
    days_done    INTEGER NOT NULL DEFAULT 0,
    days_total   INTEGER NOT NULL DEFAULT 0,
    offers_found INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_scan_status ON hotel_scan(status, created_at);

CREATE TABLE IF NOT EXISTS api_budget (
    source TEXT NOT NULL,
    month  TEXT NOT NULL,          -- 'YYYY-MM'
    used   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(source, month)
);
"""


ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("price_observation", "is_indicative", "INTEGER NOT NULL DEFAULT 0"),
)
"""Spalten, die spaeter dazukamen. `CREATE TABLE IF NOT EXISTS` fasst eine
bestehende Datei nicht mehr an, eine neue Spalte muss also nachgetragen
werden. `ALTER TABLE ADD COLUMN` kann SQLite, solange die Spalte nicht zum
Primaerschluessel gehoert - fuer den Fall gibt es eine eigene Tabelle.

Nur die Spalte entsteht hier, nicht ihr Inhalt: bestehende Zeilen bekommen den
Vorgabewert. Was in ihnen wirklich stand, weiss das Migrationsskript."""


def add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """Fehlende Spalten nachtragen. Idempotent: ein zweiter Lauf findet nichts."""
    added: list[str] = []
    for table, column, declaration in ADDED_COLUMNS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in present:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        added.append(f"{table}.{column}")
    return added


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
    add_missing_columns(conn)
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
