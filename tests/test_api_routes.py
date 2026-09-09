"""Der Streckennetz-Endpunkt.

"Kein Flughafen bedient" und "nichts hat geantwortet" sind zwei Aussagen. Der
Endpunkt lieferte fuer beide 200 mit leerer Liste.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from flightopt.api import main


class Stub:
    def __init__(self, name: str, destinations: set[str] | None = None,
                 fails: bool = False) -> None:
        self.name = name
        self.destinations = destinations or set()
        self.fails = fails

    async def load_routes(self, origin: str) -> set[str]:
        if self.fails:
            raise RuntimeError(f"{self.name} antwortet nicht")
        return self.destinations


class Mute:
    """Eine Quelle ohne Streckennetz. Sie wird gar nicht erst gefragt."""

    name = "mute"


def catalogue(monkeypatch, *sources) -> None:
    monkeypatch.setattr(main, "build_sources", lambda *a, **k: list(sources))


@pytest.mark.asyncio
async def test_a_served_airport_lists_its_destinations(monkeypatch):
    catalogue(monkeypatch, Stub("ryanair", {"ATH", "FCO"}), Stub("wizz", {"ATH"}), Mute())

    answer = await main.routes("ber")

    assert answer["origin"] == "BER"
    assert answer["destinations"] == ["ATH", "FCO"]
    assert answer["sources"] == {"asked": 2, "failed": []}


@pytest.mark.asyncio
async def test_an_unserved_airport_is_an_honest_empty_list(monkeypatch):
    catalogue(monkeypatch, Stub("ryanair", set()), Stub("wizz", set()))

    answer = await main.routes("XXX")

    assert answer["destinations"] == []
    assert answer["sources"]["failed"] == []


@pytest.mark.asyncio
async def test_one_broken_source_does_not_empty_the_list(monkeypatch):
    catalogue(monkeypatch, Stub("ryanair", {"ATH"}), Stub("wizz", fails=True))

    answer = await main.routes("BER")

    assert answer["destinations"] == ["ATH"]
    # Die Teilauskunft bleibt als solche erkennbar.
    assert answer["sources"] == {"asked": 2, "failed": ["wizz"]}


@pytest.mark.asyncio
async def test_all_sources_broken_is_not_an_empty_route_map(monkeypatch):
    catalogue(monkeypatch, Stub("ryanair", fails=True), Stub("wizz", fails=True))

    with pytest.raises(HTTPException) as failure:
        await main.routes("BER")

    assert failure.value.status_code == 503
    assert "ryanair" in failure.value.detail


@pytest.mark.asyncio
async def test_a_catalogue_without_route_maps_says_so(monkeypatch):
    catalogue(monkeypatch, Mute())

    with pytest.raises(HTTPException) as failure:
        await main.routes("BER")

    assert failure.value.status_code == 503
