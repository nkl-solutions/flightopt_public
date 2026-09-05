from datetime import date, timedelta
from itertools import product

import pytest

from flightopt.domain.models import LegSpec, Money, SearchSpec, StayRange
from flightopt.search.dp import (
    Combination,
    count_combinations,
    feasible_dates,
    solve,
)

START = date(2026, 10, 1)


def spec(n_legs=3, window_days=30, stay=(3, 10)):
    codes = ["BER", "AYT", "ATH", "SKG", "VIE", "TXL"]
    legs = tuple(LegSpec(codes[i], codes[i + 1]) for i in range(n_legs))
    stays = tuple(StayRange(*stay) for _ in range(n_legs - 1))
    return SearchSpec(
        legs=legs,
        stays=stays,
        window_start=START,
        window_end=START + timedelta(days=window_days - 1),
    )


def grid_from(prices: dict[int, dict[date, int]]):
    return {i: {d: Money(v) for d, v in row.items()} for i, row in prices.items()}


def flat_grid(s: SearchSpec, value=10000):
    return {
        i: {d: Money(value) for d in s.dates()} for i in range(len(s.legs))
    }


def brute_force(s: SearchSpec, grid) -> list[Combination]:
    """Reference implementation: enumerate every combination explicitly."""
    window = s.dates()
    out = []
    for start in window:
        for nights in product(*[list(st.spans()) for st in s.stays]):
            dates = [start]
            for n in nights:
                dates.append(dates[-1] + timedelta(days=n))
            if dates[-1] > window[-1]:
                continue
            try:
                total = sum(grid[i][d].minor for i, d in enumerate(dates))
            except KeyError:
                continue
            out.append(Combination(tuple(dates), Money(total)))
    out.sort(key=lambda c: (c.total.minor, c.dates))
    return out


# --- spec validation ---------------------------------------------------------


def test_route_must_be_contiguous():
    with pytest.raises(ValueError, match="not contiguous"):
        SearchSpec(
            legs=(LegSpec("BER", "AYT"), LegSpec("ATH", "BER")),
            stays=(StayRange(3, 5),),
            window_start=START,
            window_end=START + timedelta(days=20),
        )


def test_stay_count_must_match_legs():
    with pytest.raises(ValueError, match="exactly 2 stay ranges"):
        SearchSpec(
            legs=(LegSpec("BER", "AYT"), LegSpec("AYT", "ATH"), LegSpec("ATH", "BER")),
            stays=(StayRange(3, 5),),
            window_start=START,
            window_end=START + timedelta(days=20),
        )


# --- search space ------------------------------------------------------------


def test_count_matches_masterplan_numbers():
    # These are the figures the masterplan quotes; they must not silently drift.
    assert count_combinations(spec(2, 60, (3, 10))) == 428
    assert count_combinations(spec(3, 60, (3, 10))) == 3008
    assert count_combinations(spec(4, 60, (3, 10))) == 20736


def test_feasible_dates_trims_edges():
    s = spec(3, 30, (5, 5))
    allowed = feasible_dates(s)
    # leg 0 cannot depart later than window_end - 10 nights
    assert max(allowed[0]) == s.window_end - timedelta(days=10)
    # leg 2 cannot depart before window_start + 10 nights
    assert min(allowed[2]) == s.window_start + timedelta(days=10)


def test_window_too_short_yields_nothing():
    s = spec(3, 5, (10, 12))
    assert count_combinations(s) == 0
    assert solve(s, flat_grid(s)) == []


# --- optimizer correctness ---------------------------------------------------


def test_finds_the_planted_cheap_combination():
    s = spec(3, 30, (3, 10))
    grid = flat_grid(s, 20000)
    cheap = [START + timedelta(days=2), START + timedelta(days=7), START + timedelta(days=14)]
    for i, d in enumerate(cheap):
        grid[i][d] = Money(1000)
    best = solve(s, grid, top_k=1)
    assert len(best) == 1
    assert best[0].dates == tuple(cheap)
    assert best[0].total == Money(3000)


def test_matches_brute_force_on_small_input():
    s = spec(3, 18, (2, 5))
    # deterministic pseudo-random prices
    grid = {
        i: {d: Money(5000 + (i * 37 + d.day * 913) % 9000) for d in s.dates()}
        for i in range(3)
    }
    expected = brute_force(s, grid)
    got = solve(s, grid, top_k=5, max_per_start=99)
    assert [c.total for c in got] == [c.total for c in expected[:5]]


def test_results_are_sorted_and_feasible():
    s = spec(3, 40, (3, 10))
    grid = {
        i: {d: Money(3000 + (d.toordinal() * (i + 7)) % 12000) for d in s.dates()}
        for i in range(3)
    }
    res = solve(s, grid, top_k=20, max_per_start=99)
    assert len(res) == 20
    assert res == sorted(res, key=lambda c: c.total.minor)
    for c in res:
        assert len(c.dates) == 3
        for i, stay in enumerate(s.stays):
            nights = (c.dates[i + 1] - c.dates[i]).days
            assert stay.min_nights <= nights <= stay.max_nights
        assert c.total.minor == sum(grid[i][d].minor for i, d in enumerate(c.dates))


def test_missing_leg_prices_make_it_infeasible():
    s = spec(3, 30, (3, 10))
    grid = flat_grid(s)
    grid[1] = {}
    assert solve(s, grid) == []


def test_sparse_grid_only_uses_priced_dates():
    s = spec(3, 30, (3, 10))
    d0, d1, d2 = START, START + timedelta(days=5), START + timedelta(days=12)
    grid = grid_from({0: {d0: 1000}, 1: {d1: 2000}, 2: {d2: 3000}})
    res = solve(s, grid)
    assert len(res) == 1
    assert res[0].dates == (d0, d1, d2)
    assert res[0].total == Money(6000)


def test_max_per_start_diversifies_results():
    s = spec(3, 40, (3, 10))
    # Make one start date dominate: everything from day 0 is dirt cheap.
    grid = flat_grid(s, 50000)
    for d in s.dates():
        grid[1][d] = Money(100)
        grid[2][d] = Money(100)
    grid[0][START] = Money(50)

    capped = solve(s, grid, top_k=10, max_per_start=2)
    starts = [c.dates[0] for c in capped]
    assert starts.count(START) <= 2
    assert len(set(starts)) > 1


def test_zero_night_stays_allowed():
    s = SearchSpec(
        legs=(LegSpec("BER", "AYT"), LegSpec("AYT", "ATH")),
        stays=(StayRange(0, 2),),
        window_start=START,
        window_end=START + timedelta(days=10),
    )
    grid = flat_grid(s, 1000)
    res = solve(s, grid, top_k=3, max_per_start=99)
    assert res
    assert any(c.dates[0] == c.dates[1] for c in res)


# --- money -------------------------------------------------------------------


def test_money_rejects_currency_mixing():
    with pytest.raises(ValueError, match="currency mismatch"):
        Money(100, "EUR") + Money(100, "USD")


def test_money_from_major_rounds():
    assert Money.from_major(46.97).minor == 4697
    assert Money.from_major(0.1 + 0.2).minor == 30
