"""Fixed-window profiles must not collect expired or duplicate searches."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from flightopt.domain.models import LegSpec, SearchSpec, StayRange
from flightopt.jobs import daily
from flightopt.jobs.runner import JobRunner
from flightopt.storage import db


NOW = datetime(2026, 10, 5, 8)


def spec(end=date(2026, 10, 10), *, nights=3):
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(nights, nights + 2),),
        window_start=date(2026, 10, 1), window_end=end,
    )


class RecordingRunner(JobRunner):
    def __init__(self, path):
        super().__init__(str(path))
        self.started = []

    def start(self, job_id, specs, *, airlines=None):
        self.started.append((job_id, specs, airlines))


def test_expired_profiles_are_retained_but_never_dispatched(tmp_path):
    path = tmp_path / "profiles.db"
    conn = db.connect(path)
    profile_id = daily.save_profile(conn, "Vergangen", [spec(date(2026, 10, 4))], now=NOW)
    runner = RecordingRunner(path)
    assert daily.due_profiles(conn, now=NOW) == []
    assert daily.dispatch_due_profiles(conn, runner, now=NOW) == []
    row = conn.execute("SELECT * FROM search_profile WHERE id=?", (profile_id,)).fetchone()
    assert row["enabled"] == 1
    assert row["last_run_at"] is None
    assert runner.started == []


def test_minimum_stays_make_window_expired_before_last_day(tmp_path):
    conn = db.connect(tmp_path / "stays.db")
    daily.save_profile(conn, "Zu kurz", [spec(date(2026, 10, 7), nights=3)], now=NOW)
    assert daily.due_profiles(conn, now=NOW) == []


def test_dispatch_trims_past_dates_without_changing_saved_window(tmp_path):
    path = tmp_path / "trim.db"
    conn = db.connect(path)
    original = spec()
    daily.save_profile(conn, "Athen", [original], now=NOW)
    runner = RecordingRunner(path)
    daily.dispatch_due_profiles(conn, runner, now=NOW)
    assert runner.started[0][1] == [replace(original, window_start=NOW.date())]
    assert daily.list_profiles(conn, now=NOW)[0]["window_start"] == "2026-10-01"


def test_profiles_report_expired_paused_due_and_scheduled(tmp_path):
    conn = db.connect(tmp_path / "list.db")
    expired = daily.save_profile(conn, "Alt", [spec(date(2026, 10, 4))], now=NOW)
    paused = daily.save_profile(conn, "Pause", [spec()], now=NOW)
    scheduled = daily.save_profile(conn, "Morgen", [spec()], now=NOW)
    due = daily.save_profile(conn, "Jetzt", [spec()], now=NOW)
    daily.update_profile(conn, paused, enabled=False)
    daily.mark_scanned(conn, scheduled, now=NOW)
    rows = {row["id"]: row for row in daily.list_profiles(conn, now=NOW)}
    assert {key: rows[key]["status"] for key in rows} == {
        expired: "expired", paused: "paused", scheduled: "scheduled", due: "due",
    }
    assert rows[due]["routes"] == ["BER-ATH-BER"]
    assert rows[due]["last_run_at"] is None
    assert rows[scheduled]["next_run_at"] == "2026-10-06T08:00:00"


def test_edit_profile_preserves_route_and_history(tmp_path):
    conn = db.connect(tmp_path / "edit.db")
    profile_id = daily.save_profile(conn, "Alt", [spec()], airlines=["FR"], now=NOW)
    daily.mark_scanned(conn, profile_id, now=NOW)
    daily.update_profile(conn, profile_id, name="Neu", cadence_days=7, enabled=False)
    row = daily.list_profiles(conn, now=NOW)[0]
    assert (row["name"], row["cadence_days"], row["enabled"]) == ("Neu", 7, False)
    assert row["next_run_at"] == "2026-10-12T08:00:00"
    assert row["last_run_at"] == "2026-10-05T08:00:00"
    assert row["airlines"] == ["FR"]
    assert row["routes"] == ["BER-ATH-BER"]


def test_targeted_run_ignores_cadence_but_not_pause_or_active_job(tmp_path):
    path = tmp_path / "run.db"
    conn = db.connect(path)
    profile_id = daily.save_profile(conn, "Athen", [spec()], now=NOW)
    daily.mark_scanned(conn, profile_id, now=NOW)
    runner = RecordingRunner(path)
    job = daily.run_profile(conn, runner, profile_id, now=NOW)
    assert job["profile_id"] == profile_id
    assert len(runner.started) == 1
    assert daily.list_profiles(conn, now=NOW)[0]["status"] == "running"
    with pytest.raises(ValueError, match="läuft"):
        daily.run_profile(conn, runner, profile_id, now=NOW)
    assert daily.dispatch_due_profiles(conn, runner, now=NOW + timedelta(days=1)) == []
    conn.execute("UPDATE search_job SET status='done' WHERE id=?", (job["job_id"],))
    daily.update_profile(conn, profile_id, enabled=False)
    with pytest.raises(ValueError, match="pausiert"):
        daily.run_profile(conn, runner, profile_id, now=NOW)


def test_targeted_run_rejects_expired_and_unknown_profiles(tmp_path):
    path = tmp_path / "invalid.db"
    conn = db.connect(path)
    profile_id = daily.save_profile(conn, "Alt", [spec(date(2026, 10, 4))], now=NOW)
    runner = RecordingRunner(path)
    with pytest.raises(ValueError, match="abgelaufen"):
        daily.run_profile(conn, runner, profile_id, now=NOW)
    with pytest.raises(LookupError):
        daily.run_profile(conn, runner, 999, now=NOW)
    assert runner.started == []


@pytest.mark.parametrize("changes", [{"name": " "}, {"cadence_days": 0}, {"cadence_days": 31}])
def test_profile_updates_validate_values(tmp_path, changes):
    conn = db.connect(tmp_path / "validation.db")
    profile_id = daily.save_profile(conn, "Athen", [spec()], now=NOW)
    with pytest.raises(ValueError):
        daily.update_profile(conn, profile_id, **changes)
