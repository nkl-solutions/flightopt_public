"""Adapter tests against recorded responses.

Scrapers break when the remote payload changes shape, so these tests pin the
parsing against real captured JSON. They never touch the network.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from flightopt.domain.models import Money
from flightopt.sources.base import CircuitBreaker, SourceBlocked
from flightopt.sources.ryanair import RyanairSource

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def ryanair(monkeypatch):
    src = RyanairSource()

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)
    return src


def stub_json(src, payload, monkeypatch):
    async def fake(url, **kw):
        return payload

    monkeypatch.setattr(src, "fetch_json", fake)


# --- calendar ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_parses_recorded_payload(ryanair, monkeypatch):
    stub_json(ryanair, load("ryanair_calendar_BER_FCO.json"), monkeypatch)
    prices = await ryanair.calendar("BER", "FCO", date(2026, 10, 1))

    assert prices, "fixture should contain at least one priced day"
    assert all(isinstance(k, date) for k in prices)
    assert all(isinstance(v, Money) for v in prices.values())
    assert all(v.currency == "EUR" for v in prices.values())
    assert all(v.minor > 0 for v in prices.values())


@pytest.mark.asyncio
async def test_calendar_skips_sold_out_and_unpriced(ryanair, monkeypatch):
    payload = {
        "outbound": {
            "fares": [
                {"day": "2026-10-01", "soldOut": True,
                 "price": {"value": 19.99, "currencyCode": "EUR"}},
                {"day": "2026-10-02", "unavailable": True,
                 "price": {"value": 29.99, "currencyCode": "EUR"}},
                {"day": "2026-10-03", "price": None},
                {"day": "2026-10-04", "price": {"value": None}},
                {"day": "2026-10-05",
                 "price": {"value": 46.97, "currencyCode": "EUR"}},
            ]
        }
    }
    stub_json(ryanair, payload, monkeypatch)
    prices = await ryanair.calendar("BER", "ATH", date(2026, 10, 1))

    assert prices == {date(2026, 10, 5): Money(4697, "EUR")}


@pytest.mark.asyncio
async def test_calendar_handles_empty_outbound(ryanair, monkeypatch):
    stub_json(ryanair, {"outbound": {}}, monkeypatch)
    assert await ryanair.calendar("BER", "XXX", date(2026, 10, 1)) == {}


@pytest.mark.asyncio
async def test_calendar_range_filters_to_window(ryanair, monkeypatch):
    async def fake_calendar(o, d, month, *, currency="EUR"):
        first = month.replace(day=1)
        return {
            first + timedelta(days=i): Money(1000 + i) for i in range(28)
        }

    monkeypatch.setattr(ryanair, "calendar", fake_calendar)
    start, end = date(2026, 10, 10), date(2026, 11, 5)
    prices = await ryanair.calendar_range("BER", "FCO", start, end)

    assert prices
    assert min(prices) >= start
    assert max(prices) <= end


@pytest.mark.asyncio
async def test_calendar_range_survives_one_bad_month(ryanair, monkeypatch):
    from flightopt.sources.base import SourceError

    async def flaky(o, d, month, *, currency="EUR"):
        if month.month == 10:
            raise SourceError("boom")
        return {month.replace(day=3): Money(5000)}

    monkeypatch.setattr(ryanair, "calendar", flaky)
    prices = await ryanair.calendar_range(
        "BER", "FCO", date(2026, 10, 1), date(2026, 11, 30)
    )
    assert prices == {date(2026, 11, 3): Money(5000)}


# --- per-leg search ----------------------------------------------------------


@pytest.mark.asyncio
async def test_search_leg_parses_recorded_payload(ryanair, monkeypatch):
    payload = load("ryanair_oneway_BER_FCO.json")
    stub_json(ryanair, payload, monkeypatch)
    offers = await ryanair.search_leg("BER", "FCO", date(2026, 10, 1))

    assert offers
    first = offers[0]
    assert first.source == "ryanair"
    assert first.price.minor > 0
    assert first.segments
    seg = first.segments[0]
    assert seg.carrier == "FR"
    assert seg.arrival > seg.departure
    assert first.stops == 0
    assert first.is_estimate is False
    assert offers == sorted(offers, key=lambda o: o.price.minor)


@pytest.mark.asyncio
async def test_search_leg_ignores_incomplete_fares(ryanair, monkeypatch):
    payload = {
        "fares": [
            {"outbound": {"price": {"value": 20.0}}},  # no dates
            {"outbound": {"departureDate": "2026-10-01T10:00:00",
                          "arrivalDate": "2026-10-01T12:00:00",
                          "price": {"value": None}}},
            {"outbound": {"departureDate": "2026-10-01T18:10:00",
                          "arrivalDate": "2026-10-01T22:00:00",
                          "flightNumber": "FR 170",
                          "departureAirport": {"iataCode": "BER"},
                          "arrivalAirport": {"iataCode": "FCO"},
                          "price": {"value": 46.97, "currencyCode": "EUR"}}},
        ]
    }
    stub_json(ryanair, payload, monkeypatch)
    offers = await ryanair.search_leg("BER", "FCO", date(2026, 10, 1))

    assert len(offers) == 1
    assert offers[0].price == Money(4697, "EUR")
    assert offers[0].segments[0].flight_number == "FR170"


# --- route metadata ----------------------------------------------------------


@pytest.mark.asyncio
async def test_load_routes_reads_widget_shape(ryanair, monkeypatch):
    stub_json(
        ryanair,
        [
            {"arrivalAirport": {"code": "ATH", "name": "Athens"}},
            {"arrivalAirport": {"code": "FCO", "name": "Rome"}},
            {"arrivalAirport": {}},
        ],
        monkeypatch,
    )
    dests = await ryanair.load_routes("BER")

    assert dests == {"ATH", "FCO"}
    assert ryanair.supports_route("BER", "ATH")
    assert not ryanair.supports_route("BER", "AYT")


@pytest.mark.asyncio
async def test_unknown_origin_is_optimistic(ryanair):
    # Before load_routes runs we must not filter out a route we simply have not
    # looked up yet, otherwise the first search of a session prices nothing.
    assert ryanair.supports_route("XXX", "YYY")


@pytest.mark.asyncio
async def test_load_routes_failure_is_not_fatal(ryanair, monkeypatch):
    from flightopt.sources.base import SourceError

    async def boom(url, **kw):
        raise SourceError("upstream down")

    monkeypatch.setattr(ryanair, "fetch_json", boom)
    assert await ryanair.load_routes("BER") == set()


# --- circuit breaker ---------------------------------------------------------


def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(threshold=3, cooldown=60)
    assert not cb.is_open
    for _ in range(2):
        cb.record_block()
    assert not cb.is_open
    cb.record_block()
    assert cb.is_open
    assert cb.remaining() > 0


def test_breaker_success_resets_failures():
    cb = CircuitBreaker(threshold=3, cooldown=60)
    cb.record_block()
    cb.record_block()
    cb.record_success()
    cb.record_block()
    assert not cb.is_open


def test_breaker_closes_after_cooldown():
    cb = CircuitBreaker(threshold=1, cooldown=0.0)
    cb.record_block()
    assert not cb.is_open  # cooldown already elapsed


@pytest.mark.asyncio
async def test_open_breaker_short_circuits_fetch(ryanair):
    ryanair.breaker = CircuitBreaker(threshold=1, cooldown=600)
    ryanair.breaker.record_block()
    with pytest.raises(SourceBlocked, match="circuit open"):
        await ryanair.fetch_json("https://example.invalid")
