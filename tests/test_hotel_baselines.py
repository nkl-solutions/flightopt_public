"""Die zwei Hotel-Ebenen: eigene Historie je Objekt, Peer-Gruppe je Stadt.

Dazu die dritte Trennung, die seit dem 2026-09-09 dazugehoert: Richtwerte und
Haendlerpreise liegen nicht mehr in derselben Verteilung.
"""

from __future__ import annotations

from datetime import date, datetime

from flightopt.storage import db
from flightopt.storage.baseline import (
    POPULATION_ESTIMATE,
    POPULATION_VERIFIED,
    detect_price_signal,
    ensure_hotel_baseline,
    refresh_baselines,
    refresh_hotel_baselines,
)

TRAVEL = date(2026, 11, 10)
OBSERVED = datetime(2026, 9, 8, 12, 0)

OLD_HOTEL_BASELINE_SCHEMA = """
-- Die Tabelle, wie sie bis zum 2026-09-09 aussah: ohne `population`. Absichtlich
-- wortwoertlich hier und nicht aus dem heutigen Schema abgeleitet - eine Kopie
-- der Vergangenheit aendert sich nicht mehr, ein abgeleiteter Text schon.
CREATE TABLE IF NOT EXISTS hotel_baseline (
    scope            TEXT NOT NULL,
    group_key        TEXT NOT NULL,
    weekday          INTEGER NOT NULL,
    leadtime_bucket  TEXT NOT NULL,
    stay_key         TEXT NOT NULL,
    currency         TEXT NOT NULL,
    median_minor     INTEGER NOT NULL,
    mad_minor        INTEGER NOT NULL,
    n                INTEGER NOT NULL,
    computed_at      TEXT NOT NULL,
    PRIMARY KEY(scope, group_key, weekday, leadtime_bucket, stay_key, currency)
);
"""


def a_property(conn, *, key="trivago:melia", name="Melia Athens", city="Athens",
               stars=4, cc="GR") -> None:
    conn.execute(
        "INSERT INTO hotel_property(property_key, source, name, city, country, "
        "country_code, stars, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        (key, "trivago", name, city, "Greece", cc, stars,
         "2026-09-01T10:00:00", "2026-09-08T10:00:00"),
    )


def observe(conn, prices, *, key="trivago:melia", cc="GR", party_size=2, nights=1,
            is_estimate=0) -> None:
    for price in prices:
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
            "travel_date, return_or_nights, party_size, currency, price_total_minor, "
            "is_estimate) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), "trivago", "hotel", f"{cc}|{key}",
             TRAVEL.isoformat(), str(nights), party_size, "EUR", int(price),
             int(is_estimate)),
        )


def rows(conn, scope):
    return conn.execute(
        "SELECT group_key, stay_key, population, median_minor, mad_minor, n "
        "FROM hotel_baseline WHERE scope=? ORDER BY group_key, stay_key, population",
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


# -- Die dritte Trennung: Richtwert oder Haendlerpreis ----------------------


def test_estimates_and_merchant_prices_never_share_a_distribution(tmp_path):
    """Dieselbe Trennung, die bei den Fluegen die Mediane deutlich verschob.

    Ein Richtwert eines Vergleichsportals und der Preis, den der Haendler
    selbst anzeigt, sind zwei Produkte. Der Median einer gemischten Verteilung
    misst weder das eine noch das andere - und der Fehler ist unsichtbar: kein
    Log, keine Ausnahme, nur eine Zahl, die daneben liegt.
    """
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [12000] * 5, is_estimate=1)
    observe(conn, [9000] * 5, is_estimate=0)

    refresh_hotel_baselines(conn, now=OBSERVED)
    own = {r["population"]: (r["median_minor"], r["n"]) for r in rows(conn, "own")}

    assert own == {POPULATION_ESTIMATE: (12000, 5), POPULATION_VERIFIED: (9000, 5)}
    conn.close()


