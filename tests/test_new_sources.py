"""Wizz Air and Aegean adapters, against recorded responses.

Both parse formats that are easy to get subtly wrong: Wizz reports a currency
that follows the departure market, and Aegean wraps dates in the old Microsoft
JSON format. A silent mistake in either produces plausible but wrong prices,
which is worse than an error.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from flightopt.domain.models import Money
from flightopt.sources.aegean import AegeanSource, parse_ms_date
from flightopt.sources.wizz import BASE_RE, MAX_WINDOW_DAYS, WizzSource

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def stub(src, payload, monkeypatch):
    async def fake(url, **kw):
        return payload

    monkeypatch.setattr(src, "fetch_json", fake)


@pytest.fixture
def wizz(monkeypatch):
    src = WizzSource()
    src._base = "https://be.wizzair.com/99.0.0/Api"  # skip discovery

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)
    return src


@pytest.fixture
def aegean(monkeypatch):
    src = AegeanSource()

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)
    return src


# --- Wizz: base URL discovery ------------------------------------------------


def test_base_url_regex_matches_page_config():
    page = 'window.__NUXT__.config={public:{x:1,apiUrl:"https://be.wizzair.com/29.15.0/Api",y:2}}'
    assert BASE_RE.search(page).group(1) == "https://be.wizzair.com/29.15.0/Api"


def test_base_url_regex_accepts_a_future_version():
    # The whole point of reading it live is surviving a version bump.
    page = 'apiUrl:"https://be.wizzair.com/31.2.7/Api"'
    assert BASE_RE.search(page).group(1) == "https://be.wizzair.com/31.2.7/Api"


def test_base_url_regex_ignores_other_hosts():
    assert BASE_RE.search('apiUrl:"https://evil.example.com/29.15.0/Api"') is None


# --- Wizz: prices ------------------------------------------------------------


@pytest.mark.asyncio
async def test_wizz_calendar_parses_recorded_payload(wizz, monkeypatch):
    stub(wizz, load("wizz_timetable_BER_BEG.json"), monkeypatch)
    prices = await wizz.calendar_range("BER", "BEG", date(2026, 10, 1), date(2026, 10, 31))

    assert prices
    assert all(isinstance(v, Money) and v.minor > 0 for v in prices.values())
    assert all(date(2026, 10, 1) <= d <= date(2026, 10, 31) for d in prices)


@pytest.mark.asyncio
async def test_wizz_skips_a_foreign_currency(wizz, monkeypatch):
    # Wizz prices in the departure market's currency and ignores any request to
    # change it. Converting silently would produce a wrong total.
    stub(wizz, {"outboundFlights": [
        {"departureDate": "2026-10-01T00:00:00",
         "price": {"amount": 25000, "currencyCode": "HUF"}},
        {"departureDate": "2026-10-02T00:00:00",
         "price": {"amount": 49.99, "currencyCode": "EUR"}},
    ]}, monkeypatch)
    prices = await wizz.calendar_range("BUD", "LTN", date(2026, 10, 1), date(2026, 10, 5))

    assert prices == {date(2026, 10, 2): Money(4999, "EUR")}


@pytest.mark.asyncio
async def test_wizz_keeps_the_cheapest_flight_of_a_day(wizz, monkeypatch):
    stub(wizz, {"outboundFlights": [
        {"departureDate": "2026-10-01T00:00:00", "price": {"amount": 99.0, "currencyCode": "EUR"}},
        {"departureDate": "2026-10-01T00:00:00", "price": {"amount": 45.0, "currencyCode": "EUR"}},
    ]}, monkeypatch)
    prices = await wizz.calendar_range("BER", "BEG", date(2026, 10, 1), date(2026, 10, 1))

    assert prices == {date(2026, 10, 1): Money(4500, "EUR")}


@pytest.mark.asyncio
async def test_wizz_ignores_rows_without_a_price(wizz, monkeypatch):
    stub(wizz, {"outboundFlights": [
        {"departureDate": "2026-10-01T00:00:00", "price": {"amount": None}},
        {"departureDate": None, "price": {"amount": 30.0, "currencyCode": "EUR"}},
        {"departureDate": "2026-10-03T00:00:00", "price": {"amount": 30.0, "currencyCode": "EUR"}},
    ]}, monkeypatch)
    prices = await wizz.calendar_range("BER", "BEG", date(2026, 10, 1), date(2026, 10, 5))

    assert prices == {date(2026, 10, 3): Money(3000, "EUR")}


@pytest.mark.asyncio
async def test_wizz_splits_a_window_past_the_limit(wizz, monkeypatch):
    # The endpoint rejects windows longer than MAX_WINDOW_DAYS, so a long search
    # has to be chunked rather than silently truncated.
    seen: list[tuple[date, date]] = []

    async def fake(origin, destination, start, end):
        seen.append((start, end))
        return []

    monkeypatch.setattr(wizz, "_timetable", fake)
    await wizz.calendar_range("BER", "BEG", date(2026, 10, 1), date(2026, 12, 31))

    assert len(seen) > 1
    assert all((e - s).days < MAX_WINDOW_DAYS for s, e in seen)
    assert seen[0][0] == date(2026, 10, 1)
    assert seen[-1][1] == date(2026, 12, 31)


@pytest.mark.asyncio
async def test_wizz_search_leg_reports_the_departure_time(wizz, monkeypatch):
    stub(wizz, {"outboundFlights": [{
        "departureStation": "BER", "arrivalStation": "BEG",
        "departureDate": "2026-10-04T00:00:00",
        "departureDates": ["2026-10-04T13:15:00"],
        "price": {"amount": 45.99, "currencyCode": "EUR"},
    }]}, monkeypatch)
    offers = await wizz.search_leg("BER", "BEG", date(2026, 10, 4))

    assert len(offers) == 1
    assert offers[0].price == Money(4599, "EUR")
    assert offers[0].segments[0].departure.hour == 13
    assert offers[0].deep_link


@pytest.mark.asyncio
async def test_wizz_search_leg_rejects_another_day(wizz, monkeypatch):
    stub(wizz, {"outboundFlights": [{
        "departureDate": "2026-10-09T00:00:00",
        "price": {"amount": 45.99, "currencyCode": "EUR"},
    }]}, monkeypatch)
    assert await wizz.search_leg("BER", "BEG", date(2026, 10, 4)) == []


@pytest.mark.asyncio
async def test_wizz_route_map_builds_the_network(wizz, monkeypatch):
    stub(wizz, {"cities": [
        {"iata": "BER", "connections": [{"iata": "BEG"}, {"iata": "BUD"}, {}]},
        {"iata": "BUD", "connections": [{"iata": "BER"}]},
        {"connections": [{"iata": "XXX"}]},
    ]}, monkeypatch)
    assert await wizz.load_routes("BER") == {"BEG", "BUD"}
    assert wizz.supports_route("BER", "BEG")
    assert not wizz.supports_route("BER", "AYT")
    assert not wizz.supports_route("ZZZ", "BEG")


def test_wizz_route_is_allowed_before_the_map_loads(wizz):
    assert wizz.supports_route("BER", "BEG")


# --- Aegean: dates -----------------------------------------------------------


def test_ms_date_is_unwrapped():
    assert parse_ms_date('"\\/Date(1793491200000)\\/"') == date(2026, 11, 1)


def test_ms_date_handles_the_double_escaped_form():
    # This is the shape the live endpoint actually returns.
    assert parse_ms_date('"\\"\\\\/Date(1793491200000)\\\\/\\""') == date(2026, 11, 1)


def test_ms_date_reads_utc_not_local_time():
    # Reading the stamp in local time would shift the whole calendar by a day
    # for anyone east of Greenwich, which is every user of this tool.
    midnight_utc = (date(2026, 11, 1) - date(1970, 1, 1)).days * 86_400_000
    assert parse_ms_date(f"/Date({midnight_utc})/") == date(2026, 11, 1)


@pytest.mark.parametrize("raw", [None, "", "not a date", "/Date(abc)/"])
def test_bad_ms_dates_return_nothing(raw):
    assert parse_ms_date(raw) is None


# --- Aegean: prices ----------------------------------------------------------


@pytest.mark.asyncio
async def test_aegean_calendar_parses_recorded_payload(aegean, monkeypatch):
    stub(aegean, load("aegean_lowfares_ATH_SKG.json"), monkeypatch)
    prices = await aegean.calendar("ATH", "SKG", date(2026, 11, 1))

    assert prices
    assert all(isinstance(d, date) for d in prices)
    assert all(v.minor > 0 and v.currency == "EUR" for v in prices.values())


@pytest.mark.asyncio
async def test_aegean_adds_the_service_fee(aegean, monkeypatch):
    stub(aegean, {"Outbound": [
        {"Date": "/Date(1793491200000)/", "Price": 58.62, "ServiceFee": 8.0},
    ]}, monkeypatch)
    prices = await aegean.calendar("ATH", "SKG", date(2026, 11, 1))

    assert prices == {date(2026, 11, 1): Money(6662, "EUR")}


@pytest.mark.asyncio
async def test_aegean_skips_rows_with_an_error(aegean, monkeypatch):
    stub(aegean, {"Outbound": [
        {"Date": "/Date(1793491200000)/", "Price": 10.0, "Error": "no flights"},
        {"Date": "/Date(1793577600000)/", "Price": 55.0, "ServiceFee": 0.0},
    ]}, monkeypatch)
    prices = await aegean.calendar("ATH", "SKG", date(2026, 11, 1))

    assert prices == {date(2026, 11, 2): Money(5500, "EUR")}


@pytest.mark.asyncio
async def test_aegean_handles_an_empty_month(aegean, monkeypatch):
    stub(aegean, {"Outbound": None, "Inbound": None}, monkeypatch)
    assert await aegean.calendar("ATH", "XXX", date(2026, 10, 1)) == {}


@pytest.mark.asyncio
async def test_aegean_offer_carries_a_booking_link(aegean, monkeypatch):
    stub(aegean, {"Outbound": [
        {"Date": "/Date(1793491200000)/", "Price": 58.62, "ServiceFee": 0.0},
    ]}, monkeypatch)
    offers = await aegean.search_leg("ATH", "SKG", date(2026, 11, 1))

    assert len(offers) == 1
    # No times are known, but a result the user cannot book is useless.
    assert offers[0].segments == ()
    assert "aegeanair.com" in offers[0].deep_link
    assert offers[0].is_estimate is False


@pytest.mark.asyncio
async def test_aegean_offer_absent_when_the_day_has_no_fare(aegean, monkeypatch):
    stub(aegean, {"Outbound": [
        {"Date": "/Date(1793577600000)/", "Price": 58.62},
    ]}, monkeypatch)
    assert await aegean.search_leg("ATH", "SKG", date(2026, 11, 1)) == []


# --- both --------------------------------------------------------------------


def test_adapters_declare_their_carrier():
    assert WizzSource().carrier == "W6"
    assert AegeanSource().carrier == "A3"


# --- adapter contract --------------------------------------------------------


def test_every_adapter_can_be_constructed():
    """Catches name collisions between adapter fields and the base class.

    A property added to `HttpSource` shadows any instance attribute of the same
    name, and the failure only shows up when the adapter is instantiated. That
    once broke every search at runtime while all unit tests stayed green.
    """
    from flightopt.sources.aegean import AegeanSource
    from flightopt.sources.britishairways import BritishAirwaysSource
    from flightopt.sources.condor import CondorSource
    from flightopt.sources.eurowings import EurowingsSource
    from flightopt.sources.icelandair import IcelandairSource
    from flightopt.sources.kiwi import KiwiSource
    from flightopt.sources.ryanair import RyanairSource
    from flightopt.sources.wizz import WizzSource

    built = [
        RyanairSource(), WizzSource(), AegeanSource(), CondorSource(),
        EurowingsSource(), BritishAirwaysSource(), IcelandairSource(),
        KiwiSource(), KiwiSource(carriers=["XQ", "EW"]),
    ]
    for src in built:
        assert isinstance(src.carriers, tuple)
        assert src.supports_calendar or src.supports_search


def test_kiwi_filter_is_not_the_adapter_carrier_list():
    # Kiwi represents no airline of its own; its carrier list is a filter it
    # sends upstream. Conflating the two would badge results with a tail.
    from flightopt.sources.kiwi import KiwiSource

    src = KiwiSource(carriers=["XQ"])
    assert src.carrier_filter == ["XQ"]
    assert src.carriers == ()
