"""Verify arbeitet die Kandidaten der Reihe nach ab, nicht die Beine.

Die Oberflaeche meldet einen Kandidaten erst als geprueft, wenn alle seine
Beine da sind. Die alte Sortierung nach (Bein, Datum) loeste deshalb zuerst
Bein 0 fuer alle auf und den letzten Abschnitt des Favoriten oft zuletzt -
also genau die Zeile, auf die der Nutzer schaut, wurde am spaetesten gruen.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from flightopt.domain.models import (
    Cabin,
    LegSpec,
    Money,
    Offer,
    Pax,
    SearchSpec,
    Segment,
    StayRange,
)
from flightopt.search.dp import Combination
from flightopt.search.verify import pairs_by_rank, verify


def make_spec(legs: int = 3) -> SearchSpec:
    codes = ["BER", "ATH", "PMI", "BER"][: legs + 1]
    return SearchSpec(
        legs=tuple(LegSpec(codes[i], codes[i + 1]) for i in range(legs)),
        stays=tuple(StayRange(1, 20) for _ in range(legs - 1)),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 12, 31),
        pax=Pax(adults=1),
        cabin=Cabin.ECONOMY,
        currency="EUR",
    )


def combo(*days: tuple[int, int, int], price: int = 10000) -> Combination:
    return Combination(dates=tuple(date(*d) for d in days), total=Money(price, "EUR"))


class RecordingSource:
    """Merkt sich, in welcher Reihenfolge nach Bein-Datum-Paaren gefragt wurde."""

    supports_calendar = False
    supports_search = True
    name = "recorder"
    carrier = "RC"
    carriers = ("RC",)
    indicative = False

    def __init__(self) -> None:
        self.asked: list[tuple[str, date]] = []

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def search_leg(self, origin, destination, day, *, pax=None, cabin=None,
                         currency="EUR"):
        self.asked.append((origin, day))
        # Ein echter Abruf gibt die Kontrolle ab; ohne das liefe jeder Task
        # ohne Unterbrechung durch und die Reihenfolge waere trivial.
        await asyncio.sleep(0)
        return [
            Offer(
                source=self.name,
                origin=origin,
                destination=destination,
                travel_date=day,
                price=Money(5000, currency),
                segments=(
                    Segment(
                        carrier="RC",
                        flight_number="RC1",
                        origin=origin,
                        destination=destination,
                        departure=datetime.combine(day, datetime.min.time()),
                        arrival=datetime.combine(day, datetime.min.time()),
                    ),
                ),
            )
        ]


# -- Punkt 4: Reihenfolge nach Rang ------------------------------------------


def test_pairs_come_in_the_order_the_ranking_needs_them():
    shortlist = [
        # Der Favorit fliegt spaet - nach der alten Sortierung waeren seine
        # Daten hinten gelandet.
        combo((2026, 12, 20), (2026, 12, 24), (2026, 12, 28)),
        combo((2026, 10, 1), (2026, 10, 5), (2026, 10, 9)),
    ]

    assert pairs_by_rank(shortlist) == [
        (0, date(2026, 12, 20)),
        (1, date(2026, 12, 24)),
        (2, date(2026, 12, 28)),
        (0, date(2026, 10, 1)),
        (1, date(2026, 10, 5)),
        (2, date(2026, 10, 9)),
    ]


def test_a_shared_leg_date_is_kept_once_and_at_its_best_rank():
    shortlist = [
        combo((2026, 10, 1), (2026, 10, 5), (2026, 10, 9)),
        combo((2026, 10, 1), (2026, 10, 6), (2026, 10, 9)),
        combo((2026, 10, 2), (2026, 10, 6), (2026, 10, 9)),
    ]

    ordered = pairs_by_rank(shortlist)

    assert len(ordered) == len(set(ordered)), "ein Paar wurde doppelt angefragt"
    assert ordered == [
        (0, date(2026, 10, 1)),
        (1, date(2026, 10, 5)),
        (2, date(2026, 10, 9)),
        (1, date(2026, 10, 6)),
        (0, date(2026, 10, 2)),
    ]


async def test_the_favourite_is_asked_for_first_even_with_late_dates():
    spec = make_spec()
    favourite = combo((2026, 12, 20), (2026, 12, 24), (2026, 12, 28), price=9000)
    runner_up = combo((2026, 10, 1), (2026, 10, 5), (2026, 10, 9), price=9500)
    src = RecordingSource()

    await verify(spec, [favourite, runner_up], [src], limit=10)

    favourite_pairs = {("BER", date(2026, 12, 20)), ("ATH", date(2026, 12, 24)),
                       ("PMI", date(2026, 12, 28))}
    first_three = set(src.asked[:3])
    assert first_three == favourite_pairs, (
        f"zuerst gefragt wurde {src.asked[:3]}, erwartet der Favorit"
    )