def test_the_peer_group_splits_the_populations_too(tmp_path):
    """Sonst waere die Vermischung eine Ebene tiefer wieder da."""
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [12000] * 5, is_estimate=1)
    observe(conn, [9000] * 5, is_estimate=0)

    refresh_hotel_baselines(conn, now=OBSERVED)
    peer = {r["population"]: r["median_minor"] for r in rows(conn, "peer")}

    assert peer == {POPULATION_ESTIMATE: 12000, POPULATION_VERIFIED: 9000}
    conn.close()


def test_a_price_is_measured_against_its_own_population(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [12000] * 5, is_estimate=1)
    observe(conn, [9000] * 5, is_estimate=0)
    refresh_hotel_baselines(conn, now=OBSERVED)

    as_estimate = detect_price_signal(
        conn, "trivago:melia", TRAVEL, 12000, observed_at=OBSERVED,
        entity_type="hotel", party_size=2, nights=1, stars=4, is_estimate=True,
    )
    as_merchant = detect_price_signal(
        conn, "trivago:melia", TRAVEL, 9000, observed_at=OBSERVED,
        entity_type="hotel", party_size=2, nights=1, stars=4, is_estimate=False,
    )

    assert as_estimate["median_minor"] == 12000
    assert as_merchant["median_minor"] == 9000
    # Beide liegen genau auf ihrem eigenen Median. Gegen die andere Verteilung
    # gerechnet waere einer von beiden 25 Prozent daneben.
    assert (as_estimate["tier"], as_merchant["tier"]) == ("normal", "normal")
    conn.close()


def test_a_population_without_enough_points_gets_no_baseline_at_all(tmp_path):
    """Keine Zahl ist die richtige Antwort und keine Luecke.

    Die Alternative waere, den Haendlerpreis gegen Richtwerte zu messen - und
    genau das war der Fehler. Bei den Fluegen hiess dieselbe Entscheidung:
    gepruefte Legs bleiben vorerst ohne Basis.
    """
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, [12000] * 5, is_estimate=1)
    observe(conn, [9000] * 3, is_estimate=0)
    refresh_hotel_baselines(conn, now=OBSERVED)

    verdict = detect_price_signal(
        conn, "trivago:melia", TRAVEL, 9000, observed_at=OBSERVED,
        entity_type="hotel", party_size=2, nights=1, stars=4, is_estimate=False,
    )

    assert [r["population"] for r in rows(conn, "own")] == [POPULATION_ESTIMATE]
    assert verdict["tier"] == "unknown"
    assert verdict["median_minor"] is None
    conn.close()


def test_an_old_table_is_rebuilt_instead_of_carrying_mixed_rows(tmp_path):
    """Eine Datei aus der Zeit vor der Trennung darf nicht stumm weiterrechnen.

    Die alten Zeilen sind gemischt und lassen sich nachtraeglich keiner
    Grundgesamtheit zuordnen - sie enthalten beide. Weggeworfen werden sie
    trotzdem gefahrlos: `hotel_baseline` ist abgeleitet und wird aus der
    Beobachtungshistorie neu gerechnet, und die traegt `is_estimate` je Zeile.

    Ohne diesen Umbau liefe jeder Hotellauf gegen eine Tabelle ohne die neue
    Spalte und scheiterte an jedem einzelnen INSERT.
    """
    conn = db.connect(tmp_path / "h.db")
    conn.executescript(OLD_HOTEL_BASELINE_SCHEMA)
    conn.execute(
        "INSERT INTO hotel_baseline(scope, group_key, weekday, leadtime_bucket, "
        "stay_key, currency, median_minor, mad_minor, n, computed_at) "
        "VALUES('own','trivago:melia',1,'14-29','p2n1','EUR',9000,200,5,'2026-09-08')"
    )
    conn.commit()

    ensure_hotel_baseline(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(hotel_baseline)")}
    assert "population" in columns
    assert conn.execute("SELECT COUNT(*) c FROM hotel_baseline").fetchone()["c"] == 0
    # Und die neue Historie fuellt sie sofort wieder.
    a_property(conn)
    observe(conn, [9000] * 5)
    assert refresh_hotel_baselines(conn, now=OBSERVED) == 2
    conn.close()
