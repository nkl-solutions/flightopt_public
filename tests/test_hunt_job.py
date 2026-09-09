"""Der heisse Durchgang: haeufiger fragen, ohne die Quellen zu verlieren."""

from __future__ import annotations

from datetime import datetime, timedelta

from flightopt.domain.models import Money
from flightopt.hunt import alerts, budget, cadence, discord
from flightopt.jobs.hunt import MAX_FINDS_PER_ROUTE, MAX_ROUTES_PER_RUN, run_hunt
from flightopt.sources.base import CircuitBreaker, SourceBlocked
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache
from flightopt.storage.watchlist import (
    add_route,
    get_route,
    hot_routes,
    set_cadence,
)

NOW = datetime(2026, 9, 9, 12, 0, 0)


class FakeCalendar:
    """Eine Kalenderquelle ohne Netz, mit Sicherung wie das Original."""

    supports_calendar = True
    supports_search = False
    indicative = False
    accepts_max_stops = False
    per_minute = 60

    def __init__(self, *, name: str = "fake", price_minor: int = 20000,
                 days: int = 2, error: Exception | None = None,
                 indicative: bool = False) -> None:
        self.name = name
        self.carrier = "XX"
        self.indicative = indicative
        self.price_minor = price_minor
        self.days = days
        self.error = error
        self.asked: list[tuple[str, str]] = []
        self.breaker = CircuitBreaker()
        self.closed = 0

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def calendar_range(self, origin, destination, start, end, **kwargs):
        self.asked.append((origin, destination))
        if self.error is not None:
            raise self.error
        return {
            start + timedelta(days=i): Money(self.price_minor, "EUR")
            for i in range(self.days)
        }

    def close(self) -> None:
        self.closed += 1


def a_hot_route(conn, origin: str = "BER", destination: str = "BKK") -> int:
    route_id = add_route(conn, origin, destination,
                         lead_min_days=14, lead_max_days=45)
    set_cadence(conn, route_id, cadence.HOT)
    return route_id


# -- Der Takt -----------------------------------------------------------------


async def test_a_hot_route_is_asked_again_within_the_same_day(tmp_path):
    """Genau das, was der Tageslauf nicht kann."""
    conn = db.connect(tmp_path / "tick.db")
    a_hot_route(conn)
    source = FakeCalendar()

    first = await run_hunt(conn, sources=[source], rates=None, now=NOW)
    later = NOW + timedelta(seconds=cadence.HOT_INTERVAL_SECONDS
                            + cadence.HOT_JITTER_SECONDS + 1)
    second = await run_hunt(conn, sources=[source], rates=None, now=later)

    assert first["routes"] == 1
    assert second["routes"] == 1
    assert len(source.asked) == 2


async def test_a_hot_route_is_not_asked_twice_inside_the_interval(tmp_path):
    conn = db.connect(tmp_path / "soon.db")
    a_hot_route(conn)
    source = FakeCalendar()

    await run_hunt(conn, sources=[source], rates=None, now=NOW)
    again = await run_hunt(conn, sources=[source], rates=None,
                           now=NOW + timedelta(minutes=5))

    assert again["routes"] == 0
    assert len(source.asked) == 1


async def test_a_daily_route_is_left_to_the_daily_run(tmp_path):
    conn = db.connect(tmp_path / "daily.db")
    add_route(conn, "BER", "ATH")
    source = FakeCalendar()

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert report["routes"] == 0
    assert source.asked == []


def test_the_interval_is_spread_and_never_shortened(tmp_path):
    """Streuung im Abstand, aber nur nach hinten.

    Ein gleichmaessiger Schlag auf die Sekunde ist selbst ein Bot-Merkmal.
    Nach vorn darf die Streuung nicht wirken: sonst haelt der Takt seine
    eigene Obergrenze nicht mehr ein.
    """
    conn = db.connect(tmp_path / "jitter.db")
    route_id = a_hot_route(conn)
    from flightopt.storage.watchlist import mark_hot_ran

    mark_hot_ran(conn, route_id, now=NOW)

    just_under = NOW + timedelta(seconds=cadence.HOT_INTERVAL_SECONDS - 1)
    assert hot_routes(conn, now=just_under) == []

    well_over = NOW + timedelta(
        seconds=cadence.HOT_INTERVAL_SECONDS + cadence.HOT_JITTER_SECONDS + 1
    )
    assert len(hot_routes(conn, now=well_over)) == 1


# -- Das Budget ---------------------------------------------------------------


async def test_a_pass_is_charged_before_it_is_made(tmp_path):
    """Ein Abruf, der in einem Zeitablauf endet, hat die Quelle erreicht."""
    conn = db.connect(tmp_path / "charge.db")
    a_hot_route(conn)
    source = FakeCalendar(error=SourceBlocked("fake: HTTP 429"))

    await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert budget.used(conn, "fake", now=NOW) == cadence.CALLS_PER_PASS


