"""Per-leg evidence must follow existing lookups without additional requests."""

from dataclasses import asdict
from datetime import date
import socket

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import Money, Offer
from flightopt.search import verify as verify_module
from flightopt.search.verify import VerifiedItinerary, verify
from flightopt.sources.base import SourceError
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache
from tests.test_verify_order import combo, make_spec

DAY = date(2026, 10, 1)


class DaySource:
    supports_search = True
    supports_calendar = False

    def __init__(self, name="airline", *, price=5000, currency="EUR",
                 carrier="AA", indicative=False, estimate=False, error=None,
                 supports=True, extra_prices=()):
        self.name = name
        self.price = price
        self.currency = currency
        self.carrier = carrier
        self.indicative = indicative
        self.estimate = estimate
        self.error = error
        self.supports = supports
        self.extra_prices = extra_prices
        self.asked = []

    def supports_route(self, origin, destination):
        return self.supports

    async def search_leg(self, origin, destination, day, *, pax, cabin, currency):
        self.asked.append((origin, destination, day, pax.total, cabin.value, currency))
        if self.error is not None:
            raise self.error
        prices = [] if self.price is None else [self.price, *self.extra_prices]
        return [Offer(source=self.name, origin=origin, destination=destination,
                      travel_date=day, price=Money(price, self.currency),
                      is_estimate=self.estimate) for price in prices]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect

    def forbidden(*args, **kwargs):
        pytest.fail("Verification diagnostics must not open network connections")

    def local_only(sock, address):
        # Windows asyncio creates its self-pipe using a loopback socket pair.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr("socket.socket.connect", local_only)
    monkeypatch.setattr("flightopt.sources.base.HttpSource._new_session", forbidden)


@pytest.fixture
def cache(tmp_path):
    conn = db.connect(str(tmp_path / "verify.db"))
    yield SqliteCache(conn)
    conn.close()


async def run_one(sources, **kwargs):
    items, report = await verify(make_spec(1), [combo((2026, 10, 1))], sources, **kwargs)
    return items[0], report


def evidence(item):
    assert hasattr(item, "verification"), "Per-leg diagnostics are missing"
    assert len(item.verification) == len(item.offers)
    return asdict(item.verification[0])


def test_diagnostic_defaults_and_legacy_itinerary_construction():
    assert hasattr(verify_module, "LegVerification"), "Diagnostic dataclass is missing"
    assert asdict(verify_module.LegVerification("unsupported")) == {
        "status": "unsupported", "method": None, "source": None,
        "checked_sources": (), "failed_sources": (),
    }
    first = VerifiedItinerary(combo((2026, 10, 1)), [None])
    second = VerifiedItinerary(combo((2026, 10, 1)), [None])
    assert first.verification == second.verification == []
    first.verification.append(verify_module.LegVerification("unsupported"))
    assert second.verification == []


@pytest.mark.parametrize("kind", ["none", "calendar_only", "unsupported_route"])
async def test_no_usable_day_source_is_unsupported(kind):
    source = DaySource(supports=kind != "unsupported_route")
    source.supports_search = kind != "calendar_only"
    source.supports_calendar = kind == "calendar_only"
    item, report = await run_one([] if kind == "none" else [source])
    assert evidence(item) == {
        "status": "unsupported", "method": None, "source": None,
        "checked_sources": (), "failed_sources": (),
    }
    assert source.asked == []
    assert (report.calls, report.cache_hits, report.confirmed, report.partial) == (0, 0, 0, 1)
    assert report.errors == []


async def test_successful_empty_day_search_is_unavailable():
    source = DaySource(price=None)
    item, report = await run_one([source])
    assert evidence(item) == {
        "status": "unavailable", "method": None, "source": None,
        "checked_sources": ("airline",), "failed_sources": (),
    }
    assert source.asked == [("BER", "ATH", DAY, 1, "economy", "EUR")]
    assert (report.calls, report.partial, report.errors) == (1, 1, [])


@pytest.mark.parametrize("error_type", [SourceError, RuntimeError])
async def test_search_failure_is_error_and_contains_only_source_names(error_type):
    source = DaySource(error=error_type("secret-token=do-not-copy"))
    item, report = await run_one([source])
    assert evidence(item) == {
        "status": "error", "method": None, "source": None,
        "checked_sources": ("airline",), "failed_sources": ("airline",),
    }
    assert "secret-token" not in repr(item.verification)
    assert (report.calls, report.partial, len(report.errors)) == (1, 1, 1)
    assert len(source.asked) == 1


@pytest.mark.parametrize("rates", [None, Rates()])
async def test_fx_failure_remains_error_when_another_source_returns_empty(rates):
    foreign = DaySource("foreign", currency="ZZZ", extra_prices=(4000,))
    empty = DaySource("empty", price=None, carrier="")
    item, report = await run_one([empty, foreign], rates=rates)
    assert evidence(item) == {
        "status": "error", "method": None, "source": None,
        "checked_sources": ("foreign", "empty"), "failed_sources": ("foreign",),
    }
    assert (report.calls, report.partial, len(report.errors)) == (2, 1, 1)
    assert len(foreign.asked) == len(empty.asked) == 1


