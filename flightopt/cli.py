"""Command line entry point.

    uv run flightopt search BER TYO SEL BER --window 2027-03-01 2027-04-30 \
        --stay 7-14 --stay 4-8

Prints the cheapest date combinations for the route. Stops may be typed as IATA
codes, city names or group codes, exactly as in the web form; group codes fan
out into concrete routes, which are all searched and ranked together.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta
from itertools import product
from typing import Any, Mapping

from flightopt.domain import airports as airport_registry
from flightopt.domain.models import Cabin, LegSpec, Pax, SearchSpec, StayRange
from flightopt.jobs.runner import JobRunner, build_catalogue, preload_routes
from flightopt.search.dp import Combination, count_combinations, feasible_dates, solve
from flightopt.search.grid import build_grid
from flightopt.storage import db, fx_store
from flightopt.storage.cache import SqliteCache, SqliteHistory


def parse_day(value: str) -> date:
    """A date, or a ValueError that repeats what was typed.

    `date.fromisoformat` says "month must be in 1..12" and never mentions the
    string it choked on, which is useless in a shell that just ate a typo.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--window: {value!r} ist kein Datum (YYYY-MM-DD): {exc}") from exc


def parse_window(raw: list[str]) -> tuple[date, date]:
    """Accepts '2027-03-01 2027-04-30', '2027-03-01:2027-04-30' or one date."""
    if len(raw) >= 2:
        return parse_day(raw[0]), parse_day(raw[1])
    value = raw[0]
    if ":" in value:
        a, b = value.split(":", 1)
        return parse_day(a), parse_day(b)
    start = parse_day(value)
    return start, start + timedelta(days=59)


def parse_stay(raw: str) -> StayRange:
    if "-" in raw:
        a, b = raw.split("-", 1)
        return StayRange(int(a), int(b))
    n = int(raw)
    return StayRange(n, n)


def resolve_stops(tokens: list[str]) -> list[str]:
    """Turn what was typed into codes, taking the first hit like the form."""
    out: list[str] = []
    for token in tokens:
        code = airport_registry.resolve(token)
        if code is None:
            raise SystemExit(f"unbekannter Ort: {token!r}")
        out.append(code)
    return out


def build_specs(args: argparse.Namespace) -> list[SearchSpec]:
    stops = resolve_stops(args.airports)
    if len(stops) < 2:
        raise SystemExit("need at least 2 airports (origin and destination)")
    airport_registry.check_variant_budget(stops)

    stays_raw = args.stay or ["3-10"]
    start, end = parse_window(args.window)

    specs: list[SearchSpec] = []
    for concrete in product(*(airport_registry.expand_code(code) for code in stops)):
        if any(concrete[i] == concrete[i + 1] for i in range(len(concrete) - 1)):
            continue
        legs = tuple(
            LegSpec(concrete[i], concrete[i + 1]) for i in range(len(concrete) - 1)
        )
        if len(legs) == 1:
            stays: tuple[StayRange, ...] = ()
        elif len(stays_raw) == 1:
            stays = tuple(parse_stay(stays_raw[0]) for _ in range(len(legs) - 1))
        elif len(stays_raw) == len(legs) - 1:
            stays = tuple(parse_stay(s) for s in stays_raw)
        else:
            raise SystemExit(
                f"give either one --stay for all stops or exactly {len(legs) - 1}"
            )
        specs.append(
            SearchSpec(
                legs=legs,
                stays=stays,
                window_start=start,
                window_end=end,
                pax=Pax(adults=args.adults),
                cabin=Cabin(args.cabin),
                currency=args.currency,
                max_stops=args.max_stops,
            )
        )
    if not specs:
        raise SystemExit("Flughafen-Gruppen ergeben keine gueltige Route.")
    return specs


def format_amount(value: float) -> str:
    """48500.0 -> '48.500'. Ein Yen-Preis mit Nachkommastellen waere gelogen."""
    text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    # Von der englischen in die deutsche Schreibweise, ohne sich selbst zu
    # ueberschreiben: erst den Tausendertrenner parken, dann tauschen.
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def format_leg(leg: Mapping[str, Any], currency: str = "EUR") -> str:
    """One line per leg: price, the fare's own currency, stops when known."""
    bits = [
        f"{leg['origin']}-{leg['destination']}",
        str(leg["date"]),
        f"{leg['price']:.2f} {currency}",
    ]
    native = leg.get("price_native")
    if native:
        bits.append(
            f"umgerechnet aus {format_amount(native['amount'])} {native['currency']}"
        )
    stops = leg.get("stops")
    if stops is not None:
        # None heisst: kein Flugplan gesehen. Dann steht hier nichts, statt
        # einen Direktflug zu behaupten, den niemand geprueft hat.
        plural = "e" if stops > 1 else ""
        bits.append("Direktflug" if stops == 0 else f"{stops} Umstieg{plural}")
    return "  ".join(bits)