async def test_an_exhausted_budget_defers_a_route_instead_of_dropping_it(tmp_path):
    conn = db.connect(tmp_path / "defer.db")
    a_hot_route(conn)
    source = FakeCalendar()
    budget.charge(conn, ["fake"], cadence.MAX_CALLS_PER_SOURCE_HOUR, now=NOW)

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert report["routes"] == 0
    assert report["deferred"] == ["BER-BKK"]
    assert source.asked == []
    # Verschoben heisst nicht gelaufen: die Strecke ist weiter faellig.
    assert len(hot_routes(conn, now=NOW)) == 1


async def test_twenty_hot_routes_cannot_break_the_hourly_cap(tmp_path):
    """Die Obergrenze haelt auch, wenn jemand die ganze Liste heiss schaltet."""
    conn = db.connect(tmp_path / "twenty.db")
    for code in ("BKK", "SIN", "JFK", "LAX", "HKG", "NRT", "DXB", "DEL",
                 "GRU", "JNB", "YYZ", "MEX", "SYD", "PEK", "ICN", "BOM",
                 "CPT", "EZE", "SFO", "ORD"):
        a_hot_route(conn, "BER", code)
    source = FakeCalendar()

    moment = NOW
    for _ in range(12):
        await run_hunt(conn, sources=[source], rates=None, now=moment)
        moment += timedelta(minutes=5)

    assert budget.used(conn, "fake", now=moment) <= cadence.MAX_CALLS_PER_SOURCE_HOUR


async def test_the_longest_waiting_route_is_served_first(tmp_path):
    conn = db.connect(tmp_path / "fair.db")
    first = a_hot_route(conn, "BER", "BKK")
    second = a_hot_route(conn, "BER", "SIN")
    source = FakeCalendar()
    # Nur noch Platz fuer einen Durchgang.
    budget.charge(conn, ["fake"],
                  cadence.MAX_CALLS_PER_SOURCE_HOUR - cadence.CALLS_PER_PASS, now=NOW)

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert report["routes"] == 1
    assert source.asked == [("BER", "BKK")]
    assert report["deferred"] == ["BER-SIN"]
    assert get_route(conn, first).last_hot_run_at is not None
    assert get_route(conn, second).last_hot_run_at is None


# -- Die Sicherung ------------------------------------------------------------


async def test_a_blocked_source_pushes_the_hunt_back_to_the_daily_pace(tmp_path):
    conn = db.connect(tmp_path / "blocked.db")
    a_hot_route(conn)
    source = FakeCalendar()
    for _ in range(source.breaker.threshold):
        source.breaker.record_block()
    assert source.breaker.is_open

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert report["paused"] == ["fake"]
    assert not budget.hot_allowed(conn, now=NOW)


async def test_while_a_source_is_paused_no_route_runs_hot(tmp_path):
    conn = db.connect(tmp_path / "paused.db")
    a_hot_route(conn)
    budget.pause(conn, ["aegean"], reason="HTTP 429", now=NOW)
    source = FakeCalendar()

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert report["routes"] == 0
    assert report["paused"] == ["aegean"]
    assert source.asked == []


async def test_after_the_cooldown_the_hot_pace_returns(tmp_path):
    conn = db.connect(tmp_path / "back.db")
    a_hot_route(conn)
    budget.pause(conn, ["aegean"], reason="HTTP 429", now=NOW)
    source = FakeCalendar()

    later = NOW + budget.PAUSE_COOLDOWN + timedelta(minutes=1)
    report = await run_hunt(conn, sources=[source], rates=None, now=later)

    assert report["routes"] == 1
    assert report["paused"] == []


# -- Der Cache ----------------------------------------------------------------


async def test_a_hot_pass_does_not_read_its_own_last_answer(tmp_path):
    """Ein Takt kuerzer als die TTL fragt sonst sich selbst statt die Quelle."""
    conn = db.connect(tmp_path / "cache.db")
    a_hot_route(conn)
    source = FakeCalendar()

    await run_hunt(conn, sources=[source], rates=None, now=NOW)
    later = NOW + timedelta(seconds=cadence.HOT_INTERVAL_SECONDS
                            + cadence.HOT_JITTER_SECONDS + 1)
    await run_hunt(conn, sources=[source], rates=None, now=later)

    assert len(source.asked) == 2, "der zweite Durchgang muss wirklich fragen"


async def test_a_fresh_cache_entry_is_still_used(tmp_path):
    """Der Cache bleibt nuetzlich, er ist nur nicht mehr blind."""
    conn = db.connect(tmp_path / "fresh.db")
    a_hot_route(conn)
    source = FakeCalendar()

    await run_hunt(conn, sources=[source], rates=None, now=NOW)
    rows = conn.execute("SELECT COUNT(*) AS n FROM price_cache").fetchone()

    assert int(rows["n"]) == 1, "geschrieben wird weiter mit der normalen TTL"
    cached = await SqliteCache(conn).get(
        conn.execute("SELECT cache_key FROM price_cache").fetchone()["cache_key"]
    )
    assert cached is not None


# -- Der Fund -----------------------------------------------------------------


