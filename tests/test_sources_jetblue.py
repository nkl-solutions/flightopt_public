"""JetBlue against the recorded JFK-LAX month.

Pinned here: the month token must stay English and upper case whatever the
process locale is, and the answer's own currency must survive to the grid
instead of being relabelled EUR.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from flightopt.domain.models import Money
from flightopt.sources.jetblue import JetBlueSource, month_token

FIXTURE = Path(__file__).parent / "fixtures" / "jetblue_bestfares_JFK_LAX.json"


@pytest.fixture
def jetblue(monkeypatch):
    src = JetBlueSource()

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)
    return src


def test_month_token_is_english_and_upper_case():
    assert month_token(date(2026, 11, 1)) == "NOVEMBER 2026"
    assert month_token(date(2027, 3, 14)) == "MARCH 2027"


@pytest.mark.asyncio
async def test_jetblue_parses_the_recorded_month(jetblue, monkeypatch):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sent: list[dict] = []

    async def fake(url, **kw):
        sent.append(kw.get("json_body") or {})
        return payload

    monkeypatch.setattr(jetblue, "fetch_json", fake)
    prices = await jetblue.calendar("JFK", "LAX", date(2026, 11, 1))

    assert prices
    assert sent[0]["month"] == "NOVEMBER 2026"
    assert sent[0]["tripType"] == "ONE_WAY"
    assert all(money.currency == "USD" and money.minor > 0 for money in prices.values())
    assert all(day.month == 11 and day.year == 2026 for day in prices)


@pytest.mark.asyncio
async def test_jetblue_reports_the_answers_currency(jetblue, monkeypatch):
    async def fake(url, **kw):
        return {"currencyCode": "USD", "outboundFares": [
            {"date": "2026-11-01", "amount": 219, "tax": 0, "seats": 8},
            {"date": "2026-11-02", "amount": 184.5, "tax": 0, "seats": 9},
            {"date": "2026-11-03", "amount": None, "tax": 0, "seats": 0},
        ]}

    monkeypatch.setattr(jetblue, "fetch_json", fake)
    prices = await jetblue.calendar_range(
        "JFK", "LAX", date(2026, 11, 1), date(2026, 11, 30)
    )

    assert prices == {
        date(2026, 11, 1): Money(21900, "USD"),
        date(2026, 11, 2): Money(18450, "USD"),
    }
