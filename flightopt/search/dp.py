"""Date-combination optimizer.

The whole point of the project: never enumerate itineraries, enumerate
(leg, date) prices and combine them with a shortest-path pass over a DAG.

Nodes are (leg_index, departure_date). An edge (i, d) -> (i+1, d') exists when
d' - d falls inside the stay range for stop i. Edge cost is the price of leg i
on date d. Complexity is O(legs * window * stay_span) instead of the
O(product of stay spans) that brute force would cost.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import date, timedelta

from flightopt.domain.models import Money, SearchSpec

PriceGrid = dict[int, dict[date, Money]]
"""leg_index -> {departure_date: cheapest price}"""


@dataclass(frozen=True, slots=True)
class Combination:
    """One feasible set of departure dates with its summed price."""

    dates: tuple[date, ...]
    total: Money

    def __str__(self) -> str:
        return f"{self.total} [{' '.join(f'{d:%Y-%m-%d}' for d in self.dates)}]"


def feasible_dates(spec: SearchSpec) -> list[set[date]]:
    """Dates each leg could plausibly depart on, given the window and stays.

    Trims the grid before any network call: a later leg cannot depart before
    the earliest possible arrival chain, and cannot depart after the window
    minus the stays still to come.
    """
    window = spec.dates()
    first, last = window[0], window[-1]
    min_before = [0] * len(spec.legs)
    min_after = [0] * len(spec.legs)
    for i in range(1, len(spec.legs)):
        min_before[i] = min_before[i - 1] + spec.stays[i - 1].min_nights
    for i in range(len(spec.legs) - 2, -1, -1):
        min_after[i] = min_after[i + 1] + spec.stays[i].min_nights

    out: list[set[date]] = []
    for i in range(len(spec.legs)):
        lo = first + timedelta(days=min_before[i])
        hi = last - timedelta(days=min_after[i])
        out.append({d for d in window if lo <= d <= hi})
    return out


def count_combinations(spec: SearchSpec) -> int:
    """How many itineraries brute force would have had to price."""
    window = spec.dates()
    counts: dict[date, int] = {d: 1 for d in window}
    for stay in spec.stays:
        nxt: dict[date, int] = {}
        for d, c in counts.items():
            for nights in stay.spans():
                d2 = d + timedelta(days=nights)
                if d2 <= window[-1]:
                    nxt[d2] = nxt.get(d2, 0) + c
        counts = nxt
        if not counts:
            return 0
    return sum(counts.values())


def solve(spec: SearchSpec, grid: PriceGrid, *, top_k: int = 20,
          max_per_start: int = 3) -> list[Combination]:
    """Return the `top_k` cheapest date combinations.

    Backward DP first computes, for every node, the cheapest completion of the
    remaining legs. A best-first expansion then walks forward, popping partial
    paths ordered by (cost so far + optimistic completion). Because the
    heuristic is exact, the k-th popped complete path is the k-th cheapest.

    `max_per_start` keeps the result diverse: without it the top 20 tend to be
    20 variants of the same departure date, which is useless for a traveller.
    """
    n_legs = len(spec.legs)
    if n_legs == 0:
        return []
    currency = spec.currency

    allowed = feasible_dates(spec)
    prices: list[dict[date, int]] = []
    for i in range(n_legs):
        leg_prices = grid.get(i, {})
        prices.append(
            {d: m.minor for d, m in leg_prices.items() if d in allowed[i]}
        )
        if not prices[-1]:
            return []  # a leg with no price at all makes every combination infeasible

    # best[i][d] = cheapest total for legs i..n-1 when leg i departs on d
    best: list[dict[date, int]] = [dict() for _ in range(n_legs)]
    best[n_legs - 1] = dict(prices[n_legs - 1])

    for i in range(n_legs - 2, -1, -1):
        stay = spec.stays[i]
        nxt = best[i + 1]
        cur: dict[date, int] = {}
        for d, own in prices[i].items():
            cheapest_rest: int | None = None
            for nights in stay.spans():
                cand = nxt.get(d + timedelta(days=nights))
                if cand is not None and (cheapest_rest is None or cand < cheapest_rest):
                    cheapest_rest = cand
            if cheapest_rest is not None:
                cur[d] = own + cheapest_rest
        best[i] = cur
        if not cur:
            return []

    # Best-first forward walk. Heap entries: (optimistic_total, cost_so_far, path)
    heap: list[tuple[int, int, tuple[date, ...]]] = [
        (total, 0, (d,)) for d, total in best[0].items()
    ]
    heapq.heapify(heap)

    results: list[Combination] = []
    per_start: dict[date, int] = {}
    seen: set[tuple[date, ...]] = set()

    while heap and len(results) < top_k:
        optimistic, spent, path = heapq.heappop(heap)
        if path in seen:
            continue
        seen.add(path)
        leg_index = len(path) - 1
        day = path[-1]
        spent_here = spent + prices[leg_index][day]

        if leg_index == n_legs - 1:
            start = path[0]
            if per_start.get(start, 0) >= max_per_start:
                continue
            per_start[start] = per_start.get(start, 0) + 1
            results.append(Combination(dates=path, total=Money(spent_here, currency)))
            continue

        stay = spec.stays[leg_index]
        for nights in stay.spans():
            nxt_day = day + timedelta(days=nights)
            rest = best[leg_index + 1].get(nxt_day)
            if rest is None:
                continue
            heapq.heappush(heap, (spent_here + rest, spent_here, path + (nxt_day,)))

    return results
