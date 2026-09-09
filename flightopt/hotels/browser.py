"""Ein Chromium, der laenger lebt als eine Suche - aber nicht laenger als noetig.

Bisher galt: ein Browser je Suchlauf, danach weg. Fuer den Handbetrieb war das
richtig; fuer eine Quelle, die alle paar Minuten von selbst laeuft, ist es der
falsche Zuschnitt. Zwei Zahlen, beide gemessen (`spike/probe_browser_memory.py`,
sechzig Ladevorgaenge derselben aufgezeichneten Ergebnisseite):

* **Speicher.** Ein Chromium mit einem Kontext je Seite steht bei rund 140 MB
  und waechst ueber sechzig Seiten auf rund 143 MB, also etwa 55 kB je Seite.
  Mit echten Booking-Seiten samt WAF-Challenge sind es rund 151 MB. Das ist
  kein Leck, aber es ist auch nicht null. Nach dem Schliessen bleibt nichts
  stehen.
* **Zeit.** Ein Start kostet rund 0,11 Sekunden. Bei einer Suche ueber dreissig
  Anreisetage ist das nichts; bei einem Durchgang alle zehn Minuten ueber Tage
  hinweg ist es ebenfalls nichts.

Daraus folgt der Zuschnitt hier, und er ist bewusst nicht "der Browser laeuft
immer":

1. **Innerhalb eines Durchgangs bleibt er stehen.** Das spart den Start je Tag
   und je Seite, und mehr will die Nebenlaeufigkeit von zwei ohnehin nicht.
2. **Nach `max_pages` Seiten faengt er neu an.** Fuenfundfuenfzig kB je Seite
   sind ueber tausend Seiten fuenfundfuenfzig MB, und niemand sieht sie
   kommen. Ein Neustart schneidet die Kurve ab, statt sie zu beobachten.
3. **Nach `max_age_seconds` ebenfalls.** Ein Prozess, der seit Stunden laeuft,
   haelt Zustand, den niemand mehr kennt.
4. **Im Leerlauf geht er weg.** Das ist der wichtigste Punkt und der
   eigentliche Grund, warum "langlebig" nicht "immer da" heisst: zwischen zwei
   Durchgaengen liegen Minuten, und in denen soll der Container so wenig
   Speicher brauchen wie ohne Booking. Ein Chromium, der eine Nacht lang
   nichts tut und trotzdem 140 MB haelt, bringt auf einem Container mit
   `mem_limit` irgendwann die Flugsuche mit um.
5. **Ein Abbruch wirft ihn weg.** Nach einer haengenden Challenge ist der
   naechste Tag mit einem frischen Prozess besser bedient als mit dem, der
   gerade nicht mehr geantwortet hat.

Dieses Modul kennt Playwright nicht. Es bekommt eine Startfunktion und einen
Gegenstand mit `close()`; das macht es ohne Browser pruefbar und haelt den
Adapter frei von Lebenszyklus-Buchhaltung.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

Launcher = Callable[[], Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class BrowserBudget:
    """Die drei Grenzen, an denen ein Chromium neu anfaengt.

    Alle drei sind Pflicht und alle drei sind groesser als null. "Unbegrenzt"
    waere auf einem VPS mit `mem_limit` keine Einstellung, sondern eine Wette.
    """

    max_pages: int = 60
    """Seiten je Prozess.

    Gemessen wurde zweierlei. Ueber sechzig Ladevorgaenge der aufgezeichneten
    Ergebnisseite wuchs der Chromium von 140,1 auf 143,1 MB, also rund 55 kB
    je Seite. Ueber zwei echte Booking-Seiten samt WAF-Challenge stand er bei
    151,3 und danach 153,1 MB - dort steckt noch Anlauf drin, aber es zeigt,
    dass eine echte Seite teurer ist als eine gespeicherte.

    Sechzig ist die Zahl, bei der auch ein zwanzigfacher Aufschlag auf den
    gemessenen Zuwachs unter siebzig MB bleibt. Der Neustart kostet dabei rund
    eine Zehntelsekunde je sechzig Seiten, also nichts.

    Im normalen Dauerbetrieb greift diese Grenze ohnehin selten: ein
    Durchgang holt je Beobachtung so viele Seiten wie Anreisetage, und danach
    schliesst die Leerlauffrist den Browser lange vor der sechzigsten Seite.
    Sie ist die Grenze fuer den langen Einzellauf, den ein Mensch startet."""

    max_age_seconds: float = 1800.0
    """Eine halbe Stunde. Laenger laeuft auch ein grosser Durchgang nicht."""

    idle_seconds: float = 120.0
    """Zwei Minuten ohne Seite, dann ist der Durchgang vorbei. Kuerzer waere
    ein Neustart mitten in einer Pause zwischen zwei Fenstern, laenger hielte
    der Prozess den Speicher ueber den ganzen Zyklus."""

    def __post_init__(self) -> None:
        if self.max_pages <= 0:
            raise ValueError("Seitenbudget muss groesser als null sein")
        if self.max_age_seconds <= 0:
            raise ValueError("Hoechstdauer muss groesser als null sein")
        if self.idle_seconds < 0:
            raise ValueError("Leerlauffrist darf nicht negativ sein")


DEFAULT_BUDGET = BrowserBudget()

ENV_MAX_PAGES = "FLIGHTOPT_HOTELS_BROWSER_PAGES"
ENV_MAX_AGE = "FLIGHTOPT_HOTELS_BROWSER_MAX_AGE"
ENV_IDLE = "FLIGHTOPT_HOTELS_BROWSER_IDLE"


def budget_from_env(env: Mapping[str, str] | None = None) -> BrowserBudget:
    """Das Budget aus der Umgebung, sonst die Voreinstellung.

    Ein unbrauchbarer Wert wird gemeldet und uebergangen, nicht durchgereicht:
    ein Tippfehler in einer Stack-Variablen darf keinen Container hinlegen.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    for key, field, cast in (
        (ENV_MAX_PAGES, "max_pages", int),
        (ENV_MAX_AGE, "max_age_seconds", float),
        (ENV_IDLE, "idle_seconds", float),
    ):
        raw = str(env.get(key, "")).strip()
        if not raw:
            continue
        try:
            values[field] = cast(raw)
        except ValueError:
            logger.warning("hotels: %s=%r ist keine Zahl, Voreinstellung gilt", key, raw)
    try:
        return BrowserBudget(**values)
    except ValueError as exc:
        logger.warning("hotels: Browser-Budget unbrauchbar (%s), Voreinstellung gilt", exc)
        return DEFAULT_BUDGET


