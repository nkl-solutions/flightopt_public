"""Der Hotel-Durchlauf als Job, nach dem Muster der Flugsuche.

Ein eigener, schlanker Runner statt einer Erweiterung von `JobRunner`: der
kennt `SearchSpec`, `search_job` und `itinerary_result`, waehrend ein Hotellauf
`HotelQuery` und `hotel_scan` fuehrt. Beides in eine Klasse zu ziehen haette
jede Methode um eine Fallunterscheidung erweitert und damit genau den Pfad
angefasst, der die Flugsuche traegt. Geteilt werden deshalb nur `Progress` und
`JobCancelled`; das Ereignisformat im Strom bleibt dasselbe.

Uebernommen ist auch die Reihenfolge am Ende: erst der gesicherte
Abschluss-Schreibvorgang mit `status<>'cancelled'`, dann die Pruefung auf
Abbruch, und `except JobCancelled` steht vor dem generischen Handler. Ein
Folgefehler nach einem Abbruch darf den Lauf nicht doch noch scheitern lassen.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Sequence

from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.scan import (
    MAX_ERRORS,
    ScanProgress,
    create_scan,
    load_scan,
    run_scan,
    scan_days,
)
from flightopt.hotels.sources.base import HotelSource
from flightopt.hotels.store import signal_for
from flightopt.jobs.runner import JobCancelled, Progress
from flightopt.storage import db, fx_store
from flightopt.storage.baseline import refresh_baselines

logger = logging.getLogger(__name__)

MAX_HOTEL_DAYS = 400
"""Das Budget eines Laufs, wie BookingX es fuhr.

Der Lauf blockiert niemanden mehr, also faellt die alte Grenze von 14 Tagen.
Eine Obergrenze bleibt trotzdem: bei zwei Anfragen je Sekunde sind 400 Tage
rund dreieinhalb Minuten je Quelle, und darueber hinaus ist ein Fenster eher
ein Vertipper als eine Absicht. Wer mehr will, teilt auf; gekuerzt wird nichts
stillschweigend.
"""

BASELINE_EVERY = 10
"""Die Baseline liest die gesamte Beobachtungshistorie.

