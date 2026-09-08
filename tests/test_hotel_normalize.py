"""Die Vorfilter: Encoding, Vergleichbarkeit, Kategorie."""

from __future__ import annotations

from flightopt.domain.fx import Rates
from flightopt.hotels.normalize import (
    category_token,
    comparison_key,
    count_verdicts,
    encoding_check,
    is_category_suspect,
    peer_key,
    stay_key,
    suspect_currencies,
)

RATES = Rates(
    base="EUR",
    rates={"USD": 1.10, "GBP": 0.85, "CHF": 0.94, "CAD": 1.50, "JPY": 165.0},
)


def test_the_dangerous_currencies_come_from_the_rate_table_not_from_a_list():
    codes = [code for code, _ in suspect_currencies(RATES)]

    assert codes == ["CAD", "CHF", "GBP", "USD"]
    # JPY faellt sofort auf und braucht die Pruefung nicht.
    assert "JPY" not in codes


def test_a_shifted_decimal_point_is_an_encoding_error_in_both_directions():
    small = encoding_check(90, 9000, rates=RATES)
    large = encoding_check(900000, 9000, rates=RATES)

    assert (small.suspect, small.kind, small.detail) == (True, "dezimal", "/100")
    assert (large.suspect, large.kind, large.detail) == (True, "dezimal", "x100")


def test_a_price_sitting_exactly_on_the_dollar_rate_is_a_currency_mixup():
    check = encoding_check(round(9000 / 1.10), 9000, rates=RATES)

    assert check.suspect
    assert check.kind == "waehrung"
    assert check.detail == "USD"


def test_a_price_error_at_a_tenth_of_the_median_is_not_an_encoding_artefact():
    # Faktor 10 ist kein Dezimalfehler und kein Kurs: 9 Euro bei Median 90
    # bleiben ein Kandidat fuer die Stufe error.
    assert not encoding_check(900, 9000, rates=RATES).suspect


def test_a_third_off_collides_with_the_canadian_rate_and_stays_a_finding():
    # 60 bei Median 90 ist exakt 1/1,50. Genau deshalb greift das Veto in
    # signals.py nur bei error und expensive und niemals bei cheap.
    check = encoding_check(6000, 9000, rates=RATES)

    assert check.suspect and check.detail == "CAD"


def test_without_a_median_there_is_nothing_to_compare_against():
    assert not encoding_check(6000, None, rates=RATES).suspect
    assert not encoding_check(6000, 0, rates=RATES).suspect


def test_occupancy_and_nights_build_the_comparison_key():
    assert stay_key(2, 1) == "p2n1"
    assert stay_key(0, 0) == "p1n1"

    key = comparison_key(
        "GR|trivago:abc",
        party_size=4,
        nights=3,
        weekday=1,
        leadtime_bucket="60-119",
        currency="eur",
    )
    assert key == ("GR|trivago:abc", 1, "60-119", "p4n3", "EUR")


def test_the_peer_group_falls_back_to_the_country_when_the_city_is_missing():
    assert peer_key("GR", "Athens", 4) == "GR|athens|4"
    assert peer_key(None, None, None) == "XX|-|-"


def test_dorms_camping_boats_and_day_rooms_are_marked_by_name():
    assert category_token("Athens City Hostel") == "hostel"
    assert category_token("Backpackers Lodge") == "backpackers"
    assert category_token("Camping Bella Vista") == "camping"
    assert category_token("Hausboot Spreewald") == "hausboot"
    assert category_token("Kapsel Hotel Tokio") == "kapsel"
    assert is_category_suspect("Dorm Bed Downtown")


def test_a_word_that_only_contains_a_token_is_still_a_hotel():
    # Wortgrenzen, sonst faellt die halbe Normandie aus der Verteilung.
    assert category_token("Hostellerie du Cerf") is None
    assert category_token("Bootshaus Hotel Wannsee") is None
    assert not is_category_suspect("Melia Athens")
    assert not is_category_suspect(None)


def test_the_report_can_count_what_was_sorted_out():
    assert count_verdicts(["error", "cheap", "error", "encoding_suspect"]) == {
        "error": 2,
        "cheap": 1,
        "encoding_suspect": 1,
    }
