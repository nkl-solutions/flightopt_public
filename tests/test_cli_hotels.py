"""Die Kommandozeile der Hotelsuche. Fasst kein Netz an."""

from __future__ import annotations

from datetime import date

import pytest

from flightopt import cli
from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer
from flightopt.storage import db


def parse(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def test_a_window_and_a_single_day_both_become_a_query():
    args = parse(["hotels", "search", "Athen", "--from", "2026-11-10",
                  "--to", "2026-11-20", "--nights", "2", "--adults", "2",
                  "--rooms", "1", "--stars", "4", "5"])

    assert cli.hotel_window(args) == (date(2026, 11, 10), date(2026, 11, 20))
    query = cli.hotel_query(args, date(2026, 11, 10))
    assert (query.destination, query.nights, query.adults, query.rooms) == ("Athen", 2, 2, 1)
    assert query.stars == (4, 5)
    assert query.departure == date(2026, 11, 12)

    single = parse(["hotels", "search", "Athen", "--single", "2026-11-10"])
    assert cli.hotel_window(single) == (date(2026, 11, 10), date(2026, 11, 10))


def test_a_missing_or_reversed_window_is_a_sentence_and_not_a_traceback():
    with pytest.raises(SystemExit, match="--from fehlt"):
        cli.hotel_window(parse(["hotels", "search", "Athen"]))
    with pytest.raises(SystemExit, match="liegt vor"):
        cli.hotel_window(parse(["hotels", "search", "Athen", "--from", "2026-11-20",
                                "--to", "2026-11-10"]))
    with pytest.raises(SystemExit, match="Erwachsener"):
        cli.hotel_query(
            parse(["hotels", "search", "Athen", "--single", "2026-11-10", "--adults", "0"]),
            date(2026, 11, 10),
        )


def test_the_table_shows_object_stars_date_price_and_signal(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    offers = [
        HotelOffer(
            source="trivago", property_key="trivago:a", name="Melia Athens",
            arrival=date(2026, 11, 10), departure=date(2026, 11, 12),
            price_total=Money(24000, "EUR"), stars=4, country="Greece",
        ),
        HotelOffer(
            source="trivago", property_key="trivago:b", name="Crowne Plaza Athens",
            arrival=date(2026, 11, 10), departure=date(2026, 11, 12),
            price_total=Money(28700, "USD"), price_eur=Money(26091, "EUR"),
            stars=5, country="Greece",
        ),
    ]

    rows = cli.hotel_rows(conn, offers, limit=10)
    table = cli.format_hotel_table(rows)

    # Zwei Naechte, also 120 je Nacht. Nachkommastellen nur, wenn es welche gibt.
    assert "120 EUR" in table[2]
    assert "Melia Athens" in table[2]
    assert "****" in table[2]
    assert "2026-11-10" in table[2]
    # Ohne Baseline steht "keine Basis" und nicht "normal".
    assert "keine Basis" in table[2]
    # Der Originalwert bleibt sichtbar, auch wenn in Euro gerechnet wird.
    assert "130,46 EUR" in table[3]
    assert "umgerechnet aus 287 USD" in table[3]
    conn.close()


def test_cheap_rows_come_before_expensive_ones(tmp_path):
    conn = db.connect(tmp_path / "h.db")

    def offer(key: str, minor: int) -> HotelOffer:
        return HotelOffer(
            source="trivago", property_key=key, name=key,
            arrival=date(2026, 11, 10), departure=date(2026, 11, 11),
            price_total=Money(minor, "EUR"), country="Greece",
        )

    rows = cli.hotel_rows(conn, [offer("teuer", 30000), offer("billig", 4000)], limit=10)

    assert [row["name"] for row in rows] == ["billig", "teuer"]
    conn.close()
