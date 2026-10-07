"""Finished searches and scheduler recovery share the local target evaluator."""

from datetime import date, datetime

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, Offer, SearchSpec
from flightopt.jobs import price_targets, runner as runner_module
from flightopt.jobs.daily import save_profile
from flightopt.jobs.runner import JobRunner
from flightopt.jobs.scheduler import DailyScanScheduler
from flightopt.search.dp import Combination
from flightopt.search.grid import GridReport
from flightopt.search.verify import VerifiedItinerary, VerifyReport


def offline(monkeypatch):
    async def rates(conn, **kwargs):
        return Rates()

    async def routes(*args, **kwargs):
        pass

    async def grid(spec, sources, **kwargs):
        return {0: {spec.window_start: Money(5000)}}, GridReport(filled={0: 1})

    def solve(spec, grid, **kwargs):
        return [Combination(dates=(spec.window_start,), total=Money(5000))]

    async def verify(spec, best, sources, **kwargs):
        offer = Offer(source="fixture", origin=spec.legs[0].origin,
                      destination="ATH", travel_date=spec.window_start,
                      price=Money(5000), is_estimate=False,
                      deep_link="https://booking.example.test/search")
        return [VerifiedItinerary(combination=best[0], offers=[offer])], VerifyReport(confirmed=1)

    monkeypatch.setattr(runner_module.fx_store, "current_rates", rates)
    monkeypatch.setattr(runner_module, "preload_routes", routes)
    monkeypatch.setattr(runner_module, "build_catalogue", lambda *args, **kw: [])
    monkeypatch.setattr(runner_module, "build_grid", grid)
    monkeypatch.setattr(runner_module, "solve", solve)
    monkeypatch.setattr(runner_module, "verify", verify)


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("broken", [False, True])
async def test_completion_evaluates_committed_results_without_breaking_search(
    tmp_path, monkeypatch, grouped, broken, caplog,
):
    offline(monkeypatch)
    runner = JobRunner(str(tmp_path / "jobs.db"))
    specs = [SearchSpec(legs=(LegSpec(origin, "ATH"),), stays=(),
                        window_start=date.today(), window_end=date.today())
             for origin in (["BER", "LEJ"] if grouped else ["BER"])]
    conn = runner._conn()
    pid = save_profile(conn, "Athen", specs, price_target_minor=5000)
    jid = runner.create(specs)
    conn.execute("UPDATE search_job SET profile_id=? WHERE id=?", (pid, jid))
    conn.commit()
    seen = []
    original = price_targets.evaluate_job

    def evaluate(conn, job_id, **kwargs):
        with runner._conn() as other:
            assert other.execute("SELECT status FROM search_job WHERE id=?", (jid,)).fetchone()[0] == "done"
            assert other.execute("SELECT COUNT(*) FROM itinerary_result WHERE job_id=?", (jid,)).fetchone()[0]
        seen.append(job_id)
        if broken:
            raise RuntimeError("test evaluator unavailable")
        return original(conn, job_id, **kwargs)

    monkeypatch.setattr(price_targets, "evaluate_job", evaluate)
    if grouped:
        await runner._run_many(jid, specs, airlines=[])
    else:
        await runner._run(jid, specs[0], airlines=[])
    assert runner.result(jid)["status"] == "done"
    assert seen == [jid]
    if not broken:
        assert price_targets.list_alerts(conn)[0]["price_minor"] == 5000
    else:
        assert "price target" in caplog.text.lower()
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_scheduler_recovers_targets_and_keeps_dispatch_available(tmp_path, monkeypatch, broken):
    runner = JobRunner(str(tmp_path / "scheduler.db"))
    now = datetime(2027, 1, 1, 12)
    calls = []

    def pending(conn, **kwargs):
        calls.append(kwargs["now"])
        if broken:
            raise RuntimeError("test recovery unavailable")
        return []

    monkeypatch.setattr(price_targets, "evaluate_pending", pending)
    assert await DailyScanScheduler(runner, now=lambda: now).run_once() == []
    assert calls == [now]
