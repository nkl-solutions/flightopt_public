"""Explicit ownership of newly written watch observations, without backfill."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_active_scan: ContextVar[tuple[sqlite3.Connection, int] | None] = ContextVar(
    "hotel_watch_scan", default=None
)


def ensure_watch_history(conn: sqlite3.Connection) -> None:
    """Add ownership tables without committing the caller's transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hotel_watch_scan ("
        "scan_id INTEGER PRIMARY KEY REFERENCES hotel_scan(id) ON DELETE CASCADE, "
        "watch_id INTEGER NOT NULL REFERENCES hotel_watch(id) ON DELETE CASCADE, "
        "completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1)))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_hotel_watch_scan_watch "
        "ON hotel_watch_scan(watch_id, completed)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hotel_watch_observation ("
        "observation_id INTEGER PRIMARY KEY "
        "REFERENCES price_observation(id) ON DELETE CASCADE, "
        "scan_id INTEGER NOT NULL REFERENCES hotel_watch_scan(scan_id) ON DELETE CASCADE)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_hotel_watch_observation_scan "
        "ON hotel_watch_observation(scan_id)"
    )

    def active_scan_id() -> int | None:
        scope = _active_scan.get()
        return scope[1] if scope is not None and scope[0] is conn else None

    conn.create_function("hotel_watch_scan_id", 0, active_scan_id)
    # TEMP keeps ordinary connections independent of this instrumentation.
    # NEW.id associates the exact INSERT atomically; task-local scope excludes
    # concurrent manual searches even when they share this connection.
    conn.execute(
        "CREATE TEMP TRIGGER IF NOT EXISTS capture_hotel_watch_observation "
        "AFTER INSERT ON main.price_observation "
        "WHEN NEW.entity_type='hotel' AND hotel_watch_scan_id() IS NOT NULL "
        "BEGIN "
        "INSERT INTO hotel_watch_observation(observation_id, scan_id) "
        "VALUES(NEW.id, hotel_watch_scan_id()); "
        "END"
    )


@contextmanager
def capture_watch_scan(
    conn: sqlite3.Connection, watch_id: int, scan_id: int
) -> Iterator[None]:
    """Capture only this run; exceptions and cancellation always clear scope."""
    conn.execute(
        "INSERT INTO hotel_watch_scan(scan_id, watch_id) VALUES(?,?) "
        "ON CONFLICT(scan_id) DO NOTHING",
        (scan_id, watch_id),
    )
    token = _active_scan.set((conn, scan_id))
    try:
        yield
    finally:
        _active_scan.reset(token)


def complete_watch_scan(conn: sqlite3.Connection, scan_id: int) -> int:
    """Only completed scans contribute to watch progress; prices stay global."""
    conn.execute(
        "UPDATE hotel_watch_scan SET completed=1 WHERE scan_id=? "
        "AND EXISTS(SELECT 1 FROM hotel_scan WHERE id=? AND status='done')",
        (scan_id, scan_id),
    )
    return int(conn.execute(
        "SELECT count(*) FROM hotel_watch_observation o "
        "JOIN hotel_watch_scan s ON s.scan_id=o.scan_id "
        "WHERE s.scan_id=? AND s.completed=1",
        (scan_id,),
    ).fetchone()[0])
