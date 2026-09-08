"""Fill the (leg, date) price grid from the registered sources.

This is where the request-count saving actually happens: the grid needs one
price per leg per date, and calendar endpoints deliver a whole month per call,
so a three-leg search over two months costs a handful of requests rather than
thousands.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable

from flightopt.domain import airports as airport_registry
from flightopt.domain import fx
from flightopt.domain.fx import Rates
from flightopt.domain.models import Money, Offer, SearchSpec
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
    native: dict[tuple[int, date], Money] = field(default_factory=dict)
    """(leg, date) -> price in the source's own currency, when converted."""

    def coverage(self, spec: SearchSpec) -> dict[int, float]:
        allowed = feasible_dates(spec)
        return {
            i: (self.filled.get(i, 0) / len(allowed[i]) if allowed[i] else 0.0)
            for i in range(len(spec.legs))
        }

    def missing_legs(self, spec: SearchSpec) -> list[int]:
        return [i for i in range(len(spec.legs)) if self.filled.get(i, 0) == 0]


def convert_prices(
    prices: dict[date, Money], to: str, rates: Rates | None
) -> tuple[dict[date, Money], dict[date, Money]]:
    """Bring a calendar into the search currency.

    Returns the converted prices and, separately, the originals for those days
    that needed converting. A day we cannot convert is dropped rather than
    guessed: a wrong number in the grid silently wins the optimizer.
    """
    converted: dict[date, Money] = {}
    native: dict[date, Money] = {}
    for day, money in prices.items():
        if money.currency.upper() == to.upper():
            converted[day] = money
            continue
        if rates is None:
            logger.warning("kein Kurs geladen: %s am %s entfaellt", money.currency, day)
            continue
        try:
            converted[day] = fx.convert(money, to, rates)
        except fx.UnknownCurrency as exc:
            logger.warning("%s: %s entfaellt", exc, day)
            continue
        native[day] = money
    return converted, native


def convert_offer(offer: Offer, to: str, rates: Rates | None) -> Offer:
    """Same rule for a live offer. Raises when the rate is missing."""
    if offer.price.currency.upper() == to.upper():
        return offer
    if rates is None:
        raise fx.UnknownCurrency(f"kein Kurs fuer {offer.price.currency}")
    return replace(offer, price=fx.convert(offer.price, to, rates), price_native=offer.price)


async def build_grid(
    spec: SearchSpec,
    sources: list,
    *,
    cache: SqliteCache | None = None,
    history: SqliteHistory | None = None,
    rates: Rates | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[PriceGrid, GridReport]:
    """Query every source for every leg, keeping the cheapest price per date.

    Legs are fetched concurrently, but each source paces itself internally, so
    concurrency here does not translate into a burst against one host.
    """
    allowed = feasible_dates(spec)
    report = GridReport()
    grid: PriceGrid = {i: {} for i in range(len(spec.legs))}
    tasks = []
    for index, leg in enumerate(spec.legs):
        window = allowed[index]
        if not window:
            continue
        for source in sources:
            if not getattr(source, "supports_calendar", False):
                continue
            if not source.supports_route(leg.origin, leg.destination):
                logger.info(
                    "%s: skipping %s-%s (route not served)",
                    source.name, leg.origin, leg.destination,
                )
                continue
            tasks.append((index, source))

    done = 0
    total = len(tasks)
    if on_progress:
        on_progress(done, total)

    async def fill_source(index: int, source) -> None:
        nonlocal done
        leg = spec.legs[index]
        window = allowed[index]
        if not window:
            return
        lo, hi = min(window), max(window)
        # Decided before the cache is asked: the limit is part of what makes a
        # calendar this calendar, so it belongs in the key as well as the call.
        takes_stops = bool(getattr(source, "accepts_max_stops", False))
        stops: int | None = None
        if takes_stops:
            stops = (
                spec.max_stops
                if spec.max_stops is not None
                else airport_registry.auto_max_stops(leg.origin, leg.destination)
            )

        try:
            key = cache_key(
                source.name, "calendar", leg.origin, leg.destination, lo,
                pax=spec.pax.total, cabin=spec.cabin.value, currency=spec.currency,
                max_stops=stops,
            )
            prices: dict[date, Money] = {}

            if cache is not None:
                cached = await cache.get(key)
                if cached is not None:
                    report.cache_hits += 1
                    # Der Cache haelt den Originalpreis mit seiner Waehrung, damit
                    # ein Treffer genauso umgerechnet wird wie ein frischer Abruf.
                    for raw_day, raw_price in cached.items():
                        if isinstance(raw_price, list):
                            prices[date.fromisoformat(raw_day)] = Money(
                                int(raw_price[0]), str(raw_price[1])
                            )
                        else:
                            prices[date.fromisoformat(raw_day)] = Money(
                                int(raw_price), spec.currency
                            )

            if not prices:
                try:
                    report.calls += 1
                    kwargs: dict = {"currency": spec.currency}
                    if takes_stops:
                        kwargs["max_stops"] = stops
                    prices = await source.calendar_range(
                        leg.origin, leg.destination, lo, hi, **kwargs
                    )
                except SourceError as exc:
                    report.errors.append(f"{source.name} {leg.origin}-{leg.destination}: {exc}")
                    logger.warning("%s failed on leg %d: %s", source.name, index, exc)
                    return
                except Exception as exc:  # noqa: BLE001 - one source must not kill the search
                    report.errors.append(
                        f"{source.name} {leg.origin}-{leg.destination}: {type(exc).__name__}: {exc}"
                    )
                    logger.exception("%s crashed on leg %d", source.name, index)
                    return

                if cache is not None and prices:
                    await cache.put(
                        key,
                        {d.isoformat(): [m.minor, m.currency] for d, m in prices.items()},
                        TTL_CALENDAR,
                        source=source.name,
                    )

            converted, native = convert_prices(prices, spec.currency, rates)
            usable = {d: m for d, m in converted.items() if d in window}
            if not usable:
                return

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
                    if day in native:
                        report.native[(index, day)] = native[day]
                    else:
                        report.native.pop((index, day), None)

            if history is not None:
                # Zweiteilig, ohne Quelle: die Baseline soll sagen, was diese
                # Strecke ueblich kostet, nicht was sie bei einer Quelle kostet.
                # Die Quelle steht ohnehin in der Spalte `source`. Dreiteilig
                # geschrieben und zweiteilig gelesen hiess: nie eine Baseline.
                entity = f"{leg.origin}|{leg.destination}"
                # Ob diese Quelle einen Richtwert liefert, steht drei Zeilen
                # weiter oben schon im Bericht. Dieselbe Frage, dieselbe
                # Antwort - nur wandert sie hier in die Zeile, damit die
                # Baseline sie spaeter noch beantworten kann.
                indicative = bool(getattr(source, "indicative", False))
                for day, money in usable.items():
                    await history.record(
                        source=source.name,
                        entity_type="flight",
                        entity_key=entity,
                        travel_date=day,
                        price=money,
                        party_size=spec.pax.total,
                        is_estimate=True,
                        is_indicative=indicative,
                    )
        finally:
            done += 1
            if on_progress:
                on_progress(done, total)

    await asyncio.gather(*(fill_source(index, source) for index, source in tasks))
    for index in range(len(spec.legs)):
        report.filled[index] = len(grid[index])
    return grid, report
