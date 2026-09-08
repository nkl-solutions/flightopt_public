"""Preise stehen als Anzeigetext in den Antworten, nicht als Zahl.

Die Quellen mischen Schreibweisen: "$287" neben "1.234 €" neben "129,50".
Wer hier falsch liest, verschiebt einen Preis um Faktor hundert und meldet
einen Preisfehler, den es nie gab.
"""

from __future__ import annotations

import pytest

from flightopt.domain.models import Money
from flightopt.hotels.prices import parse_count, parse_price, parse_rating, parse_stars


@pytest.mark.parametrize(
    "raw,currency,expected",
    [
        ("$287", "USD", Money(28700, "USD")),
        ("118€", "EUR", Money(11800, "EUR")),
        ("€ 129", "EUR", Money(12900, "EUR")),
        # Tausenderpunkt, deutsche Schreibweise: 1234 Euro, nicht 1,234.
        ("1.234 €", "EUR", Money(123400, "EUR")),
        ("1.234,56 €", "EUR", Money(123456, "EUR")),
        ("$1,234.56", "USD", Money(123456, "USD")),
        ("$1,234", "USD", Money(123400, "USD")),
        ("129,50", "EUR", Money(12950, "EUR")),
        ("9.99", "USD", Money(999, "USD")),
        # Geschuetztes Leerzeichen als Tausendertrenner.
        ("1 234 CHF", "CHF", Money(123400, "CHF")),
        (287, "USD", Money(28700, "USD")),
        (118.5, "EUR", Money(11850, "EUR")),
    ],
)
def test_price_strings_of_every_shape_become_minor_units(raw, currency, expected):
    assert parse_price(raw, currency) == expected


@pytest.mark.parametrize("raw", [None, "", "auf Anfrage", "-", "0", "€", True])
def test_an_unreadable_price_is_none_and_never_a_guess(raw):
    assert parse_price(raw, "EUR") is None


def test_the_currency_comes_from_the_field_and_never_from_the_symbol():
    # "$" gehoert USD, CAD, AUD und einem Dutzend weiterer Waehrungen. Geraten
    # wird nicht: was im Feld `currency` steht, gilt.
    assert parse_price("$287", "CAD") == Money(28700, "CAD")


def test_counts_ratings_and_stars_are_read_separately():
    assert parse_count("6,276") == 6276
    assert parse_count("8358") == 8358
    assert parse_count("keine") is None
    assert parse_rating("8.6") == 8.6
    assert parse_rating("8,6") == 8.6
    assert parse_rating("12") is None
    assert parse_stars(4) == 4
    assert parse_stars("5") == 5
    assert parse_stars(0) is None
    assert parse_stars("Hostel") is None
