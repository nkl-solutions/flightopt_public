"""Gemeinsames Geruest der Hotel-Quellen.

Takt und Sicherung kommen unveraendert aus `flightopt.sources.base`. Ein
zweiter Ratenbegrenzer waere ein zweiter Ort, an dem Hoeflichkeit kaputtgehen
kann, und der eine dort ist bereits gegen eine virtuelle Uhr gemessen.

Hier liegt ausserdem der Faecher ueber mehrere Anreisetage. Nebenlaeufig heisst
dabei ausdruecklich: mehrere Quellen gleichzeitig und ein kleines Fenster
gleichzeitiger Anfragen je Quelle - **nicht** mehr Anfragen pro Sekunde. Die
mittlere Rate bleibt die des Limiters, sie wird nur nicht mehr von der
Antwortzeit der vorherigen Anfrage mitbestimmt.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable, Mapping, Sequence

from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.sources.base import (  # noqa: F401 - Weitergabe an die Adapter
    CircuitBreaker,
    RateLimiter,
    SourceBlocked,
    SourceError,
)

logger = logging.getLogger(__name__)

MAX_ERRORS = 5
"""Fehler in Folge, nach denen ein Lauf endet statt weiter anzuklopfen."""

OK = "ok"
EMPTY = "leer"
"""Die Quelle sagt ausdruecklich: zu dieser Suche gibt es nichts."""
BROKEN = "struktur"
"""Die Seite kam an, war aber nicht wiederzuerkennen. Kein leeres Ergebnis."""
FAILED = "fehler"
ABORTED = "abgebrochen"


class LayoutBroken(SourceError):
    """Antwort erhalten, Aufbau nicht wiedererkannt.

    Der gefaehrlichste Fall einer Seiten-Quelle ist nicht der Ausfall, sondern
    das stille Nichts: ein umbenanntes Attribut, und der Parser liefert null
    Zeilen, die wie "heute gab es keine Hotels" aussehen. Deshalb ist das ein
    Fehler mit Text und keine leere Liste.
    """


@dataclass(slots=True)
class HotelBatch:
    """Was eine Suche ergab, samt dem, was sie nicht lesen konnte.

    Uebersprungene Zeilen stehen im Bericht, statt still zu verschwinden: ein
    unparsbarer Preis ist eine Information ueber die Quelle, kein Nichts.
    """

    offers: list[HotelOffer] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    parser: str = ""
    """Welcher Weg gelesen hat: "apollo", "dom" oder leer."""
    total_results: int | None = None
    """Was die Quelle als Gesamtzahl nennt, unabhaengig von dieser Seite."""
    empty: bool = False
    """Wahr, wenn die Quelle ausdruecklich null Treffer meldet."""

    def extend(self, other: "HotelBatch") -> "HotelBatch":
        self.offers.extend(other.offers)
        self.skipped.extend(other.skipped)
        if not self.parser:
            self.parser = other.parser
        if self.total_results is None:
            self.total_results = other.total_results
        self.empty = self.empty and other.empty
        return self


@dataclass(slots=True)
class DayResult:
    """Was ein Anreisetag ergab. Traegt den Fehler, statt ihn zu werfen.

    Ein Faecher ueber dreissig Tage darf nicht am dritten enden, nur weil eine
    Seite kaputt war. Der Aufrufer bekommt jede Zeile und entscheidet selbst.
    """

    query: HotelQuery
    batch: HotelBatch = field(default_factory=HotelBatch)
    status: str = OK
    error: str = ""

    @property
    def arrival(self) -> date:
        return self.query.arrival

    @property
    def offers(self) -> list[HotelOffer]:
        return self.batch.offers

    @property
    def failed(self) -> bool:
        """Wahr fuer alles, was keine Antwort war. `EMPTY` gehoert nicht dazu."""
        return self.status in {BROKEN, FAILED}


@dataclass(slots=True)
class FetchReport:
    """Das Zaehlwerk eines Faechers.

    "Keine Treffer" und "Struktur nicht wiedererkannt" stehen getrennt, weil
    das eine ein Ergebnis ist und das andere ein Defekt.
    """

    ok: int = 0
    empty: int = 0
    broken: int = 0
    failed: int = 0
    aborted: int = 0
    offers: int = 0
    notes: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, results: Sequence[DayResult]) -> "FetchReport":
        report = cls()
        for result in results:
            if result.status == OK:
                report.ok += 1
            elif result.status == EMPTY:
                report.empty += 1
            elif result.status == BROKEN:
                report.broken += 1
            elif result.status == ABORTED:
                report.aborted += 1
            else:
                report.failed += 1
            report.offers += len(result.batch.offers)
            if result.error:
                report.notes.append(f"{result.arrival}: {result.error}")
        return report

    @property
    def days(self) -> int:
        return self.ok + self.empty + self.broken + self.failed + self.aborted

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "ok": self.ok,
            "empty": self.empty,
            "broken": self.broken,
            "failed": self.failed,
            "aborted": self.aborted,
            "offers": self.offers,
            "notes": list(self.notes),
        }


class HotelSource:
    """Basis jedes Hotel-Adapters.

    Der Vertrag ist eine einzige Methode: `search` beantwortet genau eine
    Anfrage fuer genau einen Anreisetag. Die Tagesschleife liegt bewusst
    darueber, weil keine der Quellen einen Tageskalender kennt.
    """

    name: str = "base"
    indicative: bool = True
    per_minute: int = 60
    concurrency: int = 1

    def __init__(
        self,
        *,
        per_minute: int | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.limiter = RateLimiter(
            per_minute or self.per_minute,
            concurrency=concurrency or self.concurrency,
        )
        self.breaker = CircuitBreaker()

    @classmethod
    def availability(cls, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
        """(nutzbar, Grund). Der Grund steht nur da, wenn es nicht geht."""
        return True, ""

    async def search(self, query: HotelQuery) -> HotelBatch:
        raise NotImplementedError(f"{self.name} kann nicht suchen")

    async def search_many(
        self, queries: Sequence[HotelQuery], *, max_errors: int = MAX_ERRORS
    ) -> list[DayResult]:
        """Mehrere Anreisetage, nebenlaeufig, im Takt dieser Quelle.

        Der Rueckgabewert steht in der Reihenfolge der Fragen, damit der
        Aufrufer nicht sortieren muss. Gedacht als der eine Aufruf, den ein
        Durchlauf je Quelle und Fenster braucht.
        """
        return await self.run_many(queries, self.search, max_errors=max_errors)

    async def run_many(
        self,
        queries: Sequence[HotelQuery],
        runner: Callable[[HotelQuery], Awaitable[HotelBatch]],
        *,
        max_errors: int = MAX_ERRORS,
    ) -> list[DayResult]:
        """Der Faecher selbst. `runner` ist die Arbeit fuer genau einen Tag.

        Das Fenster ist so breit wie die Nebenlaeufigkeit des Limiters, und es
        liegt hier und nicht im Limiter: nur so kann ein wartender Tag noch
        merken, dass der Lauf inzwischen abgebrochen wurde. Ein Tag, der nie
        gestartet ist, kommt als `ABORTED` zurueck und nicht als leer.
        """
        if not queries:
            return []

        results: list[DayResult] = [DayResult(query=q) for q in queries]
        window = asyncio.Semaphore(max(1, int(self.limiter.concurrency)))
        abort = asyncio.Event()
        streak = 0

        async def one(index: int, query: HotelQuery) -> None:
            nonlocal streak
            async with window:
                if abort.is_set():
                    results[index] = DayResult(
                        query, status=ABORTED, error="Lauf war bereits beendet"
                    )
                    return
                try:
                    batch = await runner(query)
                except LayoutBroken as exc:
                    results[index] = DayResult(query, status=BROKEN, error=str(exc))
                except SourceBlocked as exc:
                    # Eine Sperre ist keine Frage der Ausdauer. Sofort Schluss.
                    results[index] = DayResult(query, status=FAILED, error=str(exc))
                    abort.set()
                    return
                except Exception as exc:  # noqa: BLE001 - ein Tag, nicht der Lauf
                    results[index] = DayResult(query, status=FAILED, error=str(exc))
                else:
                    status = EMPTY if batch.empty and not batch.offers else OK
                    results[index] = DayResult(query, batch=batch, status=status)
                    streak = 0
                    return
                streak += 1
                if streak >= max_errors:
                    logger.warning(
                        "%s: %d Fehler in Folge, Faecher beendet", self.name, streak
                    )
                    abort.set()

        await asyncio.gather(
            *(one(index, query) for index, query in enumerate(queries))
        )
        return results

    def close(self) -> None:
        """Verbindungen freigeben. Darf nie werfen."""

    def __repr__(self) -> str:  # pragma: no cover - Diagnose
        return f"<{type(self).__name__} {self.name}>"


def env_flag(env: Mapping[str, str], key: str, default: str = "0") -> bool:
    """Ein Schalter aus der Umgebung, gelesen wie ueberall sonst im Projekt."""
    value = str(env.get(key, default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def header(response: Any, name: str) -> str | None:
    """Einen Antwort-Header holen, ohne auf die Schreibweise zu wetten.

    curl_cffi liefert je nach Pfad ein dict oder eine eigene Mapping-Klasse;
    HTTP-Header sind gross-/kleinschreibungsblind, die dicts sind es nicht.
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    wanted = name.lower()
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - fremde Implementierung
        return None
    for key, value in items:
        if str(key).lower() == wanted:
            return str(value)
    return None
