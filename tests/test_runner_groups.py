"""Grouped route jobs keep the best concrete candidates visible."""

from __future__ import annotations

import json
from datetime import date

import pytest

from flightopt.domain.models import LegSpec, Money, SearchSpec
from flightopt.jobs.runner import JobRunner
from flightopt.jobs.runner import checked_bag_fee_minor
from flightopt.jobs.runner import merge_variant_payloads


def result(route: str, total: float, dates: list[str]) -> dict:
    return {
        "route": route,
        "dates": dates,
        "total": total,
        "currency": "EUR",
        "verified": False,
        "estimate": total,
        "drift": None,
        "legs": [
            {
                "origin": route.split("-")[0],
                "destination": route.split("-")[-1],
                "date": dates[0],
                "price": total,
                "verified": False,
            }
        ],
    }


def test_merge_variant_payloads_marks_source_route_and_reranks_by_total():
    merged = merge_variant_payloads(
        [
            result("BER-ATH", 140.0, ["2026-10-02"]),
            result("LEJ-ATH", 99.0, ["2026-10-01"]),
        ],
        top_k=20,
    )

    assert [r["rank"] for r in merged] == [1, 2]
    assert [r["route"] for r in merged] == ["LEJ-ATH", "BER-ATH"]
    assert merged[0]["legs"][0]["route"] == "LEJ-ATH"


def test_merge_variant_payloads_deduplicates_same_route_and_dates():
    merged = merge_variant_payloads(
        [
            result("BER-ATH", 140.0, ["2026-10-02"]),
            result("BER-ATH", 120.0, ["2026-10-02"]),
        ],
        top_k=20,
    )

    assert len(merged) == 1
    assert merged[0]["total"] == 120.0


def test_leg_payload_breaks_out_checked_bag_estimate():
    day = date(2026, 10, 1)
    with_bag = SearchSpec(
        legs=(LegSpec("BER", "ATH"),),
        stays=(),
        window_start=day,
        window_end=day,
        checked_bags=1,
    )
    fee = checked_bag_fee_minor(["FR"], 1)

    leg = JobRunner._leg_payload(
        with_bag,
        [{day: Money(7900 + fee)}],
        None,
        0,
        day,
        None,
        winner={(0, day): "FR"},
    )

    assert leg["price"] == 119.0
    assert leg["base_price"] == 79.0
    assert leg["bag_fee"] == 40.0
    assert leg["checked_bags"] == 1


def spec(origin: str) -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec(origin, "ATH"),),
        stays=(),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 3),
    )


@pytest.mark.asyncio
async def test_run_many_persists_merged_variant_results(tmp_path, monkeypatch):
    runner = JobRunner(str(tmp_path / "jobs.db"))
    specs = [spec("BER"), spec("LEJ")]
    job_id = runner.create(specs)

    async def fake_variant(job_id, conn, variant, *, airlines, verify_limit):
        prices = {"BER-ATH": 140.0, "LEJ-ATH": 99.0}
        return [result(variant.route, prices[variant.route], ["2026-10-01"])]

    monkeypatch.setattr(runner, "_run_variant_payloads", fake_variant, raising=False)

    await runner._run_many(job_id, specs, airlines=[])

    stored = runner.result(job_id)
    assert stored["status"] == "done"
    assert [r["total"] for r in stored["results"]] == [99.0, 140.0]
    assert json.loads(
        runner._conn().execute("SELECT detail FROM itinerary_result WHERE rank=1").fetchone()[0]
    )[0]["route"] == "LEJ-ATH"
    assert runner._history[job_id][-1].phase == "done"
    assert runner._history[job_id][-1].detail["results"][0]["route"] == "LEJ-ATH"


@pytest.mark.asyncio
async def test_persisted_group_results_keep_route_after_runner_restart(tmp_path, monkeypatch):
    db_path = str(tmp_path / "jobs.db")
    runner = JobRunner(db_path)
    specs = [spec("BER"), spec("LEJ")]
    job_id = runner.create(specs)

    async def fake_variant(job_id, conn, variant, *, airlines, verify_limit):
        prices = {"BER-ATH": 140.0, "LEJ-ATH": 99.0}
        return [result(variant.route, prices[variant.route], ["2026-10-01"])]

    monkeypatch.setattr(runner, "_run_variant_payloads", fake_variant, raising=False)
    await runner._run_many(job_id, specs, airlines=[])

    restored = JobRunner(db_path).result(job_id)

    assert restored["results"][0]["route"] == "LEJ-ATH"
    assert restored["results"][0]["verified"] is False
    assert restored["results"][0]["estimate"] == 99.0