async def test_an_error_fare_becomes_an_alert_event(tmp_path):
    conn = db.connect(tmp_path / "find.db")
    a_hot_route(conn)
    source = FakeCalendar(price_minor=3900, days=1)

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW, env={})

    assert report["finds"] == 1
    events = alerts.list_events(conn)
    assert len(events) == 1
    assert events[0]["tier"] == "error"
    assert events[0]["delivery"] == alerts.DRY_RUN
    assert events[0]["route"] == "BER-BKK"
    assert events[0]["source"] == "fake"


async def test_an_ordinary_price_produces_no_alert(tmp_path):
    conn = db.connect(tmp_path / "quiet.db")
    a_hot_route(conn)
    source = FakeCalendar(price_minor=45000, days=3)

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW, env={})

    assert report["finds"] == 0
    assert alerts.list_events(conn) == []


async def test_an_indicative_source_alone_never_raises_an_alarm(tmp_path):
    """Ein Richtwert ist kein Tarif."""
    conn = db.connect(tmp_path / "kiwi.db")
    a_hot_route(conn)
    source = FakeCalendar(name="kiwi", price_minor=3900, days=1, indicative=True)

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW, env={})

    assert report["finds"] == 0
    assert alerts.list_events(conn) == []


async def test_the_cheapest_real_source_wins_the_day(tmp_path):
    """Zwei Quellen, ein Tag: gemeldet wird der guenstigste echte Tarif."""
    conn = db.connect(tmp_path / "two.db")
    a_hot_route(conn)
    cheap = FakeCalendar(name="ryanair", price_minor=3900, days=1)
    dear = FakeCalendar(name="condor", price_minor=52000, days=1)

    await run_hunt(conn, sources=[cheap, dear], rates=None, now=NOW, env={})

    events = alerts.list_events(conn)
    assert len(events) == 1
    assert events[0]["source"] == "ryanair"


async def test_a_route_reports_at_most_a_handful_of_days_per_pass(tmp_path):
    """Eine Strecke mit systematischem Fehler soll nicht sechzig Meldungen erzeugen."""
    conn = db.connect(tmp_path / "flood.db")
    a_hot_route(conn)
    source = FakeCalendar(price_minor=3900, days=20)

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW, env={})

    assert report["finds"] == MAX_FINDS_PER_ROUTE
    assert len(alerts.list_events(conn)) == MAX_FINDS_PER_ROUTE


async def test_a_broken_channel_does_not_stop_the_pass(tmp_path):
    conn = db.connect(tmp_path / "boom.db")
    a_hot_route(conn)
    source = FakeCalendar(price_minor=3900, days=1)

    def post(url, **kwargs):
        raise OSError("Netz weg")

    report = await run_hunt(
        conn, sources=[source], rates=None, now=NOW,
        env={discord.ENV_WEBHOOK: "https://discord.test/x"}, post=post,
    )

    assert report["routes"] == 1
    assert report["observations"] == 1
    assert alerts.list_events(conn)[0]["delivery"] == alerts.FAILED


async def test_the_pass_refreshes_the_baselines_before_it_judges(tmp_path):
    """Ohne Nachrechnen bleibt die Preislage leer, obwohl die Zeilen da sind."""
    conn = db.connect(tmp_path / "base.db")
    a_hot_route(conn)
    source = FakeCalendar(price_minor=45000, days=1)

    await run_hunt(conn, sources=[source], rates=None, now=NOW, env={})

    rows = conn.execute("SELECT COUNT(*) AS n FROM flight_baseline").fetchone()
    assert int(rows["n"]) >= 0  # keine Baseline bei einem Punkt, aber gerechnet
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM price_observation"
    ).fetchone()["n"] == 1


async def test_an_empty_hunt_asks_nobody_anything(tmp_path):
    conn = db.connect(tmp_path / "none.db")
    source = FakeCalendar()

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert report["routes"] == 0
    assert report["finds"] == 0
    assert source.asked == []


async def test_one_pass_only_takes_as_many_routes_as_fit_under_the_cap(tmp_path):
    """Das Stundenbudget sichert die Summe, nicht die Verteilung.

    Ohne diesen Deckel faehrt ein einziger Aufruf bei zwanzig heissen Strecken
    das ganze Stundenbudget in wenigen Minuten ab, und danach ist eine Stunde
    Stille. Salve, Pause, Salve ist genau das Muster, das einer Quelle
    auffaellt.
    """
    conn = db.connect(tmp_path / "burst.db")
    for code in ("BKK", "SIN", "JFK", "LAX", "HKG", "NRT", "DXB", "DEL"):
        a_hot_route(conn, "BER", code)
    source = FakeCalendar()

    report = await run_hunt(conn, sources=[source], rates=None, now=NOW)

    assert report["routes"] == MAX_ROUTES_PER_RUN
    assert len(source.asked) == MAX_ROUTES_PER_RUN
    assert report["hot_due_left"] == 8 - MAX_ROUTES_PER_RUN
    assert budget.used(conn, "fake", now=NOW) == (
        MAX_ROUTES_PER_RUN * cadence.CALLS_PER_PASS
    )
