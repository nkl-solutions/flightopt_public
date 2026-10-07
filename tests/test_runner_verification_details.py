"""Per-leg check evidence and original calendar totals survive a restart."""

import json
from datetime import date
from types import SimpleNamespace

from flightopt.domain.models import LegSpec, Money, SearchSpec
from flightopt.jobs.runner import JobRunner


DAY = date(2027, 1, 3)


def spec():
    return SearchSpec(legs=(LegSpec("BER", "ATH"),), stays=(),
                      window_start=DAY, window_end=DAY)


def test_leg_keeps_calendar_price_when_no_day_offer_exists():
    row = JobRunner._leg_payload(spec(), {0: {DAY: Money(6500)}}, None, 0, DAY, None)
    assert row["calendar_price_minor"] == 6500
    assert row["verification"] == {"status": "not_checked"}
    assert row["verified"] is False


def test_no_offer_keeps_explicit_failure_reason():
    from dataclasses import dataclass

    @dataclass
    class Check:
        status: str = "error"
        failed_sources: tuple = ("test-source",)

    live = SimpleNamespace(offers=[None], verification=[Check()])
    row = JobRunner._leg_payload(spec(), {0: {DAY: Money(6500)}}, None, 0, DAY, live)
    assert row["verification"]["status"] == "error"
    assert row["verification"]["failed_sources"] == ("test-source",)
    assert row["calendar_price_minor"] == 6500
    assert row["verified"] is False


def test_restart_preserves_original_estimate_and_drift(tmp_path):
    runner = JobRunner(str(tmp_path / "jobs.db"))
    job_id = runner.create(spec())
    conn = runner._conn()
    detail = [{"origin": "BER", "destination": "ATH", "date": DAY.isoformat(),
               "price": 80.0, "verified": True, "indicative": False,
               "calendar_price_minor": 6500,
               "verification": {"status": "verified", "method": "cache",
                                "checked_sources": ["recorded"]}}]
    conn.execute("INSERT INTO itinerary_result(job_id,rank,dates,price_total_minor,currency,is_estimate,detail) "
                 "VALUES(?,?,?,?,?,?,?)", (job_id, 1, json.dumps([DAY.isoformat()]), 8000, "EUR", 0, json.dumps(detail)))
    conn.execute("UPDATE search_job SET status='done' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    row = JobRunner(runner.db_path).result(job_id)["results"][0]
    assert row["estimate"] == 65.0
    assert row["drift"] == 15.0
    assert row["legs"][0]["verification"]["method"] == "cache"


def test_restart_does_not_invent_an_estimate_for_legacy_rows(tmp_path):
    runner = JobRunner(str(tmp_path / "old.db"))
    job_id = runner.create(spec())
    conn = runner._conn()
    conn.execute("INSERT INTO itinerary_result(job_id,rank,dates,price_total_minor,currency,is_estimate,detail) "
                 "VALUES(?,?,?,?,?,?,?)", (job_id, 1, json.dumps([DAY.isoformat()]), 8000, "EUR", 0,
                                         json.dumps([{"price": 80.0, "verified": True}])))
    conn.commit()
    conn.close()
    row = JobRunner(runner.db_path).result(job_id)["results"][0]
    assert row["estimate"] is None
    assert row["drift"] is None
