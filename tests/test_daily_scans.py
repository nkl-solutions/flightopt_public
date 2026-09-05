"""Saved searches and price baselines for autonomous daily scans."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flightopt.domain.models import LegSpec, SearchSpec, StayRange
from flightopt.jobs.daily import (
    dispatch_due_profiles,
    due_profiles,
    mark_scanned,
    save_profile,
)
from flightopt.storage import db
from flightopt.storage.baseline import detect_price_signal, refresh_baselines


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 10),
    )


def test_saved_profile_is_due_and_can_be_rescheduled(tmp_path):
    conn = db.connect(tmp_path / "daily.db")
    now = datetime(2026, 9, 5, 8, 0, 0)

    profile_id = save_profile(conn, "Athen Oktober", [spec()], now=now)

    due = due_profiles(conn, now=now)
    assert [p.id for p in due] == [profile_id]
    assert due[0].name == "Athen Oktober"
    assert due[0].specs[0].route == "BER-ATH-BER"

    mark_scanned(conn, profile_id, now=now, cadence_days=1)

    assert due_profiles(conn, now=now + timedelta(hours=23)) == []
    assert [p.id for p in due_profiles(conn, now=now + timedelta(days=1, minutes=1))] == [
        profile_id
    ]


def test_saved_profile_keeps_checked_bags(tmp_path):
    conn = db.connect(tmp_path / "daily_bags.db")
    with_bag = SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 10),
        checked_bags=1,
    )

    profile_id = save_profile(conn, "Athen mit Gepäck", [with_bag])

    due = due_profiles(conn)
    assert due[0].id == profile_id
    assert due[0].specs[0].checked_bags == 1


def test_dispatch_due_profiles_starts_jobs_and_reschedules(tmp_path):
    conn = db.connect(tmp_path / "dispatch.db")
    now = datetime(2026, 9, 5, 8, 0, 0)
    profile_id = save_profile(conn, "Athen Oktober", [spec()], now=now)

    class Runner:
        def __init__(self) -> None:
            self.created = []
            self.started = []

        def create(self, specs):
            self.created.append(specs)
            return 7

        def start(self, job_id, specs, *, airlines=None):
            self.started.append((job_id, specs, airlines))

    jobs = dispatch_due_profiles(conn, Runner(), now=now)

    assert jobs == [{"profile_id": profile_id, "job_id": 7, "name": "Athen Oktober"}]
    assert due_profiles(conn, now=now) == []
    assert due_profiles(conn, now=now + timedelta(days=1, minutes=1))[0].id == profile_id


def test_refresh_baselines_and_detects_unusually_cheap_price(tmp_path):
    conn = db.connect(tmp_path / "baseline.db")
    observed = datetime(2026, 9, 5, 8, 0, 0)
    travel = date(2026, 10, 3)
    for price in (21000, 22000, 23000, 22500, 21500):
        conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, party_size, "
            "currency, price_total_minor, is_estimate) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                observed.isoformat(timespec="seconds"),
                "test",
                "flight",
                "BER|ATH",
                travel.isoformat(),
                1,
                "EUR",
                price,
                1,
            ),
        )

    written = refresh_baselines(conn, now=observed, min_samples=5)
    signal = detect_price_signal(conn, "BER|ATH", travel, 16000, observed_at=observed)

    assert written == 1
    assert signal["status"] == "cheap"
    assert signal["median_minor"] == 22000
