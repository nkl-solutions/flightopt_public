"""HTTP 409 heisst bei Ryanair: veralteter Client, nicht kaputte Strecke.

Ein 409 darf die Quelle nicht fuer den ganzen Lauf ausknipsen. Der Adapter liest
die Client-Version genau einmal neu und versucht es genau einmal erneut - sonst
wuerde ein dauerhafter 409 die Seite in einer Schleife abrufen.
"""

from __future__ import annotations

from datetime import date

import pytest

from flightopt.sources.base import SourceError
from flightopt.sources.ryanair import CLIENT_VERSION_RE, RyanairSource


@pytest.fixture
def ryanair(monkeypatch):
    src = RyanairSource()

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)
    return src


def test_client_version_regex_reads_the_page_config():
    page = 'window.__CONFIG__={clientVersion:"3.140.0",locale:"de"}'

    assert CLIENT_VERSION_RE.search(page).group(1) == "3.140.0"
    assert CLIENT_VERSION_RE.search('{"client-version":"3.141.2"}').group(1) == "3.141.2"
    assert CLIENT_VERSION_RE.search("<html><body>nichts</body></html>") is None


async def test_a_409_triggers_exactly_one_repin_and_one_retry(ryanair, monkeypatch):
    calls: list[dict] = []
    repins: list[str] = []

    async def fake_fetch(url, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise SourceError("ryanair: HTTP 409 Availability declined")
        return {"outbound": {"fares": [
            {"day": "2026-10-05", "price": {"value": 46.97, "currencyCode": "EUR"}},
        ]}}

    async def fake_repin():
        repins.append("read")
        ryanair._client_version = "9.9.9"

    monkeypatch.setattr(ryanair, "fetch_json", fake_fetch)
    monkeypatch.setattr(ryanair, "_read_client_version", fake_repin)

    prices = await ryanair.calendar("BER", "ATH", date(2026, 10, 1))

    assert len(repins) == 1
    assert len(calls) == 2
    assert calls[1]["headers"]["client-version"] == "9.9.9"
    assert list(prices) == [date(2026, 10, 5)]


async def test_a_second_409_is_not_repinned_again(ryanair, monkeypatch):
    calls: list[dict] = []
    repins: list[str] = []

    async def always_409(url, **kw):
        calls.append(kw)
        raise SourceError("ryanair: HTTP 409 Availability declined")

    async def fake_repin():
        repins.append("read")
        ryanair._client_version = "9.9.9"

    monkeypatch.setattr(ryanair, "fetch_json", always_409)
    monkeypatch.setattr(ryanair, "_read_client_version", fake_repin)

    with pytest.raises(SourceError):
        await ryanair.calendar("BER", "ATH", date(2026, 10, 1))

    assert len(repins) == 1
    assert len(calls) == 2


async def test_another_error_is_not_mistaken_for_a_stale_client(ryanair, monkeypatch):
    repins: list[str] = []

    async def fake_fetch(url, **kw):
        raise SourceError("ryanair: HTTP 404 not found")

    async def fake_repin():
        repins.append("read")

    monkeypatch.setattr(ryanair, "fetch_json", fake_fetch)
    monkeypatch.setattr(ryanair, "_read_client_version", fake_repin)

    with pytest.raises(SourceError):
        await ryanair.calendar("BER", "ATH", date(2026, 10, 1))

    assert repins == []


async def test_search_leg_repins_as_well(ryanair, monkeypatch):
    calls: list[dict] = []

    async def fake_fetch(url, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise SourceError("ryanair: HTTP 409 Availability declined")
        return {"fares": []}

    async def fake_repin():
        ryanair._client_version = "9.9.9"

    monkeypatch.setattr(ryanair, "fetch_json", fake_fetch)
    monkeypatch.setattr(ryanair, "_read_client_version", fake_repin)

    assert await ryanair.search_leg("BER", "ATH", date(2026, 10, 5)) == []
    assert len(calls) == 2
