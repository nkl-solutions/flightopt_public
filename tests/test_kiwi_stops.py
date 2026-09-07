"""Umstiege pro Leg.

Berlin nach Tokio hat keinen Nonstop. Mit einer Umstiegsgrenze von eins liefert
Kiwi fuer solche Legs einen leeren Kalender, und die ganze Suche scheitert an
einem Teilstueck. Die Grenze haengt deshalb an der Streckenlaenge.
"""

from __future__ import annotations

from datetime import date

import pytest

from flightopt.api.main import SearchRequest
from flightopt.domain.models import LegSpec, SearchSpec, StayRange
from flightopt.search.grid import build_grid
from flightopt.sources.kiwi import KiwiSource

EMPTY_CALENDAR = {
    "data": {
        "itineraryPricesCalendar": {
            "__typename": "ItineraryPricesCalendar",
            "currency": {"code": "EUR"},
            "calendar": [],
        }
    }
}


def two_leg_spec(max_stops: int | None = None) -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "AYT"), LegSpec("AYT", "NRT")),
        stays=(StayRange(2, 3),),
        window_start=date(2027, 3, 1),
        window_end=date(2027, 3, 10),
        max_stops=max_stops,
    )


class RecordingCollector:
    """Eine Quelle, die den Wert annimmt - wie Kiwi."""

    name = "collector"
    carrier = ""
    carriers = ()
    supports_calendar = True
    supports_search = False
    indicative = True
    accepts_max_stops = True

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int | None]] = []

    def supports_route(self, origin, destination):
        return True

    async def calendar_range(self, origin, destination, start, end, *,
                             currency="EUR", max_stops=None):
        self.seen.append((origin, destination, max_stops))
        return {}


class StrictAirline:
    """Eine Quelle ohne das Flag: sie darf den Parameter nie sehen."""

    name = "strict"
    carrier = "ZZ"
    carriers = ("ZZ",)
    supports_calendar = True
    supports_search = False
    indicative = False

    def __init__(self) -> None:
        self.calls = 0

    def supports_route(self, origin, destination):
        return True

    async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
        self.calls += 1
        return {}


@pytest.mark.asyncio
async def test_short_legs_get_one_stop_long_legs_two():
    collector = RecordingCollector()
    await build_grid(two_leg_spec(), [collector])

    assert sorted(collector.seen) == [("AYT", "NRT", 2), ("BER", "AYT", 1)]


@pytest.mark.asyncio
async def test_an_explicit_limit_wins_over_the_automatic_one():
    collector = RecordingCollector()
    await build_grid(two_leg_spec(max_stops=0), [collector])

    assert sorted(collector.seen) == [("AYT", "NRT", 0), ("BER", "AYT", 0)]


@pytest.mark.asyncio
async def test_sources_without_the_flag_are_called_unchanged():
    strict = StrictAirline()
    await build_grid(two_leg_spec(), [strict])

    assert strict.calls == 2


@pytest.mark.asyncio
async def test_kiwi_puts_the_stop_count_into_the_request(monkeypatch):
    src = KiwiSource()
    seen: dict = {}

    async def fake(url, *, json_body=None, headers=None, **kw):
        seen["body"] = json_body
        return EMPTY_CALENDAR

    monkeypatch.setattr(src, "fetch_json", fake)
    await src.calendar_range("BER", "NRT", date(2027, 3, 1), date(2027, 3, 5), max_stops=2)

    assert seen["body"]["variables"]["filter"]["maxStopsCount"] == 2


@pytest.mark.asyncio
async def test_kiwi_falls_back_to_its_own_default(monkeypatch):
    src = KiwiSource(max_stops=1)
    seen: dict = {}

    async def fake(url, *, json_body=None, headers=None, **kw):
        seen["body"] = json_body
        return EMPTY_CALENDAR

    monkeypatch.setattr(src, "fetch_json", fake)
    await src.calendar_range("BER", "AYT", date(2027, 3, 1), date(2027, 3, 5))

    assert seen["body"]["variables"]["filter"]["maxStopsCount"] == 1


def test_the_api_passes_the_stop_limit_into_the_spec():
    req = SearchRequest(
        airports=["BER", "NRT"],
        trip="one_way",
        window_start=date(2027, 3, 1),
        window_end=date(2027, 4, 30),
        max_stops=2,
    )

    assert req.to_spec().max_stops == 2
    assert SearchRequest(
        airports=["BER", "NRT"],
        trip="one_way",
        window_start=date(2027, 3, 1),
        window_end=date(2027, 4, 30),
    ).to_spec().max_stops is None


def test_the_stop_limit_is_serialised_with_the_job():
    from flightopt.jobs.runner import spec_to_dict

    assert spec_to_dict(two_leg_spec(max_stops=2))["max_stops"] == 2
    assert spec_to_dict(two_leg_spec())["max_stops"] is None


# --- Umstiege gehoeren in den Cache-Schluessel --------------------------------


class RecordingCache:
    """Merkt sich nur, wonach der Grid gefragt hat."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def get(self, key):
        self.keys.append(key)
        return None

    async def put(self, key, payload, ttl, *, source=""):
        self.keys.append(key)


def test_the_cache_key_separates_stop_limits():
    from flightopt.storage.cache import cache_key

    day = date(2027, 3, 1)
    nonstop = cache_key("kiwi", "calendar", "BER", "NRT", day, max_stops=0)
    two = cache_key("kiwi", "calendar", "BER", "NRT", day, max_stops=2)

    assert nonstop != two


def test_a_key_without_a_stop_limit_keeps_its_old_shape():
    """Sonst waeren alle bestehenden Cache-Zeilen mit einem Schlag wertlos."""
    import hashlib

    from flightopt.storage.cache import cache_key

    day = date(2027, 3, 1)
    old = hashlib.sha256(
        b"kiwi|calendar|BER|NRT|2027-03-01|1|economy|EUR"
    ).hexdigest()[:32]

    assert cache_key("kiwi", "calendar", "BER", "NRT", day) == old
    assert cache_key("kiwi", "calendar", "BER", "NRT", day, max_stops=None) == old


@pytest.mark.asyncio
async def test_two_stop_limits_never_share_a_cached_calendar():
    nonstop, two = RecordingCache(), RecordingCache()

    await build_grid(two_leg_spec(max_stops=0), [RecordingCollector()], cache=nonstop)
    await build_grid(two_leg_spec(max_stops=2), [RecordingCollector()], cache=two)

    assert nonstop.keys and two.keys
    assert set(nonstop.keys).isdisjoint(two.keys)


@pytest.mark.asyncio
async def test_a_source_without_the_flag_keeps_its_old_cache_key():
    """Ihre Preise haengen nicht an Umstiegen, also darf der Schluessel nicht wandern."""
    from flightopt.storage.cache import cache_key

    seen = RecordingCache()
    await build_grid(two_leg_spec(max_stops=0), [StrictAirline()], cache=seen)

    assert cache_key(
        "strict", "calendar", "BER", "AYT", date(2027, 3, 1),
        pax=1, cabin="economy", currency="EUR",
    ) in seen.keys


def test_a_saved_profile_remembers_the_stop_limit():
    from flightopt.jobs.daily import _spec_from_dict
    from flightopt.jobs.runner import spec_to_dict

    for wanted in (0, 1, 2, None):
        restored = _spec_from_dict(spec_to_dict(two_leg_spec(max_stops=wanted)))
        assert restored.max_stops == wanted