class BrowserPool:
    """Haelt hoechstens einen Browser und weiss, wann er gehen soll.

    Hoechstens einen, nicht mehrere: die Quelle faechert ueber Kontexte auf und
    nicht ueber Prozesse, und ein zweiter Chromium kostet noch einmal alles.
    """

    def __init__(
        self,
        launcher: Launcher,
        *,
        budget: BrowserBudget = DEFAULT_BUDGET,
        clock: Clock = time.monotonic,
        sleep: Sleeper | None = None,
        name: str = "booking",
    ) -> None:
        self._launch = launcher
        self.budget = budget
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._name = name
        self._browser: Any = None
        self._started_at = 0.0
        self._last_used = 0.0
        self._pages_on_browser = 0
        self._in_flight = 0
        self._idle_task: asyncio.Task | None = None
        # Ein einziges Schloss fuer Start, Neustart und Schliessen. Zwei Tage,
        # die gleichzeitig merken "der Browser ist alt", duerfen nicht zwei
        # Prozesse starten und einen davon vergessen.
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self.starts = 0
        self.recycles = 0
        self.pages = 0

    # -- Auskunft --------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._browser is not None

    @property
    def stats(self) -> dict[str, Any]:
        """Was der Dauerbetrieb ueber sich selbst weiss."""
        return {
            "name": self._name,
            "running": self.running,
            "pages": self.pages,
            "pages_on_browser": self._pages_on_browser,
            "starts": self.starts,
            "recycles": self.recycles,
            "age_seconds": round(self._clock() - self._started_at, 1) if self.running else 0.0,
            "budget": {
                "max_pages": self.budget.max_pages,
                "max_age_seconds": self.budget.max_age_seconds,
                "idle_seconds": self.budget.idle_seconds,
            },
        }

    # -- Lebenslauf ------------------------------------------------------------

    def _stale(self) -> str:
        """Warum dieser Browser gehen soll, oder ein leerer Text."""
        if self._browser is None:
            return ""
        if self._pages_on_browser >= self.budget.max_pages:
            return f"{self._pages_on_browser} Seiten erreicht"
        if self._clock() - self._started_at >= self.budget.max_age_seconds:
            return f"{self.budget.max_age_seconds:.0f}s Hoechstdauer erreicht"
        return ""

    async def _shutdown(self, reason: str) -> None:
        """Den laufenden Browser schliessen. Darf nie werfen.

        Aufrufer halten dabei `self._lock`, und `_in_flight` ist null: einen
        Browser unter einer offenen Seite wegzuschliessen ist ein Absturz und
        kein Neustart.
        """
        browser, self._browser = self._browser, None
        self._pages_on_browser = 0
        if browser is None:
            return
        logger.info("hotels: %s-Browser wird geschlossen (%s)", self._name, reason)
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - Aufraeumen bricht nie einen Lauf ab
            logger.warning(
                "hotels: %s-Browser liess sich nicht schliessen", self._name, exc_info=True
            )

    async def _start(self) -> Any:
        """Einen Browser starten und die Leerlauf-Wache dazu."""
        self._browser = await self._launch()
        self._started_at = self._clock()
        self._last_used = self._started_at
        self._pages_on_browser = 0
        self.starts += 1
        self._arm_idle_watch()
        return self._browser

    def _arm_idle_watch(self) -> None:
        if self.budget.idle_seconds <= 0:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(self._watch_idle())

    async def _watch_idle(self) -> None:
        """Schliesst den Browser, sobald lange genug niemand ihn wollte.

        Eigene Aufgabe statt einer Pruefung beim naechsten Zugriff: die Pause
        zwischen zwei Durchgaengen ist genau die Zeit, in der niemand zugreift,
        und genau in ihr soll der Speicher frei sein.
        """
        try:
            while True:
                await self._sleep(self.budget.idle_seconds)
                async with self._lock:
                    if self._browser is None:
                        return
                    if self._in_flight:
                        continue
                    idle_for = self._clock() - self._last_used
                    if idle_for + 1e-9 < self.budget.idle_seconds:
                        continue
                    await self._shutdown(f"{idle_for:.0f}s Leerlauf")
                    return
        except asyncio.CancelledError:  # pragma: no cover - Abbau
            raise
        except Exception:  # noqa: BLE001 - eine Wache legt nie den Lauf hin
            logger.warning("hotels: Leerlauf-Wache endete mit Fehler", exc_info=True)

    async def acquire(self) -> Any:
        """Einen laufenden Browser, notfalls einen frischen."""
        async with self._lock:
            reason = self._stale()
            if reason and self._in_flight == 0:
                await self._shutdown(reason)
                self.recycles += 1
            elif reason:
                # Alt, aber es haengen noch Seiten daran. Der Neustart wartet
                # bis zur naechsten Anfrage; ihn hier zu erzwingen hiesse, die
                # laufende Seite abzureissen.
                logger.debug(
                    "hotels: %s-Browser ist faellig (%s), %d Seiten noch offen",
                    self._name, reason, self._in_flight,
                )
            browser = self._browser or await self._start()
            self._in_flight += 1
            self._idle.clear()
            self._last_used = self._clock()
            return browser

    async def release(self, *, keep: bool = True) -> None:
        """Eine Seite ist durch. `keep=False` wirft den Browser weg."""
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self.pages += 1
            self._pages_on_browser += 1
            self._last_used = self._clock()
            if self._in_flight == 0:
                self._idle.set()
            if not keep and self._browser is not None and self._in_flight == 0:
                await self._shutdown("Seite abgebrochen")
                self.recycles += 1

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Any]:
        """Ein Browser fuer die Dauer genau einer Seite.

        Faellt der Block mit einer Ausnahme aus, gilt der Browser als
        verdaechtig und wird weggeworfen: eine haengende Challenge hinterlaesst
        einen Prozess, dem der naechste Tag nicht mehr trauen muss.
        """
        browser = await self.acquire()
        keep = True
        try:
            yield browser
        except BaseException:
            keep = False
            raise
        finally:
            await self.release(keep=keep)

    async def close(self) -> None:
        """Alles zumachen. Zweimal aufgerufen schliesst trotzdem einmal."""
        task, self._idle_task = self._idle_task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        async with self._lock:
            await self._shutdown("Pool geschlossen")


