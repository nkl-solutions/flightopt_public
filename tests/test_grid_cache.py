"""Der Kalender-Cache: ein Treffer muss zur gestellten Frage gehoeren.

Der Schluessel enthaelt bewusst jeden Parameter, der die Antwort veraendert.
Beim Kalender veraendert nicht nur der Preis die Antwort, sondern auch ihr
Umfang - und genau der stand bisher nicht darin.
"""

from __future__ import annotations

from datetime import date, timedelta

from flightopt.domain.models import LegSpec, Money, SearchSpec
from flightopt.search.grid import build_grid
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache

FIRST = date(2026, 10, 1)


class Calendar:
    """Eine Quelle, die genau das Fenster beantwortet, nach dem gefragt wird."""

    name = "stub"
    supports_calendar = True
    carrier = "XX"
    indicative = False

    def __init__(self) -> None:
        self.windows: list[tuple[date, date]] = []

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
        self.windows.append((start, end))
        prices: dict[date, Money] = {}
        day = start
        while day <= end:
            prices[day] = Money(9900, currency)
            day += timedelta(days=1)
        return prices


def spec_over(days: int) -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"),),
        stays=(),
        window_start=FIRST,
        window_end=FIRST + timedelta(days=days - 1),
    )


async def test_a_wider_window_does_not_take_the_narrow_windows_calendar(tmp_path):
    """Im Schluessel stand der Fensteranfang, nicht das Fensterende.

    Eine Suche ueber drei Tage fuellte den Cache mit drei Tagen. Eine spaetere
    ueber zehn traf denselben Schluessel, sah eine nicht-leere Antwort und
    fragte gar nicht erst nach: das Gitter bekam drei Tage, `calls` blieb bei
    null und `errors` leer. Die duenne Abdeckung sah aus wie Angebotsmangel.
    """
    conn = db.connect(tmp_path / "grid.db")
    source = Calendar()
    cache = SqliteCache(conn)

    try:
        narrow, _ = await build_grid(spec_over(3), [source], cache=cache)
        wide, report = await build_grid(spec_over(10), [source], cache=cache)
    finally:
        conn.close()

    assert len(narrow[0]) == 3
    assert len(wide[0]) == 10
    assert [(lo, hi) for lo, hi in source.windows] == [
        (FIRST, FIRST + timedelta(days=2)),
        (FIRST, FIRST + timedelta(days=9)),
    ]
    assert report.cache_hits == 0


async def test_the_same_window_asked_twice_is_still_one_call(tmp_path):
    """Die Gegenprobe: der Cache soll ja etwas sparen."""
    conn = db.connect(tmp_path / "same.db")
    source = Calendar()
    cache = SqliteCache(conn)

    try:
        await build_grid(spec_over(5), [source], cache=cache)
        grid, report = await build_grid(spec_over(5), [source], cache=cache)
    finally:
        conn.close()

    assert len(source.windows) == 1
    assert len(grid[0]) == 5
    assert report.cache_hits == 1
