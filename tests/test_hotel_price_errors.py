"""Preisfehler gegen echte Beobachtungen: Eigenhistorie, Peer-Gruppe, Vorfilter."""

from __future__ import annotations

from datetime import date, datetime

from flightopt.domain.fx import Rates
from flightopt.storage import db
from flightopt.storage.baseline import detect_price_signal, refresh_baselines

TRAVEL = date(2026, 11, 10)
OBSERVED = datetime(2026, 9, 8, 12, 0)
RATES = Rates(base="EUR", rates={"USD": 1.10, "CHF": 0.94, "JPY": 165.0})

# Median 90 Euro, Streuung 8 Euro. Genau die Lage, an der gemessen wurde, dass
# 60 Euro und 9 Euro beide auf `cheap` landen.
SPREAD = [7000, 7600, 8200, 8400, 8800, 9000, 9200, 9600, 9800, 10400, 11000]
FLAT = [9000] * 11
TIGHT = [8800, 8900, 8900, 9000, 9000, 9000, 9000, 9000, 9100, 9100, 9200]


def a_property(conn, *, key="trivago:melia", name="Melia Athens", city="Athens",
               stars=4, cc="GR") -> None:
    conn.execute(
        "INSERT INTO hotel_property(property_key, source, name, city, country, "
        "country_code, stars, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        (key, "trivago", name, city, "Greece", cc, stars,
         "2026-09-01T10:00:00", "2026-09-08T10:00:00"),
    )


def observe(conn, prices, *, key="trivago:melia", cc="GR", party_size=2, nights=1,
            currency="EUR") -> None:
    for price in prices:
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
            "travel_date, return_or_nights, party_size, currency, price_total_minor) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), "trivago", "hotel", f"{cc}|{key}",
             TRAVEL.isoformat(), str(nights), party_size, currency, int(price)),
        )


def signal(conn, price_minor, *, key="trivago:melia", cc="GR", party_size=2,
           nights=1, **kwargs):
    return detect_price_signal(
        conn, f"{cc}|{key}", TRAVEL, price_minor,
        observed_at=OBSERVED, entity_type="hotel",
        party_size=party_size, nights=nights, **kwargs,
    )


def prepared(tmp_path, prices, **kwargs):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, prices, **kwargs)
    refresh_baselines(conn, entity_type="hotel", now=OBSERVED)
    return conn


def test_sixty_euro_is_a_bargain_and_nine_euro_is_a_price_error(tmp_path):
    conn = prepared(tmp_path, SPREAD)

    bargain = signal(conn, 6000)
    broken = signal(conn, 900)

    assert (bargain["tier"], bargain["status"]) == ("cheap", "cheap")
    assert (broken["tier"], broken["status"]) == ("error", "cheap")
    assert broken["basis"] == "own"
    assert broken["n"] == 11
    conn.close()


def test_a_mad_of_zero_does_not_divide_by_zero_and_still_finds_the_error(tmp_path):
    conn = prepared(tmp_path, FLAT)

    bargain = signal(conn, 6000)
    broken = signal(conn, 900)

    assert bargain["mad_minor"] == 0
    assert bargain["tier"] == "cheap"
    assert broken["tier"] == "error"
    assert broken["reason"] == "unter 30 Prozent des Medians"
    conn.close()


def test_a_shifted_decimal_point_is_sorted_out_instead_of_reported(tmp_path):
    conn = prepared(tmp_path, TIGHT)

    verdict = signal(conn, 90)

    assert verdict["tier"] == "encoding_suspect"
    assert verdict["tier"] != "error"
    conn.close()


def test_a_price_on_the_dollar_rate_is_sorted_out_instead_of_reported(tmp_path):
    conn = prepared(tmp_path, TIGHT)

    verdict = signal(conn, round(9000 / 1.10), rates=RATES)

    assert verdict["tier"] == "encoding_suspect"
    assert verdict["reason"] == "Waehrungsverwechslung, Faktor entspricht USD"
    conn.close()


