"""Die Nachbesserung der bestehenden Beobachtungen, gegen eine Wegwerf-Datei.

Die Migration fasst echte Daten an, die niemand zurueckholen kann. Sie muss
deshalb zweimal laufen duerfen, ohne beim zweiten Mal etwas anderes zu tun als
beim ersten.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from flightopt.storage import db

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_baseline_quality.py"

TRAVEL = date(2026, 11, 10)
OBSERVED = datetime(2026, 9, 8, 12, 0)


def load_module():
    spec = importlib.util.spec_from_file_location("migrate_baseline_quality", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def old_shaped_db(path: Path) -> None:
    """Eine Datei, wie sie vor der Aenderung aussah: ohne `is_indicative`.

    Genau der Fall, den die Migration vorfindet - inklusive der gemischten
    Baseline, die dabei entstanden ist.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE price_observation (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at        TEXT NOT NULL,
            source             TEXT NOT NULL,
            entity_type        TEXT NOT NULL,
            entity_key         TEXT NOT NULL,
            travel_date        TEXT NOT NULL,
            return_or_nights   TEXT,
            party_size         INTEGER NOT NULL DEFAULT 1,
            currency           TEXT NOT NULL,
            price_total_minor  INTEGER NOT NULL,
            is_estimate        INTEGER NOT NULL DEFAULT 0,
            raw_hash           TEXT
        );
        CREATE TABLE price_baseline (
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
    )
    rows = [("aegean", 20000)] * 5 + [("kiwi", 9000)] * 10
    for source, price in rows:
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
            "travel_date, party_size, currency, price_total_minor, is_estimate) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), source, "flight", "BER|ATH",
             TRAVEL.isoformat(), 1, "EUR", price, 1),
        )
    conn.execute(
        "INSERT INTO price_baseline VALUES('flight','BER|ATH',1,'60-119','EUR',"
        "9000,0,15,'2026-09-08T12:00:00')"
    )
    conn.commit()
    conn.close()


def run(path: Path) -> dict:
    module = load_module()
    before = module.old_flight_baselines(path)
    conn = db.connect(path)
    result = module.migrate(conn, before)
    conn.close()
    return result


def test_the_migration_marks_the_portal_rows_and_rebuilds_the_baseline(tmp_path):
    path = tmp_path / "flightopt.db"
    old_shaped_db(path)

    result = run(path)

    assert result["marked"] == 10
    assert result["indicative"] == 10
    # Die gemischte Zeile aus `price_baseline` ist weg, an ihrer Stelle steht
    # eine Baseline aus den fuenf echten Tarifen.
    assert result["dropped"] == 1
    assert result["written"] == 1
    (median_minor, n, is_estimate), = result["after"].values()
    assert (median_minor, n, is_estimate) == (20000, 5, 1)


def test_a_second_run_changes_nothing(tmp_path):
    path = tmp_path / "flightopt.db"
    old_shaped_db(path)

    first = run(path)
    second = run(path)

    assert second["marked"] == 0
    assert second["dropped"] == 0
    assert second["indicative"] == first["indicative"]
    assert second["written"] == first["written"]
    assert second["after"] == first["after"]


def test_the_migration_leaves_the_hotel_rows_alone(tmp_path):
    path = tmp_path / "flightopt.db"
    old_shaped_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
        "travel_date, return_or_nights, party_size, currency, price_total_minor, "
        "is_estimate) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (OBSERVED.isoformat(timespec="seconds"), "trivago", "hotel", "GR|trivago:melia",
         TRAVEL.isoformat(), "1", 2, "EUR", 9000, 1),
    )
    conn.execute(
        "INSERT INTO price_baseline VALUES('hotel','GR|trivago:melia',1,'60-119','EUR',"
        "9000,200,5,'2026-09-08T12:00:00')"
    )
    conn.commit()
    conn.close()

    run(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    hotel_obs = conn.execute(
        "SELECT is_indicative FROM price_observation WHERE entity_type='hotel'"
    ).fetchall()
    hotel_baseline = conn.execute(
        "SELECT COUNT(*) c FROM price_baseline WHERE entity_type='hotel'"
    ).fetchone()["c"]
    conn.close()

    # Hotelquellen sind auf ihre Weise auch naeherungsweise, aber das steckt
    # bei ihnen in `is_estimate` und traegt ihre eigenen Baselines. Ein
    # Kennzeichen hier wuerde ihnen jede Vergleichsgruppe nehmen.
    assert [row["is_indicative"] for row in hotel_obs] == [0]
    assert hotel_baseline == 1


def test_a_break_in_the_middle_leaves_the_baselines_standing(tmp_path):
    """`DELETE FROM flight_baseline` lief ausserhalb jeder Transaktion.

    Die Verbindung steht auf `isolation_level=None`, jede Anweisung war also
    sofort endgueltig. Brach das Neurechnen danach ab, waren die alten
    Baselines weg und die neuen nicht da - und zwar dauerhaft.
    """
    path = tmp_path / "flightopt.db"
    old_shaped_db(path)
    run(path)

    module = load_module()
    conn = db.connect(path)
    try:
        before_rows = conn.execute("SELECT COUNT(*) c FROM flight_baseline").fetchone()["c"]
        marked_before = conn.execute(
            "SELECT COUNT(*) c FROM price_observation WHERE is_indicative=1"
        ).fetchone()["c"]

        def explode(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        module.refresh_flight_baselines = explode
        try:
            module.migrate(conn, {})
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("der Abbruch kam nicht durch")

        after_rows = conn.execute("SELECT COUNT(*) c FROM flight_baseline").fetchone()["c"]
        marked_after = conn.execute(
            "SELECT COUNT(*) c FROM price_observation WHERE is_indicative=1"
        ).fetchone()["c"]
        assert conn.in_transaction is False
    finally:
        conn.close()

    assert before_rows == 1
    assert after_rows == before_rows
    assert marked_after == marked_before
