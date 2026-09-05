"""Phase two: turn shortlisted date combinations into bookable flights.

The calendar pass gives a cheapest-price-per-day figure with no flight behind
it. This pass asks each source for the actual departures on the dates that
survived the optimizer, so every result ends up with times, flight numbers and
a link to the airline's own booking page.

Only the shortlist is verified. Pricing all 2,800 combinations live would be
both slow and a good way to get blocked; pricing the best 10 costs a couple of
dozen requests because most of them share leg-date pairs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from flightopt.domain.models import Money, Offer, SearchSpec
from flightopt.search.dp import Combination
from flightopt.sources.base import SourceError
from flightopt.storage.cache import TTL_VERIFY, SqliteCache, cache_key

logger = logging.getLogger(__name__)


@dataclass
class VerifiedItinerary:
    """A combination after live lookup."""

    combination: Combination
    offers: list[Offer | None]
    """One entry per leg. None where no source could confirm a flight."""

    @property
    def complete(self) -> bool:
        return all(o is not None for o in self.offers)

    @property
    def total(self) -> Money | None:
        if not self.complete:
            return None
        total = Money(0, self.offers[0].price.currency)  # type: ignore[union-attr]
        for offer in self.offers:
            total = total + offer.price  # type: ignore[union-attr]
        return total

    @property
    def drift(self) -> int | None:
        """Live total minus the estimate, in minor units. Positive = got dearer."""
        live = self.total
        if live is None:
            return None
        return live.minor - self.combination.total.minor


@dataclass
class VerifyReport:
    calls: int = 0
    cache_hits: int = 0
    confirmed: int = 0
    partial: int = 0
    errors: list[str] = field(default_factory=list)


async def verify(
    spec: SearchSpec,
    combinations: list[Combination],
    sources: list,
    *,
    cache: SqliteCache | None = None,
    limit: int = 10,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[VerifiedItinerary], VerifyReport]:
    """Look up real flights for the first `limit` combinations."""
    shortlist = combinations[:limit]
    report = VerifyReport()
    if not shortlist:
        return [], report

    # Many combinations reuse the same leg-date, so resolve the distinct set once.
    wanted: set[tuple[int, date]] = {
        (i, day) for combo in shortlist for i, day in enumerate(combo.dates)
    }
    resolved: dict[tuple[int, date], Offer | None] = {}
    done = 0
    total = len(wanted)
    if on_progress:
        on_progress(0, total)

    for index, day in sorted(wanted, key=lambda p: (p[0], p[1])):
        leg = spec.legs[index]
        best: Offer | None = None

        for source in sources:
            if not getattr(source, "supports_search", False):
                continue
            if not source.supports_route(leg.origin, leg.destination):
                continue

            key = cache_key(
                source.name, "verify", leg.origin, leg.destination, day,
                pax=spec.pax.total, cabin=spec.cabin.value, currency=spec.currency,
            )
            offers: list[Offer] = []
            cached = await cache.get(key) if cache is not None else None
            if cached is not None:
                report.cache_hits += 1
                offers = [_offer_from_cache(row, source.name) for row in cached]
            else:
                try:
                    report.calls += 1
                    offers = await source.search_leg(
                        leg.origin, leg.destination, day,
                        pax=spec.pax, cabin=spec.cabin, currency=spec.currency,
                    )
                except SourceError as exc:
                    report.errors.append(
                        f"{source.name} {leg.origin}-{leg.destination} {day}: {exc}"
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(
                        f"{source.name} {leg.origin}-{leg.destination} {day}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    logger.exception("verify crashed on %s", leg)
                    continue

                if cache is not None:
                    await cache.put(
                        key, [_offer_to_cache(o) for o in offers],
                        TTL_VERIFY, source=source.name,
                    )

            for offer in offers:
                if best is None or offer.price.minor < best.price.minor:
                    best = offer

        resolved[(index, day)] = best
        done += 1
        if on_progress:
            on_progress(done, total)

    out: list[VerifiedItinerary] = []
    for combo in shortlist:
        offers = [resolved.get((i, d)) for i, d in enumerate(combo.dates)]
        item = VerifiedItinerary(combination=combo, offers=offers)
        if item.complete:
            report.confirmed += 1
        else:
            report.partial += 1
        out.append(item)

    # Re-rank on the live totals; a verified cheap option can overtake one that
    # only looked cheaper in the estimate. Unconfirmed ones sink to the bottom.
    out.sort(
        key=lambda it: (
            0 if it.complete else 1,
            it.total.minor if it.total else it.combination.total.minor,
        )
    )
    return out, report


def _offer_to_cache(offer: Offer) -> dict:
    return {
        "price": offer.price.minor,
        "currency": offer.price.currency,
        "origin": offer.origin,
        "destination": offer.destination,
        "travel_date": offer.travel_date.isoformat(),
        "deep_link": offer.deep_link,
        "segments": [
            {
                "carrier": s.carrier,
                "flight_number": s.flight_number,
                "origin": s.origin,
                "destination": s.destination,
                "departure": s.departure.isoformat(),
                "arrival": s.arrival.isoformat(),
            }
            for s in offer.segments
        ],
    }


def _offer_from_cache(row: dict, source: str) -> Offer:
    from datetime import datetime

    from flightopt.domain.models import Segment

    return Offer(
        source=source,
        origin=row["origin"],
        destination=row["destination"],
        travel_date=date.fromisoformat(row["travel_date"]),
        price=Money(row["price"], row["currency"]),
        segments=tuple(
            Segment(
                carrier=s["carrier"],
                flight_number=s["flight_number"],
                origin=s["origin"],
                destination=s["destination"],
                departure=datetime.fromisoformat(s["departure"]),
                arrival=datetime.fromisoformat(s["arrival"]),
            )
            for s in row.get("segments", [])
        ),
        deep_link=row.get("deep_link"),
        is_estimate=False,
    )
