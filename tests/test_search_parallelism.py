"""Search phases should overlap independent remote work."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from flightopt.domain.models import Cabin, LegSpec, Money, Offer, Pax, SearchSpec, StayRange
from flightopt.jobs.runner import preload_routes
from flightopt.search.dp import Combination
from flightopt.search.grid import build_grid
from flightopt.search.verify import verify


DAY = date(2026, 10, 1)


class CalendarSource:
    supports_calendar = True
    supports_search = False
    carrier = "FA"
    carriers = ("FA",)
    indicative = False

    def __init__(self, name: str, ready: asyncio.Event, both_started: asyncio.Event):
        self.name = name
        self.ready = ready
        self.both_started = both_started

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
        self.ready.set()
        await self.both_started.wait()
        return {DAY: Money(10000, currency)}


class SearchSource:
    supports_calendar = False
    supports_search = True
    name = "fast"
    carrier = "FA"
    carriers = ("FA",)

    def __init__(self, origin: str, ready: asyncio.Event, both_started: asyncio.Event):
        self.origin = origin
        self.ready = ready
        self.both_started = both_started

    def supports_route(self, origin: str, destination: str) -> bool:
        return origin == self.origin

    async def search_leg(
        self,
        origin,
        destination,
        day,
        *,
        pax: Pax = Pax(),
        cabin: Cabin = Cabin.ECONOMY,
        currency="EUR",
    ):
        self.ready.set()
        await self.both_started.wait()
        return [
            Offer(
                source=self.name,
                origin=origin,
                destination=destination,
                travel_date=day,
                price=Money(10000, currency),
                fetched_at=datetime(2026, 1, 1),
            )
        ]


class RouteSource:
    def __init__(self, ready: asyncio.Event, both_started: asyncio.Event):
        self.ready = ready
        self.both_started = both_started
        self.seen = []

    async def load_routes(self, origin: str):
        self.seen.append(origin)
        self.ready.set()
        await self.both_started.wait()
        return {"ATH"}


def one_leg_spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"),),
        stays=(),
        window_start=DAY,
        window_end=DAY,
    )


def two_leg_spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(0, 0),),
        window_start=DAY,
        window_end=DAY,
    )


@pytest.mark.asyncio
async def test_build_grid_accepts_progress_callback():
    events = []

    await build_grid(one_leg_spec(), [], on_progress=lambda done, total: events.append((done, total)))

    assert events[-1] == (0, 0)


@pytest.mark.asyncio
async def test_build_grid_queries_sources_for_same_leg_concurrently():
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()
    both_started = asyncio.Event()

    async def release_when_both_started():
        await first_ready.wait()
        await second_ready.wait()
        both_started.set()

    releaser = asyncio.create_task(release_when_both_started())
    try:
        await asyncio.wait_for(
            build_grid(
                one_leg_spec(),
                [
                    CalendarSource("one", first_ready, both_started),
                    CalendarSource("two", second_ready, both_started),
                ],
            ),
            timeout=0.2,
        )
    finally:
        releaser.cancel()


@pytest.mark.asyncio
async def test_preload_routes_queries_sources_concurrently():
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()
    both_started = asyncio.Event()
    first = RouteSource(first_ready, both_started)
    second = RouteSource(second_ready, both_started)

    async def release_when_both_started():
        await first_ready.wait()
        await second_ready.wait()
        both_started.set()

    releaser = asyncio.create_task(release_when_both_started())
    try:
        await asyncio.wait_for(
            preload_routes([first, second], one_leg_spec().legs),
            timeout=0.2,
        )
    finally:
        releaser.cancel()

    assert first.seen == ["BER"]
    assert second.seen == ["BER"]


@pytest.mark.asyncio
async def test_verify_queries_distinct_leg_dates_concurrently():
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()
    both_started = asyncio.Event()
    spec = two_leg_spec()
    combo = Combination((DAY, DAY), Money(20000))

    async def release_when_both_started():
        await first_ready.wait()
        await second_ready.wait()
        both_started.set()

    releaser = asyncio.create_task(release_when_both_started())
    try:
        verified, report = await asyncio.wait_for(
            verify(
                spec,
                [combo],
                [
                    SearchSource("BER", first_ready, both_started),
                    SearchSource("ATH", second_ready, both_started),
                ],
                limit=1,
            ),
            timeout=0.2,
        )
    finally:
        releaser.cancel()

    assert report.confirmed == 1
    assert verified[0].complete is True