Sie nach jedem einzelnen Tag neu zu rechnen kostet bei einem Fenster von 400
Tagen mehr als die Abrufe selbst. Also einmal vor dem ersten Tag und danach
alle zehn fertigen Tage. Das Signal eines Tages steht damit fest, sobald der
Tag fertig ist, und wandert nicht mehr unter der Zeile weg.
"""

TERMINAL = ("done", "failed", "cancelled")

HOTEL_SIGNAL_ORDER = {"error": 0, "cheap": 1, "normal": 2, "expensive": 3, "unknown": 4}

SourceFactory = Callable[[], Sequence[HotelSource]]


def hotel_row(conn: sqlite3.Connection, offer: HotelOffer) -> dict[str, Any]:
    """Eine Tabellenzeile aus einem Angebot, samt Preissignal.

    `signal` traegt weiterhin den Status des Detektors. Liefert er zusaetzlich
    eine Stufe, steht sie als `tier` daneben, statt den Status zu ersetzen: die
    Oberflaeche nimmt die Stufe, wenn es sie gibt, und faellt sonst zurueck.
    """
    signal = signal_for(conn, offer)
    per_night = offer.price_per_night
    row: dict[str, Any] = {
        "source": offer.source,
        "property_key": offer.property_key,
        "name": offer.name,
        "stars": offer.stars,
        "date": offer.arrival.isoformat(),
        "nights": offer.nights,
        "price_per_night": per_night.major,
        "price_total": offer.price.major,
        "currency": per_night.currency,
        "native": (
            {"amount": offer.price_total.major, "currency": offer.price_total.currency}
            if offer.converted
            else None
        ),
        "review_rating": offer.review_rating,
        "review_count": offer.review_count,
        "signal": signal.get("status", "unknown"),
        "url": offer.url,
        "city": offer.city,
        "indicative": offer.indicative,
    }
    for extra in ("tier", "reason", "basis", "n"):
        if signal.get(extra) is not None:
            row[extra] = signal[extra]
    return row


def signal_rank(row: dict[str, Any]) -> int:
    """`error` zuerst, `unknown` zuletzt. Die Stufe schlaegt den Status."""
    key = row.get("tier") or row.get("signal") or "unknown"
    return HOTEL_SIGNAL_ORDER.get(str(key), 9)


def sort_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (signal_rank(row), row["price_per_night"]))


def stored_rows(conn: sqlite3.Connection, scan: dict[str, Any]) -> list[dict[str, Any]]:
    """Die Zeilen eines gespeicherten Laufs, aus der Beobachtungshistorie.

    Ein eigener Ergebnisspeicher waere eine zweite Wahrheit neben
    `price_observation`. Der Lauf kennt sein Fenster, seine Naechte, seine
    Belegung und seinen Zeitraum, und das genuegt, um genau die Zeilen
    wiederzufinden, die er geschrieben hat.
    """
    started = str(scan.get("created_at") or "")
    if not started:
        return []
    finished = str(scan.get("finished_at") or scan.get("updated_at") or "")
    if scan.get("status") in TERMINAL and finished:
        # `created_at` steht auf Sekunden, `observed_at` traegt Mikrosekunden.
        # Ohne diesen Nachschlag faellt die letzte Beobachtung aus dem Fenster.
        ended = finished + "~"
    else:
        ended = "9999"

    nights = int(scan.get("nights") or 1)
    party = int(scan.get("adults") or 0) + int(scan.get("children") or 0)
    rows = conn.execute(
        "SELECT o.entity_key, o.source, o.travel_date, o.currency, "
        "o.price_total_minor, o.is_estimate, o.observed_at, "
        "p.name, p.city, p.country, p.stars, p.review_rating, p.review_count, p.url "
        "FROM price_observation o "
        "LEFT JOIN hotel_property p "
        "  ON p.property_key = substr(o.entity_key, instr(o.entity_key, '|') + 1) "
        "WHERE o.entity_type='hotel' AND o.travel_date BETWEEN ? AND ? "
        "  AND o.return_or_nights=? AND o.party_size=? "
        "  AND o.observed_at>=? AND o.observed_at<=? "
        "ORDER BY o.observed_at",
        (
            str(scan["window_start"]),
            str(scan["window_end"]),
            str(nights),
            party,
            started,
            ended,
        ),
    ).fetchall()

    # Dieselbe Unterkunft am selben Tag kann mehrfach beobachtet worden sein;
    # gezeigt wird die juengste Zeile, nicht die erste.
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        latest[(row["entity_key"], row["travel_date"])] = row

    out: list[dict[str, Any]] = []
    for (entity_key, travel_date), row in latest.items():
        property_key = entity_key.split("|", 1)[-1]
        arrival = date.fromisoformat(str(travel_date))
        price = Money(int(row["price_total_minor"]), str(row["currency"]))
        offer = HotelOffer(
            source=str(row["source"]),
            property_key=property_key,
            name=str(row["name"] or property_key),
            arrival=arrival,
            departure=arrival + timedelta(days=nights),
            price_total=price,
            price_eur=price if price.currency == "EUR" else None,
            stars=row["stars"],
            city=row["city"],
            country=row["country"],
            review_rating=row["review_rating"],
            review_count=row["review_count"],
            url=row["url"],
            party_size=party or 1,
            indicative=bool(row["is_estimate"]),
        )
        out.append(hotel_row(conn, offer))
    return sort_rows(out)


class HotelJobRunner:
    """Haelt die laufenden Hoteldurchlaeufe und ihre Fortschrittsstroeme."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or str(db.DEFAULT_DB)
        self._queues: dict[int, list[asyncio.Queue]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._results: dict[int, dict[str, Any]] = {}
        # Der Browser abonniert erst kurz nach dem Start, sonst gingen die
        # ersten Ereignisse an niemanden. Also aufheben und nachspielen.
        self._history: dict[int, list[Progress]] = {}
        # Abbruch wirkt zwischen zwei Tagen, nicht mitten in einem Abruf.
        self._cancelled: set[int] = set()

    # -- Verkabelung ----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def subscribe(self, scan_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for past in self._history.get(scan_id, []):
            q.put_nowait(past)
        self._queues.setdefault(scan_id, []).append(q)
        return q

    def unsubscribe(self, scan_id: int, q: asyncio.Queue) -> None:
        subs = self._queues.get(scan_id)
        if subs and q in subs:
            subs.remove(q)

    def _emit(self, scan_id: int, progress: Progress) -> None:
        self._history.setdefault(scan_id, []).append(progress)
        for q in self._queues.get(scan_id, []):
            q.put_nowait(progress)

    # -- Lebenslauf -----------------------------------------------------------

    def create(self, query: HotelQuery, *, window_start: date, window_end: date) -> int:
        conn = self._conn()
        try:
            scan_id = create_scan(
                conn, query, window_start=window_start, window_end=window_end
            )
            conn.commit()
            return scan_id
        finally:
            conn.close()

    def start(
        self,
        scan_id: int,
        query: HotelQuery,
        *,
        window_start: date,
        window_end: date,
        sources: SourceFactory | Sequence[HotelSource],
        max_errors: int = MAX_ERRORS,
    ) -> None:
        # Ein wiederaufgenommener Lauf behaelt seine Kennung, aber nicht seinen
        # Verlauf: sonst spielt der Strom dem Browser zuerst das Ende des
        # vorigen Anlaufs vor und schliesst sich sofort wieder.
        self._history.pop(scan_id, None)
        self._results.pop(scan_id, None)
        self._cancelled.discard(scan_id)
        self._tasks[scan_id] = asyncio.create_task(
            self._run(
                scan_id,
                query,
                window_start=window_start,
                window_end=window_end,
                sources=sources,
                max_errors=max_errors,
            )
        )

    def status(self, scan_id: int) -> str | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT status FROM hotel_scan WHERE id=?", (scan_id,)
            ).fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    def scan(self, scan_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            return load_scan(conn, scan_id)
        finally:
            conn.close()

    def result(self, scan_id: int) -> dict[str, Any] | None:
        if scan_id in self._results:
            return self._results[scan_id]
        conn = self._conn()
        try:
            scan = load_scan(conn, scan_id)
            if scan is None:
                return None
            return {
                "status": scan["status"],
                "error": scan["error"],
                "rows": stored_rows(conn, scan),
            }
        finally:
            conn.close()

    def has_history(self, scan_id: int) -> bool:
        """Falsch fuer Laeufe, die dieser Prozess nicht gefahren hat."""
        return bool(self._history.get(scan_id))

    def cancel(self, scan_id: int) -> str | None:
        """Markiert den Lauf als abgebrochen. Fertige Laeufe bleiben stehen."""
        current = self.status(scan_id)
        if current is None or current in TERMINAL:
            return current
        self._cancelled.add(scan_id)
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE hotel_scan SET status='cancelled', updated_at=?, "
                "finished_at=COALESCE(finished_at, ?) WHERE id=?",
                (db.now(), db.now(), scan_id),
            )
            conn.commit()
        finally:
            conn.close()
        self._results.setdefault(
            scan_id, {"status": "cancelled", "error": None, "rows": []}
        )
        return "cancelled"

    def _raise_if_cancelled(self, scan_id: int) -> None:
        if scan_id in self._cancelled:
            raise JobCancelled("Durchlauf abgebrochen")

    # -- die eigentliche Arbeit -----------------------------------------------

    async def _run(
        self,
        scan_id: int,
        query: HotelQuery,
        *,
        window_start: date,
        window_end: date,
        sources: SourceFactory | Sequence[HotelSource],
        max_errors: int = MAX_ERRORS,
    ) -> None:
        conn = self._conn()
        live: list[HotelSource] = []
        collected: list[dict[str, Any]] = []
        total = len(scan_days(window_start, window_end))
        finished = 0
        try:
            self._raise_if_cancelled(scan_id)
            live = list(sources() if callable(sources) else sources)
            if not live:
                raise ValueError("Kein Quellen-Katalog fuer den Durchlauf")
            rates = await fx_store.current_rates(conn)
            refresh_baselines(conn, entity_type="hotel")
            since_refresh = 0

            self._emit(
                scan_id,
                Progress(
                    "planning",
                    f"{query.destination}: {total} Anreisetage, "
                    f"{query.nights} Naechte je Aufenthalt",
                    done=0,
                    total=total,
                    detail={
                        "destination": query.destination,
                        "nights": query.nights,
                        "window": [window_start.isoformat(), window_end.isoformat()],
                        "sources": [source.name for source in live],
                    },
                ),
            )

            async def on_day(update: ScanProgress) -> None:
                nonlocal since_refresh, finished
                # Der Abbruch greift zwischen zwei Tagen: ein halb fertiger Tag
                # ist billiger als ein hart abgerissener Abruf.
                self._raise_if_cancelled(scan_id)
                if update.status in TERMINAL:
                    # Das Ende meldet der Runner selbst, mit dem vollen Ergebnis.
                    return
                since_refresh += 1
                if update.offers and since_refresh >= BASELINE_EVERY:
                    refresh_baselines(conn, entity_type="hotel")
                    since_refresh = 0
                rows = [hotel_row(conn, offer) for offer in update.offers]
                collected.extend(rows)
                finished = update.days_done
                self._emit(
                    scan_id,
                    Progress(
                        "day",
                        update.message,
                        done=update.days_done,
                        total=update.days_total,
                        detail={
                            "date": update.day.isoformat(),
                            "rows": rows,
                            "found": len(collected),
                        },
                    ),
                )
                # Noch einmal, nach der Meldung: wer waehrend dieses Tages auf
                # Abbrechen gedrueckt hat, soll nicht noch einen ganzen Tag
                # abgerufen bekommen.
                self._raise_if_cancelled(scan_id)

            result = await run_scan(
                conn,
                query,
                window_start=window_start,
                window_end=window_end,
                sources=live,
                rates=rates,
                scan_id=scan_id,
                on_progress=on_day,
                max_errors=max_errors,
            )
            # `run_scan` schreibt sein Ende ohne Ruecksicht auf einen Abbruch,
            # der genau dazwischen kam. Also hier pruefen, bevor 'fertig' faellt.
            self._raise_if_cancelled(scan_id)
            if result.status == "failed":
                raise RuntimeError(result.error or "Der Durchlauf ist gescheitert")

            payload = sort_rows(collected)
            # Ein Abbruch zwischen Pruefpunkt und Abschluss darf den Lauf nicht
            # doch noch auf fertig drehen.
            conn.execute(
                "UPDATE hotel_scan SET status='done', updated_at=?, offers_found=?, "
                "finished_at=COALESCE(finished_at, ?) WHERE id=? AND status<>'cancelled'",
                (db.now(), len(payload), db.now(), scan_id),
            )
            conn.commit()
            self._raise_if_cancelled(scan_id)

            self._results[scan_id] = {"status": "done", "error": None, "rows": payload}
            self._emit(
                scan_id,
                Progress(
                    "done",
                    f"{len(payload)} Angebote aus {result.days_done} von {total} Tagen",
                    done=result.days_done,
                    total=total,
                    detail={
                        "rows": payload,
                        "skipped": len(result.skipped),
                        "errors": result.errors[:5],
                    },
                ),
            )

        except JobCancelled as stop:
            self._cancelled.discard(scan_id)
            # `cancel()` hat Status und Endzeitpunkt bereits geschrieben. Der
            # Schreibvorgang bleibt idempotent und laesst die Zeit stehen.
            conn.execute(
                "UPDATE hotel_scan SET status='cancelled', updated_at=?, "
                "finished_at=COALESCE(finished_at, ?) WHERE id=?",
                (db.now(), db.now(), scan_id),
            )
            conn.commit()
            payload = sort_rows(collected)
            self._results[scan_id] = {
                "status": "cancelled",
                "error": None,
                "rows": payload,
            }
            self._emit(
                scan_id,
                Progress(
                    "cancelled",
                    str(stop),
                    done=finished,
                    total=total,
                    detail={"rows": payload},
                ),
            )
        except Exception as exc:  # noqa: BLE001 - jeder Fehler gehoert vor die Augen
            logger.exception("hotel scan %s failed", scan_id)
            message = str(exc) or type(exc).__name__
            # Nach einem Abbruch kommt der Folgefehler oft erst hier an. Er darf
            # den bereits geschriebenen Abbruch nicht in ein Scheitern drehen.
            aborted = scan_id in self._cancelled
            self._cancelled.discard(scan_id)
            conn.execute(
                "UPDATE hotel_scan SET status='failed', updated_at=?, error=?, "
                "finished_at=COALESCE(finished_at, ?) WHERE id=? AND status<>'cancelled'",
                (db.now(), message, db.now(), scan_id),
            )
            conn.commit()
            if aborted:
                # Ohne Endereignis haengt jeder spaete Abonnent des Stroms fest.
                self._emit(
                    scan_id,
                    Progress(
                        "cancelled", "Durchlauf abgebrochen",
                        done=finished, total=total,
                    ),
                )
            else:
                self._results[scan_id] = {
                    "status": "failed",
                    "error": message,
                    "rows": sort_rows(collected),
                }
                self._emit(
                    scan_id, Progress("failed", message, done=finished, total=total)
                )
        finally:
            for source in live:
                try:
                    source.close()
                except Exception:  # noqa: BLE001 - ein Aufraeumer bricht nichts ab
                    logger.warning("hotels: %s liess sich nicht schliessen", source.name)
            conn.close()
