"""Der Tagesscan waermt den Preiskalender-Cache nebenbei auf.

Fuer gespeicherte Profile war eine eigene Vorwaermung angedacht. Sie waere
doppelte Arbeit: `dispatch_due_profiles` startet je faelligem Profil einen
kompletten Suchlauf, und `build_grid` schreibt jeden geholten Kalender unter
demselben Schluessel in den Cache, den eine spaetere Suche mit derselben Spec
wieder liest. Der Test haelt genau das fest - kein neuer Code, aber ab jetzt
eine Zusicherung statt einer Vermutung.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, SearchSpec, StayRange
from flightopt.jobs import runner as runner_module
from flightopt.jobs.daily import dispatch_due_profiles, save_profile
from flightopt.jobs.runner import JobRunner
from flightopt.search.grid import build_grid
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache

NOW = datetime(2026, 9, 5, 8, 0, 0)


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 12),
    )


class CountingCalendar:
    """Zaehlt jeden echten Kalenderabruf, damit ein Cache-Treffer sichtbar wird."""

    name = "counting"
    carrier = "FR"
    carriers = ("FR",)
    supports_calendar = True
    supports_search = False
    indicative = False
    accepts_max_stops = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
        self.calls.append((origin, destination))
        await asyncio.sleep(0)
        day = start
        out: dict[date, Money] = {}
        while day <= end:
            out[day] = Money(9900, currency)
            day += timedelta(days=1)
        return out


@pytest.fixture
def offline(monkeypatch):
    async def no_routes(sources, legs):
        return None

    async def fake_rates(conn, **kwargs):
        return Rates()

    monkeypatch.setattr(runner_module, "preload_routes", no_routes)
    monkeypatch.setattr(runner_module.fx_store, "current_rates", fake_rates)


async def test_a_daily_scan_leaves_the_calendar_cache_warm(offline, tmp_path, monkeypatch):
    path = tmp_path / "prewarm.db"
    conn = db.connect(path)
    saved = spec()
    save_profile(conn, "Athen Oktober", [saved], now=NOW)

    source = CountingCalendar()
    monkeypatch.setattr(
        runner_module, "build_catalogue", lambda wanted, *, conn, env=None: [source]
    )

    runner = JobRunner(str(path))
    jobs = dispatch_due_profiles(conn, runner, now=NOW)
    assert len(jobs) == 1

    await runner._tasks[jobs[0]["job_id"]]
    assert runner.result(jobs[0]["job_id"])["status"] == "done", "der Scan lief nicht durch"

    # Der Scan hat beide Beine wirklich geholt.
    assert source.calls == [("BER", "ATH"), ("ATH", "BER")]

    # Und eine Suche mit derselben Spec kommt danach ohne einen einzigen
    # Abruf aus - genau das, was eine eigene Vorwaermung geleistet haette.
    later = CountingCalendar()
    fresh = db.connect(path)
    grid, report = await build_grid(saved, [later], cache=SqliteCache(fresh))

    assert later.calls == [], "der Kalender wurde trotz warmem Cache neu geholt"
    assert report.cache_hits == 2
    assert report.calls == 0
    assert report.filled[0] > 0 and report.filled[1] > 0
    assert grid[0] and grid[1]


async def test_without_the_scan_the_same_search_has_to_fetch(offline, tmp_path):
    """Gegenprobe: der Treffer oben kommt vom Scan, nicht von einem leeren Cache."""
    path = tmp_path / "cold.db"
    conn = db.connect(path)

    source = CountingCalendar()
    _, report = await build_grid(spec(), [source], cache=SqliteCache(conn))

    assert source.calls == [("BER", "ATH"), ("ATH", "BER")]
    assert report.cache_hits == 0
    assert report.calls == 2
