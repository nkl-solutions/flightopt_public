"""Der Runner meldet Zwischenstaende, damit die Tabelle nicht eine Minute leer bleibt."""

from __future__ import annotations

from datetime import date

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, SearchSpec, StayRange
from flightopt.jobs import runner as runner_module
from flightopt.jobs.runner import JobRunner
from flightopt.search.dp import Combination
from flightopt.search.grid import GridReport
from flightopt.search.verify import VerifyReport

OUT = date(2026, 10, 1)
BACK_A = date(2026, 10, 5)
BACK_B = date(2026, 10, 6)


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 12),
    )


@pytest.fixture
def offline(monkeypatch):
    """Keine Quelle wird angefasst. Geprueft wird nur die Reihenfolge der Ereignisse."""

    async def no_routes(sources, legs):
        return None

    async def fake_build_grid(s, sources, **kwargs):
        grid = {
            0: {OUT: Money(9900)},
            1: {BACK_A: Money(11100), BACK_B: Money(13100)},
        }
        report = GridReport(
            filled={0: 1, 1: 2},
            calls=2,
            winner={(0, OUT): "FR", (1, BACK_A): "FR", (1, BACK_B): "FR"},
        )
        return grid, report

    def fake_solve(s, grid, **kwargs):
        return [
            Combination(dates=(OUT, BACK_A), total=Money(21000)),
            Combination(dates=(OUT, BACK_B), total=Money(23000)),
        ]

    async def fake_verify(s, best, sources, **kwargs):
        return [], VerifyReport()

    async def fake_rates(conn, **kwargs):
        return Rates()

    monkeypatch.setattr(runner_module, "preload_routes", no_routes)
    monkeypatch.setattr(runner_module, "build_grid", fake_build_grid)
    monkeypatch.setattr(runner_module, "solve", fake_solve)
    monkeypatch.setattr(runner_module, "verify", fake_verify)
    monkeypatch.setattr(runner_module.fx_store, "current_rates", fake_rates)


async def test_partial_lands_after_the_dp_and_before_verifying(offline, tmp_path):
    runner = JobRunner(str(tmp_path / "events.db"))
    s = spec()
    job_id = runner.create(s)

    await runner._run(job_id, s)

    phases = [p.phase for p in runner._history[job_id]]
    assert phases.count("partial") == 1
    assert phases.index("solving") < phases.index("partial")
    assert phases.index("partial") < phases.index("verifying")

    partial = next(p for p in runner._history[job_id] if p.phase == "partial")
    assert [r["rank"] for r in partial.detail["results"]] == [1, 2]
    assert all(r["verified"] is False for r in partial.detail["results"])
    assert partial.detail["results"][0]["total"] == 210.0
    assert partial.detail["results"][0]["dates"] == ["2026-10-01", "2026-10-05"]
    assert partial.detail["results"][0]["legs"][0]["origin"] == "BER"
    assert partial.total == 2


async def test_every_candidate_gets_its_own_verified_event(offline, tmp_path):
    runner = JobRunner(str(tmp_path / "each.db"))
    s = spec()
    job_id = runner.create(s)

    await runner._run(job_id, s)

    events = [p for p in runner._history[job_id] if p.phase == "verified"]

    assert [p.detail["result"]["rank"] for p in events] == [1, 2]
    assert [p.done for p in events] == [1, 2]
    assert [p.total for p in events] == [2, 2]
    assert events[0].detail["result"]["dates"] == ["2026-10-01", "2026-10-05"]
    assert runner._history[job_id][-1].phase == "done"
