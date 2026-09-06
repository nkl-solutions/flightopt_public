"""Run a search as a tracked job so the UI can follow it live.

A search takes seconds to minutes and touches several hosts, so it cannot block
an HTTP request. Jobs are persisted in SQLite (restart-safe) and emit progress
events that the API streams to the browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from collections.abc import Sequence
from typing import Any, Callable

from flightopt.domain import airlines as airline_registry
from flightopt.domain.models import Money, SearchSpec
from flightopt.search.dp import count_combinations, feasible_dates, solve
from flightopt.search.grid import build_grid
from flightopt.search.verify import verify
from flightopt.sources.aegean import AegeanSource
from flightopt.sources.britishairways import BritishAirwaysSource
from flightopt.sources.condor import CondorSource
from flightopt.sources.eurowings import EurowingsSource
from flightopt.sources.icelandair import IcelandairSource
from flightopt.sources.kiwi import KiwiSource
from flightopt.sources.ryanair import RyanairSource
from flightopt.sources.wizz import WizzSource
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache, SqliteHistory

logger = logging.getLogger(__name__)


@dataclass
class Progress:
    phase: str
    message: str
    done: int = 0
    total: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def spec_to_dict(spec: SearchSpec) -> dict[str, Any]:
    return {
        "airports": [spec.legs[0].origin] + [leg.destination for leg in spec.legs],
        "window_start": spec.window_start.isoformat(),
        "window_end": spec.window_end.isoformat(),
        "stays": [[s.min_nights, s.max_nights] for s in spec.stays],
        "adults": spec.pax.adults,
        "cabin": spec.cabin.value,
        "currency": spec.currency,
        "checked_bags": spec.checked_bags,
    }


def specs_to_dict(specs: Sequence[SearchSpec]) -> dict[str, Any]:
    if len(specs) == 1:
        return spec_to_dict(specs[0])
    return {
        "variants": [spec_to_dict(spec) for spec in specs],
        "variant_count": len(specs),
        "window_start": specs[0].window_start.isoformat(),
        "window_end": specs[0].window_end.isoformat(),
    }


def merge_variant_payloads(payloads: Sequence[dict[str, Any]], *,
                           top_k: int = 20) -> list[dict[str, Any]]:
    best: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for payload in payloads:
        route = str(payload.get("route") or "")
        dates = tuple(payload.get("dates") or ())
        key = (route, dates)
        current = best.get(key)
        if current is None or float(payload.get("total", 0)) < float(current.get("total", 0)):
            item = dict(payload)
            item["route"] = route
            item["legs"] = [dict(leg, route=route) for leg in payload.get("legs", [])]
            best[key] = item

    rows = sorted(
        best.values(),
        key=lambda row: (
            float(row.get("total", 0)),
            tuple(row.get("dates") or ()),
            str(row.get("route") or ""),
        ),
    )[:top_k]
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def checked_bag_fee_minor(carriers: Sequence[str], checked_bags: int) -> int:
    if checked_bags <= 0 or not carriers:
        return 0
    return checked_bags * airline_registry.checked_bag_minor(carriers[0])


class JobRunner:
    """Owns running searches and their progress streams."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or str(db.DEFAULT_DB)
        self._queues: dict[int, list[asyncio.Queue]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._results: dict[int, dict[str, Any]] = {}
        # The browser subscribes a moment after the job starts, so the first
        # events would otherwise be emitted to nobody. Keep them and replay.
        self._history: dict[int, list[Progress]] = {}

    # -- plumbing -------------------------------------------------------------

    def _conn(self):
        return db.connect(self.db_path)

    def subscribe(self, job_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for past in self._history.get(job_id, []):
            q.put_nowait(past)
        self._queues.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: int, q: asyncio.Queue) -> None:
        subs = self._queues.get(job_id)
        if subs and q in subs:
            subs.remove(q)

    def _emit(self, job_id: int, progress: Progress) -> None:
        self._history.setdefault(job_id, []).append(progress)
        for q in self._queues.get(job_id, []):
            q.put_nowait(progress)

    # -- lifecycle ------------------------------------------------------------

    def create(self, spec: SearchSpec | Sequence[SearchSpec]) -> int:
        specs = [spec] if isinstance(spec, SearchSpec) else list(spec)
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO search_job(spec, status, created_at) VALUES(?,?,?)",
            (json.dumps(specs_to_dict(specs)), "pending", db.now()),
        )
        job_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return job_id

    def start(self, job_id: int, spec: SearchSpec | Sequence[SearchSpec],
              airlines: list[str] | None = None) -> None:
        specs = [spec] if isinstance(spec, SearchSpec) else list(spec)
        if len(specs) == 1:
            coro = self._run(job_id, specs[0], airlines=airlines or [])
        else:
            coro = self._run_many(job_id, specs, airlines=airlines or [])
        self._tasks[job_id] = asyncio.create_task(
            coro
        )

    def result(self, job_id: int) -> dict[str, Any] | None:
        if job_id in self._results:
            return self._results[job_id]
        conn = self._conn()
        row = conn.execute(
            "SELECT status, error FROM search_job WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            conn.close()
            return None
        rows = conn.execute(
            "SELECT rank, dates, price_total_minor, currency, is_estimate, detail "
            "FROM itinerary_result WHERE job_id=? ORDER BY rank",
            (job_id,),
        ).fetchall()
        conn.close()
        results = []
        for r in rows:
            detail = json.loads(r["detail"]) if r["detail"] else []
            route = None
            if detail and isinstance(detail[0], dict):
                route = detail[0].get("route")
            total = r["price_total_minor"] / 100
            results.append(
                {
                    "rank": r["rank"],
                    "route": route,
                    "dates": json.loads(r["dates"]),
                    "total": total,
                    "currency": r["currency"],
                    "verified": not bool(r["is_estimate"]),
                    "estimate": total,
                    "drift": None,
                    "legs": detail,
                }
            )
        return {
            "status": row["status"],
            "error": row["error"],
            "results": results,
        }

    def has_history(self, job_id: int) -> bool:
        """False for jobs this process did not run, e.g. after a restart."""
        return bool(self._history.get(job_id))

    def status(self, job_id: int) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT status FROM search_job WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return row["status"] if row else None

    # -- the actual work ------------------------------------------------------

    @staticmethod
    def _leg_payload(spec, grid, combo, i, day, live, winner=None,
                     indicative=None, by_source=None) -> dict[str, Any]:
        """One leg for the UI: estimate always, real flight when confirmed."""
        leg = spec.legs[i]
        estimated_by = (winner or {}).get((i, day))
        estimated_carriers = [estimated_by] if estimated_by else []
        estimate_bag_minor = checked_bag_fee_minor(estimated_carriers, spec.checked_bags)
        out: dict[str, Any] = {
            "origin": leg.origin,
            "destination": leg.destination,
            "date": day.isoformat(),
            "price": grid[i][day].minor / 100,
            "verified": False,
        }
        if estimated_by:
            out["carriers"] = [estimated_by]
        if estimate_bag_minor:
            out["base_price"] = max(0, grid[i][day].minor - estimate_bag_minor) / 100
            out["bag_fee"] = estimate_bag_minor / 100
            out["checked_bags"] = spec.checked_bags
        # A price from a comparison site is a guide, not a fare. Say so, or the
        # total looks firmer than it is.
        out["indicative"] = (i, day) in (indicative or set())
        offer = live.offers[i] if live is not None else None
        if offer is None:
            return out
        out["price"] = offer.price.major
        out["verified"] = True
        # Offers without segments still belong to an airline. Fall back to the
        # carrier of the adapter that returned this offer, never to whoever won
        # the estimate: those are often different sources.
        fallback = (by_source or {}).get(offer.source)
        out["carriers"] = list(offer.carriers) or ([fallback] if fallback else [])
        live_bag_minor = checked_bag_fee_minor(out["carriers"], spec.checked_bags)
        if live_bag_minor:
            out["base_price"] = out["price"]
            out["bag_fee"] = live_bag_minor / 100
            out["checked_bags"] = spec.checked_bags
            out["price"] = round(out["price"] + out["bag_fee"], 2)
        # The live price replaces the estimate, so this cell is no longer one.
        out["indicative"] = False
        out["deep_link"] = offer.deep_link
        out["source"] = offer.source
        out["stops"] = offer.stops
        if offer.segments:
            first, last = offer.segments[0], offer.segments[-1]
            out["depart"] = first.departure.strftime("%H:%M")
            out["arrive"] = last.arrival.strftime("%H:%M")
            out["flights"] = " / ".join(s.flight_number for s in offer.segments)
            minutes = int((last.arrival - first.departure).total_seconds() // 60)
            out["duration"] = f"{minutes // 60}h {minutes % 60:02d}m"
        return out

    @staticmethod
    def _apply_checked_bag_estimates(spec: SearchSpec, grid, winner) -> None:
        if spec.checked_bags <= 0:
            return
        for i, days in enumerate(grid):
            for day, price in list(days.items()):
                carrier = (winner or {}).get((i, day))
                fee_minor = checked_bag_fee_minor([carrier] if carrier else [], spec.checked_bags)
                if fee_minor:
                    days[day] = Money(price.minor + fee_minor, price.currency)

    async def _run_variant_payloads(self, job_id: int, conn, spec: SearchSpec, *,
                                    airlines: list[str] | None = None,
                                    verify_limit: int = 20) -> list[dict[str, Any]]:
        combos = count_combinations(spec)
        allowed = feasible_dates(spec)
        cells = sum(len(a) for a in allowed)
        self._emit(
            job_id,
            Progress(
                "planning",
                f"{spec.route}: {combos:,} Kombinationen, {cells} Leg-Tage zu bepreisen",
                detail={
                    "combinations": combos,
                    "cells": cells,
                    "route": spec.route,
                    "window": [spec.window_start.isoformat(), spec.window_end.isoformat()],
                },
            ),
        )
        if combos == 0:
            raise ValueError(
                f"{spec.route}: Keine Kombination passt in das Fenster. Fenster verlängern "
                "oder Aufenthalte verkürzen."
            )

        cache = SqliteCache(conn)
        history = SqliteHistory(conn)
        wanted = set(airlines or [])
        catalogue = (RyanairSource(), WizzSource(), AegeanSource(),
                     CondorSource(), EurowingsSource(),
                     BritishAirwaysSource(), IcelandairSource(),
                     KiwiSource())
        sources = []
        for s in catalogue:
            if not wanted:
                sources.append(s)
            elif isinstance(s, KiwiSource):
                sources.append(KiwiSource(carriers=sorted(wanted)))
            elif wanted & set(s.carriers):
                sources.append(s)
        if wanted and not sources:
            names = ", ".join(sorted(wanted))
            raise ValueError(
                f"Für {names} gibt es noch keine Preisquelle. "
                f"Aktuell können nur Ryanair-Flüge abgefragt werden."
            )

        self._emit(job_id, Progress("routes", f"{spec.route}: Prüfe bediente Strecken"))
        for source in sources:
            loader = getattr(source, "load_routes", None)
            if loader is None:
                continue
            seen: set[str] = set()
            for leg in spec.legs:
                if leg.origin not in seen:
                    await loader(leg.origin)
                    seen.add(leg.origin)

        self._emit(
            job_id,
            Progress("fetching", f"{spec.route}: Hole Preiskalender", total=len(spec.legs)),
        )

        def report_source(done: int, total: int) -> None:
            self._emit(
                job_id,
                Progress(
                    "fetching",
                    f"{spec.route}: Preiskalender {done}/{total}",
                    done=done,
                    total=total,
                ),
            )

        grid, report = await build_grid(
            spec, sources, cache=cache, history=history,
            on_progress=report_source,
        )
        coverage = report.coverage(spec)
        self._emit(
            job_id,
            Progress(
                "fetched",
                f"{spec.route}: {report.calls} Abrufe, {report.cache_hits} Cache-Treffer",
                done=len(spec.legs),
                total=len(spec.legs),
                detail={
                    "calls": report.calls,
                    "cache_hits": report.cache_hits,
                    "legs": [
                        {
                            "origin": leg.origin,
                            "destination": leg.destination,
                            "dates": report.filled.get(i, 0),
                            "coverage": round(coverage[i], 3),
                        }
                        for i, leg in enumerate(spec.legs)
                    ],
                    "errors": report.errors[:5],
                },
            ),
        )

        missing = report.missing_legs(spec)
        if missing:
            names = ", ".join(
                f"{spec.legs[i].origin}-{spec.legs[i].destination}" for i in missing
            )
            raise ValueError(
                f"{spec.route}: Keine Preise für {names}. Jedes Teilstück braucht "
                f"mindestens einen Preis, bevor Kombinationen gebildet werden können."
            )
        self._apply_checked_bag_estimates(spec, grid, report.winner)

        self._emit(job_id, Progress("solving", f"{spec.route}: Kombiniere Datumsvarianten"))
        best = solve(spec, grid, top_k=20, max_per_start=3)
        if not best:
            raise ValueError(
                f"{spec.route}: Keine gültige Kombination aus den vorhandenen Preisen."
            )

        def report_verify(done: int, total: int) -> None:
            self._emit(
                job_id,
                Progress(
                    "verifying",
                    f"{spec.route}: Prüfe echte Flüge {done}/{total}",
                    done=done,
                    total=total,
                ),
            )

        self._emit(job_id, Progress("verifying", f"{spec.route}: Prüfe echte Flüge"))
        verified, _ = await verify(
            spec, best, sources, cache=cache, limit=verify_limit,
            on_progress=report_verify,
        )
        live_by_dates = {tuple(v.combination.dates): v for v in verified}
        by_source = {s.name: (s.carrier or s.name.upper()) for s in sources}

        rows = []
        for combo in best:
            live = live_by_dates.get(tuple(combo.dates))
            legs = [
                self._leg_payload(spec, grid, combo, i, d, live,
                                  report.winner, report.indicative, by_source)
                for i, d in enumerate(combo.dates)
            ]
            confirmed = live is not None and live.complete
            shown = sum(round(l["price"] * 100) for l in legs)
            drift = round((shown - combo.total.minor) / 100, 2) if confirmed else None
            rows.append((Money(shown, combo.total.currency), confirmed, combo, legs, drift))
        rows.sort(key=lambda row: row[0].minor)

        payload = []
        for rank, (total, confirmed, combo, legs, drift) in enumerate(rows, 1):
            payload.append(
                {
                    "rank": rank,
                    "route": spec.route,
                    "dates": [d.isoformat() for d in combo.dates],
                    "total": total.major,
                    "currency": total.currency,
                    "verified": confirmed,
                    "estimate": combo.total.major,
                    "drift": drift,
                    "legs": legs,
                }
            )
        return payload

    async def _run_many(self, job_id: int, specs: Sequence[SearchSpec], *,
                        airlines: list[str] | None = None,
                        verify_limit: int = 20) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE search_job SET status='running', started_at=? WHERE id=?",
            (db.now(), job_id),
        )
        conn.commit()

        try:
            all_payloads: list[dict[str, Any]] = []
            for done, spec in enumerate(specs, 1):
                self._emit(
                    job_id,
                    Progress(
                        "routes",
                        f"Route {done}/{len(specs)}: {spec.route}",
                        done=done - 1,
                        total=len(specs),
                    ),
                )
                all_payloads.extend(
                    await self._run_variant_payloads(
                        job_id, conn, spec, airlines=airlines or [],
                        verify_limit=verify_limit,
                    )
                )

            payload = merge_variant_payloads(all_payloads, top_k=20)
            conn.execute("DELETE FROM itinerary_result WHERE job_id=?", (job_id,))
            for row in payload:
                conn.execute(
                    "INSERT INTO itinerary_result("
                    "job_id, rank, dates, price_total_minor, currency, is_estimate, detail) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        job_id,
                        row["rank"],
                        json.dumps(row["dates"]),
                        round(float(row["total"]) * 100),
                        row["currency"],
                        0 if row.get("verified") else 1,
                        json.dumps(row["legs"]),
                    ),
                )

            conn.execute(
                "UPDATE search_job SET status='done', finished_at=? WHERE id=?",
                (db.now(), job_id),
            )
            conn.commit()

            self._results[job_id] = {"status": "done", "error": None, "results": payload}
            confirmed_n = sum(1 for p in payload if p.get("verified"))
            self._emit(
                job_id,
                Progress(
                    "done",
                    f"{len(payload)} Kandidaten aus {len(specs)} Routenvarianten, "
                    f"{confirmed_n} mit echtem Flug bestätigt",
                    done=len(specs),
                    total=len(specs),
                    detail={"results": payload},
                ),
            )

        except Exception as exc:  # noqa: BLE001 - surface every failure to the UI
            logger.exception("job %s failed", job_id)
            message = str(exc) or type(exc).__name__
            conn.execute(
                "UPDATE search_job SET status='failed', finished_at=?, error=? WHERE id=?",
                (db.now(), message, job_id),
            )
            conn.commit()
            self._results[job_id] = {"status": "failed", "error": message, "results": []}
            self._emit(job_id, Progress("failed", message))
        finally:
            conn.close()

    async def _run(self, job_id: int, spec: SearchSpec, *,
                   airlines: list[str] | None = None, verify_limit: int = 20) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE search_job SET status='running', started_at=? WHERE id=?",
            (db.now(), job_id),
        )
        conn.commit()

        try:
            combos = count_combinations(spec)
            allowed = feasible_dates(spec)
            cells = sum(len(a) for a in allowed)
            self._emit(
                job_id,
                Progress(
                    "planning",
                    f"{combos:,} Kombinationen, {cells} Leg-Tage zu bepreisen",
                    detail={
                        "combinations": combos,
                        "cells": cells,
                        "route": spec.route,
                        "window": [spec.window_start.isoformat(), spec.window_end.isoformat()],
                    },
                ),
            )
            if combos == 0:
                raise ValueError(
                    "Keine Kombination passt in das Fenster. Fenster verlaengern "
                    "oder Aufenthalte verkuerzen."
                )

            cache = SqliteCache(conn)
            history = SqliteHistory(conn)
            # An empty filter means "everything we can price"; otherwise keep
            # only the sources whose carrier the person actually asked for.
            wanted = set(airlines or [])
            catalogue = (RyanairSource(), WizzSource(), AegeanSource(),
                         CondorSource(), EurowingsSource(),
                         BritishAirwaysSource(), IcelandairSource(),
                         KiwiSource())
            # Kiwi has no carrier of its own, so a carrier filter is
            # passed through to it instead of excluding it.
            sources = []
            for s in catalogue:
                if not wanted:
                    sources.append(s)
                elif isinstance(s, KiwiSource):
                    sources.append(KiwiSource(carriers=sorted(wanted)))
                elif wanted & set(s.carriers):
                    sources.append(s)
            if not wanted:
                pass
            elif not sources:
                names = ", ".join(sorted(wanted))
                raise ValueError(
                    f"Für {names} gibt es noch keine Preisquelle. "
                    f"Aktuell können nur Ryanair-Flüge abgefragt werden."
                )

            self._emit(job_id, Progress("routes", "Prüfe, welche Strecken bedient werden"))
            for source in sources:
                loader = getattr(source, "load_routes", None)
                if loader is None:
                    continue
                seen: set[str] = set()
                for leg in spec.legs:
                    if leg.origin not in seen:
                        await loader(leg.origin)
                        seen.add(leg.origin)

            self._emit(
                job_id,
                Progress("fetching", "Hole Preiskalender", total=len(spec.legs)),
            )

            def report_source(done: int, total: int) -> None:
                self._emit(
                    job_id,
                    Progress(
                        "fetching",
                        f"Preiskalender {done}/{total}",
                        done=done,
                        total=total,
                    ),
                )

            grid, report = await build_grid(
                spec, sources, cache=cache, history=history,
                on_progress=report_source,
            )

            coverage = report.coverage(spec)
            self._emit(
                job_id,
                Progress(
                    "fetched",
                    f"{report.calls} Abrufe, {report.cache_hits} aus dem Cache",
                    done=len(spec.legs),
                    total=len(spec.legs),
                    detail={
                        "calls": report.calls,
                        "cache_hits": report.cache_hits,
                        "legs": [
                            {
                                "origin": leg.origin,
                                "destination": leg.destination,
                                "dates": report.filled.get(i, 0),
                                "coverage": round(coverage[i], 3),
                            }
                            for i, leg in enumerate(spec.legs)
                        ],
                        "errors": report.errors[:5],
                    },
                ),
            )

            missing = report.missing_legs(spec)
            if missing:
                names = ", ".join(
                    f"{spec.legs[i].origin}-{spec.legs[i].destination}" for i in missing
                )
                raise ValueError(
                    f"Keine Preise für {names}. Jedes Teilstück braucht mindestens "
                    f"einen Preis, bevor Kombinationen gebildet werden können."
                )
            self._apply_checked_bag_estimates(spec, grid, report.winner)

            self._emit(job_id, Progress("solving", "Kombiniere Datumsvarianten"))
            best = solve(spec, grid, top_k=20, max_per_start=3)
            if not best:
                raise ValueError("Keine gültige Kombination aus den vorhandenen Preisen.")

            # Phase two: the shortlist gets real flights, times and booking links.
            def report_verify(done: int, total: int) -> None:
                self._emit(
                    job_id,
                    Progress(
                        "verifying",
                        f"Prüfe echte Flüge {done}/{total}",
                        done=done,
                        total=total,
                    ),
                )

            self._emit(job_id, Progress("verifying", "Prüfe echte Flüge"))
            verified, vreport = await verify(
                spec, best, sources, cache=cache, limit=verify_limit,
                on_progress=report_verify,
            )
            live_by_dates = {
                tuple(v.combination.dates): v for v in verified
            }
            by_source = {s.name: (s.carrier or s.name.upper()) for s in sources}

            payload = []
            conn.execute("DELETE FROM itinerary_result WHERE job_id=?", (job_id,))
            # Build every row first, then rank. Verification can change a
            # price, so ranking before that would leave the list out of order
            # against the totals it displays.
            rows = []
            for combo in best:
                live = live_by_dates.get(tuple(combo.dates))
                legs = [
                    self._leg_payload(spec, grid, combo, i, d, live,
                                      report.winner, report.indicative, by_source)
                    for i, d in enumerate(combo.dates)
                ]
                confirmed = live is not None and live.complete
                # Verification can confirm some legs and not others. Summing the
                # prices actually displayed keeps the total honest; taking the
                # estimate total would contradict the rows beneath it.
                shown = sum(round(l["price"] * 100) for l in legs)
                drift = round((shown - combo.total.minor) / 100, 2) if confirmed else None
                rows.append(
                    (Money(shown, combo.total.currency), confirmed, combo, legs, drift)
                )
            rows.sort(key=lambda row: row[0].minor)

            for rank, (total, confirmed, combo, legs, drift) in enumerate(rows, 1):
                conn.execute(
                    "INSERT INTO itinerary_result("
                    "job_id, rank, dates, price_total_minor, currency, is_estimate, detail) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        job_id,
                        rank,
                        json.dumps([d.isoformat() for d in combo.dates]),
                        total.minor,
                        total.currency,
                        0 if confirmed else 1,
                        json.dumps(legs),
                    ),
                )
                payload.append(
                    {
                        "rank": rank,
                        "dates": [d.isoformat() for d in combo.dates],
                        "total": total.major,
                        "currency": total.currency,
                        "verified": confirmed,
                        "estimate": combo.total.major,
                        "drift": drift,
                        "legs": legs,
                    }
                )

            conn.execute(
                "UPDATE search_job SET status='done', finished_at=? WHERE id=?",
                (db.now(), job_id),
            )
            conn.commit()

            self._results[job_id] = {"status": "done", "error": None, "results": payload}
            confirmed_n = sum(1 for p in payload if p.get("verified"))
            self._emit(
                job_id,
                Progress(
                    "done",
                    f"{len(payload)} Varianten, {confirmed_n} mit echtem Flug bestätigt "
                    f"({vreport.calls} Abrufe)",
                    done=len(spec.legs),
                    total=len(spec.legs),
                    detail={"results": payload},
                ),
            )

        except Exception as exc:  # noqa: BLE001 - surface every failure to the UI
            logger.exception("job %s failed", job_id)
            message = str(exc) or type(exc).__name__
            conn.execute(
                "UPDATE search_job SET status='failed', finished_at=?, error=? WHERE id=?",
                (db.now(), message, job_id),
            )
            conn.commit()
            self._results[job_id] = {"status": "failed", "error": message, "results": []}
            self._emit(job_id, Progress("failed", message))
        finally:
            conn.close()