# --------------------------------------------------------------------------
# Einer je Prozess, nicht einer je Lauf
# --------------------------------------------------------------------------

_POOLS: dict[str, BrowserPool] = {}
"""Die gemeinsamen Pools dieses Prozesses.

Warum ueberhaupt gemeinsam: der Durchlauf baut sich den Quellen-Katalog bei
jedem Start neu (`build_hotel_sources`). Haenge der Browser an der
Quelleninstanz, startete im Dauerbetrieb jeder Durchgang einen eigenen
Chromium - und der lange Browser waere ein langer Name fuer denselben
Zuschnitt wie vorher. Der Pool ueberlebt die Quelle; wann der Browser darin
geht, entscheidet weiterhin das Budget.
"""


def shared_pool(
    name: str, launcher: Launcher, *, budget: BrowserBudget | None = None
) -> BrowserPool:
    """Den Pool dieses Namens, notfalls einen neuen."""
    pool = _POOLS.get(name)
    if pool is None:
        pool = _POOLS[name] = BrowserPool(
            launcher, budget=budget or budget_from_env(), name=name
        )
    return pool


async def close_shared_pools() -> None:
    """Alle gemeinsamen Pools schliessen und vergessen."""
    pools = list(_POOLS.values())
    _POOLS.clear()
    for pool in pools:
        await pool.close()


def reset_shared_pools() -> None:
    """Nur fuer Tests: die Tabelle leeren, ohne etwas zu schliessen."""
    _POOLS.clear()
