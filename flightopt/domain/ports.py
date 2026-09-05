"""Interfaces every adapter and store implements.

Keeping these as Protocols means sources stay decoupled from storage and from
each other; adding a source is one new file plus a registry entry.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from flightopt.domain.models import Cabin, Money, Offer, Pax


@runtime_checkable
class PriceSource(Protocol):
    """A source of per-leg prices.

    `calendar` is the cheap bulk path (one call covers a whole month) and may
    return estimates. `search_leg` is the precise path for a single date.
    A source may implement only one of them; capability flags say which.
    """

    name: str
    supports_calendar: bool
    supports_search: bool

    def supports_route(self, origin: str, destination: str) -> bool:
        """False when the source is known not to fly this pair."""
        ...

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Cheapest price per day for the month containing `month`."""
        ...

    async def search_leg(
        self,
        origin: str,
        destination: str,
        day: date,
        *,
        pax: Pax,
        cabin: Cabin = Cabin.ECONOMY,
        currency: str = "EUR",
    ) -> list[Offer]:
        """All offers for one leg on one date."""
        ...


@runtime_checkable
class MultiCitySource(Protocol):
    """A source that can price a whole multi-leg itinerary as one ticket."""

    name: str

    async def search_itinerary(
        self,
        legs: list[tuple[str, str, date]],
        *,
        pax: Pax,
        cabin: Cabin = Cabin.ECONOMY,
        currency: str = "EUR",
    ) -> list[Offer]:
        ...


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def put(self, key: str, value: Any, ttl: timedelta) -> None: ...

    async def purge_expired(self) -> int: ...


class HistoryRepo(Protocol):
    """Append-only price observations. Never updates a row.

    Domain-agnostic on purpose: hotels write here too, with
    entity_type="hotel" and their own entity_key shape.
    """

    async def record(
        self,
        *,
        source: str,
        entity_type: str,
        entity_key: str,
        travel_date: date,
        price: Money,
        observed_at: datetime | None = None,
        **extra: Any,
    ) -> None: ...

    async def series(
        self, entity_type: str, entity_key: str, travel_date: date | None = None
    ) -> list[tuple[datetime, Money]]: ...