@pytest.mark.parametrize("failure", ["search", "fx"])
async def test_another_winner_is_verified_and_keeps_prior_failure(failure):
    failed = DaySource("failed", error=SourceError("private") if failure == "search" else None,
                       currency="ZZZ" if failure == "fx" else "EUR")
    winner = DaySource("winner", carrier="")
    item, report = await run_one([winner, failed])
    assert evidence(item) == {
        "status": "verified", "method": "live", "source": "winner",
        "checked_sources": ("failed", "winner"), "failed_sources": ("failed",),
    }
    assert item.offers[0].source == "winner"
    assert (report.calls, report.confirmed, report.partial, len(report.errors)) == (2, 1, 0, 1)


@pytest.mark.parametrize("estimate,indicative,status", [
    (False, False, "verified"), (True, False, "estimate"),
    (False, True, "indicative"), (True, True, "indicative"),
])
async def test_selected_offer_classification_survives_cache(cache, estimate, indicative, status):
    source = DaySource(estimate=estimate, indicative=indicative)
    live, first = await run_one([source], cache=cache)
    cached, second = await run_one([source], cache=cache)
    expected = {"status": status, "source": "airline",
                "checked_sources": ("airline",), "failed_sources": ()}
    assert evidence(live) == dict(expected, method="live")
    assert evidence(cached) == dict(expected, method="cache")
    assert (first.calls, first.cache_hits, second.calls, second.cache_hits) == (1, 0, 0, 1)
    assert (first.confirmed, second.confirmed) == (1, 1)
    assert len(source.asked) == 1


async def test_cached_empty_is_checked_without_another_provider_call(cache):
    source = DaySource(price=None)
    live, _ = await run_one([source], cache=cache)
    cached, report = await run_one([source], cache=cache)
    expected = {"status": "unavailable", "method": None, "source": None,
                "checked_sources": ("airline",), "failed_sources": ()}
    assert evidence(live) == evidence(cached) == expected
    assert (report.calls, report.cache_hits, report.partial) == (0, 1, 1)
    assert len(source.asked) == 1


@pytest.mark.parametrize("cached_winner", [True, False])
async def test_method_belongs_to_selected_offer_not_last_queried_source(cache, cached_winner):
    warm = DaySource("warm", price=4000 if cached_winner else 6000)
    await run_one([warm], cache=cache)
    fresh = DaySource("fresh", price=5000)
    sources = [warm, fresh] if cached_winner else [fresh, warm]
    item, report = await run_one(sources, cache=cache)
    assert evidence(item) == {
        "status": "verified", "method": "cache" if cached_winner else "live",
        "source": "warm" if cached_winner else "fresh",
        "checked_sources": tuple(s.name for s in sources), "failed_sources": (),
    }
    assert (report.calls, report.cache_hits) == (1, 1)
    assert len(warm.asked) == len(fresh.asked) == 1


async def test_airline_selection_preserves_order_ties_and_skips_collectors():
    collector = DaySource("collector", carrier="", price=1)
    first = DaySource("first", price=5000)
    tied = DaySource("tied", price=5000)
    failed = DaySource("failed", error=SourceError("private"))
    item, report = await run_one([collector, first, tied, failed])
    assert evidence(item) == {
        "status": "verified", "method": "live", "source": "first",
        "checked_sources": ("first", "tied", "failed"), "failed_sources": ("failed",),
    }
    assert item.offers[0].price == Money(5000)
    assert collector.asked == []
    assert (report.calls, report.confirmed) == (3, 1)


async def test_shared_pairs_share_diagnostics_and_do_not_duplicate_calls():
    source = DaySource(error=SourceError("same failure"))
    combinations = [combo((2026, 10, 1), (2026, 10, 5)),
                    combo((2026, 10, 1), (2026, 10, 6))]
    items, report = await verify(make_spec(2), combinations, [source])
    assert len(items[0].verification) == len(items[1].verification) == 2
    assert items[0].verification[0] is items[1].verification[0]
    assert [d.status for item in items for d in item.verification] == ["error"] * 4
    assert [d.failed_sources for item in items for d in item.verification] == [("airline",)] * 4
    assert len(source.asked) == report.calls == 3
    assert (report.partial, len(report.errors)) == (2, 1)


@pytest.mark.parametrize("limit", [0, 1, 2])
async def test_only_shortlist_is_queried_and_diagnostics_follow_live_ranking(limit):
    class DateSource(DaySource):
        async def search_leg(self, origin, destination, day, **kwargs):
            self.price = 8000 if day == DAY else 4000
            return await super().search_leg(origin, destination, day, **kwargs)

    source = DateSource()
    combinations = [combo((2026, 10, 1), price=5000),
                    combo((2026, 10, 2), price=6000),
                    combo((2026, 10, 3), price=7000)]
    progress = []
    items, report = await verify(make_spec(1), combinations, [source], limit=limit,
                                 on_progress=lambda done, total: progress.append((done, total)))
    assert len(items) == len(source.asked) == report.calls == report.confirmed == limit
    assert all(evidence(item)["status"] == "verified" for item in items)
    assert [call[2] for call in source.asked] == [DAY, date(2026, 10, 2)][:limit]
    assert [item.combination for item in items] == combinations[:limit][::-1]
    assert progress == ([(0, limit), *[(i, limit) for i in range(1, limit + 1)]] if limit else [])
