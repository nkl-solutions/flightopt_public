"""HTTP handler behavior for saved search profiles."""

from __future__ import annotations

import base64
from datetime import date

import pytest

from flightopt.api import main
from flightopt.jobs.scheduler import DailyScanScheduler
from flightopt.jobs.runner import JobRunner


@pytest.mark.asyncio
async def test_profile_endpoint_saves_expanded_search(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "profiles.db")))
    req = main.ProfileRequest(
        name="Ost nach Athen",
        airports=["DE-OST", "ATH"],
        trip="one_way",
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 3),
    )

    response = await main.save_profile_endpoint(req)

    assert response["name"] == "Ost nach Athen"
    assert response["variants"] == 3
    assert response["next_run_at"]


@pytest.mark.asyncio
async def test_dispatch_endpoint_starts_due_profiles(monkeypatch, tmp_path):
    started = []

    class Runner(JobRunner):
        def start(self, job_id, specs, *, airlines=None):
            started.append((job_id, specs, airlines))

    runner = Runner(str(tmp_path / "dispatch.db"))
    monkeypatch.setattr(main, "runner", runner)
    await main.save_profile_endpoint(
        main.ProfileRequest(
            name="Athen",
            airports=["BER", "ATH"],
            trip="one_way",
            window_start=date(2026, 10, 1),
            window_end=date(2026, 10, 3),
        )
    )

    response = await main.dispatch_profiles_endpoint()

    assert response["jobs"][0]["name"] == "Athen"
    assert started[0][1][0].route == "BER-ATH"


@pytest.mark.asyncio
async def test_scheduler_status_endpoint_reports_controlled_state(monkeypatch, tmp_path):
    scheduler = DailyScanScheduler(JobRunner(str(tmp_path / "scheduler.db")))
    monkeypatch.setattr(main, "daily_scheduler", scheduler)

    response = await main.scheduler_status_endpoint()

    assert response == {"running": False, "interval_seconds": 600}


def test_scheduler_autostart_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("FLIGHTOPT_DAILY_SCANS", raising=False)

    assert main.scheduler_autostart_enabled() is True


def test_basic_auth_is_off_until_credentials_are_set(monkeypatch):
    monkeypatch.delenv("FLIGHTOPT_BASIC_USER", raising=False)
    monkeypatch.delenv("FLIGHTOPT_BASIC_PASSWORD", raising=False)

    assert main.basic_auth_config() is None
    assert main.basic_auth_valid(None) is True


def test_basic_auth_accepts_matching_header(monkeypatch):
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.setenv("FLIGHTOPT_BASIC_PASSWORD", "secret")
    encoded = base64.b64encode(b"dev:secret").decode("ascii")

    assert main.basic_auth_valid(f"Basic {encoded}") is True
    assert main.basic_auth_valid("Basic nope") is False


def test_basic_auth_requires_complete_configuration(monkeypatch):
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.delenv("FLIGHTOPT_BASIC_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="must be set together"):
        main.basic_auth_config()


@pytest.mark.asyncio
async def test_startup_starts_scheduler_when_enabled(monkeypatch):
    class Scheduler:
        def __init__(self) -> None:
            self.started = False

        def start(self):
            self.started = True

    scheduler = Scheduler()
    monkeypatch.setattr(main, "daily_scheduler", scheduler)
    monkeypatch.setenv("FLIGHTOPT_DAILY_SCANS", "1")

    await main.start_daily_scheduler()

    assert scheduler.started is True
