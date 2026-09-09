"""Der Bestand wandert auf den stabilen Hotel-Schluessel.

Die Migration fasst echte Daten an, die niemand zurueckholen kann. Sie muss
deshalb zweimal laufen duerfen, ohne beim zweiten Mal etwas anderes zu tun als
beim ersten.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

from flightopt.storage import db
from flightopt.storage.baseline import ensure_hotel_baseline

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_hotel_entity_keys.py"

TRAVEL = date(2026, 11, 10)
OBSERVED = datetime(2026, 9, 8, 12, 0)


def load_module():
    spec = importlib.util.spec_from_file_location("migrate_hotel_entity_keys", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def observe(conn, entity_key: str, price: int) -> None:
    conn.execute(
        "INSERT INTO price_observation("
        "observed_at, source, entity_type, entity_key, travel_date, "
        "return_or_nights, party_size, currency, price_total_minor, is_estimate) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            OBSERVED.isoformat(timespec="seconds"), "trivago", "hotel", entity_key,
            TRAVEL.isoformat(), "1", 2, "EUR", price, 1,
        ),
    )


def split_history(path: Path):
    """Genau der Schaden: dieselbe Unterkunft unter zwei Schluesseln.

    Drei Beobachtungen mit Land, drei ohne. Getrennt erreicht keine Seite die
    fuenf, ab denen es eine Baseline gibt.
    """
    conn = db.connect(path)
    for price in (12000, 12500, 13000):
        observe(conn, "GR|trivago:abc", price)
    for price in (12200, 12700, 13200):
        observe(conn, "XX|trivago:abc", price)
    conn.execute(
        "INSERT INTO hotel_property(property_key, source, name, city, country, "
        "country_code, stars, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        ("trivago:abc", "trivago", "Melia Athens", "Athens", "Greece", "GR", 4,
         "2026-09-01T10:00:00", "2026-09-08T10:00:00"),
    )
    ensure_hotel_baseline(conn)
    conn.commit()
    return conn


def test_two_halves_of_one_property_become_one_history(tmp_path):
    path = tmp_path / "hotels.db"
    conn = split_history(path)
    before = {row["entity_key"] for row in conn.execute(
        "SELECT DISTINCT entity_key FROM price_observation")}
    assert before == {"GR|trivago:abc", "XX|trivago:abc"}
    conn.close()

    module = load_module()
    conn = db.connect(path)
    changed, _ = module.migrate(conn)

    assert changed == 6
    keys = {row["entity_key"] for row in conn.execute(
        "SELECT DISTINCT entity_key FROM price_observation")}
    assert keys == {"trivago:abc"}
    # Sechs Beobachtungen unter einem Schluessel reichen fuer eine Baseline,
    # zweimal drei reichten fuer keine.
    row = conn.execute(
        "SELECT group_key, n FROM hotel_baseline WHERE scope='own'"
    ).fetchone()
    assert row["group_key"] == "trivago:abc"
    assert row["n"] == 6
    conn.close()


def test_the_orphaned_own_baselines_are_dropped_and_the_peers_stay(tmp_path):
    path = tmp_path / "hotels.db"
    conn = split_history(path)
    conn.execute(
        "INSERT INTO hotel_baseline(scope, group_key, weekday, leadtime_bucket, "
        "stay_key, currency, population, median_minor, mad_minor, n, computed_at) "
        "VALUES('own','GR|trivago:abc',1,'60-119','p2n1','EUR','estimate',12500,300,5,'x')",
    )
    conn.execute(
        "INSERT INTO hotel_baseline(scope, group_key, weekday, leadtime_bucket, "
        "stay_key, currency, population, median_minor, mad_minor, n, computed_at) "
        "VALUES('peer','GR|Athens|4',1,'60-119','p2n1','EUR','estimate',12900,400,9,'x')",
    )
    conn.commit()
    conn.close()

    module = load_module()
    conn = db.connect(path)
    _, dropped = module.migrate(conn)

    assert dropped == 1
    stale = conn.execute(
        "SELECT COUNT(*) AS n FROM hotel_baseline "
        "WHERE scope='own' AND group_key LIKE '%|%'"
    ).fetchone()
    assert stale["n"] == 0
    peers = conn.execute(
        "SELECT COUNT(*) AS n FROM hotel_baseline WHERE scope='peer'"
    ).fetchone()
    # Der Peer-Schluessel ist ein anderer und wird nicht mit weggeraeumt.
    assert peers["n"] >= 1
    conn.close()


def test_a_second_run_changes_nothing(tmp_path):
    path = tmp_path / "hotels.db"
    conn = split_history(path)
    conn.close()

    module = load_module()
    conn = db.connect(path)
    module.migrate(conn)
    after_first = conn.execute(
        "SELECT entity_key, price_total_minor FROM price_observation ORDER BY id"
    ).fetchall()

    changed, dropped = module.migrate(conn)

    assert (changed, dropped) == (0, 0)
    after_second = conn.execute(
        "SELECT entity_key, price_total_minor FROM price_observation ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in after_first] == [tuple(r) for r in after_second]
    conn.close()
