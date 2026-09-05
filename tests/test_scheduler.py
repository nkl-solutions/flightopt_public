"""The daily scan scheduler only dispatches due saved profiles."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from flightopt.domain.models import LegSpec, SearchSpec
from flightopt.jobs.daily import save_profile
from flightopt.jobs.scheduler import DailyScanScheduler
from flightopt.storage import db


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
