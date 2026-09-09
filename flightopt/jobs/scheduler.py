"""Small in-process scheduler for saved daily scans."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from flightopt.hotels.watch import run_hotel_watches
from flightopt.jobs.daily import dispatch_due_profiles
from flightopt.jobs.hunt import run_hunt
from flightopt.jobs.watchlist import run_watchlist
from flightopt.storage.cache import SqliteCache

logger = logging.getLogger(__name__)


async def purge_cache(conn) -> int:
    """Abgelaufene Cache-Zeilen wegraeumen. Fehler beenden nichts.

    Ein abgelaufener Eintrag wird nie wieder gelesen, `SqliteCache.get`
    behandelt ihn wie nicht vorhanden. Ohne Aufraeumen bleibt er trotzdem in
    derselben Datei stehen wie die Beobachtungshistorie, und die Datei waechst
    mit jedem Abruf. Das Raeumen ist Beiwerk des Laufs, kein Zweck: es darf
    einen Versand nicht scheitern lassen.
    """
    try:
        removed = await SqliteCache(conn).purge_expired()
    except Exception as exc:  # noqa: BLE001 - Hausputz bricht keinen Lauf ab
        logger.warning("cache: abgelaufene Zeilen nicht geraeumt (%s)", exc)
        return 0
    if removed:
        logger.info("cache: %d abgelaufene Zeilen geraeumt", removed)
    return removed


class DailyScanScheduler:
    def __init__(self, runner, *, interval_seconds: int = 600,
                 now: Callable[[], datetime] | None = None) -> None:
        self.runner = runner
        self.interval_seconds = interval_seconds
        self.now = now or datetime.now
        self._task: asyncio.Task | None = None

    async def run_once(self) -> list[dict[str, Any]]:
        conn = self.runner._conn()
        try:
            await purge_cache(conn)
            return dispatch_due_profiles(conn, self.runner, now=self.now())
        finally:
            conn.close()

    async def collect_once(self) -> dict[str, Any]:
        """Die faelligen Beobachtungen einsammeln.

        Eigener Auftrag neben `run_once`, weil es ein anderer ist: eine
        gespeicherte Suche laeuft den ganzen Ablauf und liefert eine
        Ergebnisliste, eine Beobachtung holt nur Kalender und schreibt sie weg.
        """
        conn = self.runner._conn()
        try:
            return await run_watchlist(conn, now=self.now())
        finally:
            conn.close()

    async def hunt_once(self) -> dict[str, Any]:
        """Die heissen Strecken abfragen und Fehltarife melden.

        Dritter Auftrag neben Versand und Aufzeichnung, weil es ein dritter
        ist: er laeuft im Minutentakt statt im Tagestakt, er verbucht sein
        Budget je Quelle, und er darf ausfallen, ohne dass jemand etwas
        verliert - ein verpasster Fehltarif ist aergerlich, eine verpasste
        Aufzeichnung ist fort.
        """
        conn = self.runner._conn()
        try:
            return await run_hunt(conn, now=self.now())
        finally:
            conn.close()

    async def hotels_once(self) -> dict[str, Any]:
        """Die faelligen Hotelbeobachtungen abarbeiten.

        Vierter Auftrag, und er ist ein vierter: eine Flugbeobachtung holt
        einen Kalender mit sechzig Tagen in einer Anfrage, ein Hotelfenster
        braucht eine Anfrage je Anreisetag und Quelle. Deshalb deckelt
        `run_hotel_watches` sich selbst auf drei Beobachtungen je Durchgang,
        und deshalb steht dieser Teil zuletzt.

        Ohne diesen Aufruf trieb nichts die Schleife an: `hotel_watch` fuellte
        sich mit Beobachtungen, die nie faellig wurden. Und ohne fuenf
        Beobachtungen je Gruppe entsteht keine Baseline - eine
        Preisfehler-Erkennung, die niemand ausloest, findet nichts.

        Der Quellen-Katalog wird hier bewusst nicht uebergeben:
        `run_hotel_watches` baut ihn selbst und gibt ihn im `finally` wieder
        frei. Was `BookingSource.close()` dabei freigibt, ist die HTTP-Sitzung
        - der gemeinsame Browser-Pool bleibt mit Absicht stehen, sonst
        startete jeder Takt einen eigenen Chromium.
        """
        conn = self.runner._conn()
        try:
            return await run_hotel_watches(conn, now=self.now())
        finally:
            conn.close()

    async def tick(self) -> dict[str, Any]:
        """Ein Durchgang: Versand, Aufzeichnung, Jagd, Hotels.

        Jeder Teil steht fuer sich. Riss der Versand die Aufzeichnung mit,
        hielte ein dauerhaft kaputtes Profil die Preishistorie fuer immer an -
        und jeder Tag ohne Aufzeichnung ist unwiederbringlich, weil der Preis
        von gestern morgen nicht mehr zu haben ist.

        Die Jagd steht bewusst nach der Aufzeichnung. Sie fragt fremde Server
        im Minutentakt; laeuft sie danach, hat eine heisse Strecke, die
        ohnehin gerade dran war, ihren Zeitstempel schon gesetzt und wird
        nicht zweimal geholt.

        Die Hotels stehen zuletzt und mit eigenem `try`. Sie sind der einzige
        Teil, der einen Browser fuehrt, und damit der, der am ehesten
        ausfaellt. Ein kaputter Chromium darf die Flugjagd nicht anhalten -
        und ein Ausfall der Jagd nicht die Hotels.
        """
        jobs: list[dict[str, Any]] = []
        try:
            jobs = await self.run_once()
        except Exception:  # noqa: BLE001 - die Aufzeichnung laeuft trotzdem
            logger.exception("Tagesplaner: Versand gescheitert")
        watch: dict[str, Any] = {}
        try:
            watch = await self.collect_once()
        except Exception:  # noqa: BLE001 - der Versand ist schon durch
            logger.exception("Tagesplaner: Aufzeichnung gescheitert")
        hunt: dict[str, Any] = {}
        try:
            hunt = await self.hunt_once()
        except Exception:  # noqa: BLE001 - Aufzeichnung und Versand sind durch
            logger.exception("Tagesplaner: Jagd gescheitert")
        hotels: dict[str, Any] = {}
        try:
            hotels = await self.hotels_once()
        except Exception:  # noqa: BLE001 - die drei davor sind durch
            logger.exception("Tagesplaner: Hotelbeobachtung gescheitert")
        return {"jobs": jobs, "watchlist": watch, "hunt": hunt, "hotels": hotels}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ein Durchgang darf scheitern
                # Vorher riss ein einziger Fehler die Schleife ab: der
                # Tagesplaner war fuer den Rest der Prozesslaufzeit tot, und
                # der Grund landete nirgends - asyncio meldet eine nie
                # abgeholte Task-Ausnahme erst beim Aufraeumen des Objekts.
                logger.exception("Tagesplaner: Durchgang gescheitert, naechster Versuch folgt")
            await asyncio.sleep(self.interval_seconds)
