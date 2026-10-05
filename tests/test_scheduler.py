"""The daily scan scheduler only dispatches due saved profiles."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, SearchSpec
from flightopt.hotels import watch
from flightopt.hotels.browser import reset_shared_pools, shared_pool
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.sources.base import HotelBatch, HotelSource
from flightopt.jobs import scheduler as scheduler_module
from flightopt.jobs.daily import save_profile
from flightopt.jobs.scheduler import DailyScanScheduler
from flightopt.storage import db


class ClosingStub(HotelSource):
    """Eine Quelle, die mitzaehlt, wie oft sie freigegeben wurde."""

    name = "booking"

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    async def search(self, query: HotelQuery) -> HotelBatch:
        return HotelBatch(
            offers=[
                HotelOffer(
                    source=self.name,
                    property_key="booking:1",
                    name="Hotel Grande",
                    arrival=query.arrival,
                    departure=query.departure,
                    price_total=Money(9000, "EUR"),
                    stars=4,
                    city="Athens",
                    country="Greece",
                    party_size=query.party_size,
                )
            ]
        )

    def close(self) -> None:
        self.closed += 1


class CountingLauncher:
    """Startet nie einen Browser. Der Test fragt nur, ob der Pool stehen bleibt."""

    def __init__(self) -> None:
        self.started = 0

    async def __call__(self):  # pragma: no cover - kein Test oeffnet eine Seite
        raise AssertionError("dieser Test startet keinen Browser")


async def no_network_rates(conn, **kwargs) -> Rates:
    """Kein Test fragt die EZB."""
    return Rates(base="EUR", rates={"USD": 1.10}, fetched_at=datetime(2026, 9, 5, 8, 0))


class Runner:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.started = []

    def _conn(self):
        return db.connect(self.db_path)

    def create(self, specs):
        return 99

    def start(self, job_id, specs, *, airlines=None):
        self.started.append((job_id, specs, airlines))


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"),),
        stays=(),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 3),
    )


@pytest.mark.asyncio
async def test_scheduler_tick_dispatches_due_profiles(tmp_path):
    path = str(tmp_path / "scheduler.db")
    conn = db.connect(path)
    save_profile(conn, "Athen", [spec()], now=datetime(2026, 9, 5, 8, 0, 0))
    conn.close()
    runner = Runner(path)
    scheduler = DailyScanScheduler(runner, now=lambda: datetime(2026, 9, 5, 8, 1, 0))

    jobs = await scheduler.run_once()

    assert jobs == [{"profile_id": 1, "job_id": 99, "name": "Athen"}]
    assert runner.started[0][1][0].route == "BER-ATH"


@pytest.mark.asyncio
async def test_scheduler_tick_is_quiet_when_nothing_is_due(tmp_path):
    path = str(tmp_path / "scheduler.db")
    runner = Runner(path)
    scheduler = DailyScanScheduler(runner, now=lambda: datetime(2026, 9, 5, 8, 1, 0))

    assert await scheduler.run_once() == []
    assert runner.started == []


def cache_rows(path: str) -> set[str]:
    conn = db.connect(path)
    try:
        return {row["cache_key"] for row in conn.execute("SELECT cache_key FROM price_cache")}
    finally:
        conn.close()


def seed_cache(path: str) -> None:
    """Ein abgelaufener und ein gueltiger Eintrag."""
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO price_cache(cache_key, source, payload, fetched_at, expires_at) "
        "VALUES(?,?,?,?,?)",
        ("alt", "ryanair", "{}", db.now(), db.expires(timedelta(hours=-1))),
    )
    conn.execute(
        "INSERT INTO price_cache(cache_key, source, payload, fetched_at, expires_at) "
        "VALUES(?,?,?,?,?)",
        ("frisch", "ryanair", "{}", db.now(), db.expires(timedelta(hours=6))),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_scheduler_tick_purges_expired_cache_rows(tmp_path):
    """`purge_expired` hatte keinen Aufrufer, der Cache wuchs nur.

    Ein abgelaufener Eintrag wird nie wieder gelesen, belegt aber weiter Platz
    in derselben Datei wie die Beobachtungshistorie.
    """
    path = str(tmp_path / "purge.db")
    seed_cache(path)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    await scheduler.run_once()

    assert cache_rows(path) == {"frisch"}


@pytest.mark.asyncio
async def test_a_broken_tick_does_not_kill_the_scheduler(tmp_path, caplog):
    """Ein Fehler im Versand beendete die Schleife lautlos fuer immer.

    Der Tagesplaner war danach tot, ohne dass irgendwo stand warum.
    """
    path = str(tmp_path / "boom.db")
    scheduler = DailyScanScheduler(Runner(path), interval_seconds=0)
    calls: list[int] = []

    async def run_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Tabelle weg")
        scheduler._task.cancel()
        return []

    scheduler.run_once = run_once
    scheduler.start()
    with pytest.raises(asyncio.CancelledError):
        await scheduler._task

    assert len(calls) == 2
    assert "Tabelle weg" in caplog.text


@pytest.mark.asyncio
async def test_the_tick_also_records_the_watchlist(tmp_path, monkeypatch):
    """Der Tagesplaner arbeitet beides ab: gespeicherte Suchen und Beobachtungen.

    Ohne diesen zweiten Auftrag lief der Planer mit `search_profile` ins Leere
    und die Preishistorie blieb stehen, wo sie war.
    """
    path = str(tmp_path / "tick.db")
    seen: list[str] = []

    async def fake_run(conn, *, now=None):
        seen.append(str(now))
        return {"routes": 1, "observations": 60, "calls": 1, "errors": [], "due_left": 0}

    monkeypatch.setattr(scheduler_module, "run_watchlist", fake_run)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    report = await scheduler.tick()

    assert seen == ["2026-09-05 08:00:00"]
    assert report["watchlist"]["observations"] == 60
    assert report["jobs"] == []


@pytest.mark.asyncio
async def test_a_broken_dispatch_still_records_the_watchlist(tmp_path, monkeypatch, caplog):
    """Zwei Auftraege, zwei Schicksale.

    Sonst haelt ein dauerhaft kaputter Versand die Aufzeichnung fuer immer an,
    und jeder Tag ohne Aufzeichnung ist unwiederbringlich.
    """
    path = str(tmp_path / "half.db")
    ran: list[int] = []

    async def fake_run(conn, *, now=None):
        ran.append(1)
        return {"routes": 1, "observations": 3, "calls": 1, "errors": [], "due_left": 0}

    async def broken():
        raise RuntimeError("Versand kaputt")

    monkeypatch.setattr(scheduler_module, "run_watchlist", fake_run)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))
    scheduler.run_once = broken

    report = await scheduler.tick()

    assert ran == [1]
    assert report["jobs"] == []
    assert report["watchlist"]["observations"] == 3
    assert "Versand kaputt" in caplog.text


@pytest.mark.asyncio
async def test_the_tick_also_runs_the_hunt(tmp_path, monkeypatch):
    """Dritter Auftrag neben Versand und Aufzeichnung."""
    path = str(tmp_path / "hunt.db")
    seen: list[str] = []

    async def fake_hunt(conn, *, now=None):
        seen.append(str(now))
        return {"routes": 1, "finds": 1, "alerts": [7]}

    monkeypatch.setattr(scheduler_module, "run_hunt", fake_hunt)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    report = await scheduler.tick()

    assert seen == ["2026-09-05 08:00:00"]
    assert report["hunt"]["alerts"] == [7]


@pytest.mark.asyncio
async def test_a_broken_hunt_leaves_the_recording_alone(tmp_path, monkeypatch, caplog):
    """Ein verpasster Fehltarif ist aergerlich, eine verpasste Aufzeichnung ist fort."""
    path = str(tmp_path / "brokenhunt.db")
    recorded: list[int] = []

    async def fake_run(conn, *, now=None):
        recorded.append(1)
        return {"routes": 1, "observations": 5, "calls": 1, "errors": [], "due_left": 0}

    async def broken_hunt(conn, *, now=None):
        raise RuntimeError("Jagd kaputt")

    monkeypatch.setattr(scheduler_module, "run_watchlist", fake_run)
    monkeypatch.setattr(scheduler_module, "run_hunt", broken_hunt)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    report = await scheduler.tick()

    assert recorded == [1]
    assert report["watchlist"]["observations"] == 5
    assert report["hunt"] == {}
    assert "Jagd kaputt" in caplog.text


# -- Der vierte Auftrag: die Hotelbeobachtung ------------------------------


def hotel_report(**extra) -> dict:
    """Der Rueckgabewert eines Hoteldurchgangs, im Zuschnitt von `run_hotel_watches`."""
    return {
        "watches": 1, "days": 7, "observations": 14, "errors": [], "due_left": 0,
        **extra,
    }


@pytest.mark.asyncio
async def test_the_tick_also_runs_the_hotel_watches(tmp_path, monkeypatch):
    """Vierter Auftrag neben Versand, Aufzeichnung und Jagd.

    Ohne ihn trieb nichts die Hotelschleife an: die Beobachtungen standen in
    der Tabelle und wurden nie faellig abgearbeitet. Und ohne fuenf
    Beobachtungen je Gruppe entsteht keine Baseline, also auch kein Urteil.
    """
    path = str(tmp_path / "hotels.db")
    seen: list[str] = []

    async def fake_hotels(conn, *, now=None):
        seen.append(str(now))
        return hotel_report()

    monkeypatch.setattr(scheduler_module, "run_hotel_watches", fake_hotels)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    report = await scheduler.tick()

    assert seen == ["2026-09-05 08:00:00"]
    assert report["hotels"]["observations"] == 14


@pytest.mark.asyncio
async def test_a_broken_hotel_pass_does_not_stop_the_flight_hunt(
    tmp_path, monkeypatch, caplog
):
    """Ein kaputter Hotellauf darf die Flugjagd nicht anhalten.

    Der Hotelweg faehrt einen Browser gegen fremde Server; er faellt oefter
    aus als der Rest. Riss er die Jagd mit, kostete ein kaputter Chromium
    jeden Fehltarif des Tages.
    """
    path = str(tmp_path / "brokenhotels.db")
    hunted: list[int] = []

    async def broken_hotels(conn, *, now=None):
        raise RuntimeError("Chromium weg")

    async def fake_hunt(conn, *, now=None):
        hunted.append(1)
        return {"routes": 1, "finds": 1, "alerts": [7]}

    monkeypatch.setattr(scheduler_module, "run_hotel_watches", broken_hotels)
    monkeypatch.setattr(scheduler_module, "run_hunt", fake_hunt)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    report = await scheduler.tick()

    assert hunted == [1]
    assert report["hunt"]["alerts"] == [7]
    assert report["hotels"] == {}
    assert "Chromium weg" in caplog.text


@pytest.mark.asyncio
async def test_a_broken_hunt_does_not_stop_the_hotel_pass(tmp_path, monkeypatch, caplog):
    """Und genau so herum. Vier Auftraege, vier Schicksale."""
    path = str(tmp_path / "brokenhunt2.db")

    async def broken_hunt(conn, *, now=None):
        raise RuntimeError("Jagd kaputt")

    async def fake_hotels(conn, *, now=None):
        return hotel_report(observations=3)

    monkeypatch.setattr(scheduler_module, "run_hunt", broken_hunt)
    monkeypatch.setattr(scheduler_module, "run_hotel_watches", fake_hotels)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    report = await scheduler.tick()

    assert report["hunt"] == {}
    assert report["hotels"]["observations"] == 3
    assert "Jagd kaputt" in caplog.text


@pytest.mark.asyncio
async def test_the_hotel_pass_frees_its_sources_but_keeps_the_browser(
    tmp_path, monkeypatch
):
    """Der Vertrag des Handbetriebs gilt auch fuer den Planer.

    `BookingSource.close()` gibt die HTTP-Sitzung frei und laesst den Browser
    ausdruecklich stehen: der Pool ist gemeinsam und ueberlebt die Quelle,
    damit der naechste Durchgang keinen neuen Chromium startet. Das ist kein
    Leck, und wer es "repariert", zahlt je Takt einen Browserstart.
    """
    path = str(tmp_path / "vertrag.db")
    conn = db.connect(path)
    watch.ensure_hotel_watch(conn)
    watch.add_watch(
        conn, "Athen", lead_min_days=10, lead_max_days=11,
        now=datetime(2026, 9, 5, 8, 0, 0),
    )
    conn.close()

    source = ClosingStub()
    monkeypatch.setattr(watch, "build_hotel_sources", lambda env=None: [source])
    monkeypatch.setattr(watch.fx_store, "current_rates", no_network_rates)
    launcher = CountingLauncher()
    reset_shared_pools()
    pool = shared_pool("booking", launcher)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: datetime(2026, 9, 5, 8, 0, 0))

    try:
        report = await scheduler.hotels_once()

        assert report["watches"] == 1
        assert report["observations"] == 2
        # Die Quelle wird freigegeben - genau einmal, im finally des Durchgangs.
        assert source.closed == 1
        # Der Pool des Prozesses steht noch. Waere er hier zu, startete der
        # naechste Takt einen zweiten Chromium.
        assert shared_pool("booking", launcher) is pool
    finally:
        reset_shared_pools()


@pytest.mark.asyncio
async def test_hotel_collection_does_not_overlap_manual_and_scheduled_runs(tmp_path, monkeypatch):
    scheduler = DailyScanScheduler(Runner(str(tmp_path / "overlap.db")))
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def collect(conn, *, now=None):
        calls.append(1)
        started.set()
        await release.wait()
        return hotel_report()

    monkeypatch.setattr(scheduler_module, "run_hotel_watches", collect)
    first = asyncio.create_task(scheduler.hotels_once())
    await started.wait()
    second = asyncio.create_task(scheduler.hotels_once())
    await asyncio.sleep(0)
    assert len(calls) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_watchlist_and_hunt_share_the_flight_collection_lock(tmp_path, monkeypatch):
    scheduler = DailyScanScheduler(Runner(str(tmp_path / "flightoverlap.db")))
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def collect(conn, *, now=None):
        calls.append("watch")
        started.set()
        await release.wait()
        return {}

    async def hunt(conn, *, now=None):
        calls.append("hunt")
        return {}

    monkeypatch.setattr(scheduler_module, "run_watchlist", collect)
    monkeypatch.setattr(scheduler_module, "run_hunt", hunt)
    first = asyncio.create_task(scheduler.collect_once())
    await started.wait()
    second = asyncio.create_task(scheduler.hunt_once())
    await asyncio.sleep(0)
    assert calls == ["watch"]
    release.set()
    await asyncio.gather(first, second)
    assert calls == ["watch", "hunt"]


@pytest.mark.asyncio
async def test_hotel_lane_starts_while_flight_collection_is_waiting(tmp_path):
    scheduler = DailyScanScheduler(Runner(str(tmp_path / "lanes.db")))
    hotel_started = asyncio.Event()

    async def slow_flights():
        await asyncio.wait_for(hotel_started.wait(), timeout=1)
        return {"observations": 2}

    async def hotels():
        hotel_started.set()
        return hotel_report()

    async def hunt():
        return {}

    scheduler.collect_once = slow_flights
    scheduler.hotels_once = hotels
    scheduler.hunt_once = hunt
    report = await scheduler.tick()
    assert report["watchlist"] == {"observations": 2}
    assert report["hotels"]["observations"] == 14


@pytest.mark.asyncio
async def test_cancelled_collection_releases_lock_and_closes_connection(tmp_path, monkeypatch):
    scheduler = DailyScanScheduler(Runner(str(tmp_path / "cancel.db")))
    started = asyncio.Event()
    connections = []

    async def collect(conn, *, now=None):
        connections.append(conn)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler_module, "run_hotel_watches", collect)
    task = asyncio.create_task(scheduler.hotels_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")

    async def next_collect(conn, *, now=None):
        return hotel_report(watches=0)

    monkeypatch.setattr(scheduler_module, "run_hotel_watches", next_collect)
    assert (await asyncio.wait_for(scheduler.hotels_once(), timeout=1))["watches"] == 0


@pytest.mark.asyncio
async def test_stopping_scheduler_cancels_both_background_lanes(tmp_path):
    scheduler = DailyScanScheduler(Runner(str(tmp_path / "stoplanes.db")))
    flights_started, hotels_started = asyncio.Event(), asyncio.Event()
    closed = []

    async def collect():
        flights_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append("flights")

    async def hotels():
        hotels_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append("hotels")

    scheduler.collect_once = collect
    scheduler.hotels_once = hotels
    scheduler.start()
    await asyncio.wait_for(asyncio.gather(flights_started.wait(), hotels_started.wait()), timeout=1)
    await scheduler.stop()
    assert sorted(closed) == ["flights", "hotels"]
    assert scheduler._task is None


@pytest.mark.asyncio
async def test_waiting_hotel_run_rechecks_due_watches(tmp_path, monkeypatch):
    path = str(tmp_path / "deduplicated.db")
    moment = datetime(2026, 10, 5, 8, 0)
    conn = db.connect(path)
    watch.add_watch(conn, "Athen", lead_min_days=14, lead_max_days=14, now=moment)
    conn.close()
    started, release = asyncio.Event(), asyncio.Event()

    class SlowSource(ClosingStub):
        calls = 0

        async def search(self, query):
            self.calls += 1
            started.set()
            await release.wait()
            return await super().search(query)

    source = SlowSource()
    monkeypatch.setattr(watch, "build_hotel_sources", lambda env=None: [source])
    monkeypatch.setattr(watch.fx_store, "current_rates", no_network_rates)
    scheduler = DailyScanScheduler(Runner(path), now=lambda: moment)
    first = asyncio.create_task(scheduler.hotels_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(scheduler.hotels_once())
    await asyncio.sleep(0)
    release.set()
    first_report, second_report = await asyncio.gather(first, second)
    assert first_report["watches"] == 1
    assert first_report["observations"] == 1
    assert second_report["watches"] == 0
    assert second_report["observations"] == 0
    assert source.calls == 1
    assert source.closed == 1
