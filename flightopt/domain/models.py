"""Core value objects.

Money is stored in minor units (cents) everywhere to avoid float drift.
Every price that enters the system passes through here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable


class Cabin(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium-economy"
    BUSINESS = "business"
    FIRST = "first"


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: str = "EUR"

    @classmethod
    def from_major(cls, value: float, currency: str = "EUR") -> "Money":
        return cls(minor=int(round(value * 100)), currency=currency)

    @property
    def major(self) -> float:
        return self.minor / 100

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")
        return Money(self.minor + other.minor, self.currency)

    def __lt__(self, other: "Money") -> bool:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")
        return self.minor < other.minor

    def __str__(self) -> str:
        return f"{self.major:.2f} {self.currency}"


@dataclass(frozen=True, slots=True)
class Pax:
    adults: int = 1
    children: int = 0
    infants: int = 0

    @property
    def total(self) -> int:
        return self.adults + self.children + self.infants


@dataclass(frozen=True, slots=True)
class Segment:
    """One physical flight."""

    carrier: str
    flight_number: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime

    @property
    def duration(self) -> timedelta:
        return self.arrival - self.departure


@dataclass(frozen=True, slots=True)
class Offer:
    """One priced option for one leg on one date, as returned by a source."""

    source: str
    origin: str
    destination: str
    travel_date: date
    price: Money
    segments: tuple[Segment, ...] = ()
    fetched_at: datetime = field(default_factory=lambda: datetime.now())
    deep_link: str | None = None
    is_estimate: bool = False
    """True for calendar/cached prices that still need live verification."""

    @property
    def stops(self) -> int:
        return max(0, len(self.segments) - 1)

    @property
    def duration(self) -> timedelta | None:
        if not self.segments:
            return None
        return self.segments[-1].arrival - self.segments[0].departure

    @property
    def carriers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.carrier for s in self.segments))


@dataclass(frozen=True, slots=True)
class LegSpec:
    """One hop of the requested route."""

    origin: str
    destination: str


@dataclass(frozen=True, slots=True)
class StayRange:
    """Allowed nights at the stop that follows a leg."""

    min_nights: int
    max_nights: int

    def __post_init__(self) -> None:
        if self.min_nights < 0:
            raise ValueError("min_nights must be >= 0")
        if self.max_nights < self.min_nights:
            raise ValueError("max_nights must be >= min_nights")

    def spans(self) -> Iterable[int]:
        return range(self.min_nights, self.max_nights + 1)


@dataclass(frozen=True, slots=True)
class SearchSpec:
    """A complete search request.

    legs = [BER->AYT, AYT->ATH, ATH->BER]
    stays = [3-10 nights in Antalya, 3-10 nights in Athens]  (one fewer than legs)
    """

    legs: tuple[LegSpec, ...]
    stays: tuple[StayRange, ...]
    window_start: date
    window_end: date
    pax: Pax = Pax()
    cabin: Cabin = Cabin.ECONOMY
    currency: str = "EUR"
    checked_bags: int = 0

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("a search needs at least one leg")
        if len(self.stays) != len(self.legs) - 1:
            raise ValueError(
                f"need exactly {len(self.legs) - 1} stay ranges for {len(self.legs)} legs, "
                f"got {len(self.stays)}"
            )
        if self.window_end < self.window_start:
            raise ValueError("window_end must be >= window_start")
        if self.checked_bags < 0:
            raise ValueError("checked_bags must be >= 0")
        for i in range(len(self.legs) - 1):
            if self.legs[i].destination != self.legs[i + 1].origin:
                raise ValueError(
                    f"route is not contiguous: leg {i} ends at {self.legs[i].destination} "
                    f"but leg {i + 1} starts at {self.legs[i + 1].origin}"
                )

    @property
    def window_days(self) -> int:
        return (self.window_end - self.window_start).days + 1

    def dates(self) -> list[date]:
        return [self.window_start + timedelta(days=i) for i in range(self.window_days)]

    @property
    def route(self) -> str:
        return "-".join([self.legs[0].origin] + [leg.destination for leg in self.legs])


@dataclass(frozen=True, slots=True)
class Itinerary:
    """One complete date combination with its per-leg offers."""

    spec_route: str
    dates: tuple[date, ...]
    offers: tuple[Offer, ...]

    @property
    def total(self) -> Money:
        total = Money(0, self.offers[0].price.currency)
        for offer in self.offers:
            total = total + offer.price
        return total

    @property
    def is_estimate(self) -> bool:
        return any(o.is_estimate for o in self.offers)

    @property
    def stops(self) -> int:
        return sum(o.stops for o in self.offers)

    @property
    def travel_days(self) -> int:
        return (self.dates[-1] - self.dates[0]).days

    @property
    def air_minutes(self) -> int | None:
        durations = [o.duration for o in self.offers]
        if any(d is None for d in durations):
            return None
        return int(sum(d.total_seconds() for d in durations) // 60)  # type: ignore[union-attr]

    def __str__(self) -> str:
        legs = " ".join(f"{d:%d.%m}" for d in self.dates)
        flag = "~" if self.is_estimate else " "
        return f"{flag}{self.total} [{legs}] {self.stops} stops"
