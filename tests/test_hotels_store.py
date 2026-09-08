"""Beobachtungen, Stammdaten und die Umrechnung nach Euro."""

from __future__ import annotations

from datetime import date, datetime

from flightopt.domain.fx import Rates
from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer
from flightopt.hotels.store import record_offers, signal_for, upsert_property
from flightopt.storage import db
from flightopt.storage.baseline import refresh_baselines

RATES = Rates(base="EUR", rates={"USD": 1.10, "CHF": 0.94})


def offer(**kwargs) -> HotelOffer:
    base = dict(
        source="trivago",
        property_key="trivago:abc",
        name="Melia Athens",
        arrival=date(2026, 11, 10),
        departure=date(2026, 11, 11),
        price_total=Money(11800, "EUR"),
        stars=4,
        city="Athens",
        country="Greece",
        review_rating=8.5,
        review_count=8358,
        url="https://example.invalid/melia",
        party_size=2,
    )
    base.update(kwargs)
    return HotelOffer(**base)


async def test_an_offer_becomes_a_hotel_observation_with_nights_and_occupancy(tmp_path):
    conn = db.connect(tmp_path / "h.db")

    report = await record_offers(
        conn,
        [offer(departure=date(2026, 11, 13))],
        rates=RATES,
        observed_at=datetime(2026, 9, 8, 12, 0),
    )

    assert report.written == 1
    row = conn.execute("SELECT * FROM price_observation").fetchone()
    assert row["entity_type"] == "hotel"
    # <cc>|<property_key>: die Baseline rechnet je Objekt, nicht je Stadt.
    assert row["entity_key"] == "GR|trivago:abc"
    assert row["travel_date"] == "2026-11-10"
    assert row["return_or_nights"] == "3"
    assert row["party_size"] == 2
    assert (row["currency"], row["price_total_minor"]) == ("EUR", 11800)
    assert row["is_estimate"] == 1
    conn.close()


async def test_a_foreign_price_is_stored_in_euro_and_keeps_its_original(tmp_path):
    conn = db.connect(tmp_path / "h.db")

    report = await record_offers(
        conn, [offer(price_total=Money(28700, "USD"))], rates=RATES
    )

    row = conn.execute("SELECT currency, price_total_minor FROM price_observation").fetchone()
    assert (row["currency"], row["price_total_minor"]) == ("EUR", 26091)
    written = report.offers[0]
    assert written.price_total == Money(28700, "USD")
    assert written.price_eur == Money(26091, "EUR")
    assert written.converted is True
    conn.close()


async def test_a_currency_without_a_rate_is_skipped_instead_of_written_raw(tmp_path):
    conn = db.connect(tmp_path / "h.db")

    report = await record_offers(
        conn, [offer(price_total=Money(2870000, "JPY"))], rates=RATES
    )

    assert report.written == 0
    assert report.skipped == ["Melia Athens: kein Kurs fuer JPY"]
    assert conn.execute("SELECT COUNT(*) c FROM price_observation").fetchone()["c"] == 0
    conn.close()


async def test_the_property_row_keeps_first_seen_and_never_loses_filled_fields(tmp_path):
    conn = db.connect(tmp_path / "h.db")

    upsert_property(conn, offer(), now="2026-09-01T10:00:00")
    upsert_property(
        conn,
        offer(name="Melia Athens Hotel", city=None, lat=None, review_count=9000),
        now="2026-09-08T10:00:00",
    )

    row = conn.execute("SELECT * FROM hotel_property").fetchone()
    assert row["first_seen"] == "2026-09-01T10:00:00"
    assert row["last_seen"] == "2026-09-08T10:00:00"
    assert row["name"] == "Melia Athens Hotel"
    # Eine Antwort ohne Stadt loescht die bekannte Stadt nicht.
    assert row["city"] == "Athens"
    assert row["country_code"] == "GR"
    assert row["review_count"] == 9000
    conn.close()


async def test_the_flight_detector_reads_hotel_baselines_unchanged(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    observed = datetime(2026, 9, 8, 12, 0)
    # Zehn Naechte um 120 Euro bauen die Baseline, ein Ausreisser prueft sie.
    await record_offers(
        conn,
        [offer(price_total=Money(12000 + step * 50, "EUR")) for step in range(10)],
        rates=RATES,
        observed_at=observed,
    )
    assert refresh_baselines(conn, entity_type="hotel", now=observed) == 1

    cheap = signal_for(conn, offer(price_total=Money(3000, "EUR")), observed_at=observed)
    plain = signal_for(conn, offer(price_total=Money(12200, "EUR")), observed_at=observed)

    assert cheap["status"] == "cheap"
    assert plain["status"] == "normal"
    assert cheap["n"] == 10
    conn.close()


def test_without_a_baseline_the_signal_says_unknown_and_not_normal(tmp_path):
    conn = db.connect(tmp_path / "h.db")

    assert signal_for(conn, offer())["status"] == "unknown"
    conn.close()
