"""Die vier Stufen, ohne Datenbank: nur Preis, Vergleichsgruppe, Stammdaten."""

from __future__ import annotations

from flightopt.domain.fx import Rates
from flightopt.hotels.signals import (
    TIER_RANK,
    Baseline,
    PlausibilityLimits,
    classify,
)

RATES = Rates(base="EUR", rates={"USD": 1.10, "CHF": 0.94, "JPY": 165.0})

# Median 90 Euro, Streuung 8 Euro, elf Beobachtungen: die Lage, an der
# gemessen wurde, dass drei Stufen zu wenig sind.
SPREAD = Baseline(median_minor=9000, mad_minor=800, n=11)
FLAT = Baseline(median_minor=9000, mad_minor=0, n=11)
TIGHT = Baseline(median_minor=9000, mad_minor=100, n=11)


def test_sixty_euro_is_a_bargain_and_nine_euro_is_an_error():
    bargain = classify(6000, SPREAD, stars=4, rates=RATES)
    broken = classify(900, SPREAD, stars=4, rates=RATES)

    assert bargain.tier == "cheap"
    assert broken.tier == "error"
    assert broken.reason == "6-fache Streuung unter dem Median"
    # `status` behaelt seine alte Bedeutung: beide liegen unter dem Band.
    assert (bargain.status, broken.status) == ("cheap", "cheap")


def test_a_mad_of_zero_never_divides_and_the_percentage_rule_carries():
    # Liegt ueber die Haelfte der Werte auf dem Median, ist mad exakt null.
    bargain = classify(6000, FLAT, stars=4, rates=RATES)
    broken = classify(900, FLAT, stars=4, rates=RATES)

    assert bargain.tier == "cheap"
    assert broken.tier == "error"
    assert broken.reason == "unter 30 Prozent des Medians"


def test_the_plausibility_floor_finds_an_error_without_any_history():
    signal = classify(1200, None, stars=3, rates=RATES)

    assert signal.tier == "error"
    # Der Zusatz gehoert dazu: ohne Historie traegt nur die Schranke, und
    # die Oberflaeche zeigt genau diesen Satz neben dem Wort "Preisfehler".
    assert signal.reason == "unter der Schranke von 30 Euro je Nacht, ohne Vergleichspreise"
    # Ohne Baseline gibt es keine Aussage ueber die Lage, nur ueber die Hoehe.
    assert (signal.status, signal.basis, signal.n) == ("unknown", "none", 0)


def test_the_floor_counts_per_night_and_not_per_stay():
    four_nights = classify(8000, None, nights=4, stars=4, rates=RATES)
    one_night = classify(8000, None, nights=1, stars=4, rates=RATES)

    assert four_nights.tier == "error"
    assert one_night.tier == "unknown"


def test_a_dorm_is_marked_instead_of_being_measured_against_hotel_prices():
    signal = classify(1200, None, name="Athens City Hostel", rates=RATES)

    assert signal.category_suspect
    assert signal.tier == "unknown"


def test_the_floor_is_configurable_and_falls_back_without_stars():
    cheap_market = PlausibilityLimits(per_stars={4: 2000}, default_minor=800)

    assert classify(2500, None, stars=4, limits=cheap_market).tier == "unknown"
    assert classify(1900, None, stars=4, limits=cheap_market).tier == "error"
    assert classify(1900, None, stars=None, limits=cheap_market).tier == "unknown"


def test_a_shifted_decimal_point_is_sorted_out_and_not_reported_as_an_error():
    signal = classify(90, TIGHT, stars=4, rates=RATES)

    assert signal.tier == "encoding_suspect"
    assert signal.reason == "Dezimalfehler, Faktor /100 zum Median"


def test_a_price_on_the_dollar_rate_is_sorted_out_and_not_reported_as_an_error():
    signal = classify(round(9000 / 1.10), TIGHT, stars=4, rates=RATES)

    assert signal.tier == "encoding_suspect"
    assert signal.reason == "Waehrungsverwechslung, Faktor entspricht USD"


def test_a_factor_of_a_hundred_upwards_is_sorted_out_too():
    signal = classify(900000, TIGHT, stars=4, rates=RATES)

    assert signal.tier == "encoding_suspect"
    assert signal.status == "expensive"


def test_the_ranking_puts_errors_first_and_sorted_out_rows_last():
    order = sorted(
        ["unknown", "expensive", "error", "normal", "encoding_suspect", "cheap"],
        key=lambda tier: TIER_RANK[tier],
    )

    assert order == [
        "error", "cheap", "normal", "expensive", "encoding_suspect", "unknown"
    ]


def test_the_floor_only_applies_to_the_currency_it_was_written_in():
    assert classify(1200, None, stars=3, currency="USD", rates=RATES).tier == "unknown"


# --------------------------------------------------------------------------
# Was ein Urteil traegt, und was es nicht traegt
# --------------------------------------------------------------------------

THIN = Baseline(median_minor=9000, mad_minor=800, n=6)


def test_a_floor_error_without_history_says_so_in_its_reason():
    """Am ersten Tag traegt nur die Schranke, und das muss dranstehen.

    Die Oberflaeche zeigt das Wort "Preisfehler" und daneben genau diesen
    Satz - auf dem Telefon sogar nur das Wort. Wer dort nicht liest, dass
    keine einzige Vergleichsbeobachtung dahintersteht, liest eine Sicherheit,
    die es nicht gibt.
    """
    signal = classify(1200, None, stars=3, rates=RATES)

    assert signal.tier == "error"
    assert signal.evidence == "schranke"
    assert "ohne Vergleichspreise" in signal.reason
    assert signal.n == 0


def test_a_thin_baseline_names_how_thin_it_is():
    """Fuenf Punkte reichen fuer einen Median und sind trotzdem duenn."""
    signal = classify(900, THIN, stars=4, rates=RATES)

    assert signal.tier == "error"
    assert signal.thin is True
    assert "6 Vergleichspreise" in signal.reason


def test_a_thick_baseline_stays_silent_about_its_thickness():
    """Was nicht schwach ist, braucht keinen Zusatz. Sonst liest ihn niemand."""
    signal = classify(900, SPREAD, stars=4, rates=RATES)

    assert signal.thin is False
    assert signal.reason == "6-fache Streuung unter dem Median"


def test_every_verdict_names_the_rule_that_carried_it():
    """`reason` ist Text und wird uebersetzt. `evidence` ist die Regel."""
    assert classify(900, SPREAD, stars=4, rates=RATES).evidence == "streuung"
    assert classify(900, FLAT, stars=4, rates=RATES).evidence == "anteil"
    assert classify(1200, None, stars=3, rates=RATES).evidence == "schranke"
    assert classify(9000, SPREAD, stars=4, rates=RATES).evidence == "band"
    assert classify(5000, None, stars=1, rates=RATES).evidence == "keine"


def test_the_signal_dictionary_carries_the_new_fields():
    """Die Oberflaeche liest ein dict, kein Objekt."""
    data = classify(1200, None, stars=3, rates=RATES).as_dict()

    assert data["evidence"] == "schranke"
    assert data["thin"] is False
    assert data["reason"].startswith("unter der Schranke")
