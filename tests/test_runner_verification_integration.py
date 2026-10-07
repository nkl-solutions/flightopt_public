"""Real verification evidence survives runner jobs, cache reuse and reloads."""

from datetime import date, datetime, timedelta
import json
import socket

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, Offer, SearchSpec, StayRange
from flightopt.jobs import runner as runner_module
from flightopt.jobs.runner import JobRunner
from flightopt.search.grid import GridReport
from flightopt.sources.base import SourceError

DAY = date(2027, 1, 3)
NEXT_DAY = DAY + timedelta(days=1)


class OfflineSource:
    supports_search = True
    supports_calendar = False
    carrier = "ZZ"

    def __init__(self, name, outcomes, *, estimate=False, indicative=False):
        self.name = name
        self.outcomes = outcomes
        self.estimate = estimate
        self.indicative = indicative
        self.asked = []

    def supports_route(self, origin, destination):
        return (origin, destination) in self.outcomes

    async def search_leg(self, origin, destination, day, *, pax, cabin, currency):
        self.asked.append((origin, destination, day, pax.total, cabin.value, currency))
        outcome = self.outcomes[(origin, destination)]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return []
        return [Offer(source=self.name, origin=origin, destination=destination,
                      travel_date=day, price=Money(outcome, currency),
                      deep_link="https://example.invalid/booking",
                      is_estimate=self.estimate)]


@pytest.fixture
def offline(monkeypatch):
    original_connect = socket.socket.connect

    def forbidden(*args, **kwargs):
        pytest.fail("Runner verification integration must stay offline")

    def local_only(sock, address):
        # Windows asyncio needs loopback sockets for its internal self-pipe.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr("flightopt.sources.base.HttpSource._new_session", forbidden)
    monkeypatch.setattr("curl_cffi.requests.Session.request", forbidden)

    def configure(sources, calendar_prices):
        async def fixed_rates(conn, **kwargs):
            return Rates(fetched_at=datetime(2027, 1, 1))

        async def calendar_grid(spec, catalogue, **kwargs):
            grid = {
                index: {DAY + timedelta(days=index): Money(calendar_prices[(leg.origin, leg.destination)])}
                for index, leg in enumerate(spec.legs)
            }
            return grid, GridReport(filled={index: 1 for index in grid})

        monkeypatch.setattr(runner_module, "build_catalogue", lambda wanted, **kwargs: sources)
        monkeypatch.setattr(runner_module.fx_store, "current_rates", fixed_rates)
        monkeypatch.setattr(runner_module, "build_grid", calendar_grid)

    return configure


def two_leg_spec(origin):
    return SearchSpec(
        legs=(LegSpec(origin, "ATH"), LegSpec("ATH", "PMI")),
        stays=(StayRange(1, 1),), window_start=DAY, window_end=NEXT_DAY,
    )


def expected_diagnostic(status, *, method=None, source=None, checked=(), failed=()):
    return {"status": status, "method": method, "source": source,
            "checked_sources": list(checked), "failed_sources": list(failed)}


def restored_rows(db_path, job_id, running):
    current = running.result(job_id)
    assert current["status"] == "done", current
    fresh = JobRunner(db_path)
    assert fresh.has_history(job_id) is False
    restored = fresh.result(job_id)
    assert restored["status"] == "done", restored
    assert restored["error"] is None
    assert len(restored["results"]) == len(current["results"])
    for memory, stored in zip(current["results"], restored["results"], strict=True):
        for key in ("rank", "dates", "currency", "total", "verified", "estimate", "drift"):
            assert stored[key] == memory[key], key
        assert stored["route"] == memory.get("route")
        assert stored["legs"] == json.loads(json.dumps(memory["legs"]))
    return restored["results"]


