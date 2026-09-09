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

-- Hier stand `search_task`: eine Zeile je Abruf, mit Status und Versuchen.
-- Kein einziger Codepfad hat sie je geschrieben oder gelesen, und in der
-- laufenden Datenbank stand keine Zeile darin. Sie sah aus, als ueberlebe ein
-- Lauf den Prozess-Kill, und genau das tat sie nicht. Was sie versprach,
-- leistet `price_cache` schon: ein neu gestarteter Job nimmt jeden noch
-- gueltigen Abruf aus dem Cache und fragt die Quellen nicht erneut. Eine
-- zweite Buchfuehrung darueber waere eine zweite Wahrheit, und die auf dem
-- heissen Pfad - ein Schreibvorgang je Leg-Tag je Quelle. Was stattdessen
-- fehlte, steht jetzt in `mark_interrupted_runs`.

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

-- Die Beobachtungsliste. Eine Zeile je Strecke, deren Preiskalender taeglich
-- aufgezeichnet wird.
--
-- Eigene Tabelle statt einer Zeile in `search_profile`, weil eine Beobachtung
-- etwas anderes ist als eine gespeicherte Suche. Ein Profil traegt eine ganze
-- Route mit Aufenthalten, Reisenden und einem *festen* Fenster - und hoert
-- damit nach dem Reisetag auf, Sinn zu ergeben. Eine Beobachtung traegt eine
-- Teilstrecke und ein *rollendes* Vorlauf-Fenster; sie laeuft weiter, solange
-- niemand sie abschaltet. Ausserdem loest ein Profil den ganzen Suchlauf aus
-- (Gitter, Kombinatorik, Live-Pruefung), waehrend hier nur der billige
-- Kalenderteil gebraucht wird.
--
-- Das Paar (origin, destination) ist eindeutig: zweimal dieselbe Strecke
-- waeren zweimal dieselben Abrufe am selben Tag bei denselben Quellen.
CREATE TABLE IF NOT EXISTS watch_route (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    origin         TEXT NOT NULL,
    destination    TEXT NOT NULL,
    lead_min_days  INTEGER NOT NULL,   -- ab wie vielen Tagen Vorlauf
    lead_max_days  INTEGER NOT NULL,   -- bis wie vielen Tagen Vorlauf
    currency       TEXT NOT NULL DEFAULT 'EUR',
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    last_run_at    TEXT,
    -- 'daily' | 'hot'. Der Takt, in dem diese Strecke gefragt wird. Zwei Werte
    -- und kein Zwischenwert: aus dem heissen Takt folgt, wie viele Strecken
    -- gleichzeitig heiss laufen duerfen (`flightopt.hunt.cadence`), und eine
    -- frei einstellbare Zahl liesse sich nicht mehr gegen eine Grenze halten.
    cadence        TEXT NOT NULL DEFAULT 'daily',
    -- Eigener Zeitstempel neben `last_run_at`, weil er eine andere Frage
    -- beantwortet: "einmal am Tag" ist ein Kalendertag, "alle zwanzig Minuten"
    -- ist ein Abstand in Sekunden. Ein Feld fuer beides waere ein Feld mit
    -- zwei Bedeutungen.
    last_hot_run_at TEXT,
    UNIQUE(origin, destination)
);
CREATE INDEX IF NOT EXISTS ix_watch_due ON watch_route(enabled, last_run_at);

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
    retries      INTEGER NOT NULL DEFAULT 0,   -- Nachfassen ueber alle Tage
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

