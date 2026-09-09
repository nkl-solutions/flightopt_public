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
    # Der `property_key` allein: er traegt Quelle und Objekt-ID und ist damit
    # schon eindeutig. Das Land davor war kein Gewinn, nur ein Risiko.
    assert row["entity_key"] == "trivago:abc"
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


async def test_an_answer_without_a_country_does_not_overwrite_a_known_code(tmp_path):
    """'XX' ist ein Platzhalter und keine Auskunft.

    Es ist aber auch nicht NULL, also liess COALESCE es durch: eine Quelle,
    die das Land einmal nicht mitschickt, ersetzte einen richtigen ISO-Code
    durch den Platzhalter. Danach zeigt der Nachbarschluessel der Baseline auf
    eine andere Grundgesamtheit, und aus einem Urteil wird 'unknown' oder ein
    Urteil gegen die falschen Nachbarn.
    """
    conn = db.connect(tmp_path / "cc.db")

    upsert_property(conn, offer(), now="2026-09-01T10:00:00")
    upsert_property(conn, offer(country=None), now="2026-09-08T10:00:00")

    row = conn.execute("SELECT * FROM hotel_property").fetchone()
    assert row["country_code"] == "GR"
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


def test_a_missing_country_does_not_split_the_price_history():
    """Der Beobachtungsschluessel muss ueber die Zeit stabil bleiben.

    Liefert eine Quelle das Land einmal nicht, wanderte dieselbe Unterkunft
    von 'GR|trivago:abc' nach 'XX|trivago:abc'. Ihre Historie zerfiel damit in
    zwei Grundgesamtheiten, von denen keine mehr die fuenf Beobachtungen
    erreicht, ab denen es eine Baseline gibt.

    Ein frueherer Fix schuetzt die Stammdaten vor demselben Loch. Am
    Beobachtungsschluessel half er nicht: der entsteht am Angebot und nicht in
    der Tabelle.
    """
    with_country = offer()
    without_country = offer(country=None)

    assert with_country.entity_key == without_country.entity_key
    assert with_country.entity_key == "trivago:abc"
    # Das Land geht dabei nicht verloren, es steht weiter am Angebot und in
    # den Stammdaten.
    assert with_country.country_code == "GR"


async def test_the_baseline_holds_a_property_together_across_a_missing_country(tmp_path):
    conn = db.connect(tmp_path / "split.db")
    observed = datetime(2026, 9, 8, 12, 0)
    mixed = [
        offer(price_total=Money(12000 + step * 50, "EUR"),
              country="Greece" if step % 2 else None)
        for step in range(10)
    ]

    await record_offers(conn, mixed, rates=RATES, observed_at=observed)
    assert refresh_baselines(conn, entity_type="hotel", now=observed) == 1

    keys = {row["entity_key"] for row in
            conn.execute("SELECT DISTINCT entity_key FROM price_observation")}
    assert keys == {"trivago:abc"}
    assert signal_for(conn, offer(), observed_at=observed)["n"] == 10
    conn.close()


async def test_a_trivago_row_is_written_as_an_indicative_estimate(tmp_path):
    """Zwei Kennzeichen, zwei Aussagen - und beide gehoeren in die Historie.

    `is_indicative` sagt, ob die **Quelle** ein Vergleichsportal mit bekanntem
    Aufschlag ist. `is_estimate` sagt, ob dieser eine **Preis** ein Richtwert
    ist. Bisher stand in der ersten Spalte bei jeder Hotelzeile eine Null, also
    stand dort nichts. Die Historie ist fortschreibend: was heute nicht
    mitgeschrieben wird, ist morgen nicht nachtragbar.
    """
    conn = db.connect(tmp_path / "h.db")

    await record_offers(conn, [offer()], rates=RATES)

    row = conn.execute(
        "SELECT source, is_indicative, is_estimate FROM price_observation"
    ).fetchone()
    assert row["source"] == "trivago"
    assert row["is_indicative"] == 1
    assert row["is_estimate"] == 1
    conn.close()


async def test_a_booking_row_is_written_as_a_merchant_price(tmp_path):
    """Booking ist kein Vergleichsportal, und ein belegter Preis kein Richtwert."""
    conn = db.connect(tmp_path / "h.db")

    await record_offers(
        conn,
        [offer(source="booking", property_key="booking:gr/sparta", indicative=False)],
        rates=RATES,
    )

    row = conn.execute(
        "SELECT source, is_indicative, is_estimate FROM price_observation"
    ).fetchone()
    assert row["source"] == "booking"
    assert row["is_indicative"] == 0
    assert row["is_estimate"] == 0
    conn.close()


