"""Was ein Prozess-Neustart mit haengengebliebenen Laeufen macht.

`search_task` war dafuer angelegt und blieb leer. Die Frage, die sie
beantworten sollte - ueberlebt ein Lauf den Prozess-Kill - stellt sich eine
Ebene hoeher, an `search_job` und `hotel_scan`, und dort wird sie hier
beantwortet.
"""

from __future__ import annotations

import sqlite3

from starlette.testclient import TestClient

from flightopt.api import main
from flightopt.jobs.runner import JobRunner
from flightopt.storage import db


def running_job(conn: sqlite3.Connection, status: str = "running") -> int:
    cur = conn.execute(
        "INSERT INTO search_job(spec, status, created_at) VALUES(?,?,?)",
        ("{}", status, db.now()),
    )
    return int(cur.lastrowid)


def running_scan(conn: sqlite3.Connection, status: str = "running") -> int:
    cur = conn.execute(
        "INSERT INTO hotel_scan(destination, window_start, window_end, status, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
        ("Athen", "2026-10-01", "2026-10-02", status, db.now(), db.now()),
    )
    return int(cur.lastrowid)


def test_a_fresh_file_never_gets_the_dead_table(tmp_path):
    conn = db.connect(tmp_path / "neu.db")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()

    assert "search_task" not in tables
    # Der Grund fuer den Wegfall bleibt im Schema stehen, als Kommentar.
    assert "CREATE TABLE IF NOT EXISTS search_task" not in db.SCHEMA


def test_an_existing_file_loses_the_dead_table(tmp_path):
    """Eine Tabelle, die aussieht als tue sie etwas, ist schlimmer als keine."""
    path = tmp_path / "alt.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE search_task (id INTEGER PRIMARY KEY, job_id INTEGER)")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()

    assert "search_task" not in tables


def test_a_job_that_did_not_survive_the_restart_is_marked(tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    try:
        pending = running_job(conn, "pending")
        running = running_job(conn, "running")
        finished = running_job(conn, "done")
        conn.commit()

        counts = db.mark_interrupted_runs(conn)

        rows = {
            int(r["id"]): (r["status"], r["error"])
            for r in conn.execute("SELECT id, status, error FROM search_job")
        }
    finally:
        conn.close()

    assert counts == {"search_job": 2, "hotel_scan": 0}
    assert rows[pending][0] == "failed"
    assert rows[running][0] == "failed"
    assert db.INTERRUPTED in rows[running][1]
    # Ein fertiger Lauf wird nicht nachtraeglich schlechtgeredet.
    assert rows[finished] == ("done", None)


def test_a_hotel_scan_that_did_not_survive_the_restart_is_marked(tmp_path):
    conn = db.connect(tmp_path / "scans.db")
    try:
        running = running_scan(conn)
        cancelled = running_scan(conn, "cancelled")
        conn.commit()

        counts = db.mark_interrupted_runs(conn)

        rows = {
            int(r["id"]): (r["status"], r["error"], r["finished_at"])
            for r in conn.execute(
                "SELECT id, status, error, finished_at FROM hotel_scan"
            )
        }
    finally:
        conn.close()

    assert counts == {"search_job": 0, "hotel_scan": 1}
    assert rows[running][0] == "failed"
    assert rows[running][2], "ohne Endzeitpunkt findet stored_rows die Zeilen nicht"
    assert rows[cancelled][0] == "cancelled"


def test_running_twice_changes_nothing_the_second_time(tmp_path):
    conn = db.connect(tmp_path / "idempotent.db")
    try:
        running_job(conn)
        conn.commit()

        first = db.mark_interrupted_runs(conn)
        second = db.mark_interrupted_runs(conn)
    finally:
        conn.close()

    assert first["search_job"] == 1
    assert second["search_job"] == 0


def test_startup_closes_the_runs_of_a_dead_process(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIGHTOPT_DAILY_SCANS", "0")
    runner = JobRunner(str(tmp_path / "startup.db"))
    monkeypatch.setattr(main, "runner", runner)
    conn = runner._conn()
    job_id = running_job(conn)
    conn.commit()
    conn.close()

    with TestClient(main.app):
        pass

    conn = runner._conn()
    try:
        row = conn.execute(
            "SELECT status FROM search_job WHERE id=?", (job_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["status"] == "failed"


def test_a_file_from_before_the_hunt_still_opens(tmp_path):
    """Der Fehler, den kein Test mit einer frischen Datei findet.

    `CREATE TABLE IF NOT EXISTS` fasst eine vorhandene Tabelle nicht mehr an,
    `CREATE INDEX` dagegen laeuft - und scheitert an einer Spalte, die es in
    der alten Datei noch nicht gibt. Auf einer frischen Datei war alles gruen,
    die laufende kam nicht mehr hoch.
    """
    path = tmp_path / "old.db"
    conn = db.connect(path)
    # Zurueck auf den Stand vor dem Takt: erst der Index, dann die Spalten.
    conn.execute("DROP INDEX IF EXISTS ix_watch_hot")
    conn.execute("ALTER TABLE watch_route DROP COLUMN cadence")
    conn.execute("ALTER TABLE watch_route DROP COLUMN last_hot_run_at")
    conn.close()

    again = db.connect(path)

    columns = {row["name"] for row in again.execute("PRAGMA table_info(watch_route)")}
    assert {"cadence", "last_hot_run_at"} <= columns
    indexes = {
        row["name"]
        for row in again.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='watch_route'"
        )
    }
    assert "ix_watch_hot" in indexes
    again.close()
