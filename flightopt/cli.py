"""Command line entry point.

    uv run flightopt search BER ATH SKG BER --window 2026-10-01:2026-11-30 --stay 3-10

Prints the cheapest date combinations for the route. Prices from calendar
endpoints are estimates and are marked as such until phase-2 verification runs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

from flightopt.domain.models import Cabin, LegSpec, Pax, SearchSpec, StayRange
from flightopt.search.dp import count_combinations, solve
from flightopt.search.grid import build_grid
from flightopt.sources.ryanair import RyanairSource
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache, SqliteHistory


def parse_window(raw: str) -> tuple[date, date]:
    if ":" in raw:
        a, b = raw.split(":", 1)
        return date.fromisoformat(a), date.fromisoformat(b)
    start = date.fromisoformat(raw)
    return start, start + timedelta(days=59)


def parse_stay(raw: str) -> StayRange:
    if "-" in raw:
        a, b = raw.split("-", 1)
        return StayRange(int(a), int(b))
    n = int(raw)
    return StayRange(n, n)


def build_spec(args: argparse.Namespace) -> SearchSpec:
    stops = [s.upper() for s in args.airports]
    if len(stops) < 3:
        raise SystemExit("need at least 3 airports (origin, one stop, return)")
    legs = tuple(LegSpec(stops[i], stops[i + 1]) for i in range(len(stops) - 1))

    stays_raw = args.stay or ["3-10"]
    if len(stays_raw) == 1:
        stays = tuple(parse_stay(stays_raw[0]) for _ in range(len(legs) - 1))
    elif len(stays_raw) == len(legs) - 1:
        stays = tuple(parse_stay(s) for s in stays_raw)
    else:
        raise SystemExit(
            f"give either one --stay for all stops or exactly {len(legs) - 1}"
        )

    start, end = parse_window(args.window)
    return SearchSpec(
        legs=legs,
        stays=stays,
        window_start=start,
        window_end=end,
        pax=Pax(adults=args.adults),
        cabin=Cabin(args.cabin),
        currency=args.currency,
    )


async def run_search(args: argparse.Namespace) -> int:
    spec = build_spec(args)
    combos = count_combinations(spec)
    grid_cells = sum(len(s) for s in __import__(
        "flightopt.search.dp", fromlist=["feasible_dates"]
    ).feasible_dates(spec))

    print(f"route   {spec.route}")
    print(f"window  {spec.window_start} .. {spec.window_end}  ({spec.window_days} days)")
    print(f"stays   {', '.join(f'{s.min_nights}-{s.max_nights}n' for s in spec.stays)}")
    print(f"space   {combos:,} combinations, {grid_cells} leg-dates to price")
    if combos == 0:
        print("\nNo combination fits: widen the window or shorten the stays.")
        return 1
    print()

    conn = db.connect(args.db)
    cache = SqliteCache(conn)
    history = SqliteHistory(conn)
    sources = [RyanairSource()]

    for source in sources:
        loader = getattr(source, "load_routes", None)
        if loader is not None:
            seen = set()
            for leg in spec.legs:
                if leg.origin not in seen:
                    await loader(leg.origin)
                    seen.add(leg.origin)

    grid, report = await build_grid(spec, sources, cache=cache, history=history)

    print(f"fetched {report.calls} calls, {report.cache_hits} cache hits")
    for name, n in sorted(report.per_source.items()):
        print(f"        {name}: {n} priced dates")
    coverage = report.coverage(spec)
    for i, leg in enumerate(spec.legs):
        pct = coverage[i] * 100
        mark = "  " if pct > 0 else "!!"
        print(f"  {mark}leg {i} {leg.origin}-{leg.destination}: "
              f"{report.filled.get(i, 0)} dates ({pct:.0f}% of window)")
    for err in report.errors[:5]:
        print(f"  error: {err}")

    missing = report.missing_legs(spec)
    if missing:
        names = ", ".join(
            f"{spec.legs[i].origin}-{spec.legs[i].destination}" for i in missing
        )
        print(f"\nNo prices for: {names}")
        print("Every leg needs at least one price before combinations can be ranked.")
        return 2

    results = solve(spec, grid, top_k=args.top, max_per_start=args.per_start)
    if not results:
        print("\nNo feasible combination from the prices that were available.")
        return 2

    print(f"\ntop {len(results)} by total price (~ = estimate, not yet verified)\n")
    width = max(len(spec.legs) * 12, 30)
    header = "  ".join(f"{leg.origin}-{leg.destination}" for leg in spec.legs)
    print(f"{'#':>3}  {'total':>11}  {header}")
    for rank, combo in enumerate(results, 1):
        dates = "  ".join(f"{d:%a %d.%m}" for d in combo.dates)
        nights = "/".join(
            str((combo.dates[i + 1] - combo.dates[i]).days) for i in range(len(combo.dates) - 1)
        )
        print(f"{rank:>3}  ~{combo.total.major:>9.2f}  {dates}   [{nights}n]")

    conn.commit()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flightopt", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="find cheapest date combinations for a route")
    s.add_argument("airports", nargs="+", help="e.g. BER ATH SKG BER")
    s.add_argument("--window", required=True, help="YYYY-MM-DD:YYYY-MM-DD")
    s.add_argument("--stay", action="append", help="nights per stop, e.g. 3-10")
    s.add_argument("--adults", type=int, default=1)
    s.add_argument("--cabin", default="economy",
                   choices=[c.value for c in Cabin])
    s.add_argument("--currency", default="EUR")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--per-start", type=int, default=3,
                   help="max results sharing the same first departure date")
    s.add_argument("--db", default=str(db.DEFAULT_DB))
    s.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "search":
        return asyncio.run(run_search(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
