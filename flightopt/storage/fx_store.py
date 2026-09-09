"""Exchange rates on the same SQLite file, refreshed at most once a day.

A search touches three legs and several sources; fetching the ECB file per leg
would be rude and pointless, because reference rates change once per business
day. `fetched_at` is the time we stored the rates, not the ECB reference day,
because the TTL asks "how long ago did we ask", not "how old is the quote".
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from flightopt.domain import fx
from flightopt.domain.fx import Rates
from flightopt.storage import db

logger = logging.getLogger(__name__)

TTL_FX = timedelta(hours=24)


def save_rates(conn: sqlite3.Connection, rates: Rates, *, now: datetime | None = None) -> None:
    """Replace the stored table with exactly what the source delivered.

    An upsert would keep a currency the ECB has retired (HRK, for example)
    alive forever, and `load_rates` reads the freshest `fetched_at` of all
    rows, so that stale row would ride along under a fresh timestamp. The
    connection runs with `isolation_level=None`, so the transaction is spelled
    out by hand instead of relying on the driver. For the same reason this must
    not be called while a transaction is already open: the `BEGIN IMMEDIATE`
    below would fail, and the `COMMIT` would end a transaction it does not own.

    Das `COMMIT` steht mit im geschuetzten Teil, denn es kann selbst scheitern
    (belegte Datei, volle Platte). Stand es daneben, blieb die Transaktion in
    diesem Fall offen: die Verbindung hielt bis zu ihrem Ende die
    Schreibsperre, jeder weitere Schreibvorgang desselben Laufs lief in den
    busy_timeout, und im Log stand nur "Kurse nicht gespeichert".

    An empty table is not saved at all. Deleting every rate because a source
    answered with nothing would leave the search unable to price any foreign
    currency, which is worse than keeping yesterday's numbers.
    """
    if not rates.rates:
        logger.warning("FX: leere Kurstabelle, gespeicherte Kurse bleiben stehen")
        return
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    rows = [(c.upper(), float(r), stamp) for c, r in rates.rates.items()]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM fx_rate")
        conn.executemany(
            "INSERT INTO fx_rate(currency, rate, fetched_at) VALUES(?,?,?)", rows
        )
        conn.execute("COMMIT")
    except Exception:
        # Ein gescheitertes COMMIT laesst die Transaktion in SQLite aktiv, das
        # Zuruecknehmen ist also richtig. Sollte sie doch schon beendet sein,
        # darf der Rollback-Fehler den urspruenglichen nicht verdecken.
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def load_rates(
    conn: sqlite3.Connection, *, now: datetime | None = None, ttl: timedelta = TTL_FX
) -> Rates | None:
    rows = conn.execute("SELECT currency, rate, fetched_at FROM fx_rate").fetchall()
    if not rows:
        return None
    newest = max(db.parse_dt(r["fetched_at"]) for r in rows)
    if (now or datetime.now()) - newest > ttl:
        return None
    return Rates(
        base="EUR",
        rates={r["currency"]: float(r["rate"]) for r in rows},
        fetched_at=newest,
    )


async def current_rates(
    conn: sqlite3.Connection, *, now: datetime | None = None, client: Any = None
) -> Rates:
    """Cache, then ECB, then the checked-in snapshot. Never raises.

    "Never raises" has to hold per tier, not just for the happy path: a single
    unreadable `fetched_at` in the table would otherwise take down every search
    that touches a foreign currency. Each tier logs and hands over to the next.
    """
    try:
        cached = load_rates(conn, now=now)
    except Exception as exc:  # noqa: BLE001 - unlesbare Zeile, kein Abbruch
        logger.warning("FX: gespeicherte Kurse unlesbar (%s), frage die EZB", exc)
        cached = None
    if cached is not None:
        return cached
    try:
        fresh = await fx.fetch_ecb_rates(client)
    except Exception as exc:  # noqa: BLE001 - jede Stoerung endet im Snapshot
        logger.warning("FX: %s, nutze den Snapshot", exc)
        return _fallback()
    try:
        save_rates(conn, fresh, now=now)
    except Exception as exc:  # noqa: BLE001 - gerechnet wird trotzdem
        logger.warning("FX: Kurse nicht gespeichert (%s)", exc)
    return fresh


def _fallback() -> Rates:
    """Die letzte Stufe. Auch sie darf nicht werfen, sonst war alles umsonst."""
    try:
        return fx.load_fallback()
    except Exception as exc:  # noqa: BLE001
        logger.error("FX: auch der Snapshot fehlt (%s), keine Umrechnung", exc)
        return Rates(base="EUR", rates={})
