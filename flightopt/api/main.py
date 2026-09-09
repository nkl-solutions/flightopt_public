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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from flightopt.domain import airlines as airline_registry
from flightopt.domain import airports as airport_registry
from flightopt.domain.models import Cabin, LegSpec, Pax, SearchSpec, StayRange
from flightopt.domain.natural_search import parse_search_text
from flightopt.hotels import watch as hotel_watch
from flightopt.hotels.browser import budget_from_env, close_shared_pools
from flightopt.hotels.models import HotelQuery
from flightopt.hotels.registry import build_hotel_sources, source_report
from flightopt.hotels.scan import load_scan, scan_days
from flightopt.hotels.store import HOTEL_BASELINE_SPLITS_POPULATIONS
from flightopt.hunt import alerts, budget, cadence, discord
from flightopt.hunt import history as hunt_history_mod
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
from flightopt.jobs.scheduler import DailyScanScheduler, purge_cache
from flightopt.search.dp import count_combinations, feasible_dates
from flightopt.sources.registry import build_sources
from flightopt.storage import db, watchlist
from flightopt.storage.cache import TTL_CALENDAR, last_observation_by_source
from flightopt.trip.stays import StayOptions

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
AUTH_REALM = "flightopt"

SOURCE_SILENT_AFTER = timedelta(days=3)
"""Ab wann eine Quelle im Bericht als still gilt.

Die gespeicherten Suchen laufen taeglich, drei Tage ohne eine einzige
geschriebene Beobachtung sind also kein Zufall mehr. "Still" heisst
ausdruecklich nicht "kaputt": eine Quelle, die nur Transatlantik bepreist,
schweigt auch dann, wenn niemand danach gesucht hat. Der Bericht nennt die
Zahl der Tage, den Schluss zieht ein Mensch.
"""

runner = JobRunner()
hotel_runner = HotelJobRunner()
daily_scheduler = DailyScanScheduler(
    runner,
    interval_seconds=int(os.getenv("FLIGHTOPT_SCAN_INTERVAL_SECONDS", "600")),
)


