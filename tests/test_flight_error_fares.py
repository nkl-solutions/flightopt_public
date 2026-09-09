"""Die vierte Stufe fuer Fluege: wann ist ein billiger Flug ein Fehltarif.

Die Testfaelle sind bewusst mit echten Marktpreisen belegt und nicht mit
runden Zahlen: die teure Frage ist nicht "erkennt er den Fehler", sondern
"laesst er den Schlussverkauf in Ruhe". Wer zweimal umsonst geweckt wird,
schaltet ab.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from flightopt.hotels.signals import Baseline
from flightopt.hunt import errorfare
from flightopt.storage import db
from flightopt.storage.baseline import detect_price_signal, refresh_baselines

TRAVEL = date(2026, 11, 20)
OBSERVED = datetime(2026, 9, 9, 8, 0, 0)


def judge(price_minor: int, *, entity_key: str = "BER|BKK",
          baseline: Baseline | None = None, is_indicative: bool = False,
          currency: str = "EUR", party_size: int = 1):
    return errorfare.classify_flight(
        price_minor,
        baseline,
        entity_key=entity_key,
        currency=currency,
        party_size=party_size,
        is_indicative=is_indicative,
    )


# -- Ohne Historie: nur die Entfernung traegt ---------------------------------


def test_a_long_haul_for_forty_euro_is_flagged_without_any_history():
    """Der Fall aus dem Auftrag: 8622 km fuer 40 Euro, kein Vergleichswert."""
    verdict = judge(4000)

    assert verdict.tier == "error"
    assert "Entfernung" in verdict.reason or "km" in verdict.reason
    assert verdict.distance_km is not None and verdict.distance_km > 8000


def test_a_plausible_long_haul_fare_stays_quiet():
    """250 Euro nach Bangkok sind ein gutes Angebot und kein Fehler."""
    assert judge(25000).tier != "error"


def test_short_haul_without_history_is_never_an_error():
    """Neun Euro fuer 521 km sind ein Produkt und kein Versehen.

    Ohne Historie laesst sich ein Lockangebot von einem Fehltarif nicht
    unterscheiden - bei Ryanair und Wizz ist der einstellige Preis der
    Normalfall. Deshalb greift die Schranke ohne Historie erst ab
    `NO_HISTORY_MIN_KM`.
    """
    assert judge(900, entity_key="BER|VIE").tier != "error"
    assert judge(100, entity_key="BER|VIE").tier != "error"


def test_the_history_free_floor_needs_the_distance_to_be_known():
    """Ohne Koordinaten gibt es keine Schranke und damit kein Urteil."""
    verdict = judge(1000, entity_key="ZZZ|QQQ")

    assert verdict.distance_km is None
    assert verdict.tier != "error"


def test_a_foreign_currency_has_no_floor():
    """Die Schranken sind in Euro gemessen und gelten nur dort.

    Vierzigtausend Forint sind nicht vierzig Euro, und ein Kurs im Detektor
    waere eine zweite Stelle, an der umgerechnet wird.
    """
    assert judge(4000, currency="HUF").tier != "error"


# -- Mit Historie: beide Winkel muessen zusammenkommen ------------------------


def test_a_seasonal_sale_is_not_an_error_even_far_below_the_median():
    """19,99 Euro auf einer Strecke mit Median 60 Euro: ein Schlussverkauf.

    Der Preis liegt bei einem Drittel des Medians und trotzdem ueber der
    Schranke der Entfernung. Genau dieser Fall ist es, der bei einer reinen
    Prozentregel jeden Monat Fehlalarm ausloest.
    """
    baseline = Baseline(6000, 800, 20)

    assert judge(1999, entity_key="BER|BCN", baseline=baseline).tier == "cheap"


def test_a_price_below_the_floor_and_far_below_the_median_is_an_error():
    """Vier Euro auf derselben Strecke: unter der Schranke und unter dem Viertel."""
    baseline = Baseline(6000, 800, 20)

    verdict = judge(400, entity_key="BER|BCN", baseline=baseline)

    assert verdict.tier == "error"
    assert "Median" in verdict.reason
    assert str(errorfare.DEFAULT_PLAUSIBILITY.floor_minor(1502) // 100) in verdict.reason


def test_the_statistical_angle_alone_is_not_enough():
    """Weit unter dem Median, aber ueber der Schranke: guenstig, nicht falsch."""
    baseline = Baseline(40000, 2000, 30)

    assert judge(2500, entity_key="BER|BCN", baseline=baseline).tier == "cheap"


def test_a_thin_history_does_not_carry_the_statistical_angle():
    """Unter zehn Vergleichspreisen sagt der Median zu wenig.

    Fuenf Punkte reichen fuer eine Baseline, aber nicht fuer die Behauptung,
    ein Preis sei unmoeglich. Hotels lassen die Verhaeltnisregel ab fuenf zu;
    Flugpreise schwanken staerker, und darum steht die Schwelle hier hoeher.
    """
    thin = Baseline(6000, 800, 9)
    thick = Baseline(6000, 800, 10)

    assert judge(400, entity_key="BER|BCN", baseline=thin).tier != "error"
    assert judge(400, entity_key="BER|BCN", baseline=thick).tier == "error"


def test_six_times_the_spread_below_the_median_also_carries():
    """Die zweite statistische Bedingung, fuer breit streuende Strecken."""
    baseline = Baseline(20000, 1500, 15)

    verdict = judge(1000, entity_key="BER|BCN", baseline=baseline)

    assert verdict.tier == "error"
    assert "Streuung" in verdict.reason


# -- Was nie ein Fehltarif ist ------------------------------------------------


def test_an_indicative_price_never_becomes_an_error():
    """Ein Richtwert ist kein Tarif.

    Kiwi traegt einen gemessenen Aufschlag und preist ausserdem ein anderes
    Produkt: jede Airline, bis zu einem Umstieg. Ein Ein-Stopp-Preis unter
    jedem Direkttarif ist dort der Normalfall und kein Fund.
    """
    baseline = Baseline(6000, 800, 20)

    verdict = judge(400, entity_key="BER|BCN", baseline=baseline, is_indicative=True)

    assert verdict.tier == "cheap"
    assert judge(4000, is_indicative=True).tier != "error"


def test_the_price_is_measured_per_traveller():
    """Vier Reisende fuer 160 Euro sind vierzig Euro je Person."""
    for_one = judge(4000)
    for_four = judge(16000, party_size=4)

    assert for_one.tier == "error"
    assert for_four.tier == "error"
    assert judge(16000).tier != "error"


# -- Der Weg durch detect_price_signal ----------------------------------------


def observe(conn, price_minor: int, *, entity_key: str = "BER|BKK",
            source: str = "ryanair", indicative: int = 0) -> None:
    conn.execute(
        "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
        "travel_date, party_size, currency, price_total_minor, is_estimate, "
        "is_indicative) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (OBSERVED.isoformat(timespec="seconds"), source, "flight", entity_key,
         TRAVEL.isoformat(), 1, "EUR", price_minor, 1, indicative),
    )


def test_the_flight_detector_now_knows_a_fourth_tier(tmp_path):
    conn = db.connect(tmp_path / "e.db")
    for price in (48000, 49000, 50000, 50000, 51000, 52000, 50500,
                  49500, 50200, 50800, 51500, 49800):
        observe(conn, price)
    refresh_baselines(conn, now=OBSERVED)

    verdict = detect_price_signal(
        conn, "BER|BKK", TRAVEL, 4000, observed_at=OBSERVED
    )

    assert verdict["tier"] == "error"
    assert verdict["status"] == "cheap", "die alten drei Stufen bleiben, wie sie waren"
    assert verdict["distance_km"] == pytest.approx(8622, abs=5)
    assert verdict["n"] == 12


def test_an_indicative_price_stays_cheap_on_the_way_through(tmp_path):
    conn = db.connect(tmp_path / "i.db")
    for price in (48000, 49000, 50000, 50000, 51000, 52000, 50500,
                  49500, 50200, 50800, 51500, 49800):
        observe(conn, price)
    refresh_baselines(conn, now=OBSERVED)

    verdict = detect_price_signal(
        conn, "BER|BKK", TRAVEL, 4000, observed_at=OBSERVED, is_indicative=True
    )

    assert verdict["tier"] == "cheap"


def test_without_a_baseline_the_floor_still_speaks(tmp_path):
    """Der erste Tag hat keine Historie und soll trotzdem etwas finden."""
    conn = db.connect(tmp_path / "n.db")

    verdict = detect_price_signal(
        conn, "BER|BKK", TRAVEL, 4000, observed_at=OBSERVED
    )

    assert verdict["tier"] == "error"
    assert verdict["status"] == "unknown"
    assert verdict["n"] == 0
    assert verdict["basis"] == "none"


def test_a_chain_verdict_keeps_its_three_tiers(tmp_path):
    """Eine Kette wird gegen eine Summe von Medianen gehalten.

    Das ist bereits eine Naeherung. Auf eine Naeherung noch einen Fehltarif zu
    behaupten hiesse, zwei Unsicherheiten uebereinanderzulegen; die Kette
    bleibt deshalb bei drei Stufen.
    """
    from flightopt.storage.baseline import chain_price_signal

    conn = db.connect(tmp_path / "c.db")
    for price in (48000, 49000, 50000, 50000, 51000, 52000, 50500,
                  49500, 50200, 50800, 51500, 49800):
        observe(conn, price)
    refresh_baselines(conn, now=OBSERVED)

    verdict = chain_price_signal(
        conn, [("BER|BKK", TRAVEL, True)], 4000, observed_at=OBSERVED
    )

    assert verdict["tier"] == "cheap"


def test_the_reason_writes_amounts_the_german_way():
    """Ein Punkt mitten in einer Meldung mit Kommas liest sich wie ein Tippfehler."""
    baseline = Baseline(9853, 800, 12)

    verdict = judge(900, entity_key="BER|ATH", baseline=baseline)

    assert "98,53 Euro" in verdict.reason
    assert "98.53" not in verdict.reason
    assert "," in judge(4000).reason
