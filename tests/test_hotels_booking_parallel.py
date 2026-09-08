"""Mehrere Anreisetage gleichzeitig - ohne den Takt zu erhoehen.

Nebenlaeufig heisst hier: ein kleines Fenster gleichzeitiger Seiten, damit das
Warten auf eine Antwort nicht die naechste Frage aufhaelt. Die mittlere Rate
gegen booking.com bleibt bei 0,4 Anfragen pro Sekunde, und genau das misst der
erste Test - gegen eine virtuelle Uhr, damit er auf einer langsamen Maschine
nicht rot wird.

Kein Test startet einen Browser. Der Browser ist eine Attrappe, die die echte
Seitenmechanik nachstellt: Kontext auf, Seite laden, Inhalt zurueck, Kontext zu.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from flightopt.hotels.models import HotelQuery
from flightopt.hotels.sources.base import FetchReport, LayoutBroken, SourceBlocked
from flightopt.hotels.sources.booking import BookingSource, Destination
from flightopt.sources.base import RateLimiter
from tests.test_rate_limiter import VirtualClock

FIXTURES = Path(__file__).parent / "fixtures"
ATHENS = Destination("13914", "city", "Athen")
PAGE_SECONDS = 10.0
"""So lange braucht eine Seite in der Attrappe. Deutlich laenger als das
Taktintervall, damit wirklich das Fenster die Grenze setzt und nicht die Rate."""


def days(count: int) -> list[HotelQuery]:
    first = date(2026, 11, 10)
    return [
        HotelQuery(destination="Athen", arrival=first + timedelta(days=index))
        for index in range(count)
    ]


class FakeBrowser:
    """Zaehlt, wie viele Kontexte gleichzeitig offen sind, und wann geladen wird."""

    def __init__(self, html: str, clock: VirtualClock) -> None:
        self.html = html
        self.clock = clock
        self.open = 0
        self.peak = 0
        self.starts: list[float] = []

    @asynccontextmanager
    async def _context(self):
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            yield
        finally:
            self.open -= 1

    async def new_context(self, **_: object):
        holder = self._context()
        await holder.__aenter__()
        browser = self

        class Context:
            async def new_page(self):
                return Page()

            async def close(self):
                await holder.__aexit__(None, None, None)

        class Page:
            async def route(self, _pattern, _handler):
                return None

            async def goto(self, url, **_kwargs):
                browser.starts.append(browser.clock.monotonic())
                await browser.clock.sleep(PAGE_SECONDS)
                return SimpleNamespace(status=200)

            async def wait_for_selector(self, _selector, **_kwargs):
                return None

            async def content(self):
                return browser.html

        return Context()


class StubBooking(BookingSource):
    """Der echte Adapter, nur ohne Netz und ohne Chromium."""

    def __init__(self, html: str, clock: VirtualClock) -> None:
        super().__init__()
        self.limiter = RateLimiter(
            self.per_minute,
            concurrency=self.concurrency,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.fake = FakeBrowser(html, clock)

    async def resolve_destination(self, text: str) -> Destination:
        return ATHENS

    @asynccontextmanager
    async def browser(self):
        yield self.fake


class ScriptedBooking(BookingSource):
    """Liefert je Tag ein vorgegebenes Ergebnis. Fuer die Buchfuehrung."""

    def __init__(self, outcomes: dict[date, object]) -> None:
        super().__init__()
        self.outcomes = outcomes
        self.attempts: list[date] = []

    @asynccontextmanager
    async def browser(self):
        yield None

    async def search_with(self, browser, query: HotelQuery, *, pages: int = 1):
        self.attempts.append(query.arrival)
        await asyncio.sleep(0)
        outcome = self.outcomes[query.arrival]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_ten_days_keep_the_rate_and_never_open_more_than_two_pages():
    clock = VirtualClock()
    source = StubBooking(
        (FIXTURES / "booking_apollo_athen.html").read_text(encoding="utf-8"), clock
    )
    queries = days(10)
    results: list = []

    await clock.run(_collect(source.search_many(queries), results))
    starts = source.fake.starts

    # Zwei Seiten gleichzeitig, keine dritte.
    assert source.fake.peak == 2
    assert len(starts) == 10
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # 24 Anfragen je Minute sind 2,5 Sekunden Abstand, plus Jitter.
    assert min(gaps) >= 2.5
    assert (starts[-1] - starts[0]) >= 9 * 2.5
    # Und die Antwortzeit hat den Takt nicht mitgezogen: seriell waeren es
    # zehnmal 10 Sekunden Seitenzeit obendrauf.
    assert (starts[-1] - starts[0]) < 10 * PAGE_SECONDS
    assert [result.arrival for result in results[0]] == [q.arrival for q in queries]
    assert all(len(result.offers) == 6 for result in results[0])


async def _collect(coro, sink: list) -> None:
    sink.append(await coro)


async def test_empty_broken_and_ok_are_counted_apart():
    from flightopt.hotels.sources.base import HotelBatch

    queries = days(3)
    source = ScriptedBooking(
        {
            queries[0].arrival: HotelBatch(parser="apollo", total_results=2),
            queries[1].arrival: HotelBatch(parser="apollo", total_results=0, empty=True),
            queries[2].arrival: LayoutBroken("booking: nichts wiedererkannt"),
        }
    )

    results = await source.search_many(queries)
    report = FetchReport.of(results)

    assert [result.status for result in results] == ["ok", "leer", "struktur"]
    assert (report.ok, report.empty, report.broken, report.failed) == (1, 1, 1, 0)
    # "Nichts gefunden" ist ein Ergebnis, "nicht wiedererkannt" ein Defekt.
    assert any("nichts wiedererkannt" in note for note in report.notes)
    assert report.days == 3


async def test_five_errors_in_a_row_end_the_run_instead_of_hammering_on():
    queries = days(12)
    source = ScriptedBooking(
        {query.arrival: LayoutBroken("booking: kaputt") for query in queries}
    )

    results = await source.search_many(queries)
    report = FetchReport.of(results)

    assert report.broken >= 5
    assert report.aborted >= 1
    # Der Beweis, dass der Lauf wirklich aufgehoert hat zu fragen.
    assert len(source.attempts) < len(queries)


async def test_a_block_stops_everything_at_once():
    queries = days(8)
    source = ScriptedBooking(
        {query.arrival: SourceBlocked("booking: HTTP 202 auf der Ergebnisseite")
         for query in queries}
    )

    results = await source.search_many(queries)
    report = FetchReport.of(results)

    # Eine WAF-Antwort ist keine Frage der Ausdauer: nach der ersten ist
    # Schluss. Mehr als die schon laufenden Seiten koennen es nicht werden.
    assert 1 <= report.failed <= source.limiter.concurrency
    assert report.aborted == len(queries) - report.failed
    assert results[0].status == "fehler"