@pytest.mark.parametrize("grouped", [False, True], ids=["single", "grouped"])
async def test_real_verification_live_and_cached_evidence_survives_runner_reload(
    tmp_path, offline, grouped,
):
    source = OfflineSource("offline", {
        ("BER", "ATH"): 8002, ("LEJ", "ATH"): 9003, ("ATH", "PMI"): 4004,
    })
    offline([source], {("BER", "ATH"): 6501, ("LEJ", "ATH"): 7002, ("ATH", "PMI"): 3502})
    specs = [two_leg_spec("BER"), two_leg_spec("LEJ")] if grouped else [two_leg_spec("BER")]
    db_path = str(tmp_path / "jobs.db")
    jobs = []

    for cached in (False, True):
        runner = JobRunner(db_path)
        job_id = runner.create(specs if grouped else specs[0])
        jobs.append(job_id)
        if grouped:
            await runner._run_many(job_id, specs, airlines=[])
        else:
            await runner._run(job_id, specs[0])
        rows = restored_rows(db_path, job_id, runner)
        assert len(rows) == len(specs)
        by_origin = {row["legs"][0]["origin"]: row for row in rows}
        for origin, total, estimate, drift, outbound_minor in [
            ("BER", 120.06, 100.03, 20.03, 6501),
            *([("LEJ", 130.07, 105.04, 25.03, 7002)] if grouped else []),
        ]:
            row = by_origin[origin]
            assert (row["total"], row["estimate"], row["drift"]) == (total, estimate, drift)
            assert row["verified"] is True
            assert row["dates"] == ["2027-01-03", "2027-01-04"]
            assert [leg["calendar_price_minor"] for leg in row["legs"]] == [outbound_minor, 3502]
            for index, leg in enumerate(row["legs"]):
                method = "cache" if cached or (origin == "LEJ" and index == 1) else "live"
                assert leg["verification"] == expected_diagnostic(
                    "verified", method=method, source="offline", checked=("offline",),
                )
                assert leg["verified"] is True
                assert leg["source"] == "offline"
                assert leg["deep_link"] == "https://example.invalid/booking"
        assert source.asked == [
            ("BER", "ATH", DAY, 1, "economy", "EUR"),
            ("ATH", "PMI", NEXT_DAY, 1, "economy", "EUR"),
            *([("LEJ", "ATH", DAY, 1, "economy", "EUR")] if grouped else []),
        ]

    # Writing the cached job must not replace the first job's live evidence.
    original = JobRunner(db_path).result(jobs[0])["results"][0]
    assert [leg["verification"]["method"] for leg in original["legs"]] == ["live", "live"]


async def test_grouped_real_verification_preserves_mixed_statuses_after_reload(tmp_path, offline):
    firm = OfflineSource("firm", {("BER", "ATH"): 8000})
    empty = OfflineSource("empty", {("LEJ", "ATH"): None})
    failed = OfflineSource("failed", {("DRS", "ATH"): SourceError("offline lookup failed")})
    estimate = OfflineSource("estimate", {("MUC", "ATH"): 7500}, estimate=True)
    indicative = OfflineSource("indicative", {("FRA", "ATH"): 7600}, indicative=True)
    sources = [firm, empty, failed, estimate, indicative]
    origins = ["BER", "LEJ", "DRS", "HAM", "MUC", "FRA"]
    offline(sources, {(origin, "ATH"): 6500 for origin in origins})
    specs = [SearchSpec(legs=(LegSpec(origin, "ATH"),), stays=(),
                        window_start=DAY, window_end=DAY) for origin in origins]
    db_path = str(tmp_path / "grouped.db")

    for cached in (False, True):
        runner = JobRunner(db_path)
        job_id = runner.create(specs)
        await runner._run_many(job_id, specs, airlines=[])
        rows = restored_rows(db_path, job_id, runner)
        assert {row["route"] for row in rows} == {spec.route for spec in specs}
        by_origin = {row["legs"][0]["origin"]: row for row in rows}
        for origin, status, name, total, verified, drift in [
            ("BER", "verified", "firm", 80.0, True, 15.0),
            ("LEJ", "unavailable", "empty", 65.0, False, None),
            ("DRS", "error", "failed", 65.0, False, None),
            ("HAM", "unsupported", None, 65.0, False, None),
            ("MUC", "estimate", "estimate", 75.0, False, None),
            ("FRA", "indicative", "indicative", 76.0, True, 11.0),
        ]:
            row = by_origin[origin]
            leg = row["legs"][0]
            has_offer = status in ("verified", "estimate", "indicative")
            assert leg["verification"] == expected_diagnostic(
                status, method=("cache" if cached else "live") if has_offer else None,
                source=name if has_offer else None,
                checked=(name,) if name else (), failed=("failed",) if status == "error" else (),
            )
            assert leg["calendar_price_minor"] == 6500
            assert row["route"] == leg["route"] == f"{origin}-ATH"
            assert (row["total"], row["estimate"]) == (total, 65.0)
            assert row["verified"] is verified
            assert row["drift"] == drift
            assert leg["indicative"] is (status == "indicative")
        for source in sources:
            # Empty replies are cached; failures retry once in the second job.
            assert len(source.asked) == (2 if cached and source is failed else 1)
