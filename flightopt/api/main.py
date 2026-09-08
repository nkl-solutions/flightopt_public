"""Local web API.

    uv run uvicorn flightopt.api.main:app --reload --port 8000

Single user, localhost only. Search runs as a background job; the browser
follows it over Server-Sent Events rather than holding a request open for
minutes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import mimetypes
import os
import secrets
from contextlib import asynccontextmanager
from itertools import product
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from flightopt.domain import airlines as airline_registry
from flightopt.domain import airports as airport_registry
from flightopt.domain.models import Cabin, LegSpec, Pax, SearchSpec, StayRange
from flightopt.domain.natural_search import parse_search_text
from flightopt.hotels.models import HotelQuery
from flightopt.hotels.registry import build_hotel_sources, source_report
from flightopt.hotels.scan import load_scan, scan_days
from flightopt.jobs.daily import (
    collect_deals,
    dispatch_due_profiles,
    due_profiles,
    save_profile,
)
from flightopt.jobs.hotel_runner import (
    MAX_HOTEL_DAYS,
    TERMINAL as HOTEL_TERMINAL,
    HotelJobRunner,
    stored_rows,
)
from flightopt.jobs.runner import JobRunner
from flightopt.jobs.scheduler import DailyScanScheduler
from flightopt.search.dp import count_combinations, feasible_dates
from flightopt.sources.registry import build_sources
from flightopt.trip.stays import StayOptions

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
AUTH_REALM = "flightopt"

runner = JobRunner()
hotel_runner = HotelJobRunner()
daily_scheduler = DailyScanScheduler(
    runner,
    interval_seconds=int(os.getenv("FLIGHTOPT_SCAN_INTERVAL_SECONDS", "600")),
)


def scheduler_autostart_enabled() -> bool:
    value = os.getenv("FLIGHTOPT_DAILY_SCANS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def basic_auth_config() -> tuple[str, str] | None:
    user = os.getenv("FLIGHTOPT_BASIC_USER", "").strip()
    password = os.getenv("FLIGHTOPT_BASIC_PASSWORD", "")
    if not user and not password:
        return None
    if not user or not password:
        raise RuntimeError(
            "FLIGHTOPT_BASIC_USER and FLIGHTOPT_BASIC_PASSWORD must be set together"
        )
    return user, password


def basic_auth_valid(header: str | None) -> bool:
    config = basic_auth_config()
    if config is None:
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    supplied_user, sep, supplied_password = raw.partition(":")
    if not sep:
        return False
    expected_user, expected_password = config
    return secrets.compare_digest(supplied_user, expected_user) and secrets.compare_digest(
        supplied_password,
        expected_password,
    )


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


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if request.url.path == "/api/health" or basic_auth_valid(
        request.headers.get("authorization")
    ):
        return await call_next(request)
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
    )


# Die Systemtabelle fuer MIME-Typen ist je nach Plattform anders bestueckt: Linux meldet
# fuer .js gern application/javascript und .woff2 kennt Python gar nicht. Beides hier
# festnageln, damit der Mount ueberall dieselben Content-Types liefert.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")

# Die HTTP-Middleware umschliesst auch Mounts, CSS, JS und Fonts gehen daher nur mit
# gueltigem Basic-Auth heraus.
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


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
    checked_bags: int = Field(default=0, ge=0, le=2)
    max_stops: int | None = Field(default=None, ge=0, le=2)
    """None = automatisch aus der Streckenlaenge."""
    airlines: list[str] = Field(default_factory=list)
    """Restrict to these carrier codes. Empty means every source we have."""
    with_hotels: bool = False
    """Uebernachtungen mitrechnen. Standard ist aus, und aus heisst wirklich
    aus: ohne diesen Schalter laeuft die Flugsuche wie bisher und fragt keine
    einzige Unterkunft ab."""
    hotel_adults: int = Field(default=2, ge=1, le=12)
    hotel_rooms: int = Field(default=1, ge=1, le=8)

    def stay_options(self) -> StayOptions | None:
        """Der Auftrag fuer den Aufenthaltsschritt, oder gar keiner."""
        if not self.with_hotels:
            return None
        return StayOptions(
            sources=build_hotel_sources,
            adults=self.hotel_adults,
            rooms=self.hotel_rooms,
            currency=self.currency,
        )

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
        airport_registry.check_variant_budget(stops)
        variants = list(product(*(airport_registry.expand_code(code) for code in stops)))

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
            checked_bags=self.checked_bags,
            max_stops=self.max_stops,
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


class NaturalSearchRequest(BaseModel):
    text: str = Field(min_length=3, max_length=1000)


class HotelSearchRequest(BaseModel):
    """Eine Hotelsuche. Sie laeuft als Job, das Fenster darf lang sein."""

    destination: str = Field(min_length=2, max_length=120)
    arrival: date
    window_end: date | None = None
    nights: int = Field(default=1, ge=1, le=30)
    adults: int = Field(default=2, ge=1, le=12)
    children: list[int] = Field(default_factory=list)
    rooms: int = Field(default=1, ge=1, le=8)
    stars: list[int] = Field(default_factory=list)
    min_review_score: float | None = Field(default=None, ge=0, le=10)
    currency: str = "EUR"
    country: str = "DE"
    scan_id: int | None = None
    """Einen gestoppten Lauf fortsetzen statt einen neuen anzufangen."""

    def window(self) -> tuple[date, date]:
        end = self.window_end or self.arrival
        if end < self.arrival:
            raise ValueError("Das Fensterende liegt vor dem Anfang.")
        days = (end - self.arrival).days + 1
        if days > MAX_HOTEL_DAYS:
            # Gekuerzt wird nichts stillschweigend: wer 500 Tage eintippt, soll
            # das erfahren und nicht 400 Tage Ergebnisse fuer 500 halten.
            raise ValueError(
                f"Das Fenster umfasst {days} Tage. Je Lauf sind "
                f"{MAX_HOTEL_DAYS} Tage vorgesehen, teile den Zeitraum auf."
            )
        return self.arrival, end

    def to_query(self) -> HotelQuery:
        ages = [int(age) for age in self.children]
        if any(age < 0 or age > 17 for age in ages):
            raise ValueError("Kinderalter liegt zwischen 0 und 17.")
        return HotelQuery(
            destination=self.destination.strip(),
            arrival=self.arrival,
            nights=self.nights,
            adults=self.adults,
            children=tuple(ages),
            rooms=self.rooms,
            stars=tuple(sorted({int(s) for s in self.stars})),
            country=self.country,
            currency=self.currency,
            min_review_score=self.min_review_score,
        )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/hotels")
async def hotels_page() -> FileResponse:
    return FileResponse(WEB_DIR / "hotels.html")


@app.post("/api/hotels/search")
async def hotel_search(req: HotelSearchRequest) -> dict[str, Any]:
    """Startet den Durchlauf und antwortet mit seiner Kennung, nicht mit Zeilen.

    Die Zeilen kommen ueber den Ereignisstrom, Tag fuer Tag. Ein Einzeltag ist
    damit genauso schnell wie vorher, nur haelt niemand mehr die Anfrage auf.
    """
    try:
        window_start, window_end = req.window()
        query = req.to_query()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if req.scan_id is not None:
        # Wiederaufnahme: derselbe Lauf, current_day sagt dem Runner, wo er
        # weitermacht.
        if hotel_runner.status(req.scan_id) is None:
            raise HTTPException(status_code=404, detail="Unbekannter Durchlauf")
        scan_id = req.scan_id
    else:
        scan_id = hotel_runner.create(
            query, window_start=window_start, window_end=window_end
        )
    hotel_runner.start(
        scan_id, query,
        window_start=window_start, window_end=window_end,
        sources=build_hotel_sources,
    )
    return {
        "scan_id": scan_id,
        "status": "running",
        "destination": query.destination,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "nights": query.nights,
        "days_total": len(scan_days(window_start, window_end)),
        "sources": source_report(),
    }


@app.get("/api/hotels/scan/{scan_id}")
async def hotel_scan_state(scan_id: int) -> dict[str, Any]:
    """Der Stand eines Durchlaufs, auch wenn er gestoppt wurde."""
    conn = hotel_runner._conn()
    try:
        scan = load_scan(conn, scan_id)
        if scan is None:
            raise HTTPException(status_code=404, detail="Unbekannter Durchlauf")
        return scan
    finally:
        conn.close()


@app.get("/api/hotels/scans")
async def hotel_scans(limit: int = 20) -> dict[str, Any]:
    """Die letzten Laeufe: Ziel, Fenster, Stand, Treffer, Zeitpunkt."""
    conn = hotel_runner._conn()
    try:
        rows = conn.execute(
            "SELECT id, destination, window_start, window_end, nights, status, "
            "days_done, days_total, offers_found, created_at, finished_at "
            "FROM hotel_scan ORDER BY id DESC LIMIT ?",
            (max(1, min(100, limit)),),
        ).fetchall()
        return {"scans": [dict(row) for row in rows]}
    finally:
        conn.close()


@app.get("/api/hotels/scan/{scan_id}/rows")
async def hotel_scan_rows(scan_id: int) -> dict[str, Any]:
    """Das Ergebnis eines gespeicherten Laufs, aus der Beobachtungshistorie."""
    conn = hotel_runner._conn()
    try:
        scan = load_scan(conn, scan_id)
        if scan is None:
            raise HTTPException(status_code=404, detail="Unbekannter Durchlauf")
        return {
            "scan_id": scan_id,
            "status": scan["status"],
            "destination": scan["destination"],
            "window": {"start": scan["window_start"], "end": scan["window_end"]},
            "nights": scan["nights"],
            "days_done": scan["days_done"],
            "days_total": scan["days_total"],
            "rows": stored_rows(conn, scan),
        }
    finally:
        conn.close()


@app.get("/api/hotels/scan/{scan_id}/events")
async def hotel_scan_events(scan_id: int) -> StreamingResponse:
    """Fortschritt je Tag, Teilergebnisse, Abschluss. Wie bei der Flugsuche."""
    if hotel_runner.status(scan_id) is None:
        raise HTTPException(status_code=404, detail="Unbekannter Durchlauf")
    queue = hotel_runner.subscribe(scan_id)

    async def stream():
        try:
            # Ein Lauf aus einem frueheren Prozess hat keinen Verlauf mehr, nur
            # seinen Stand. Also den senden, statt auf Ereignisse zu warten,
            # die nie kommen.
            if not hotel_runner.has_history(scan_id):
                done = hotel_runner.result(scan_id)
                if done and done["status"] in HOTEL_TERMINAL:
                    if done["status"] == "cancelled":
                        message = "Durchlauf abgebrochen"
                    else:
                        message = done.get("error") or "fertig"
                    payload = {
                        "phase": done["status"],
                        "message": message,
                        "done": 0,
                        "total": 0,
                        "detail": {"rows": done["rows"]},
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
                if progress.phase in HOTEL_TERMINAL:
                    return
        finally:
            hotel_runner.unsubscribe(scan_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/hotels/scan/{scan_id}/cancel")
async def cancel_hotel_scan(scan_id: int) -> dict[str, Any]:
    """Stoppt einen laufenden Durchlauf. Der Runner prueft zwischen zwei Tagen."""
    if hotel_runner.status(scan_id) is None:
        raise HTTPException(status_code=404, detail="Unbekannter Durchlauf")
    return {"scan_id": scan_id, "status": hotel_runner.cancel(scan_id)}


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


@app.post("/api/parse-search")
async def parse_search_text_endpoint(req: NaturalSearchRequest) -> dict[str, Any]:
    """Turn a short route sentence into form values, not into prices."""
    try:
        return parse_search_text(req.text).as_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/estimate")
async def estimate(req: SearchRequest) -> dict[str, Any]:
    """Size the search before running it, so the form can warn about a huge window."""
    try:
        specs = req.to_specs()
    except airport_registry.TooManyVariants as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    """Destinations the sources can actually price from this airport.

    Jede Quelle, die ihr Streckennetz veroeffentlicht, traegt bei; eine, die
    scheitert, faellt weg, statt die Liste fuer alle anderen zu leeren.
    """
    code = origin.upper()
    loaders = [
        source.load_routes(code)
        for source in build_sources(None, env=os.environ)
        if hasattr(source, "load_routes")
    ]
    destinations: set[str] = set()
    for result in await asyncio.gather(*loaders, return_exceptions=True):
        if isinstance(result, BaseException):
            logger.warning("routes: eine Quelle scheiterte fuer %s: %s", code, result)
            continue
        destinations |= set(result)
    return {"origin": code, "destinations": sorted(destinations)}


@app.post("/api/search")
async def start_search(req: SearchRequest) -> dict[str, Any]:
    try:
        specs = req.to_specs()
    except airport_registry.TooManyVariants as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = runner.create(specs)
    runner.start(
        job_id, specs,
        airlines=[a.upper() for a in req.airlines],
        stays=req.stay_options(),
    )
    route = specs[0].route if len(specs) == 1 else f"{len(specs)} Routenvarianten"
    return {"job_id": job_id, "route": route, "with_hotels": req.with_hotels}


@app.post("/api/profiles")
async def save_profile_endpoint(req: ProfileRequest) -> dict[str, Any]:
    try:
        specs = req.to_specs()
    except airport_registry.TooManyVariants as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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


@app.get("/api/deals")
async def deals(limit: int = 50) -> dict[str, Any]:
    """Die juengsten Profil-Treffer mit Abstand zur Baseline."""
    conn = runner._conn()
    try:
        return {"deals": collect_deals(conn, limit=max(1, min(200, limit)))}
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


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict[str, Any]:
    """Bricht eine laufende Suche ab. Der Runner prueft das Flag zwischen den Phasen."""
    if runner.status(job_id) is None:
        raise HTTPException(status_code=404, detail="Unbekannter Job")
    return {"job_id": job_id, "status": runner.cancel(job_id)}


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
                if done and done["status"] in ("done", "failed", "cancelled"):
                    if done["status"] == "cancelled":
                        message = "Suche abgebrochen"
                    else:
                        message = done.get("error") or "fertig"
                    payload = {
                        "phase": done["status"],
                        "message": message,
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
                if progress.phase in ("done", "failed", "cancelled"):
                    return
        finally:
            runner.unsubscribe(job_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
