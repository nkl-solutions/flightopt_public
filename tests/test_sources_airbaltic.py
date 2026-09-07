"""airBaltic against the recorded BER-RIX answer.

The two traps are pinned here: days without a fare come back as `price: null`
and must not turn into a zero, and one call always covers about a year, so the
window has to be cut client side.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from flightopt.domain.models import Money
from flightopt.sources.airbaltic import AirBalticSource

FIXTURE = Path(__file__).parent / "fixtures" / "airbaltic_fsf_BER_RIX.json"


@pytest.fixture
def airbaltic(monkeypatch):
    src = AirBalticSource()

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)
    return src


@pytest.mark.asyncio
async def test_airbaltic_parses_the_recorded_answer(airbaltic, monkeypatch):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake(url, **kw):
        return payload

    monkeypatch.setattr(airbaltic, "fetch_json", fake)
    start, end = date(2026, 11, 1), date(2026, 11, 30)
    prices = await airbaltic.calendar_range("BER", "RIX", start, end)

    assert prices
    assert all(start <= day <= end for day in prices)
    assert all(money.currency == "EUR" and money.minor > 0 for money in prices.values())


@pytest.mark.asyncio
async def test_airbaltic_drops_days_without_a_fare(airbaltic, monkeypatch):
    async def fake(url, **kw):
        return {"success": True, "error": "", "data": [
            {"price": None, "updatedPrice": None, "date": "2026-11-01", "isDirect": True},
            {"price": 77.99, "updatedPrice": None, "date": "2026-11-02", "isDirect": True},
            {"price": 99.99, "updatedPrice": 88.5, "date": "2026-11-03", "isDirect": True},
        ]}

    monkeypatch.setattr(airbaltic, "fetch_json", fake)
    prices = await airbaltic.calendar_range(
        "BER", "RIX", date(2026, 11, 1), date(2026, 11, 30)
    )

    assert prices == {
        date(2026, 11, 2): Money(7799, "EUR"),
        date(2026, 11, 3): Money(8850, "EUR"),
    }