def scheduler_autostart_enabled() -> bool:
    value = os.getenv("FLIGHTOPT_DAILY_SCANS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


class AuthMisconfigured(RuntimeError):
    """Nur eine Haelfte des Zugangspaars ist gesetzt.

    Ein eigener Typ, damit die Middleware den Betriebsfehler vom Programmfehler
    unterscheiden kann. Vorher flog hier ein nackter RuntimeError mitten aus der
    Middleware: jede Route antwortete mit 500, `/api/health` blieb gruen, und
    der Container galt als gesund, obwohl kein einziger Aufruf durchkam.
    """


def basic_auth_config() -> tuple[str, str] | None:
    user = os.getenv("FLIGHTOPT_BASIC_USER", "").strip()
    password = os.getenv("FLIGHTOPT_BASIC_PASSWORD", "")
    if not user and not password:
        return None
    if not user or not password:
        raise AuthMisconfigured(
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


def has_live_task(owner: Any, run_id: int) -> bool:
    """Fuehrt dieser Prozess ueberhaupt einen Task fuer diesen Lauf.

    Steht ein Lauf in der Datenbank auf `running`, heisst das nur, dass ihn
    irgendwann ein Prozess gefuehrt hat. War das ein frueherer, sendet niemand
    mehr Ereignisse: der Strom wartete darauf endlos und schickte alle zwanzig
    Sekunden ein Keepalive, waehrend der Browser bei "laeuft" stehen blieb.
    """
    task = getattr(owner, "_tasks", {}).get(run_id)
    return task is not None and not task.done()


def close_interrupted_runs() -> dict[str, int]:
    """Beim Start: Laeufe aus einem toten Prozess ehrlich abschliessen.

    Ohne das steht ein Lauf, dessen Prozess weg ist, fuer immer auf `running`.
    Die Oberflaeche zeigt ihn als laufend und der Ereignisstrom wartet auf
    Ereignisse, die niemand mehr sendet.
    """
    try:
        conn = runner._conn()
    except Exception as exc:  # noqa: BLE001 - der Dienst startet trotzdem
        logger.error("Start: haengende Laeufe nicht pruefbar (%s)", exc)
        return {}
    try:
        counts = db.mark_interrupted_runs(conn)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("Start: haengende Laeufe nicht geschlossen (%s)", exc)
        return {}
    finally:
        conn.close()
    if any(counts.values()):
        logger.warning(
            "Start: %d Suchen und %d Hoteldurchlaeufe waren abgerissen",
            counts.get("search_job", 0),
            counts.get("hotel_scan", 0),
        )
    return counts


async def purge_cache_at_start() -> int:
    """Einmal beim Start abgelaufene Cache-Zeilen wegraeumen.

    Der Tagesplaner raeumt bei jedem Durchgang mit, aber er laesst sich
    abschalten (`FLIGHTOPT_DAILY_SCANS=0`). Ohne diesen Aufruf waere dann
    niemand mehr zustaendig, und der Cache waechst mit jedem Abruf weiter.
    """
    try:
        conn = runner._conn()
    except Exception as exc:  # noqa: BLE001 - Hausputz haelt keinen Start auf
        logger.warning("cache: kein Zugriff zum Aufraeumen (%s)", exc)
        return 0
    try:
        return await purge_cache(conn)
    finally:
        conn.close()


async def close_hotel_browsers() -> None:
    """Den gemeinsamen Chromium zum Prozessende zumachen.

    Die Leerlaufwache in `hotels/browser.py` erledigt es sonst auch - zwei
    Minuten nach der letzten Seite. Beim Herunterfahren ist das zwei Minuten zu
    spaet: der Prozess geht, und ein Chromium, dessen Elternprozess weg ist,
    haelt seine paar hundert Megabyte weiter. Auf einem Container mit
    `mem_limit` bringt so einer beim naechsten Start die Flugsuche mit um.

    Ein Fehler beim Schliessen haelt das Herunterfahren nicht auf. Was hier
    schiefgeht, ist in einer Sekunde ohnehin Sache des Betriebssystems.
    """
    try:
        await close_shared_pools()
    except Exception as exc:  # noqa: BLE001 - der Prozess endet ohnehin
        logger.warning("hotels: Browser nicht sauber geschlossen (%s)", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    close_interrupted_runs()
    await purge_cache_at_start()
    await start_daily_scheduler()
    try:
        yield
    finally:
        # Erst den Planer anhalten, dann den Browser: umgekehrt koennte ein
        # Durchgang, der gerade laeuft, sich noch eine Seite auf einem
        # Browser holen, der schon geschlossen wird.
        try:
            await stop_daily_scheduler()
        finally:
            await close_hotel_browsers()


app = FastAPI(title="flightopt", docs_url="/api/docs", lifespan=lifespan)


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    try:
        authorised = basic_auth_valid(request.headers.get("authorization"))
    except AuthMisconfigured as exc:
        # Halb gesetzte Zugangsdaten sind ein Betriebsfehler, kein Absturz. Er
        # gehoert als 503 nach aussen und damit auch in den Healthcheck: ein
        # Container, durch den nichts durchkommt, darf nicht gesund aussehen.
        # Der Grund steht im Log, nicht in der Antwort - wer nicht angemeldet
        # ist, erfaehrt nur, dass der Dienst nicht bereit ist.
        logger.error("Basic-Auth unvollstaendig konfiguriert: %s", exc)
        if request.url.path == "/api/health":
            return JSONResponse({"ok": False}, status_code=503)
        return PlainTextResponse("Dienst nicht einsatzbereit", status_code=503)
    if request.url.path == "/api/health" or authorised:
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


class WatchRouteRequest(BaseModel):
    """Eine Strecke fuer die Beobachtungsliste.

    Kein Reisedatum, sondern ein Vorlauf: der Eintrag soll morgen dasselbe
    fragen wie heute, nur einen Tag weiter. Ein festes Datum hoerte nach dem
    Reisetag auf, Sinn zu ergeben.
    """

    origin: str = Field(min_length=2, max_length=60)
    destination: str = Field(min_length=2, max_length=60)
    lead_min_days: int = Field(default=watchlist.DEFAULT_LEAD_MIN, ge=0,
                               le=watchlist.MAX_LEAD_DAYS)
    lead_max_days: int = Field(default=watchlist.DEFAULT_LEAD_MAX, ge=0,
                               le=watchlist.MAX_LEAD_DAYS)
    currency: str = Field(default="EUR", min_length=3, max_length=3)

    def codes(self) -> tuple[str, str]:
        """Freitext zu Flughafencodes, wie im Suchformular.

        Gruppencodes bleiben draussen: eine Beobachtung ist genau eine
        Teilstrecke, und ihre Historie haengt an genau einem Schluessel.
        `BER|ATH` ist etwas anderes als "irgendein Berliner Flughafen nach
        irgendeinem griechischen".
        """
        return self._code(self.origin, "Start"), self._code(self.destination, "Ziel")

    @staticmethod
    def _code(value: str, field: str) -> str:
        code = airport_registry.resolve(value)
        if code is None:
            raise ValueError(f"{field}: {value!r} kenne ich nicht.")
        members = airport_registry.expand_code(code)
        if len(members) != 1:
            # Mit den Codes dabei: abweisen ohne Vorschlag laesst den Nutzer
            # raten, was er stattdessen tippen soll.
            raise ValueError(
                f"{field}: {value!r} ist eine Flughafengruppe fuer "
                f"{', '.join(members)}. Eine Beobachtung braucht einen "
                f"einzelnen Flughafen."
            )
        return code


class WatchSwitchRequest(BaseModel):
    """Der Schalter und der Takt einer Beobachtung.

    Beide Felder sind wahlfrei, damit die bestehende Form `{"enabled": false}`
    weiter gilt: die Oberflaeche schickt sie so, und ein Endpunkt, der ploetzlich
    zwei Felder verlangt, bricht sie ohne Not.
    """

    enabled: bool | None = None
    cadence: str | None = None


class FindSwitchRequest(BaseModel):
    """Einen Fund abhaken oder wieder aufmachen. Geloescht wird nichts."""

    acknowledged: bool


class HotelWatchRequest(BaseModel):
    """Eine Hotelsuche fuer die Beobachtungsliste.

    Dieselben Felder wie eine Hotelsuche, nur ohne Anreisetag: an seine Stelle
    tritt ein Vorlauf-Fenster, das jeden Tag mitwandert. Ein festes Datum
    hoerte nach dem Reisetag auf, Sinn zu ergeben - dieselbe Ueberlegung wie
    bei `WatchRouteRequest`.

    Die Grenzen stehen als `ge`/`le` am Feld und ausserdem noch einmal in
    `watch.add_watch`. Das ist keine Doppelung ohne Grund: die Grenzen hier
    liefern eine lesbare 422 mit Feldnamen, die dort gelten auch fuer den
    Befehlszeilenweg, der an keinem Pydantic-Modell vorbeikommt.
    """

    destination: str = Field(min_length=2, max_length=120)
    nights: int = Field(default=1, ge=1, le=30)
    adults: int = Field(default=2, ge=1, le=12)
    children: list[int] = Field(default_factory=list)
    rooms: int = Field(default=1, ge=1, le=8)
    stars: list[int] = Field(default_factory=list)
    min_review_score: float | None = Field(default=None, ge=0, le=10)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    country: str = Field(default="DE", min_length=2, max_length=2)
    lead_min_days: int = Field(default=hotel_watch.DEFAULT_LEAD_MIN, ge=0,
                               le=hotel_watch.MAX_LEAD_DAYS)
    lead_max_days: int = Field(default=hotel_watch.DEFAULT_LEAD_MAX, ge=0,
                               le=hotel_watch.MAX_LEAD_DAYS)


class HotelWatchSwitchRequest(BaseModel):
    """Der Schalter einer Hotelbeobachtung.

    Nur ein Feld, und es ist Pflicht. `WatchSwitchRequest` daneben laesst
    beide Felder weg, weil es zwei hat und die Oberflaeche nur eines schickt;
    hier waere ein weggelassenes `enabled` ein PATCH, der nichts tut - und
    das ist keine Antwort auf die Frage, sondern eine stille Nichtantwort.
    """

    enabled: bool


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
            "days_done, days_total, offers_found, retries, created_at, finished_at "
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
            # Wie oft eine Quelle fuer diesen Lauf nachgefragt werden musste.
            # Ohne diese Zahl sieht ein schleichend unzuverlaessiger Endpunkt
            # von aussen aus wie ein gesunder.
            "retries": scan["retries"],
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
                if done and not has_live_task(hotel_runner, scan_id):
                    # Wie bei der Flugsuche: laufend in der Datenbank, aber ohne
                    # Task, der noch etwas senden koennte.
                    logger.warning(
                        "Strom fuer Durchlauf %s: Status %s, aber kein Task in "
                        "diesem Prozess", scan_id, done["status"],
                    )
                    payload = {
                        "phase": "failed",
                        "message": db.INTERRUPTED,
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


def database_health() -> tuple[bool, str]:
    """Erreichbar und lesbar, oder nicht. Kein Netz, keine fremden Server.

    Der Healthcheck laeuft im Container alle paar Sekunden. Eine echte Abfrage
    bei einer Airline waere damit ein Dauerfeuer auf fremde Server und beim
    ersten Zeitablauf ausserdem ein falsches Alarmsignal. Was billig und
    trotzdem wahr ist: kommt die eigene Datenbank hoch und laesst sie sich
    lesen.
    """
    try:
        conn = runner._conn()
    except Exception as exc:  # noqa: BLE001 - ein toter Healthcheck hilft niemandem
        logger.error("health: Datenbank nicht erreichbar (%s)", exc)
        return False, type(exc).__name__
    try:
        conn.execute("SELECT 1 FROM price_observation LIMIT 1").fetchone()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        logger.error("health: Datenbank nicht lesbar (%s)", exc)
        return False, type(exc).__name__
    finally:
        conn.close()


def source_freshness(
    names: list[str],
    last_seen: dict[str, str],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Je Quelle: wann sie zuletzt geschrieben hat und wie lange das her ist.

    `silent_days` ist None, wenn eine Quelle noch nie etwas geschrieben hat -
    das ist etwas anderes als "vor null Tagen" und darf nicht danach aussehen.
    """
    moment = now or datetime.now()
    rows: list[dict[str, Any]] = []
    for name in names:
        stamp = last_seen.get(name)
        days: int | None = None
        if stamp:
            try:
                days = max(0, (moment - db.parse_dt(stamp)).days)
            except ValueError:
                # Ein unlesbarer Zeitstempel ist kein Grund, den ganzen Bericht
                # fallen zu lassen; er zaehlt wie "noch nie geschrieben".
                logger.warning("health: unlesbares observed_at fuer %s: %r", name, stamp)
                stamp = None
        rows.append(
            {
                "name": name,
                "last_observation": stamp,
                "silent_days": days,
                "silent": days is None or timedelta(days=days) >= SOURCE_SILENT_AFTER,
            }
        )
    return rows


def hunt_report(conn: Any, source_names: list[str] | None = None, *,
                watch: dict[str, Any] | None = None) -> dict[str, Any]:
    """Der Stand der Jagd fuer `/api/health/detail`.

    Vier Fragen, die man sonst nur aus dem Log beantworten koennte: In welchem
    Takt laeuft sie wirklich? Wie viel Budget ist bei welcher Quelle noch da?
    Sperrt jemand? Und darf der Kanal ueberhaupt reden?

    `effective_interval_seconds` ist die wichtigste davon und die einzige, die
    gerechnet wird: die Jagd laeuft nur so oft, wie der Tagesplaner sie
    aufruft. Steht dessen Intervall hoeher als der heisse Takt, ist der heisse
    Takt eine Absichtserklaerung und keine Tatsache.
    """
    report: dict[str, Any] = {
        "interval_seconds": cadence.HOT_INTERVAL_SECONDS,
        "effective_interval_seconds": max(
            cadence.HOT_INTERVAL_SECONDS, daily_scheduler.interval_seconds
        ),
        "jitter_seconds": cadence.HOT_JITTER_SECONDS,
        "calls_per_pass": cadence.CALLS_PER_PASS,
        "cap_per_source_hour": cadence.MAX_CALLS_PER_SOURCE_HOUR,
        "max_hot_routes": cadence.MAX_HOT_ROUTES,
        "max_cache_age_seconds": int(cadence.MAX_CACHE_AGE.total_seconds()),
        "calendar_ttl_seconds": int(TTL_CALENDAR.total_seconds()),
        "hot_routes": int((watch or {}).get("hot", 0)),
        "hot_due": int((watch or {}).get("hot_due", 0)),
        "budget": [],
        "paused": [],
        "alerts": {},
        "channel": {
            "kind": alerts.CHANNEL_DISCORD,
            "configured": discord.configured(),
            "env": discord.ENV_WEBHOOK,
        },
    }
    if conn is None:
        return report
    report["budget"] = budget.report(conn, source_names or [])
    report["paused"] = budget.paused_sources(conn)
    report["alerts"] = alerts.summary(conn)
    return report


def hotel_report(conn: Any) -> dict[str, Any]:
    """Der Stand der Hotelbeobachtung fuer `/api/health/detail`.

    Zugeschnitten wie `hunt_report`: erst was gilt, dann was gerade ist. Die
    Zahlen, die gelten, stehen auch dann da, wenn die Datenbank nicht
    erreichbar ist - sonst sieht ein kaputter Betrieb genauso aus wie ein
    Betrieb ohne Beobachtungen.

    `interval_seconds` ist die wichtigste Zahl davon. Die Hotelschleife laeuft
    nur so oft, wie der Tagesplaner sie aufruft, und je Durchgang hoechstens
    `max_per_run` Beobachtungen. Aus beidem folgt, wie lange eine volle Runde
    ueber alle Beobachtungen wirklich dauert - im Log steht das nirgends.

    `baseline_splits_populations` gehoert dazu, weil davon abhaengt, wie stark
    ein `error` ueberhaupt ist: gegen eine gemischte Vergleichsgruppe
    gerechnet ist er eine schwaechere Aussage als gegen Haendlerpreise.
    """
    budget_now = budget_from_env()
    report: dict[str, Any] = {
        "interval_seconds": daily_scheduler.interval_seconds,
        "max_per_run": hotel_watch.MAX_WATCHES_PER_RUN,
        "min_days": hotel_watch.MIN_RECORDING_DAYS,
        "lead_days": [hotel_watch.DEFAULT_LEAD_MIN, hotel_watch.DEFAULT_LEAD_MAX],
        "max_window_days": hotel_watch.MAX_WINDOW_DAYS,
        "baseline_splits_populations": HOTEL_BASELINE_SPLITS_POPULATIONS,
        "browser": {
            "max_pages": budget_now.max_pages,
            "max_age_seconds": budget_now.max_age_seconds,
            "idle_seconds": budget_now.idle_seconds,
        },
        "watches": 0,
        "active": 0,
        "due": 0,
        "observations": 0,
        "days_recorded": 0,
        "last_run_at": None,
    }
    if conn is None:
        return report
    summary = hotel_watch.watch_report(conn)
    rows = summary["rows"]
    runs = [row["last_run_at"] for row in rows if row["last_run_at"]]
    report.update(
        {
            "watches": summary["watches"],
            "due": summary["due"],
            "active": sum(1 for row in rows if row["enabled"]),
            "last_run_at": max(runs) if runs else None,
            **hotel_history_size(conn),
        }
    )
    return report


def hotel_history_size(conn: Any) -> dict[str, int]:
    """Wie viel Hotelhistorie es gibt, gezaehlt an der Tabelle.

    Nicht ueber die Beobachtungen aufsummiert, und das ist kein Geschmack:
    eine Zeile in `price_observation` traegt kein Ziel. `watch_stats` findet
    ihre Zeilen deshalb ueber Naechte und Belegung, und zwei Suchen mit
    demselben Zuschnitt sehen beide dieselben. Wer die Zahlen der einzelnen
    Suchen addiert, zaehlt jede Beobachtung so oft, wie es Suchen gibt - bei
    zehn Beobachtungslisten desselben Zuschnitts das Zehnfache.

    `days_recorded` steht daneben, weil es die Frage beantwortet, um die es
    wirklich geht: eine Baseline braucht fuenf Beobachtungen je Gruppe, und
    Gruppen fuellen sich ueber Kalendertage. Zweitausend Beobachtungen aus
    einem einzigen Tag sind keine Historie.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS observations, "
        "COUNT(DISTINCT substr(observed_at, 1, 10)) AS days "
        "FROM price_observation WHERE entity_type='hotel'"
    ).fetchone()
    return {
        "observations": int(row["observations"] or 0),
        "days_recorded": int(row["days"] or 0),
    }


@app.get("/api/health")
async def health() -> JSONResponse:
    """Ein Urteil nach aussen, sonst nichts.

    Der Endpunkt bleibt absichtlich ohne Basic Auth erreichbar, weil der
    Container-Healthcheck ihn braucht. Deshalb steht hier nichts, was ein
    Fremder nicht sehen soll: keine Quellennamen, keine Pfade, keine
    Zaehlerstaende, aus denen sich Nutzung ablesen liesse. Die ausfuehrliche
    Auskunft steht unter `/api/health/detail` und will Zugangsdaten sehen.

    Vorher stand hier `airline_registry.LIVE`, eine feste Liste von
    IATA-Codes. Sie prueft nichts: ein Adapter, der seit Wochen 403 bekommt,
    stand weiter als "live" darin, und gruen war der Container, solange der
    Prozess ueberhaupt antwortete.
    """
    reachable, _ = database_health()
    if not reachable:
        # Ohne Datenbank kann der Dienst weder suchen noch speichern. Das
        # gehoert als 503 nach aussen, sonst haelt Docker ihn fuer gesund.
        return JSONResponse({"ok": False}, status_code=503)
    return JSONResponse({"ok": True})


@app.get("/api/health/detail")
async def health_detail() -> dict[str, Any]:
    """Die ausfuehrliche Auskunft, hinter Basic Auth.

    Der Quellen-Katalog kommt aus `build_sources` und `source_report`, also aus
    derselben Liste, die ein Suchlauf benutzt. Eine zweite, handgepflegte Liste
    waere genau die Luege, die dieser Endpunkt vorher erzaehlt hat.
    """
    reachable, failure = database_health()
    catalogue: list[dict[str, Any]] = []
    last_seen: dict[str, str] = {}
    unknown: list[str] = []
    # Ohne Aufzeichnung schweigt jede Quelle zu Recht. Die Zahl gehoert deshalb
    # neben die Stillstandstage und nicht in einen zweiten Bericht daneben.
    watch: dict[str, Any] = {}
    hunt = hunt_report(None)
    hotel_watches = hotel_report(None)
    if reachable:
        conn = runner._conn()
        sources: list[Any] = []
        try:
            last_seen = last_observation_by_source(conn)
            watch = watchlist.watchlist_report(conn)
            sources = build_sources(None, conn=conn, env=os.environ)
            catalogue = [
                {"name": source.name, "carriers": list(source.carriers)}
                for source in sources
            ]
            hunt = hunt_report(conn, [source.name for source in sources], watch=watch)
            hotel_watches = hotel_report(conn)
        finally:
            # Der Katalog wird nur gelesen, nicht befragt - trotzdem gehoert
            # das Schliessen in den finally-Zweig: sonst behaelt ein Fehler
            # beim Lesen die Sitzungen der bereits gebauten Quellen.
            for source in sources:
                source.close()
            conn.close()

    flight = source_freshness([entry["name"] for entry in catalogue], last_seen)
    for row, entry in zip(flight, catalogue):
        row["carriers"] = entry["carriers"]

    hotels = source_report()
    hotel_rows = source_freshness([entry["name"] for entry in hotels], last_seen)
    for row, entry in zip(hotel_rows, hotels):
        row["active"] = entry["active"]
        row["reason"] = entry["reason"]

    known = {entry["name"] for entry in catalogue} | {e["name"] for e in hotels}
    # Eine Quelle, die geschrieben hat und nicht mehr im Katalog steht, ist
    # entweder umbenannt oder entfallen. Beides verschwindet sonst lautlos.
    unknown = sorted(name for name in last_seen if name not in known)

    task = daily_scheduler._task
    return {
        "ok": reachable,
        "database": {"reachable": reachable, "error": failure or None},
        "scheduler": {
            "running": task is not None and not task.done(),
            "autostart": scheduler_autostart_enabled(),
            "interval_seconds": daily_scheduler.interval_seconds,
        },
        "auth": {"required": basic_auth_config() is not None},
        "silent_after_days": SOURCE_SILENT_AFTER.days,
        "sources": {"flight": flight, "hotel": hotel_rows},
        "watchlist": watch,
        "hunt": hunt,
        # Eigener Block neben `hunt`, nicht unter `sources.hotel`: dort steht,
        # welche Quelle wann zuletzt geschrieben hat, hier steht, ob und wie
        # oft ueberhaupt jemand fragt. Zwei verschiedene Fragen.
        "hotels": hotel_watches,
        "unknown_writers": unknown,
    }


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
    asked = [
        source
        for source in build_sources(None, env=os.environ)
        if hasattr(source, "load_routes")
    ]
    results = await asyncio.gather(
        *(source.load_routes(code) for source in asked), return_exceptions=True
    )
    destinations: set[str] = set()
    failed: list[str] = []
    for source, result in zip(asked, results):
        if isinstance(result, BaseException):
            # Mit Namen: welche Quelle ausfiel, stand vorher in keinem Log.
            logger.warning("routes: %s scheiterte fuer %s: %s", source.name, code, result)
            failed.append(source.name)
            continue
        destinations |= set(result)
    if not asked or len(failed) == len(asked):
        # "Kein Flughafen bedient" und "nichts hat geantwortet" sind zwei
        # Aussagen. Eine leere Liste mit 200 war genau diese Verwechslung: wer
        # sie las, hielt einen Flughafen fuer unbedient, weil das Netz stand.
        raise HTTPException(
            status_code=503,
            detail=(
                f"Kein Streckennetz fuer {code}: "
                + (f"{', '.join(failed)} scheiterten" if failed
                   else "keine Quelle fuehrt eines")
            ),
        )
    return {
        "origin": code,
        "destinations": sorted(destinations),
        # Eine Teilauskunft soll als solche erkennbar bleiben.
        "sources": {"asked": len(asked), "failed": failed},
    }


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


@app.get("/api/watchlist")
async def watchlist_endpoint() -> dict[str, Any]:
    """Die Beobachtungsliste samt Ertrag je Strecke.

    Die Zusammenfassung steht auch dann da, wenn die Liste leer ist: null
    Strecken ist eine Aussage, und sie sieht sonst genauso aus wie eine
    Aufzeichnung, die nichts findet.
    """
    conn = runner._conn()
    try:
        return {
            "routes": watchlist.routes_with_stats(conn),
            "summary": watchlist.watchlist_report(conn),
        }
    finally:
        conn.close()


@app.post("/api/watchlist")
async def add_watch_route(req: WatchRouteRequest) -> dict[str, Any]:
    conn = runner._conn()
    try:
        origin, destination = req.codes()
        route_id = watchlist.add_route(
            conn, origin, destination,
            lead_min_days=req.lead_min_days,
            lead_max_days=req.lead_max_days,
            currency=req.currency,
        )
        conn.commit()
        route = watchlist.get_route(conn, route_id)
        assert route is not None  # gerade geschrieben
        return {**route.as_dict(), **watchlist.route_stats(conn, route)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@app.patch("/api/watchlist/{route_id}")
async def switch_watch_route(route_id: int, req: WatchSwitchRequest) -> dict[str, Any]:
    """Der Schalter und der Takt. Abgeschaltet heisst nicht geloescht.

    Zwei Fehlerarten, zwei Antworten: eine unbekannte Strecke ist ein 404,
    ein abgelehnter Takt ein 400. Beides als 404 zu melden hiesse, dem
    Aufrufer zu sagen, seine Strecke gebe es nicht, obwohl sie nur ein zu
    breites Fenster hat.
    """
    conn = runner._conn()
    try:
        route = watchlist.get_route(conn, route_id)
        if route is None:
            raise HTTPException(status_code=404, detail=f"Unbekannte Strecke: {route_id}")
        if req.enabled is not None:
            route = watchlist.set_enabled(conn, route_id, req.enabled)
        if req.cadence is not None:
            try:
                route = watchlist.set_cadence(conn, route_id, req.cadence)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.commit()
        return {**route.as_dict(), **watchlist.route_stats(conn, route)}
    finally:
        conn.close()


def hotel_watch_row(conn: Any, watch_id: int,
                    *, now: datetime | None = None) -> dict[str, Any] | None:
    """Eine einzelne Zeile, im Zuschnitt der Liste.

    Gebaut wird sie ueber `watch_report` und nicht daneben. Eine zweite
    Zusammenstellung waere eine zweite Form derselben Sache: die Oberflaeche
    bekaeme aus `POST` etwas anderes als aus `GET` und muesste beides lesen.
    Die Abfragen je Zeile kosten hier nichts - es sind wenige Beobachtungen,
    und mehr als drei laufen ohnehin nicht je Durchgang.
    """
    report = hotel_watch.watch_report(conn, now=now)
    return next((row for row in report["rows"] if row["id"] == watch_id), None)


@app.get("/api/hotels/watchlist")
async def hotel_watchlist_endpoint() -> dict[str, Any]:
    """Die Hotelbeobachtungen samt Stand je Suche.

    Antwortform: `{"watches": [...], "summary": {...}}`, wie `/api/watchlist`
    sie fuer Strecken liefert. Eine Zeile traegt die Suche selbst
    (`destination`, `nights`, `adults`, `children`, `rooms`, `stars`,
    `min_review_score`, `currency`, `country`, `lead_min_days`,
    `lead_max_days`, `enabled`, `created_at`, `last_run_at`), dazu den Stand
    der Aufzeichnung (`observations`, `days_recorded`, `last_observation`,
    `min_days`, `ready`) und was der naechste Durchgang taete (`due`,
    `window`, `days_per_run`).

    Die Zusammenfassung steht auch bei leerer Liste da: null Beobachtungen ist
    eine Aussage und sieht sonst genauso aus wie eine Beobachtung ohne Funde.
    """
    conn = runner._conn()
    try:
        summary = hotel_watch.watch_report(conn)
        return {"watches": summary.pop("rows"), "summary": summary}
    finally:
        conn.close()


@app.post("/api/hotels/watchlist")
async def add_hotel_watch(req: HotelWatchRequest) -> dict[str, Any]:
    """Eine Beobachtung anlegen oder ihr Fenster verschieben.

    Zweimal dieselbe Frage bleibt eine Zeile: der Fingerabdruck haelt sie
    eindeutig, und das Vorlauf-Fenster gehoert ausdruecklich nicht dazu. Wer
    das Fenster aendert, verschiebt dieselbe Beobachtung und legt keine zweite
    an - sonst liefen zweimal dieselben Abrufe am selben Tag.
    """
    conn = runner._conn()
    try:
        watch_id = hotel_watch.add_watch(
            conn,
            req.destination,
            nights=req.nights,
            adults=req.adults,
            children=req.children,
            rooms=req.rooms,
            stars=req.stars,
            min_review_score=req.min_review_score,
            currency=req.currency,
            country=req.country,
            lead_min_days=req.lead_min_days,
            lead_max_days=req.lead_max_days,
        )
        conn.commit()
        row = hotel_watch_row(conn, watch_id)
        assert row is not None  # gerade geschrieben
        return row
    except ValueError as exc:
        # Ein zu breites oder rueckwaerts laufendes Fenster ist eine
        # abgelehnte Eingabe und kein Programmfehler.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@app.patch("/api/hotels/watchlist/{watch_id}")
async def switch_hotel_watch(watch_id: int,
                             req: HotelWatchSwitchRequest) -> dict[str, Any]:
    """Der Schalter. Abgeschaltet heisst nicht geloescht.

    Die Zeile bleibt stehen und ihre Historie auch: eine Beobachtung, die
    Wochen gebraucht hat, um fuenf Punkte je Gruppe zu sammeln, darf ein
    Klick nicht wegwerfen.
    """
    conn = runner._conn()
    try:
        if hotel_watch.get_watch(conn, watch_id) is None:
            raise HTTPException(
                status_code=404, detail=f"Unbekannte Beobachtung: {watch_id}"
            )
        hotel_watch.set_enabled(conn, watch_id, req.enabled)
        conn.commit()
        row = hotel_watch_row(conn, watch_id)
        assert row is not None  # eben noch gelesen
        return row
    finally:
        conn.close()


@app.post("/api/hotels/watchlist/run-once")
async def run_hotel_watchlist_endpoint() -> dict[str, Any]:
    """Jetzt beobachten, statt bis zum naechsten Durchgang zu warten.

    Antwortform wie ein Durchgang des Planers: `watches`, `days`,
    `observations`, `errors`, `due_left`.

    Faellig bleibt faellig: eine Beobachtung, die heute schon gelaufen ist,
    wird auch hier nicht ein zweites Mal abgerufen. Und der Deckel von drei
    Beobachtungen je Durchgang gilt genauso - ein Knopf, der die Obergrenze
    umgeht, waere keine Obergrenze.
    """
    return await daily_scheduler.hotels_once()


@app.get("/api/hunt/finds")
async def hunt_finds(limit: int = 50, open_only: bool = False,
                     tier: str | None = None) -> dict[str, Any]:
    """Die juengsten Funde samt Stand des Kanals.

    Antwortform: `{"finds": [...], "summary": {...}}`. Eine Zeile in `finds`
    traegt `id`, `created_at`, `route`, `entity_key`, `travel_date`, `tier`,
    `source`, `currency`, `price`, `price_minor`, `median`, `n`,
    `population`, `reason`, `delivery`, `delivered_at`, `error`,
    `acknowledged_at`, `booking_url`, `distance_km` und `thin`.

    `summary` sagt, was der Kanal getan hat und ob er ueberhaupt reden darf -
    ohne diese Zahl sieht ein Trockenlauf genauso aus wie ein kaputter
    Webhook.
    """
    conn = runner._conn()
    try:
        return {
            "finds": alerts.list_events(
                conn, limit=max(1, min(200, limit)), open_only=open_only, tier=tier
            ),
            "summary": alerts.summary(conn),
        }
    finally:
        conn.close()


@app.patch("/api/hunt/finds/{event_id}")
async def switch_hunt_find(event_id: int, req: FindSwitchRequest) -> dict[str, Any]:
    """Einen Fund abhaken. Die Zeile bleibt stehen, samt Begruendung."""
    conn = runner._conn()
    try:
        event = alerts.acknowledge(conn, event_id, req.acknowledged)
        conn.commit()
        return event
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@app.get("/api/hunt/history/{origin}/{destination}")
async def hunt_history(origin: str, destination: str, days: int = hunt_history_mod.DEFAULT_DAYS,
                       by: str = hunt_history_mod.BY_OBSERVED,
                       currency: str = "EUR") -> dict[str, Any]:
    """Der Preisverlauf einer Strecke, verdichtet auf eine Kurve.

    Antwortform: `route`, `entity_key`, `by`, `days`, `currency`, `points`,
    `cadence`, `hot`, `stats` und `finds`. Ein Punkt traegt `day`, `min`,
    `median`, `max` und `n`; die Betraege stehen in ganzen Waehrungseinheiten,
    weil eine Kurve sie so zeichnet.

    `cadence` und `stats` sind `null`, wenn die Strecke gar nicht in der
    Beobachtungsliste steht. Die Historie gibt es trotzdem: sie entsteht auch
    aus gewoehnlichen Suchen.
    """
    try:
        # Dieselbe Aufloesung wie beim Eintragen einer Beobachtung: "Berlin"
        # und "BER" sollen zur selben Kurve fuehren, und eine Flughafengruppe
        # hat keine, weil ihre Historie an keinem Schluessel haengt.
        codes = (
            WatchRouteRequest._code(origin, "Start"),
            WatchRouteRequest._code(destination, "Ziel"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    entity_key = f"{codes[0]}|{codes[1]}"
    conn = runner._conn()
    try:
        try:
            points = hunt_history_mod.route_series(
                conn, entity_key, days=days, by=by, currency=currency.upper()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        route = next(
            (
                candidate
                for candidate in watchlist.list_routes(conn)
                if candidate.entity_key == entity_key
            ),
            None,
        )
        return {
            "route": f"{codes[0]}-{codes[1]}",
            "entity_key": entity_key,
            "by": by,
            "days": days,
            "currency": currency.upper(),
            "points": points,
            "cadence": route.cadence if route else None,
            "hot": route.hot if route else None,
            "stats": watchlist.route_stats(conn, route) if route else None,
            "finds": [
                find for find in alerts.list_events(conn, limit=10)
                if find["entity_key"] == entity_key
            ],
        }
    finally:
        conn.close()


@app.post("/api/hunt/run-once")
async def hunt_run_once() -> dict[str, Any]:
    """Jetzt jagen, statt bis zum naechsten Durchgang zu warten.

    Faellig bleibt faellig: eine Strecke, deren letzter heisser Durchgang
    keine zwanzig Minuten her ist, wird auch hier nicht erneut abgerufen. Und
    das Stundenbudget gilt genauso - ein Knopf, der die Obergrenze umgeht,
    waere keine Obergrenze.
    """
    return await daily_scheduler.hunt_once()


@app.post("/api/watchlist/run-once")
async def run_watchlist_endpoint() -> dict[str, Any]:
    """Jetzt aufzeichnen, statt bis zum naechsten Durchgang zu warten.

    Faellig bleibt faellig: eine Strecke, die heute schon gelaufen ist, wird
    auch hier nicht ein zweites Mal abgerufen.
    """
    return await daily_scheduler.collect_once()


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
                if done and not has_live_task(runner, job_id):
                    # Steht auf laufend, wird aber von niemandem gefuehrt. Der
                    # Start raeumt solche Zeilen auf; wer den Strom davor oeffnet,
                    # soll trotzdem eine Antwort bekommen statt Keepalives.
                    logger.warning(
                        "Strom fuer Job %s: Status %s, aber kein Task in diesem "
                        "Prozess", job_id, done["status"],
                    )
                    payload = {
                        "phase": "failed",
                        "message": db.INTERRUPTED,
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