def test_thin_history_is_measured_against_the_peer_group(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    # Sechs eigene Beobachtungen tragen nicht, reichen aber fuer eine eigene
    # Baseline. Zwei Nachbarn derselben Stadt und Kategorie tragen.
    a_property(conn, key="trivago:neu", name="Neues Haus")
    observe(conn, [8600, 8800, 9000, 9000, 9200, 9400], key="trivago:neu")
    for peer in ("trivago:peer1", "trivago:peer2"):
        a_property(conn, key=peer, name=f"Hotel {peer}")
        observe(conn, [19000, 19500, 20000, 20000, 20500, 21000, 20000], key=peer)
    refresh_baselines(conn, entity_type="hotel", now=OBSERVED)

    own = conn.execute(
        "SELECT n FROM hotel_baseline WHERE scope='own' AND group_key=?",
        ("GR|trivago:neu",),
    ).fetchone()
    verdict = signal(conn, 5900, key="trivago:neu")

    assert own["n"] == 6
    assert verdict["basis"] == "peer"
    assert verdict["n"] == 20
    assert verdict["median_minor"] > 15000
    assert verdict["tier"] == "error"
    conn.close()


def test_occupancy_and_nights_are_separate_distributions(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)
    observe(conn, FLAT, party_size=2, nights=1)
    observe(conn, [20000] * 11, party_size=4, nights=1)
    observe(conn, [27000] * 11, party_size=2, nights=3)
    refresh_baselines(conn, entity_type="hotel", now=OBSERVED)

    single = signal(conn, 9000, party_size=2, nights=1)
    family = signal(conn, 9000, party_size=4, nights=1)
    longer = signal(conn, 15000, party_size=2, nights=3)

    assert single["median_minor"] == 9000
    assert family["median_minor"] == 20000
    assert longer["median_minor"] == 27000
    # Dieselbe Zahl, anderes Urteil: 90 Euro sind fuer zwei Personen normal
    # und fuer vier ein Angebot.
    assert (single["tier"], family["tier"]) == ("normal", "cheap")
    conn.close()


def test_a_dorm_is_marked_and_not_measured_against_the_hotel_floor(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn, key="trivago:dorm", name="Athens City Hostel")

    verdict = signal(conn, 1800, key="trivago:dorm")

    # 18 Euro liegen unter jeder Hotelschranke, fuer ein Bett im Schlafsaal
    # sind sie der normale Preis. Markieren statt melden.
    assert verdict["category_suspect"]
    assert verdict["tier"] == "unknown"
    conn.close()


def test_the_plausibility_floor_works_on_the_very_first_day(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)

    verdict = signal(conn, 1200)

    assert verdict["tier"] == "error"
    assert verdict["reason"] == "unter der Schranke von 45 Euro je Nacht"
    assert (verdict["status"], verdict["basis"], verdict["n"]) == ("unknown", "none", 0)
    conn.close()


def test_a_hotel_without_history_and_above_the_floor_stays_unknown(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    a_property(conn)

    assert signal(conn, 11800)["tier"] == "unknown"
    conn.close()


def test_the_flight_detector_keeps_its_three_tiers(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    for price in (19000, 19500, 20000, 20000, 20500, 21000, 20000):
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
            "travel_date, return_or_nights, party_size, currency, price_total_minor) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), "ryanair", "flight", "BER|ATH",
             TRAVEL.isoformat(), "2026-11-17", 1, "EUR", price),
        )
    refresh_baselines(conn, now=OBSERVED)

    # Derselbe Abstand zum Median waere bei einem Hotel ein Preisfehler.
    verdict = detect_price_signal(conn, "BER|ATH", TRAVEL, 1000, observed_at=OBSERVED)
    plain = detect_price_signal(conn, "BER|ATH", TRAVEL, 20000, observed_at=OBSERVED)
    blank = detect_price_signal(conn, "BER|CDG", TRAVEL, 1000, observed_at=OBSERVED)

    assert verdict["status"] == "cheap"
    assert verdict["tier"] == "cheap"
    assert (verdict["median_minor"], verdict["mad_minor"], verdict["n"]) == (20000, 500, 7)
    assert plain["status"] == "normal"
    assert blank == {
        "status": "unknown", "price_minor": 1000,
        "tier": "unknown", "reason": "keine Baseline", "basis": "none", "n": 0,
    }
    conn.close()