-- Das Hauptbuch der Jagd. Eine Zeile je verbuchtem Durchgang und Quelle,
-- gefragt wird immer nach der rollenden Stunde. Keine Aggregatzeile je
-- Kalenderstunde: die erlaubt das volle Budget um 11:59 und noch einmal um
-- 12:00, und genau diesen Doppelschlag sieht die Quelle.
CREATE TABLE IF NOT EXISTS hunt_call (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    charged_at TEXT NOT NULL,
    source     TEXT NOT NULL,
    calls      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hunt_call ON hunt_call(source, charged_at);

-- Wer uns abgewiesen hat, und bis wann deshalb niemand im heissen Takt laeuft.
-- Das Gedaechtnis des CircuitBreaker ueber den Prozess hinaus: der
-- Quellen-Katalog wird je Durchgang neu gebaut, jede Sicherung faengt also
-- sonst frisch geschlossen an und lernt nichts aus dem letzten Durchgang.
CREATE TABLE IF NOT EXISTS hunt_pause (
    source TEXT PRIMARY KEY,
    since  TEXT NOT NULL,
    until  TEXT NOT NULL,
    reason TEXT NOT NULL
);

-- Wofuer ueberhaupt gemeldet wird. Ohne eine einzige Zeile gilt die
-- eingebaute Vorgabe (jede Strecke, Stufe `error`) - eine Regel ist
-- Feineinstellung und keine Voraussetzung. Waere sie eine, liefe ein frisch
-- aufgesetzter Dienst still und niemand wuesste warum.
CREATE TABLE IF NOT EXISTS alert_rule (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key TEXT NOT NULL,                      -- 'BER|ATH' oder '*'
    condition  TEXT NOT NULL DEFAULT '{}',         -- JSON, heute nur {"tiers": [...]}
    channel    TEXT NOT NULL DEFAULT 'discord',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(entity_key, channel)
);

-- Jeder Fund, auch der nicht gemeldete. `delivery` sagt, was mit ihm
-- geschehen ist: verschickt, trocken protokolliert, unterdrueckt oder
-- gescheitert. Ohne diese Zeile liesse sich der Kanal nicht beobachten,
-- bevor er reden darf - und genau das soll er koennen.
CREATE TABLE IF NOT EXISTS alert_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         INTEGER REFERENCES alert_rule(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    entity_key      TEXT NOT NULL,
    travel_date     TEXT NOT NULL,
    tier            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT '',
    currency        TEXT NOT NULL DEFAULT 'EUR',
    price_minor     INTEGER NOT NULL,
    median_minor    INTEGER,
    n               INTEGER NOT NULL DEFAULT 0,
    population      TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '{}',
    delivery        TEXT NOT NULL DEFAULT 'pending',
    delivered_at    TEXT,
    error           TEXT,
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_alert_event_key
    ON alert_event(entity_key, travel_date, tier, created_at);
CREATE INDEX IF NOT EXISTS ix_alert_event_recent ON alert_event(created_at);
"""


ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("price_observation", "is_indicative", "INTEGER NOT NULL DEFAULT 0"),
    ("hotel_scan", "retries", "INTEGER NOT NULL DEFAULT 0"),
    ("watch_route", "cadence", "TEXT NOT NULL DEFAULT 'daily'"),
    ("watch_route", "last_hot_run_at", "TEXT"),
)
"""Spalten, die spaeter dazukamen. `CREATE TABLE IF NOT EXISTS` fasst eine
bestehende Datei nicht mehr an, eine neue Spalte muss also nachgetragen
werden. `ALTER TABLE ADD COLUMN` kann SQLite, solange die Spalte nicht zum
Primaerschluessel gehoert - fuer den Fall gibt es eine eigene Tabelle.

Nur die Spalte entsteht hier, nicht ihr Inhalt: bestehende Zeilen bekommen den
Vorgabewert. Was in ihnen wirklich stand, weiss das Migrationsskript."""


LATE_SCHEMA = """
-- Alles, was eine nachgetragene Spalte braucht. Laeuft **nach**
-- `add_missing_columns` und deshalb nicht in `SCHEMA`.
--
-- Der Grund ist ein Fehler, der sich nur auf einer bestehenden Datei zeigt und
-- den kein Test mit `tmp_path` je findet: `CREATE TABLE IF NOT EXISTS` fasst
-- eine vorhandene Tabelle nicht mehr an, `CREATE INDEX` dagegen laeuft und
-- scheitert an der Spalte, die es noch nicht gibt. Auf einer frischen Datei
-- ist alles gruen, auf der laufenden kommt der Dienst nicht mehr hoch.
CREATE INDEX IF NOT EXISTS ix_watch_hot
    ON watch_route(enabled, cadence, last_hot_run_at);
"""


DROPPED_TABLES: tuple[str, ...] = ("search_task",)
"""Tabellen, die es nicht mehr gibt.

`CREATE TABLE IF NOT EXISTS` legt sie in einer bestehenden Datei nicht wieder
an, laesst sie aber auch stehen. Eine leere Tabelle, die aussieht als fuehre
jemand darin Buch, ist schlimmer als keine: der naechste Leser baut darauf.
"""

UNUSED_RUN_STATES = ("pending", "running")
"""Zustaende, die einen lebenden Prozess voraussetzen."""

INTERRUPTED = "Abgebrochen: der Prozess endete, waehrend der Lauf lief."


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


def drop_dead_tables(conn: sqlite3.Connection) -> list[str]:
    """Tabellen ohne Leser und ohne Schreiber wegraeumen. Idempotent."""
    dropped: list[str] = []
    for table in DROPPED_TABLES:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            continue
        conn.execute(f"DROP TABLE {table}")
        dropped.append(table)
    return dropped


def mark_interrupted_runs(conn: sqlite3.Connection) -> dict[str, int]:
    """Laeufe schliessen, die einen Prozessende nicht ueberlebt haben.

    Ein Job steht auf `running`, solange sein Task laeuft. Endet der Prozess,
    bleibt die Zeile so stehen: die Oberflaeche zeigt den Lauf fuer immer als
    laufend, und der Ereignisstrom wartet auf Ereignisse, die kein Task mehr
    sendet. Beim Start ist jeder solche Lauf per Definition tot - dieser
    Prozess fuehrt keinen davon.

    Der Fehlertext sagt, was passiert ist, statt einfach `failed` zu setzen:
    "abgebrochen, weil der Prozess endete" ist etwas anderes als "die Suche ist
    gescheitert".

    Die Annahme dahinter ist die des ganzen Werkzeugs: ein Prozess auf einer
    Datei. Zwei gleichzeitig laufende Prozesse wuerden einander die Laeufe
    abschreiben. Aufgerufen wird das deshalb beim Start, nicht laufend.

    Ein Hotellauf bleibt trotzdem wiederaufnehmbar: `current_day` traegt die
    Fortsetzung, und ein `failed` steht ihr nicht im Weg.
    """
    marks = ",".join("?" for _ in UNUSED_RUN_STATES)
    jobs = conn.execute(
        f"UPDATE search_job SET status='failed', error=?, "
        f"finished_at=COALESCE(finished_at, ?) WHERE status IN ({marks})",
        (INTERRUPTED, now(), *UNUSED_RUN_STATES),
    )
    job_count = int(jobs.rowcount or 0)
    scans = conn.execute(
        f"UPDATE hotel_scan SET status='failed', error=?, updated_at=?, "
        f"finished_at=COALESCE(finished_at, ?) WHERE status IN ({marks})",
        (INTERRUPTED, now(), now(), *UNUSED_RUN_STATES),
    )
    return {"search_job": job_count, "hotel_scan": int(scans.rowcount or 0)}


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
    # Erst jetzt, denn hier steht, was eine nachgetragene Spalte voraussetzt.
    conn.executescript(LATE_SCHEMA)
    drop_dead_tables(conn)
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
