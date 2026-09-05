"""Fill the (leg, date) price grid from the registered sources.

This is where the request-count saving actually happens: the grid needs one
price per leg per date, and calendar endpoints deliver a whole month per call,
so a three-leg search over two months costs a handful of requests rather than
thousands.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

from flightopt.domain.models import Money, SearchSpec
from flightopt.search.dp import PriceGrid, feasible_dates
from flightopt.sources.base import SourceError
from flightopt.storage.cache import TTL_CALENDAR, SqliteCache, SqliteHistory, cache_key

logger = logging.getLogger(__name__)


@dataclass
class GridReport:
    """What the grid pass actually managed to fetch."""

    filled: dict[int, int] = field(default_factory=dict)
    """leg_index -> number of priced dates"""
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cache_hits: int = 0
    calls: int = 0
    winner: dict[tuple[int, date], str] = field(default_factory=dict)
    """(leg, date) -> carrier code whose price won that cell."""
    indicative: set[tuple[int, date]] = field(default_factory=set)
    """Cells won by a source whose prices are known to be approximate."""

    def coverage(self, spec: SearchSpec) -> dict[int, float]:
        allowed = feasible_dates(spec)
        return {
            i: (self.filled.get(i, 0) / len(allowed[i]) if allowed[i] else 0.0)
            for i in range(len(spec.legs))
        }

    def missing_legs(self, spec: SearchSpec) -> list[int]:
        return [i for i in range(len(spec.legs)) if self.filled.get(i, 0) == 0]


async def build_grid(
    spec: SearchSpec,
    sources: list,
    *,
    cache: SqliteCache | None = None,
    history: SqliteHistory | None = None,
) -> tuple[PriceGrid, GridReport]:
    """Query every source for every leg, keeping the cheapest price per date.

    Legs are fetched concurrently, but each source paces itself internally, so
    concurrency here does not translate into a burst against one host.
    """
    allowed = feasible_dates(spec)
    report = GridReport()
    grid: PriceGrid = {i: {} for i in range(len(spec.legs))}

    async def fill_leg(index: int) -> None:
        leg = spec.legs[index]
        window = allowed[index]
        if not window:
            return
        lo, hi = min(window), max(window)

        for source in sources:
            if not getattr(source, "supports_calendar", False):
                continue
            if not source.supports_route(leg.origin, leg.destination):
                logger.info(
                    "%s: skipping %s-%s (route not served)",
                    source.name, leg.origin, leg.destination,
                )
                continue

            key = cache_key(
                source.name, "calendar", leg.origin, leg.destination, lo,
                pax=spec.pax.total, cabin=spec.cabin.value, currency=spec.currency,
            )
            prices: dict[date, Money] = {}

            if cache is not None:
                cached = await cache.get(key)
                if cached is not None:
                    report.cache_hits += 1
                    prices = {
                        date.fromisoformat(k): Money(v, spec.currency)
                        for k, v in cached.items()
                    }

            if not prices:
                try:
                    report.calls += 1
                    prices = await source.calendar_range(
                        leg.origin, leg.destination, lo, hi, currency=spec.currency
                    )
                except SourceError as exc:
                    report.errors.append(f"{source.name} {leg.origin}-{leg.destination}: {exc}")
                    logger.warning("%s failed on leg %d: %s", source.name, index, exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - one source must not kill the search
                    report.errors.append(
                        f"{source.name} {leg.origin}-{leg.destination}: {type(exc).__name__}: {exc}"
                    )
                    logger.exception("%s crashed on leg %d", source.name, index)
                    continue

                if cache is not None and prices:
                    await cache.put(
                        key,
                        {d.isoformat(): m.minor for d, m in prices.items()},
                        TTL_CALENDAR,
                        source=source.name,
                    )

            usable = {d: m for d, m in prices.items() if d in window}
            if not usable:
                continue

            report.per_source[source.name] = report.per_source.get(source.name, 0) + len(usable)
            for day, money in usable.items():
                current = grid[index].get(day)
                if current is None or money.minor < current.minor:
                    grid[index][day] = money
                    # Remember who won, so the result can name the airline even
                    # for a price that never reached live verification.
                    report.winner[(index, day)] = (
                        getattr(source, "carrier", "") or source.name.upper()
                    )
                    if getattr(source, "indicative", False):
                        report.indicative.add((index, day))
                    else:
                        report.indicative.discard((index, day))

            if history is not None:
                entity = f"{leg.origin}|{leg.destination}|{source.name}"
                for day, money in usable.items():
                    await history.record(
                        source=source.name,
                        entity_type="flight",
                        entity_key=entity,
                        travel_date=day,
                        price=money,
                        party_size=spec.pax.total,
                        is_estimate=True,
                    )

        report.filled[index] = len(grid[index])

    await asyncio.gather(*(fill_leg(i) for i in range(len(spec.legs))))
    return grid, report
