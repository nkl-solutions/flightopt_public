"""Der Zeitraum-Durchlauf: ein Fenster Tage je Quelle, jede Zeile dokumentiert.

Keine der Quellen kennt einen Tageskalender, also liegt die Tagesschleife hier
und nicht im Adapter. Gefragt wird aber nicht mehr Tag fuer Tag: jede Quelle
bekommt ein Fenster von Anreisetagen auf einmal und faechert es in ihrem
eigenen Takt auf (`HotelSource.run_many`). Das Fenster ist so breit wie die
breiteste Nebenlaeufigkeit im Katalog, also ein Tag bei Trivago und zwei bei
Booking. Eine Quelle mit einem Platz laeuft damit weiterhin streng
nacheinander.

Was der Faecher nicht veraendern darf, steht weiter unten im Code, hier die
Kurzfassung: der Fortschritt kommt weiterhin je fertigem Tag und in
Datumsreihenfolge, ein Abbruch wirkt mitten im Fenster und nicht erst danach,
und ein Tag, der wegen Abbruch nie gestartet ist, wird als Fehlversuch
verbucht und nicht als leeres Ergebnis.

Vom Vorlaeufer-Projekt uebernommen sind drei Muster, und nur diese drei:

* **Wiederaufnahme.** `hotel_scan.current_day` haelt den letzten fertigen Tag.
  Ein Lauf, der abbricht, faengt dort an und nicht von vorn.
* **Dedup.** Dieselbe Kombination aus Quelle, Ziel, Belegung, Filter und Datum
  wird nicht zweimal am selben Tag geholt. Das Ergebnis liegt im vorhandenen
  `price_cache`, also kann ein wiederaufgenommener Lauf die Zeilen trotzdem
  zeigen, ohne die Quelle noch einmal zu fragen.
* **Abbruch nach Fehlerserie.** Fuenf Fehler hintereinander beenden den Lauf.

Bewusst nicht uebernommen ist die feste Preisschwelle je Sternekategorie. Wir
schreiben jede Beobachtung, also tragen Median und MAD.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Iterator, Sequence

from flightopt.domain.fx import Rates
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.sources.base import HotelBatch, HotelSource, retries_of
from flightopt.hotels.store import record_offers
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache

logger = logging.getLogger(__name__)

TTL_HOTEL_DAY = timedelta(hours=24)
MAX_ERRORS = 5


@dataclass(slots=True)
class ScanProgress:
    scan_id: int
    day: date
    days_done: int
    days_total: int
    offers: list[HotelOffer] = field(default_factory=list)
    message: str = ""
    status: str = "running"
    retries: int = 0
    """Nachfassen an genau diesem Tag, ueber alle Quellen zusammen."""


@dataclass(slots=True)
class ScanResult:
    scan_id: int
    status: str
    days_done: int
    days_total: int
    offers: list[HotelOffer] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error: str | None = None
    retries: int = 0
    """Nachfassen ueber den ganzen Lauf, auch fuer am Ende verlorene Tage.

    Ein Lauf, der nur mit Wiederholungen gruen wurde, sieht sonst genauso aus
    wie einer, der es auf Anhieb war. Das ist der Unterschied zwischen "die
    Quelle laeuft" und "die Quelle laeuft noch".
    """


ProgressHook = Callable[[ScanProgress], Any | Awaitable[Any]]


def day_cache_key(source: str, query: HotelQuery) -> str:
    """Alles, was die Antwort veraendern kann, in einen Schluessel.

    Ziel, Datum, Naechte, Belegung, Zimmer, Sterne, Bewertung und Waehrung: wer
    einen davon weglaesst, haelt die Antwort auf eine andere Frage fuer einen
    Treffer.
    """
    raw = "|".join(
        [
            "hotel",
            source,
            query.destination.casefold(),
            query.arrival.isoformat(),
            str(query.nights),
            str(query.adults),
            ",".join(str(age) for age in query.children),
            str(query.rooms),
            ",".join(str(star) for star in sorted(query.stars)),
            str(query.min_review_score),
            query.currency.upper(),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def scan_days(
    window_start: date, window_end: date, *, resume_after: date | None = None
) -> list[date]:
    """Die Anreisetage des Fensters, aufsteigend.

    `resume_after` ist der letzte fertige Tag; er selbst wird nicht wiederholt.
    """
    if window_end < window_start:
        raise ValueError("Fensterende liegt vor dem Anfang")
    start = window_start
    if resume_after is not None and resume_after >= window_start:
        start = resume_after + timedelta(days=1)
    days: list[date] = []
    current = start
    while current <= window_end:
        days.append(current)
        current += timedelta(days=1)
    return days


def create_scan(
    conn: sqlite3.Connection,
    query: HotelQuery,
    *,
    window_start: date,
    window_end: date,
    now: str | None = None,
) -> int:
    stamp = now or db.now()
    filters = json.dumps(
        {
            "stars": sorted(query.stars),
            "min_review_score": query.min_review_score,
            "currency": query.currency.upper(),
            "country": query.country.upper(),
            "children_ages": list(query.children),
        },
        ensure_ascii=False,
    )
    cursor = conn.execute(
        "INSERT INTO hotel_scan("
        "destination, window_start, window_end, nights, adults, children, rooms, "
        "filters, status, days_total, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            query.destination,
            window_start.isoformat(),
            window_end.isoformat(),
            query.nights,
            query.adults,
            len(query.children),
            query.rooms,
            filters,
            "pending",
            len(scan_days(window_start, window_end)),
            stamp,
            stamp,
        ),
    )
    return int(cursor.lastrowid)


def load_scan(conn: sqlite3.Connection, scan_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM hotel_scan WHERE id=?", (scan_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        data["filters"] = json.loads(data.get("filters") or "{}")
    except json.JSONDecodeError:
        data["filters"] = {}
    return data


def _resume_after(conn: sqlite3.Connection, scan_id: int) -> date | None:
    row = conn.execute(
        "SELECT current_day FROM hotel_scan WHERE id=?", (scan_id,)
    ).fetchone()
    if row is None or not row["current_day"]:
        return None
    try:
        return date.fromisoformat(str(row["current_day"]))
    except ValueError:
        return None


def _mark(
    conn: sqlite3.Connection,
    scan_id: int,
    *,
    status: str | None = None,
    current_day: date | None = None,
    days_done: int | None = None,
    offers_found: int | None = None,
    retries: int | None = None,
    error: str | None = None,
    finished: bool = False,
) -> None:
    sets = ["updated_at=?"]
    values: list[Any] = [db.now()]
    if status is not None:
        sets.append("status=?")
        values.append(status)
    if current_day is not None:
        sets.append("current_day=?")
        values.append(current_day.isoformat())
    if days_done is not None:
        sets.append("days_done=?")
        values.append(days_done)
    if offers_found is not None:
        sets.append("offers_found=?")
        values.append(offers_found)
    if retries is not None:
        sets.append("retries=?")
        values.append(retries)
    if error is not None:
        sets.append("error=?")
        values.append(error)
    if finished:
        sets.append("finished_at=?")
        values.append(db.now())
    elif status == "running":
        # Ein Lauf, der wieder laeuft, ist nicht fertig. Bliebe der
        # Endzeitpunkt des ersten Anlaufs stehen - `cancel` und die
        # Fehlerwege nehmen ihn per COALESCE in Schutz -, dann grenzte
        # `stored_rows` die Beobachtungen darauf ein und verwuerfe jede Zeile
        # des zweiten Anlaufs: beobachtet nach dem "Ende".
        sets.append("finished_at=NULL")
    values.append(scan_id)
    conn.execute(f"UPDATE hotel_scan SET {', '.join(sets)} WHERE id=?", tuple(values))


async def _notify(hook: ProgressHook | None, progress: ScanProgress) -> None:
    if hook is None:
        return
    outcome = hook(progress)
    if inspect.isawaitable(outcome):
        await outcome


async def _cached(
    source: HotelSource, query: HotelQuery, cache: SqliteCache | None
) -> HotelBatch | None:
    """Was heute schon geholt wurde, sonst nichts.

    Getrennt vom Abruf, weil ein Treffer gar nicht erst in den Faecher darf:
    ein Platz im Fenster, der einen Cache-Eintrag aus der Datei liest, haelt
    einen Tag auf, der wirklich ins Netz muesste.
    """
    if cache is None:
        return None
    stored = await cache.get(day_cache_key(source.name, query))
    if stored is None:
        return None
    return HotelBatch(offers=[HotelOffer.from_dict(raw) for raw in stored])


async def _fetch(
    source: HotelSource, query: HotelQuery, cache: SqliteCache | None
) -> HotelBatch:
    """Ein Anreisetag bei einer Quelle, frisch, und danach im Cache."""
    batch = await source.search(query)
    if cache is not None:
        await cache.put(
            day_cache_key(source.name, query),
            [offer.as_dict() for offer in batch.offers],
            TTL_HOTEL_DAY,
            source=source.name,
        )
    return batch


def fan_width(sources: Sequence[HotelSource]) -> int:
    """Wie viele Anreisetage ein Fenster fasst.

    Die breiteste Quelle gibt das Mass vor: ein Fenster, das schmaler ist als
    ihre Nebenlaeufigkeit, laesst ihre Plaetze leer stehen. Fest verdrahtet
    ist hier nichts, damit eine Quelle mit genau einem Platz (Trivago) auf ein
    Fenster von einem Tag fuehrt und sich damit gar nichts aendert.
    """
    return max(
        [1] + [int(getattr(source.limiter, "concurrency", 1) or 1) for source in sources]
    )


def _windows(days: Sequence[date], width: int) -> Iterator[list[date]]:
    step = max(1, width)
    for start in range(0, len(days), step):
        yield list(days[start : start + step])


@dataclass(slots=True)
class _DaySlot:
    """Was ein Anreisetag von den Quellen zurueckbekommen hat.

    Der Faecher liefert die Tage in beliebiger Reihenfolge, die Tabelle und der
    Fortschrittsbalken brauchen sie nach Datum. Also sammelt je Tag ein Fach
    die Antworten, und `ready` sagt, wann alle Quellen dieses Tages durch sind.
    """

    day: date
    pending: int
    batch: HotelBatch = field(default_factory=HotelBatch)
    failures: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    retries: int = 0
    """Nachfassen aller Quellen an diesem Tag, auch das vergebliche."""
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def _tick(self) -> None:
        self.pending -= 1
        if self.pending <= 0:
            self.ready.set()

    def keep(self, batch: HotelBatch) -> None:
        self.retries += batch.retries
        self.batch.extend(batch)
        self._tick()

    def from_cache(self, source: str, batch: HotelBatch) -> None:
        self.skipped.append(f"{self.day}: {source} heute schon geholt")
        self.keep(batch)

    def fail(self, source: str, reason: str, *, retries: int = 0) -> None:
        self.failures += 1
        self.retries += retries
        self.errors.append(f"{self.day}: {source}: {reason}")
        logger.warning("hotels: %s scheiterte am %s: %s", source, self.day, reason)
        self._tick()


async def _serve(
    source: HotelSource,
    queries: Sequence[HotelQuery],
    slots: dict[date, _DaySlot],
    cache: SqliteCache | None,
    served: set[date],
    *,
    max_errors: int,
) -> None:
    """Ein Fenster Anreisetage fuer genau eine Quelle.

    Der eigene `runner` traegt jede Antwort sofort in ihr Fach ein, statt auf
    das Ende des Faechers zu warten: nur so kann der Aufrufer den ersten Tag
    melden, waehrend der zweite noch laeuft. Der Rueckgabewert von `run_many`
    wird trotzdem gelesen, denn Tage, die wegen Abbruch nie gestartet sind,
    haben den `runner` nie gesehen und wuerden ihr Fach sonst nie freigeben.
    """
    todo: list[HotelQuery] = []
    for query in queries:
        stored = await _cached(source, query, cache)
        if stored is None:
            todo.append(query)
        else:
            served.add(query.arrival)
            slots[query.arrival].from_cache(source.name, stored)
    if not todo:
        return

    async def runner(query: HotelQuery) -> HotelBatch:
        try:
            batch = await _fetch(source, query, cache)
        except Exception as exc:  # noqa: BLE001 - `run_many` fuehrt Buch, wir melden
            served.add(query.arrival)
            # Auch ein am Ende verlorener Tag hat etwas gekostet. Was, haengt
            # an der Ausnahme - sonst faellt das Nachfassen genau dort unter
            # den Tisch, wo es am meisten ueber die Quelle aussagt.
            slots[query.arrival].fail(source.name, str(exc), retries=retries_of(exc))
            raise
        served.add(query.arrival)
        slots[query.arrival].keep(batch)
        return batch

    for outcome in await source.run_many(todo, runner, max_errors=max_errors):
        if outcome.arrival in served:
            continue
        # Uebrig bleibt nur, was nie gestartet ist. Ein abgebrochener Tag ist
        # ein Fehlversuch und kein leeres Ergebnis.
        served.add(outcome.arrival)
        slots[outcome.arrival].fail(
            source.name,
            outcome.error or f"Tag {outcome.status}",
            retries=outcome.retries,
        )


async def _fan_source(
    source: HotelSource,
    queries: Sequence[HotelQuery],
    slots: dict[date, _DaySlot],
    cache: SqliteCache | None,
    *,
    max_errors: int,
) -> None:
    """Wie `_serve`, aber ohne Weg, ein Fach offen liegen zu lassen.

    Ein Fach, das niemand freigibt, laesst die Auswertung ewig warten. Also
    endet jeder Fehlweg dieser Quelle hier und traegt die noch offenen Tage
    als Fehlversuch ein.
    """
    served: set[date] = set()
    try:
        await _serve(source, queries, slots, cache, served, max_errors=max_errors)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - eine Quelle, nicht der Lauf
        for query in queries:
            if query.arrival not in served:
                served.add(query.arrival)
                slots[query.arrival].fail(
                    source.name, str(exc), retries=retries_of(exc)
                )


async def _fan_window(
    sources: Sequence[HotelSource],
    query: HotelQuery,
    window: Sequence[date],
    slots: dict[date, _DaySlot],
    cache: SqliteCache | None,
    *,
    max_errors: int,
) -> None:
    """Alle Quellen gleichzeitig ueber dasselbe Fenster von Anreisetagen."""
    queries = [query.on(day) for day in window]
    await asyncio.gather(
        *(
            _fan_source(source, queries, slots, cache, max_errors=max_errors)
            for source in sources
        )
    )


async def run_scan(
    conn: sqlite3.Connection,
    query: HotelQuery,
    *,
    window_start: date,
    window_end: date,
    sources: Sequence[HotelSource],
    rates: Rates,
    cache: SqliteCache | None = None,
    scan_id: int | None = None,
    on_progress: ProgressHook | None = None,
    max_errors: int = MAX_ERRORS,
    observed_at: datetime | None = None,
) -> ScanResult:
    """Den Zeitraum in Fenstern abgehen und jede Zeile wegschreiben."""
    if not sources:
        raise ValueError("Kein Quellen-Katalog fuer den Durchlauf")
    if cache is None:
        cache = SqliteCache(conn)
    if scan_id is None:
        scan_id = create_scan(
            conn, query, window_start=window_start, window_end=window_end
        )

    days = scan_days(window_start, window_end, resume_after=_resume_after(conn, scan_id))
    total = len(scan_days(window_start, window_end))
    already = total - len(days)
    result = ScanResult(scan_id=scan_id, status="running", days_done=already, days_total=total)
    _mark(conn, scan_id, status="running")

    streak = 0
    # Der frueheste Tag dieses Laufs, den keine Quelle beantwortet hat.
    lost: date | None = None
    for window in _windows(days, fan_width(sources)):
        slots = {day: _DaySlot(day, pending=len(sources)) for day in window}
        # Der Faecher laeuft neben der Auswertung. Sonst kaeme der Fortschritt
        # erst am Ende des Fensters, und ein Abbruch wirkte genauso spaet.
        fan = asyncio.create_task(
            _fan_window(sources, query, window, slots, cache, max_errors=max_errors)
        )
        try:
            for day in window:
                slot = slots[day]
                await slot.ready.wait()
                result.errors.extend(slot.errors)
                result.skipped.extend(slot.skipped)
                # Vor der Fallunterscheidung: ein Tag, den die Quelle am Ende
                # doch nicht hergab, hat trotzdem Nachfragen gekostet, und
                # gerade der sagt etwas ueber sie aus.
                result.retries += slot.retries

                if slot.failures == len(sources):
                    # Erst wenn keine Quelle geantwortet hat, ist der Tag
                    # gescheitert.
                    streak += 1
                    if lost is None:
                        lost = day
                    if streak >= max_errors:
                        result.status = "failed"
                        result.error = f"{max_errors} Fehler in Folge, Lauf beendet"
                        _mark(conn, scan_id, status="failed", error=result.error,
                              retries=result.retries, finished=True)
                        await _notify(
                            on_progress,
                            ScanProgress(scan_id, day, result.days_done, total,
                                         message=result.error, status="failed",
                                         retries=slot.retries),
                        )
                        return result
                    await _notify(
                        on_progress,
                        ScanProgress(scan_id, day, result.days_done, total,
                                     message=f"{day}: keine Quelle hat geantwortet",
                                     retries=slot.retries),
                    )
                    continue

                streak = 0
                written = await record_offers(
                    conn, slot.batch.offers, rates=rates, observed_at=observed_at
                )
                result.offers.extend(written.offers)
                result.skipped.extend(slot.batch.skipped)
                result.skipped.extend(written.skipped)
                result.days_done += 1
                _mark(
                    conn,
                    scan_id,
                    # `current_day` ist der Stand der Wiederaufnahme, kein
                    # Hochwasserstand. Sobald ein Tag ausgefallen ist, darf die
                    # Marke nicht mehr darueber hinauswandern: sonst liegt er
                    # dahinter und wird nie wieder gefragt - und beim naechsten
                    # Anlauf zaehlt ihn `already` auch noch als erledigt mit.
                    current_day=None if lost is not None else day,
                    days_done=result.days_done,
                    offers_found=len(result.offers),
                    retries=result.retries,
                )
                await _notify(
                    on_progress,
                    ScanProgress(
                        scan_id, day, result.days_done, total,
                        offers=written.offers,
                        message=f"{day}: {len(written.offers)} Angebote",
                        retries=slot.retries,
                    ),
                )
        finally:
            # Ein Abbruch aus der Fortschrittsmeldung heraus, ein Ende nach
            # Fehlerserie: in beiden Faellen darf kein Rest des Faechers
            # weiterlaufen und weitere Tage abrufen.
            fan.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await fan

    result.status = "done"
    _mark(
        conn,
        scan_id,
        status="done",
        days_done=result.days_done,
        offers_found=len(result.offers),
        retries=result.retries,
        finished=True,
    )
    await _notify(
        on_progress,
        ScanProgress(scan_id, window_end, result.days_done, total,
                     message="fertig", status="done"),
    )
    return result
