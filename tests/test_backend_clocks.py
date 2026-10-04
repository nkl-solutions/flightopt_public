"""Injected job clocks govern stored prices, baselines and exchange-rate TTLs."""

from datetime import datetime, timedelta

import pytest

from flightopt.domain import fx
from flightopt.domain.fx import Rates
from flightopt.domain.models import Money
from flightopt.hunt import cadence
from flightopt.jobs.hunt import run_hunt
from flightopt.jobs.watchlist import collect_route, run_watchlist
from flightopt.storage import db, fx_store, watchlist
from flightopt.storage.baseline import detect_price_signal
from flightopt.storage.cache import SqliteHistory


class Calendar:
    name = "clock-test"
    supports_calendar = True
    indicative = False

    def __init__(self):
        self.calls = 0

    def supports_route(self, origin, destination):
        return True

    async def calendar_range(self, origin, destination, start, end, **kwargs):
        self.calls += 1
        return {start: Money(20000, "EUR")}


MOMENTS = [datetime(2001, 9, 5, 8), datetime(2099, 9, 5, 8)]


@pytest.mark.parametrize("moment", MOMENTS, ids=["past", "future"])
@pytest.mark.parametrize("run", [run_watchlist, run_hunt], ids=["daily", "hunt"])
async def test_job_clock_controls_history_baseline_and_cache_reuse(tmp_path, run, moment):
    conn = db.connect(tmp_path / "clock.db")
    try:
        route_id = watchlist.add_route(
            conn, "BER", "ATH", lead_min_days=14, lead_max_days=40,
        )
        if run is run_hunt:
            watchlist.set_cadence(conn, route_id, cadence.HOT)
        travel = moment.date() + timedelta(days=14)
        history = SqliteHistory(conn)
        for days in range(1, 5):
            await history.record(
                source="clock-test", entity_type="flight", entity_key="BER|ATH",
                travel_date=travel, price=Money(20000, "EUR"), is_estimate=True,
                observed_at=moment - timedelta(days=days),
            )
        source = Calendar()
        rates = Rates(fetched_at=moment)

        report = await run(conn, sources=[source], rates=rates, now=moment, env={})

        assert report["errors"] == []
        assert report["observations"] == 1
        assert conn.execute(
            "SELECT observed_at FROM price_observation ORDER BY id DESC LIMIT 1"
        ).fetchone()["observed_at"] == moment.isoformat(timespec="seconds")
        baseline = conn.execute("SELECT * FROM flight_baseline").fetchone()
        assert baseline["n"] == 5
        assert baseline["leadtime_bucket"] == "14-29"
        assert baseline["computed_at"] == moment.isoformat(timespec="seconds")
        assert detect_price_signal(
            conn, "BER|ATH", travel, 20000, observed_at=moment, is_estimate=True,
        )["status"] == "normal"

        later = moment + timedelta(minutes=1)
        cached = await collect_route(
            conn, watchlist.get_route(conn, route_id), [source], rates=rates,
            now=later, max_cache_age=cadence.MAX_CACHE_AGE,
        )

        assert cached["cache_hits"] == 1
        assert source.calls == 1
        assert conn.execute(
            "SELECT observed_at FROM price_observation ORDER BY id DESC LIMIT 1"
        ).fetchone()["observed_at"] == later.isoformat(timespec="seconds")
        assert conn.execute("SELECT fetched_at FROM price_cache").fetchone()[
            "fetched_at"
        ] == moment.isoformat(timespec="seconds")
    finally:
        conn.close()


@pytest.mark.parametrize("moment", MOMENTS, ids=["past", "future"])
@pytest.mark.parametrize("run", [run_watchlist, run_hunt], ids=["daily", "hunt"])
async def test_job_clock_reuses_fresh_exchange_rates(tmp_path, monkeypatch, run, moment):
    conn = db.connect(tmp_path / "fx-clock.db")
    try:
        route_id = watchlist.add_route(conn, "BER", "ATH")
        if run is run_hunt:
            watchlist.set_cadence(conn, route_id, cadence.HOT)
        fx_store.save_rates(conn, Rates(rates={"USD": 1.1}), now=moment)
        fetches = []

        async def unavailable(client=None):
            fetches.append(client)
            raise OSError("No network in clock tests")

        monkeypatch.setattr(fx, "fetch_ecb_rates", unavailable)

        report = await run(conn, sources=[Calendar()], now=moment, env={})

        assert report["observations"] == 1
        assert fetches == []
    finally:
        conn.close()
