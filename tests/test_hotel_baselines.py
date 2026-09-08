"""Die zwei Hotel-Ebenen: eigene Historie je Objekt, Peer-Gruppe je Stadt."""

from __future__ import annotations

from datetime import date, datetime

from flightopt.storage import db
from flightopt.storage.baseline import refresh_baselines, refresh_hotel_baselines

TRAVEL = date(2026, 11, 10)
OBSERVED = datetime(2026, 9, 8, 12, 0)


def a_property(conn, *, key="trivago:melia", name="Melia Athens", city="Athens",
               stars=4, cc="GR") -> None:
    conn.execute(
        "INSERT INTO hotel_property(property_key, source, name, city, country, "
        "country_code, stars, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        (key, "trivago", name, city, "Greece", cc, stars,
         "2026-09-01T10:00:00", "2026-09-08T10:00:00"),
    )


def observe(conn, prices, *, key="trivago:melia", cc="GR", party_size=2, nights=1) -> None:
    for price in prices:
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
            "travel_date, return_or_nights, party_size, currency, price_total_minor) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), "trivago", "hotel", f"{cc}|{key}",
             TRAVEL.isoformat(), str(nights), party_size, "EUR", int(price)),
        )


def rows(conn, scope):
    return conn.execute(
        "SELECT group_key, stay_key, median_minor, mad_minor, n FROM hotel_baseline "
        "WHERE scope=? ORDER BY group_key, stay_key",
        (scope,),
    ).fetchall()


def test_every_observation_lands_in_its_own_and_in_a_peer_distribution(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [8600, 8800, 9000, 9200, 9400])

    assert refresh_hotel_baselines(conn, now=OBSERVED) == 2
    own = rows(conn, "own")
    peer = rows(conn, "peer")

    assert [r["group_key"] for r in own] == ["GR|trivago:melia"]
    assert [r["group_key"] for r in peer] == ["GR|athens|4"]
    assert (own[0]["median_minor"], own[0]["mad_minor"], own[0]["n"]) == (9000, 200, 5)
    conn.close()


def test_occupancy_and_nights_never_share_a_distribution(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [9000] * 5, party_size=2, nights=1)
    observe(conn, [20000] * 5, party_size=4, nights=1)
    observe(conn, [27000] * 5, party_size=2, nights=3)
    refresh_hotel_baselines(conn, now=OBSERVED)

    own = {r["stay_key"]: r["median_minor"] for r in rows(conn, "own")}

    assert own == {"p2n1": 9000, "p4n1": 20000, "p2n3": 27000}
    conn.close()


def test_a_dorm_keeps_its_own_history_but_stays_out_of_the_peer_group(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn, key="trivago:hotel", name="Grande Bretagne")
    observe(conn, [20000] * 5, key="trivago:hotel")
    a_property(conn, key="trivago:dorm", name="Athens City Hostel")
    observe(conn, [1800] * 5, key="trivago:dorm")
    refresh_hotel_baselines(conn, now=OBSERVED)

    peer = rows(conn, "peer")
    own = {r["group_key"]: r["median_minor"] for r in rows(conn, "own")}

    assert len(peer) == 1
    # Ein Bett fuer 18 Euro wuerde den Median der Vier-Sterne-Gruppe halbieren
    # und danach faellt kein echter Fehler mehr auf.
    assert (peer[0]["median_minor"], peer[0]["n"]) == (20000, 5)
    assert own["GR|trivago:dorm"] == 1800
    conn.close()


def test_a_group_below_the_minimum_gets_no_baseline_at_all(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [9000] * 4)

    assert refresh_hotel_baselines(conn, now=OBSERVED) == 0
    assert rows(conn, "own") == []
    conn.close()


def test_the_hotel_levels_ride_along_with_the_usual_refresh(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [9000] * 5)

    # Der Rueckgabewert zaehlt weiterhin nur die `price_baseline`-Zeilen.
    assert refresh_baselines(conn, entity_type="hotel", now=OBSERVED) == 1
    assert len(rows(conn, "own")) == 1
    assert len(rows(conn, "peer")) == 1
    conn.close()
