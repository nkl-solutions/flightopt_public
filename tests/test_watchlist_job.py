"""Der Tageslauf der Beobachtungsliste: Kalender einsammeln, sonst nichts."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from flightopt.domain.models import Money
from flightopt.jobs.watchlist import MAX_ROUTES_PER_RUN, run_watchlist
from flightopt.sources.base import SourceError
from flightopt.storage import db
from flightopt.storage.watchlist import add_route, get_route, list_routes, set_enabled


class FakeCalendar:
    """Eine Quelle mit Kalender und ohne Netz."""

    name = "fake"
    carrier = "XX"
    supports_calendar = True
    supports_search = False
    indicative = False
    accepts_max_stops = False

    def __init__(self, *, days: int = 3, error: Exception | None = None) -> None:
        self.days = days
        self.error = error
        self.asked: list[tuple[str, str, date, date]] = []
        self.closed = 0

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def calendar_range(self, origin, destination, start, end, **kwargs):
        self.asked.append((origin, destination, start, end))
        if self.error is not None:
            raise self.error
        return {
            start + timedelta(days=i): Money(12000 + i * 100, "EUR")
            for i in range(self.days)
        }

    def close(self) -> None:
        self.closed += 1


def observations(conn, entity_key: str = "BER|ATH") -> list[tuple]:
    return [
        (row["source"], row["entity_key"], row["travel_date"],
         row["price_total_minor"], row["is_estimate"])
        for row in conn.execute(
            "SELECT source, entity_key, travel_date, price_total_minor, is_estimate "
            "FROM price_observation WHERE entity_key=? ORDER BY travel_date",
            (entity_key,),
        )
    ]


@pytest.mark.asyncio
async def test_a_watched_route_writes_its_calendar_into_the_history(tmp_path):
    conn = db.connect(tmp_path / "collect.db")
    add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=40)
    source = FakeCalendar(days=3)
    now = datetime(2026, 9, 5, 8, 0, 0)

    report = await run_watchlist(conn, sources=[source], rates=None, now=now)

    rows = observations(conn)
    assert len(rows) == 3
    assert rows[0][0] == "fake"
    assert rows[0][1] == "BER|ATH"
    # Ein Kalenderpreis ist eine Schaetzung, nicht der Tarif eines Fluges.
    assert rows[0][4] == 1
    assert report["observations"] == 3
    assert report["routes"] == 1
    assert report["errors"] == []
    assert get_route(conn, list_routes(conn)[0].id).last_run_at == "2026-09-05T08:00:00"


@pytest.mark.asyncio
async def test_the_asked_window_rolls_with_the_day(tmp_path):
    """Das Fenster kommt aus dem Vorlauf, nicht aus einem gespeicherten Datum."""
    conn = db.connect(tmp_path / "roll.db")
    add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=40)
    source = FakeCalendar(days=1)

    await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 5, 8, 0, 0)
    )

    assert source.asked == [("BER", "ATH", date(2026, 9, 15), date(2026, 10, 15))]


@pytest.mark.asyncio
async def test_a_route_is_asked_once_a_day(tmp_path):
    """Dieselben Quellen wie die Suche: eine Sperre traefe beide."""
    conn = db.connect(tmp_path / "once.db")
    add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=40)
    source = FakeCalendar(days=2)

    await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 5, 8, 0, 0)
    )
    again = await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 5, 20, 0, 0)
    )

    assert len(source.asked) == 1
    assert again["routes"] == 0
    assert len(observations(conn)) == 2

    tomorrow = await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 6, 8, 0, 0)
    )

    assert len(source.asked) == 2
    assert tomorrow["routes"] == 1


@pytest.mark.asyncio
async def test_a_switched_off_route_is_left_alone(tmp_path):
    conn = db.connect(tmp_path / "off.db")
    route_id = add_route(conn, "BER", "ATH")
    set_enabled(conn, route_id, False)
    source = FakeCalendar(days=2)

    report = await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 5, 8, 0, 0)
    )

    assert source.asked == []
    assert report["routes"] == 0
    assert observations(conn) == []


@pytest.mark.asyncio
async def test_a_stubborn_source_does_not_stop_the_next_route(tmp_path):
    """Eine stumme Strecke gilt trotzdem als gelaufen.

    Sonst versucht dieselbe Zeile es im naechsten Durchgang wieder, und aus
    einem Fehlschlag wird ein Dauerfeuer gegen eine Quelle, die ohnehin nichts
    hergibt.
    """
    conn = db.connect(tmp_path / "boom.db")
    add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=40)
    add_route(conn, "BER", "FCO", lead_min_days=10, lead_max_days=40)
    now = datetime(2026, 9, 5, 8, 0, 0)

    class Picky(FakeCalendar):
        async def calendar_range(self, origin, destination, start, end, **kwargs):
            if destination == "ATH":
                raise SourceError("fake: 403")
            return await FakeCalendar.calendar_range(
                self, origin, destination, start, end, **kwargs
            )

    report = await run_watchlist(conn, sources=[Picky(days=2)], rates=None, now=now)

    assert observations(conn, "BER|ATH") == []
    assert len(observations(conn, "BER|FCO")) == 2
    assert report["observations"] == 2
    assert any("BER-ATH" in note for note in report["errors"])
    assert all(route.last_run_at == "2026-09-05T08:00:00" for route in list_routes(conn))


@pytest.mark.asyncio
async def test_one_tick_only_takes_a_handful_of_routes(tmp_path):
    """Der Rest bleibt faellig und kommt im naechsten Durchgang dran."""
    conn = db.connect(tmp_path / "cap.db")
    for code in ("ATH", "FCO", "MAD", "LIS", "OSL", "ARN", "HEL", "DUB",
                 "PRG", "VIE", "ZRH", "CDG"):
        add_route(conn, "BER", code, lead_min_days=10, lead_max_days=12)
    assert len(list_routes(conn)) > MAX_ROUTES_PER_RUN
    source = FakeCalendar(days=1)

    report = await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 5, 8, 0, 0)
    )

    assert report["routes"] == MAX_ROUTES_PER_RUN
    assert len(source.asked) == MAX_ROUTES_PER_RUN
    assert report["due_left"] == len(list_routes(conn)) - MAX_ROUTES_PER_RUN


@pytest.mark.asyncio
async def test_the_run_refreshes_the_flight_baselines(tmp_path):
    """Ohne Nachrechnen bleibt die Preislage leer, obwohl die Zeilen da sind."""
    conn = db.connect(tmp_path / "baseline.db")
    add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=40)
    travel = date(2026, 10, 1)
    for day in (2, 3, 4, 6):
        conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, party_size, "
            "currency, price_total_minor, is_estimate) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                datetime(2026, 9, day, 8, 0, 0).isoformat(timespec="seconds"),
                "fake", "flight", "BER|ATH", travel.isoformat(), 1, "EUR",
                22000 + day * 10, 1,
            ),
        )

    class OneDay(FakeCalendar):
        async def calendar_range(self, origin, destination, start, end, **kwargs):
            self.asked.append((origin, destination, start, end))
            return {travel: Money(22500, "EUR")}

    await run_watchlist(
        conn, sources=[OneDay()], rates=None, now=datetime(2026, 9, 5, 8, 0, 0)
    )

    rows = conn.execute(
        "SELECT n FROM flight_baseline WHERE entity_key='BER|ATH'"
    ).fetchall()
    assert [int(row["n"]) for row in rows] == [5]


@pytest.mark.asyncio
async def test_an_empty_watchlist_asks_nobody_anything(tmp_path):
    """Kein Eintrag, kein Abruf, und auch kein gebauter Quellen-Katalog."""
    conn = db.connect(tmp_path / "empty.db")
    source = FakeCalendar()

    report = await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 5, 8, 0, 0)
    )

    assert report == {
        "routes": 0, "observations": 0, "calls": 0, "errors": [], "due_left": 0,
        "finds": 0, "alerts": [],
    }
    assert source.asked == []


@pytest.mark.asyncio
async def test_the_route_network_is_asked_once_per_airport(tmp_path):
    """Zwei Strecken ab Berlin sind ein Streckennetz, nicht zwei.

    Nicht jede Quelle merkt sich ihre Antwort, und der Abruf zaehlt bei
    denselben Servern wie die Preisabfrage.
    """
    conn = db.connect(tmp_path / "preload.db")
    add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=12)
    add_route(conn, "BER", "FCO", lead_min_days=10, lead_max_days=12)

    class Mapped(FakeCalendar):
        def __init__(self) -> None:
            FakeCalendar.__init__(self, days=1)
            self.loaded: list[str] = []

        async def load_routes(self, origin: str) -> set[str]:
            self.loaded.append(origin)
            return {"ATH", "FCO"}

    source = Mapped()

    await run_watchlist(
        conn, sources=[source], rates=None, now=datetime(2026, 9, 5, 8, 0, 0)
    )

    assert source.loaded == ["BER"]
    assert len(source.asked) == 2