async def test_a_booking_price_with_excluded_charges_stays_an_estimate(tmp_path):
    """Dieselbe Quelle, andere Antwort: das Kennzeichen haengt am Angebot."""
    conn = db.connect(tmp_path / "h.db")

    await record_offers(
        conn,
        [offer(source="booking", property_key="booking:gr/sparta", indicative=True)],
        rates=RATES,
    )

    row = conn.execute(
        "SELECT is_indicative, is_estimate FROM price_observation"
    ).fetchone()
    assert row["is_indicative"] == 0
    assert row["is_estimate"] == 1
    conn.close()


async def test_an_unknown_source_counts_as_indicative(tmp_path):
    """Im Zweifel Richtwert. Eine Quelle, die niemand kennt, ist kein Beleg."""
    conn = db.connect(tmp_path / "h.db")

    await record_offers(conn, [offer(source="irgendwas")], rates=RATES)

    row = conn.execute("SELECT is_indicative FROM price_observation").fetchone()
    assert row["is_indicative"] == 1
    conn.close()


def test_the_signal_names_which_population_the_price_belongs_to(tmp_path):
    """Ein Haendlerpreis und ein Richtwert sind nicht dasselbe Produkt."""
    conn = db.connect(tmp_path / "h.db")

    estimate = signal_for(conn, offer())
    merchant = signal_for(
        conn, offer(source="booking", property_key="booking:gr/x", indicative=False)
    )

    assert estimate["population"] == "estimate"
    assert merchant["population"] == "verified"
    conn.close()


async def test_a_merchant_price_is_never_measured_against_estimates(tmp_path):
    """Die Hotel-Baseline trennt die beiden Grundgesamtheiten seit dem 2026-09-09.

    Vorher lagen Richtwerte und Haendlerpreise in einer Verteilung, und an
    jedem Urteil ueber einen Haendlerpreis hing der Zusatz "Vergleichsgruppe
    enthaelt auch Richtwerte". Der Zusatz war ehrlich, aber ein Zusatz ist
    keine Trennung: der Median lag trotzdem daneben.

    Jetzt bekommt ein Haendlerpreis, fuer dessen Art es keine Historie gibt,
    kein Urteil aus der Baseline. Das ist dieselbe Entscheidung wie bei den
    Fluegen - gepruefte Legs bleiben ohne Basis, bis fuenf gepruefte Preise
    beisammen sind - und die Alternative waere genau der alte Fehler.
    """
    conn = db.connect(tmp_path / "h.db")
    for day in range(1, 8):
        await record_offers(
            conn,
            [offer(price_total=Money(9000 + day, "EUR"))],
            rates=RATES,
            observed_at=datetime(2026, 9, day, 12, 0),
        )
    refresh_baselines(conn, entity_type="hotel")

    verdict = signal_for(
        conn,
        offer(price_total=Money(9000, "EUR"), indicative=False, source="booking"),
        observed_at=datetime(2026, 9, 8, 12, 0),
    )

    assert verdict["population"] == "verified"
    assert verdict["population_split"] is True
    # Sieben Richtwerte um 90 Euro liegen in der Datei. Sie sagen ueber einen
    # Haendlerpreis nichts, also kommt hier auch keine Zahl heraus.
    assert verdict["tier"] == "unknown"
    assert verdict["median_minor"] is None
    conn.close()


async def test_an_estimate_still_finds_the_history_of_its_own_kind(tmp_path):
    """Die Trennung kostet den Richtwerten nichts - sie sind unter sich."""
    conn = db.connect(tmp_path / "h.db")
    for day in range(1, 8):
        await record_offers(
            conn,
            [offer(price_total=Money(9000 + day, "EUR"))],
            rates=RATES,
            observed_at=datetime(2026, 9, day, 12, 0),
        )
    refresh_baselines(conn, entity_type="hotel")

    verdict = signal_for(
        conn,
        offer(price_total=Money(9004, "EUR")),
        observed_at=datetime(2026, 9, 8, 12, 0),
    )

    assert verdict["population"] == "estimate"
    assert verdict["median_minor"] == 9004
    assert verdict["n"] == 7
    conn.close()


def test_no_judgement_carries_the_old_mixing_note_any_more(tmp_path):
    """Der Zusatz faellt von selbst weg, sobald wirklich getrennt wird."""
    conn = db.connect(tmp_path / "h.db")

    estimate = signal_for(conn, offer())
    merchant = signal_for(conn, offer(indicative=False, source="booking"))

    assert "Richtwerte" not in estimate["reason"]
    assert "Richtwerte" not in merchant["reason"]
    conn.close()
