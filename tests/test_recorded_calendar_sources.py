"""Recorded payloads for calendar sources that have no per-flight test data.

These tests pin the brittle parts of each public calendar response. They never
touch the network; the live captures sit in tests/fixtures.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from flightopt.domain.models import Money
from flightopt.sources.britishairways import BritishAirwaysSource, city_code
from flightopt.sources.condor import CondorSource
from flightopt.sources.eurowings import EurowingsSource
from flightopt.sources.icelandair import IcelandairSource
from flightopt.sources.kiwi import KiwiSource

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def stub_fetch_json(src, payload, monkeypatch, seen=None):
    async def fake(url, **kw):
        if seen is not None:
            seen.append((url, kw))
        return payload

    monkeypatch.setattr(src, "fetch_json", fake)


@pytest.mark.asyncio
async def test_eurowings_parses_recorded_centrallowfare_payload(monkeypatch):
    payload = load("eurowings_centrallowfare_DUS_PMI.json")
    src = EurowingsSource()

    async def fake_fetch(origin, destination):
        assert (origin, destination) == ("DUS", "PMI")
        return payload

    monkeypatch.setattr(src, "_fetch", fake_fetch)

    prices = await src.calendar_range(
        "DUS", "PMI", date(2026, 9, 5), date(2026, 9, 8)
    )

    assert prices == {
        date(2026, 9, 5): Money(19499, "EUR"),
        date(2026, 9, 6): Money(7999, "EUR"),
        date(2026, 9, 7): Money(6499, "EUR"),
        date(2026, 9, 8): Money(4999, "EUR"),
    }


@pytest.mark.asyncio
async def test_condor_parses_recorded_minor_unit_payload(monkeypatch):
    src = CondorSource()
    stub_fetch_json(src, load("condor_lowfare_FRA_HER.json"), monkeypatch)

    prices = await src.calendar("FRA", "HER", date(2026, 10, 1))

    assert {
        day: prices[day]
        for day in (date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 8))
    } == {
        date(2026, 10, 1): Money(36599, "EUR"),
        date(2026, 10, 5): Money(25599, "EUR"),
        date(2026, 10, 8): Money(22599, "EUR"),
    }


@pytest.mark.asyncio
async def test_british_airways_parses_recorded_city_code_payload(monkeypatch):
    src = BritishAirwaysSource()
    seen: list[tuple[str, dict]] = []
    stub_fetch_json(src, load("britishairways_lpbd_BER_LON.json"), monkeypatch, seen)

    prices = await src.calendar("BER", "LHR", date(2026, 11, 1))

    assert city_code("LHR") == "LON"
    assert ("fq", "currency_code:EUR") in seen[0][1]["params"]
    assert ("fq", "arrival_city:LON") in seen[0][1]["params"]
    assert prices[date(2026, 11, 2)] == Money(8550, "EUR")
    assert prices[date(2026, 11, 16)] == Money(8550, "EUR")


@pytest.mark.asyncio
async def test_icelandair_parses_recorded_payload_and_sends_correlation_header(monkeypatch):
    src = IcelandairSource()
    seen: list[tuple[str, dict]] = []
    stub_fetch_json(src, load("icelandair_bestprice_BER_KEF.json"), monkeypatch, seen)

    prices = await src.calendar_range(
        "BER", "KEF", date(2026, 10, 1), date(2026, 10, 7)
    )

    headers = seen[0][1]["headers"]
    assert headers["X-Correlation-Id"]
    assert "X-Correlation-Id" not in seen[0][1]["params"]
    assert prices == {
        date(2026, 10, 1): Money(46264, "EUR"),
        date(2026, 10, 2): Money(46264, "EUR"),
        date(2026, 10, 3): Money(46755, "EUR"),
        date(2026, 10, 4): Money(36051, "EUR"),
        date(2026, 10, 5): Money(29051, "EUR"),
        date(2026, 10, 6): Money(29051, "EUR"),
        date(2026, 10, 7): Money(29051, "EUR"),
    }


@pytest.mark.asyncio
async def test_kiwi_parses_recorded_string_amount_payload(monkeypatch):
    src = KiwiSource()
    seen: list[tuple[str, dict]] = []
    stub_fetch_json(src, load("kiwi_pricecalendar_BER_AYT.json"), monkeypatch, seen)

    prices = await src.calendar_range(
        "BER", "AYT", date(2026, 10, 1), date(2026, 10, 8)
    )

    assert src.indicative is True
    assert seen[0][1]["json_body"]["variables"]["filter"]["maxStopsCount"] == 1
    assert prices == {
        date(2026, 10, 1): Money(8400, "EUR"),
        date(2026, 10, 2): Money(11000, "EUR"),
        date(2026, 10, 3): Money(10300, "EUR"),
        date(2026, 10, 4): Money(10000, "EUR"),
        date(2026, 10, 5): Money(10800, "EUR"),
        date(2026, 10, 6): Money(9000, "EUR"),
        date(2026, 10, 7): Money(7900, "EUR"),
        date(2026, 10, 8): Money(11300, "EUR"),
    }
