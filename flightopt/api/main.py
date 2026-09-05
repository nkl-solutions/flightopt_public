"""Local web API.

    uv run uvicorn flightopt.api.main:app --reload --port 8000

Single user, localhost only. Search runs as a background job; the browser
follows it over Server-Sent Events rather than holding a request open for
minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from itertools import product
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from flightopt.domain import airlines as airline_registry
from flightopt.domain import airports as airport_registry
from flightopt.domain.models import Cabin, LegSpec, Pax, SearchSpec, StayRange
from flightopt.jobs.daily import dispatch_due_profiles, due_profiles, save_profile
from flightopt.jobs.runner import JobRunner
from flightopt.jobs.scheduler import DailyScanScheduler
from flightopt.search.dp import count_combinations, feasible_dates
from flightopt.sources.ryanair import RyanairSource

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

runner = JobRunner()
daily_scheduler = DailyScanScheduler(
    runner,
    interval_seconds=int(os.getenv("FLIGHTOPT_SCAN_INTERVAL_SECONDS", "600")),
)


def scheduler_autostart_enabled() -> bool:
    value = os.getenv("FLIGHTOPT_DAILY_SCANS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


async def start_daily_scheduler() -> None:
    if scheduler_autostart_enabled():
        daily_scheduler.start()


async def stop_daily_scheduler() -> None:
    await daily_scheduler.stop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_daily_scheduler()
    try:
        yield
    finally:
        await stop_daily_scheduler()


app = FastAPI(title="flightopt", docs_url="/api/docs", lifespan=lifespan)


class SearchRequest(BaseModel):
    airports: list[str] = Field(min_length=1)
    window_start: date
    window_end: date
    stays: list[list[int]] = Field(default_factory=list)
    trip: str = "multi"
    """one_way | return | multi. Only changes how `airports` is read."""
    adults: int = 1
    cabin: str = "economy"
    currency: str = "EUR"
    airlines: list[str] = Field(default_factory=list)
    """Restrict to these carrier codes. Empty means every source we have."""

    def _stops(self) -> list[str]:
        codes = [a.strip().upper() for a in self.airports if a.strip()]
        need = {"one_way": 2, "return": 2, "multi": 3}.get(self.trip)
        if need is None:
            raise ValueError(f"Unbekannte Reiseart: {self.trip}")
        if len(codes) < need:
            label = {
                "one_way": "Ein einfacher Flug braucht Start und Ziel.",
                "return": "Hin und zurück braucht Start und Ziel.",
                "multi": "Mehrere Stopps brauchen mindestens drei Flughäfen.",
            }[self.trip]
            raise ValueError(label)

        if self.trip == "one_way":
            return codes[:2]
        elif self.trip == "return":
            # A return trip is the two-stop case with the origin repeated.
            return [codes[0], codes[1], codes[0]]
        return codes

    def to_specs(self) -> list[SearchSpec]:
        stops = self._stops()
        variants = list(product(*(airport_registry.expand_code(code) for code in stops)))
        if len(variants) > 100:
            raise ValueError("Flughafen-Gruppen ergeben zu viele Routenvarianten.")

        specs: list[SearchSpec] = []
        for concrete in variants:
            if any(concrete[i] == concrete[i + 1] for i in range(len(concrete) - 1)):
                continue
            specs.append(self._to_spec_for(list(concrete)))
        if not specs:
            raise ValueError("Flughafen-Gruppen ergeben keine gültige Route.")
        return specs

    def _to_spec_for(self, stops: list[str]) -> SearchSpec:
        legs = tuple(LegSpec(stops[i], stops[i + 1]) for i in range(len(stops) - 1))

        raw = self.stays or [[3, 10]]
        if len(legs) == 1:
            stays: tuple[StayRange, ...] = ()
        elif len(raw) == 1:
            stays = tuple(StayRange(raw[0][0], raw[0][1]) for _ in range(len(legs) - 1))
        elif len(raw) == len(legs) - 1:
            stays = tuple(StayRange(a, b) for a, b in raw)
        else:
            raise ValueError(
                f"Es braucht entweder einen Aufenthalt für alle Stopps "
                f"oder genau {len(legs) - 1}."
            )
        return SearchSpec(
            legs=legs,
            stays=stays,
            window_start=self.window_start,
            window_end=self.window_end,
            pax=Pax(adults=self.adults),
            cabin=Cabin(self.cabin),
            currency=self.currency,
        )

    def to_spec(self) -> SearchSpec:
        specs = self.to_specs()
        if len(specs) != 1:
            raise ValueError(
                "Diese Eingabe ergibt mehrere Routenvarianten; nutze to_specs()."
            )
        return specs[0]


class ProfileRequest(SearchRequest):
    name: str
    cadence_days: int = Field(default=1, ge=1, le=30)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "sources": [a.code for a in airline_registry.LIVE]}


@app.get("/api/airports")
async def airports(q: str = "", limit: int = 8) -> dict[str, Any]:
    """Type-ahead for the route fields so nobody has to recall IATA codes."""
    return {"results": [a.as_dict() for a in airport_registry.search(q, limit)]}


@app.get("/api/airlines")
async def airlines() -> dict[str, Any]:
    """Every carrier we know, and whether we can price it yet."""
    return {"airlines": airline_registry.as_dicts()}


@app.post("/api/estimate")
async def estimate(req: SearchRequest) -> dict[str, Any]:
    """Size the search before running it, so the form can warn about a huge window."""
    try:
        specs = req.to_specs()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    allowed_by_spec = [feasible_dates(spec) for spec in specs]
    return {
        "route": specs[0].route if len(specs) == 1 else f"{len(specs)} Routenvarianten",
        "combinations": sum(count_combinations(spec) for spec in specs),
        "cells": sum(len(days) for allowed in allowed_by_spec for days in allowed),
        "window_days": max(spec.window_days for spec in specs),
        "variants": len(specs),
    }


@app.get("/api/routes/{origin}")
async def routes(origin: str) -> dict[str, Any]:
    """Destinations the sources can actually price from this airport."""
    source = RyanairSource()
    dests = await source.load_routes(origin.upper())
    return {"origin": origin.upper(), "destinations": sorted(dests)}


@app.post("/api/search")
async def start_search(req: SearchRequest) -> dict[str, Any]:
    try:
        specs = req.to_specs()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = runner.create(specs)
    runner.start(job_id, specs, airlines=[a.upper() for a in req.airlines])
    route = specs[0].route if len(specs) == 1 else f"{len(specs)} Routenvarianten"
    return {"job_id": job_id, "route": route}


@app.post("/api/profiles")
async def save_profile_endpoint(req: ProfileRequest) -> dict[str, Any]:
    try:
        specs = req.to_specs()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = runner._conn()
    try:
        profile_id = save_profile(
            conn,
            req.name.strip() or "Neue Suche",
            specs,
            airlines=[a.upper() for a in req.airlines],
            cadence_days=req.cadence_days,
        )
        row = conn.execute(
            "SELECT next_run_at FROM search_profile WHERE id=?",
            (profile_id,),
        ).fetchone()
        return {
            "profile_id": profile_id,
            "name": req.name.strip() or "Neue Suche",
            "variants": len(specs),
            "next_run_at": row["next_run_at"],
        }
    finally:
        conn.close()


@app.get("/api/profiles/due")
async def due_profiles_endpoint() -> dict[str, Any]:
    conn = runner._conn()
    try:
        return {
            "profiles": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "variants": len(profile.specs),
                    "next_run_at": profile.next_run_at.isoformat(timespec="seconds"),
                }
                for profile in due_profiles(conn)
            ]
        }
    finally:
        conn.close()


@app.post("/api/profiles/dispatch")
async def dispatch_profiles_endpoint() -> dict[str, Any]:
    conn = runner._conn()
    try:
        return {"jobs": dispatch_due_profiles(conn, runner)}
    finally:
        conn.close()


@app.get("/api/scanner")
async def scheduler_status_endpoint() -> dict[str, Any]:
    return {
        "running": daily_scheduler._task is not None and not daily_scheduler._task.done(),
        "interval_seconds": daily_scheduler.interval_seconds,
    }


@app.post("/api/scanner/run-once")
async def scheduler_run_once_endpoint() -> dict[str, Any]:
    return {"jobs": await daily_scheduler.run_once()}


@app.get("/api/jobs/{job_id}")
async def job(job_id: int) -> dict[str, Any]:
    result = runner.result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Unbekannter Job")
    return result


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: int) -> StreamingResponse:
    if runner.status(job_id) is None:
        raise HTTPException(status_code=404, detail="Unbekannter Job")
    queue = runner.subscribe(job_id)

    async def stream():
        try:
            # A job from a previous process has no replayable history, so send
            # its stored outcome instead of waiting for events that never come.
            if not runner.has_history(job_id):
                done = runner.result(job_id)
                if done and done["status"] in ("done", "failed"):
                    payload = {
                        "phase": done["status"],
                        "message": done.get("error") or "fertig",
                        "detail": {"results": done["results"]},
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    return
            while True:
                try:
                    progress = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {progress.to_json()}\n\n"
                if progress.phase in ("done", "failed"):
                    return
        finally:
            runner.unsubscribe(job_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