async def run_one(spec: SearchSpec, args, conn, cache, history, rates,
                  sources: list | None = None) -> list[dict[str, Any]]:
    """One route variant: a row per combination, with its legs.

    `sources` comes from the caller so that one catalogue serves every variant.
    Rate limiter and circuit breaker live on those objects, and a fresh
    catalogue per variant would mean a fresh, empty breaker per variant.
    """
    combos = count_combinations(spec)
    cells = sum(len(s) for s in feasible_dates(spec))
    print(f"route   {spec.route}")
    print(f"space   {combos:,} combinations, {cells} leg-dates to price")
    if combos == 0:
        print("  no combination fits: widen the window or shorten the stays.")
        return []

    if sources is None:
        sources = build_catalogue(set(), conn=conn)
    await preload_routes(sources, spec.legs)
    grid, report = await build_grid(
        spec, sources, cache=cache, history=history, rates=rates
    )

    print(f"fetched {report.calls} calls, {report.cache_hits} cache hits")
    for name, priced in sorted(report.per_source.items()):
        print(f"        {name}: {priced} priced dates")
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
        print(f"  no prices for: {names}")
        return []

    best = solve(spec, grid, top_k=args.top, max_per_start=args.per_start)
    # The same leg payload the web UI is built from, so both name the same
    # original price and the same number of stops.
    return [
        {
            "route": spec.route,
            "combo": combo,
            "legs": [
                JobRunner._leg_payload(spec, grid, combo, i, day, None,
                                       report.winner, report.indicative, None,
                                       report.native)
                for i, day in enumerate(combo.dates)
            ],
        }
        for combo in best
    ]


async def run_search(args: argparse.Namespace) -> int:
    # Everything that can be wrong about the request is wrong here, before a
    # connection is opened. A shell wants a sentence, not a traceback.
    try:
        specs = build_specs(args)
    except (airport_registry.TooManyVariants, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(f"window  {specs[0].window_start} .. {specs[0].window_end} "
          f"({specs[0].window_days} days)")
    if len(specs) > 1:
        print(f"routes  {len(specs)} variants from the groups you typed")
    print()

    if not any(count_combinations(spec) for spec in specs):
        # Window and stays contradict each other: there is nothing to price,
        # and that is a different failure from "no source had a price".
        print("No combination fits: widen the window or shorten the stays.")
        return 1

    conn = db.connect(args.db)
    cache = SqliteCache(conn)
    history = SqliteHistory(conn)
    rates = await fx_store.current_rates(conn)
    sources = build_catalogue(set(), conn=conn)

    found: list[dict[str, Any]] = []
    for spec in specs:
        found.extend(
            await run_one(spec, args, conn, cache, history, rates, sources=sources)
        )
        print()

    if not found:
        print("No feasible combination from the prices that were available.")
        conn.commit()
        return 2

    found.sort(key=lambda row: row["combo"].total.minor)
    print(f"top {min(args.top, len(found))} by total price "
          f"(~ = estimate, not yet verified)\n")
    print(f"{'#':>3}  {'total':>11}  route")
    for rank, row in enumerate(found[:args.top], 1):
        combo: Combination = row["combo"]
        dates = "  ".join(f"{d:%a %d.%m}" for d in combo.dates)
        nights = "/".join(
            str((combo.dates[i + 1] - combo.dates[i]).days)
            for i in range(len(combo.dates) - 1)
        )
        print(f"{rank:>3}  ~{combo.total.major:>9.2f}  {row['route']}   "
              f"{dates}   [{nights}n]")
        for leg in row["legs"]:
            print(f"     {format_leg(leg, combo.total.currency)}")

    conn.commit()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flightopt", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="find cheapest date combinations for a route")
    s.add_argument("airports", nargs="+", help="e.g. BER Tokio Seoul BER")
    s.add_argument("--window", required=True, nargs="+",
                   help="YYYY-MM-DD YYYY-MM-DD (or YYYY-MM-DD:YYYY-MM-DD)")
    s.add_argument("--stay", action="append", help="nights per stop, e.g. 3-10")
    s.add_argument("--adults", type=int, default=1)
    s.add_argument("--cabin", default="economy", choices=[c.value for c in Cabin])
    s.add_argument("--currency", default="EUR")
    s.add_argument("--max-stops", type=int, default=None, choices=[0, 1, 2],
                   help="connections per leg; default follows the distance")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--per-start", type=int, default=3,
                   help="max results sharing the same first departure date")
    s.add_argument("--db", default=str(db.DEFAULT_DB))
    s.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "search":
        return asyncio.run(run_search(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
