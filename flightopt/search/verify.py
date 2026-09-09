"""Phase two: turn shortlisted date combinations into bookable flights.

The calendar pass gives a cheapest-price-per-day figure with no flight behind
it. This pass asks each source for the actual departures on the dates that
survived the optimizer, so every result ends up with times, flight numbers and
a link to the airline's own booking page.

Only the displayed shortlist is verified. Pricing all 2,800 combinations live
would be both slow and a good way to get blocked; pricing the visible candidate
pool stays manageable because most rows share leg-date pairs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from flightopt.domain import fx
from flightopt.domain.fx import Rates
from flightopt.domain.models import Money, Offer, SearchSpec
from flightopt.search.dp import Combination
from flightopt.search.grid import convert_offer
from flightopt.sources.base import SourceError
from flightopt.storage.cache import TTL_VERIFY, SqliteCache, SqliteHistory, cache_key

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


def pairs_by_rank(shortlist: list[Combination]) -> list[tuple[int, date]]:
    """The distinct (leg, day) pairs, ordered by the best candidate needing them.

    Sorting by `(leg_index, day)` looked tidy and was the wrong order. The UI
    streams a candidate as verified once all of its legs are in, so what
    matters is finishing whole candidates, cheapest first - not finishing leg 0
    for everybody. With the old order the favourite's outbound was resolved
    early and its last leg often last of all, so the row the user is actually
    looking at was the slowest to turn green.

    Walking the shortlist in rank order and taking each pair the first time it
    appears fixes that, and it costs nothing: the set of pairs is identical,
    only the sequence changes. Deduplication is preserved, so a leg-date shared
    by several candidates is still fetched once.

    It also settles the "first candidates first" question by construction: a
    pair that only belongs to candidate six cannot appear before a pair that
    candidate one already needed.
    """
    ordered: list[tuple[int, date]] = []
    seen: set[tuple[int, date]] = set()
    for combo in shortlist:
        for index, day in enumerate(combo.dates):
            pair = (index, day)
            if pair not in seen:
                seen.add(pair)
                ordered.append(pair)
    return ordered


async def verify(
    spec: SearchSpec,
    combinations: list[Combination],
    sources: list,
    *,
    cache: SqliteCache | None = None,
    history: SqliteHistory | None = None,
    rates: Rates | None = None,
    limit: int = 10,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[VerifiedItinerary], VerifyReport]:
    """Look up real flights for the first `limit` combinations.

    `history` schreibt die bestaetigten Preise fort, so wie `build_grid` die
    geschaetzten fortschreibt. Ohne das bliebe die gepruefte Grundgesamtheit
    fuer immer leer, und ein gepruefter Preis haette nie etwas, wogegen er
    gemessen werden koennte, das seinesgleichen ist.
    """
    shortlist = combinations[:limit]
    report = VerifyReport()
    if not shortlist:
        return [], report

    # Many combinations reuse the same leg-date, so resolve the distinct set once.
    wanted = pairs_by_rank(shortlist)
    resolved: dict[tuple[int, date], Offer | None] = {}
    done = 0
    total = len(wanted)
    if on_progress:
        on_progress(0, total)

    seen_errors: set[tuple[str, str]] = set()

    def note_error(source_name: str, where: str, message: str) -> None:
        """One line per (source, message), in the order they first appeared.

        A source that is out of budget or blocked fails identically for every
        leg-date in the shortlist. Twenty copies of the same sentence bury the
        one error that is actually different.
        """
        key = (source_name, message)
        if key in seen_errors:
            return
        seen_errors.add(key)
        report.errors.append(f"{source_name} {where}: {message}")

    async def ask(source, index: int, day: date) -> list[Offer]:
        leg = spec.legs[index]
        key = cache_key(
            source.name, "verify", leg.origin, leg.destination, day,
            pax=spec.pax.total, cabin=spec.cabin.value, currency=spec.currency,
        )
        cached = await cache.get(key) if cache is not None else None
        if cached is not None:
            report.cache_hits += 1
            return [_offer_from_cache(row, source.name) for row in cached]

        try:
            report.calls += 1
            offers = await source.search_leg(
                leg.origin, leg.destination, day,
                pax=spec.pax, cabin=spec.cabin, currency=spec.currency,
            )
        except SourceError as exc:
            note_error(source.name, f"{leg.origin}-{leg.destination} {day}", str(exc))
            return []
        except Exception as exc:  # noqa: BLE001
            note_error(
                source.name,
                f"{leg.origin}-{leg.destination} {day}",
                f"{type(exc).__name__}: {exc}",
            )
            logger.exception("verify crashed on %s", leg)
            return []

        if cache is not None:
            await cache.put(
                key, [_offer_to_cache(o) for o in offers],
                TTL_VERIFY, source=source.name,
            )
        return offers

    async def resolve_one(index: int, day: date) -> tuple[tuple[int, date], Offer | None]:
        leg = spec.legs[index]
        usable = [
            s for s in sources
            if getattr(s, "supports_search", False)
            and s.supports_route(leg.origin, leg.destination)
        ]
        # Airlines first: their price is the one that can actually be booked,
        # and a paid collector must not be spent on a leg an airline covers.
        airlines = [s for s in usable if getattr(s, "carrier", "")]
        collectors = [s for s in usable if not getattr(s, "carrier", "")]

        best: Offer | None = None
        best_indicative = False
        for group in (airlines, collectors):
            for source in group:
                for offer in await ask(source, index, day):
                    try:
                        offer = convert_offer(offer, spec.currency, rates)
                    except fx.UnknownCurrency as exc:
                        note_error(
                            source.name,
                            f"{leg.origin}-{leg.destination} {day}",
                            str(exc),
                        )
                        continue
                    if best is None or offer.price.minor < best.price.minor:
                        best = offer
                        best_indicative = bool(getattr(source, "indicative", False))
            if best is not None:
                break

        if history is not None and best is not None:
            # Derselbe Schluessel wie in `build_grid`. Die Grundgesamtheit sagt
            # aber die Quelle, nicht die Phase: hier stand fest `False`, und
            # damit galt jeder Treffer der Nachpruefung als geprueft - auch
            # einer, den seine Quelle selbst als Schaetzung ausweist. Solche
            # Zeilen landen sonst in der falschen Haelfte von
            # `flight_baseline` (siehe docs/PRICE_HISTORY.md, Abschnitt 3).
            # Geschrieben wird der reine Angebotspreis, ohne aufgeschlagene
            # Gepaeckgebuehr - so wie der Kalender ihn auch schreibt.
            await history.record(
                source=best.source,
                entity_type="flight",
                entity_key=f"{leg.origin}|{leg.destination}",
                travel_date=day,
                price=best.price,
                party_size=spec.pax.total,
                is_estimate=best.is_estimate,
                is_indicative=best_indicative,
            )

        return (index, day), best

    async def tracked_resolve(index: int, day: date) -> None:
        nonlocal done
        key, offer = await resolve_one(index, day)
        resolved[key] = offer
        done += 1
        if on_progress:
            on_progress(done, total)

    await asyncio.gather(*(tracked_resolve(index, day) for index, day in wanted))

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
        # Muss mit, sonst geht die Einstufung der Quelle auf dem Weg durch den
        # Cache verloren und der zweite Lauf innerhalb der TTL macht aus einer
        # Schaetzung einen geprueften Preis.
        "is_estimate": offer.is_estimate,
        # Gleiche Sorte Verlust: ohne Nativwaehrung loescht `_leg_payload` das
        # Feld aus der Zeile. Der Originalpreis der Airline waere dann je nach
        # Cache-Zustand mal da und mal nicht.
        "price_native": (
            None if offer.price_native is None
            else {
                "minor": offer.price_native.minor,
                "currency": offer.price_native.currency,
            }
        ),
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
        # Eintraege aus der Zeit vor diesem Feld tragen es nicht. Der alte
        # Standardwert bleibt deshalb der Standardwert, statt eine ganze
        # Cache-Generation stillschweigend umzudeuten.
        is_estimate=bool(row.get("is_estimate", False)),
        price_native=(
            Money(native["minor"], native["currency"])
            if (native := row.get("price_native")) else None
        ),
    )
