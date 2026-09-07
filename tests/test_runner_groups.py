"""Grouped route jobs keep the best concrete candidates visible."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from flightopt.domain.fx import Rates
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


def no_network(monkeypatch) -> None:
    """Katalog und Kurse liegen jetzt im Job, nicht mehr in der Variante."""
    from flightopt.jobs import runner as runner_mod

    async def fake_rates(conn, **kw):
        return Rates(base="EUR", rates={"JPY": 165.0},
                     fetched_at=datetime(2027, 3, 1))

    monkeypatch.setattr(runner_mod.fx_store, "current_rates", fake_rates)
    monkeypatch.setattr(runner_mod, "build_catalogue", lambda wanted, **kw: [])


def job_probe(monkeypatch) -> dict:
    """Zaehlt, wie oft ein Job Katalog und Kurse anfasst, und laesst ihn laufen."""
    from flightopt.jobs import runner as runner_mod
    from flightopt.search.grid import GridReport

    seen: dict = {"rates_calls": 0, "catalogue_calls": 0, "handed": []}
    rates = Rates(base="EUR", rates={"JPY": 165.0}, fetched_at=datetime(2027, 3, 1))

    async def fake_rates(conn, **kw):
        seen["rates_calls"] += 1
        return rates

    def fake_catalogue(wanted, **kw):
        seen["catalogue_calls"] += 1
        return []

    async def fake_build_grid(job_spec, sources, **kw):
        seen["handed"].append(kw.get("rates"))
        day = job_spec.window_start
        legs = range(len(job_spec.legs))
        return ({i: {day: Money(10000, "EUR")} for i in legs},
                GridReport(filled={i: 1 for i in legs}))

    monkeypatch.setattr(runner_mod.fx_store, "current_rates", fake_rates)
    monkeypatch.setattr(runner_mod, "build_catalogue", fake_catalogue)
    monkeypatch.setattr(runner_mod, "build_grid", fake_build_grid)
    return seen


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
    seen_verify_limits = []

    async def fake_variant(job_id, conn, variant, *, airlines, verify_limit, **kw):
        seen_verify_limits.append(verify_limit)
        prices = {"BER-ATH": 140.0, "LEJ-ATH": 99.0}
        return [result(variant.route, prices[variant.route], ["2026-10-01"])]

    monkeypatch.setattr(runner, "_run_variant_payloads", fake_variant, raising=False)
    no_network(monkeypatch)

    await runner._run_many(job_id, specs, airlines=[])

    stored = runner.result(job_id)
    assert stored["status"] == "done"
    assert [r["total"] for r in stored["results"]] == [99.0, 140.0]
    assert json.loads(
        runner._conn().execute("SELECT detail FROM itinerary_result WHERE rank=1").fetchone()[0]
    )[0]["route"] == "LEJ-ATH"
    assert runner._history[job_id][-1].phase == "done"
    assert runner._history[job_id][-1].detail["results"][0]["route"] == "LEJ-ATH"
    assert seen_verify_limits == [20, 20]


@pytest.mark.asyncio
async def test_persisted_group_results_keep_route_after_runner_restart(tmp_path, monkeypatch):
    db_path = str(tmp_path / "jobs.db")
    runner = JobRunner(db_path)
    specs = [spec("BER"), spec("LEJ")]
    job_id = runner.create(specs)

    async def fake_variant(job_id, conn, variant, *, airlines, verify_limit, **kw):
        prices = {"BER-ATH": 140.0, "LEJ-ATH": 99.0}
        return [result(variant.route, prices[variant.route], ["2026-10-01"])]

    monkeypatch.setattr(runner, "_run_variant_payloads", fake_variant, raising=False)
    no_network(monkeypatch)
    await runner._run_many(job_id, specs, airlines=[])

    restored = JobRunner(db_path).result(job_id)

    assert restored["results"][0]["route"] == "LEJ-ATH"
    assert restored["results"][0]["verified"] is False
    assert restored["results"][0]["estimate"] == 99.0


# --- internationale Felder im Payload ----------------------------------------

from flightopt.domain.models import Itinerary, Offer, Segment  # noqa: E402
from flightopt.search.verify import VerifiedItinerary  # noqa: E402
from flightopt.search.dp import Combination  # noqa: E402


def one_leg_spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "NRT"),),
        stays=(),
        window_start=date(2027, 3, 10),
        window_end=date(2027, 3, 10),
    )


def long_haul_offer(*, native: Money | None = None) -> Offer:
    return Offer(
        source="serpapi",
        origin="BER",
        destination="NRT",
        travel_date=date(2027, 3, 10),
        price=Money(61200, "EUR"),
        segments=(
            Segment(carrier="TK", flight_number="TK 1724", origin="BER",
                    destination="IST", departure=datetime(2027, 3, 10, 11, 45),
                    arrival=datetime(2027, 3, 10, 16, 10)),
            Segment(carrier="TK", flight_number="TK 198", origin="IST",
                    destination="NRT", departure=datetime(2027, 3, 10, 17, 45),
                    arrival=datetime(2027, 3, 11, 11, 15)),
        ),
        price_native=native,
    )


def test_an_estimated_leg_reports_its_original_currency():
    day = date(2027, 3, 10)
    leg = JobRunner._leg_payload(
        one_leg_spec(),
        [{day: Money(29394, "EUR")}],
        None,
        0,
        day,
        None,
        winner={(0, day): "KIWI"},
        native={(0, day): Money(4850000, "JPY")},
    )

    assert leg["price"] == 293.94
    assert leg["price_native"] == {"amount": 48500.0, "currency": "JPY"}
    assert leg["stops"] is None
    assert "arrival_date" not in leg


def test_a_verified_leg_reports_stops_and_the_arrival_date():
    day = date(2027, 3, 10)
    combo = Combination(dates=(day,), total=Money(61200, "EUR"))
    live = VerifiedItinerary(combination=combo, offers=[long_haul_offer()])

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(65000, "EUR")}], combo, 0, day, live,
        winner={(0, day): "KIWI"}, native={(0, day): Money(4850000, "JPY")},
        by_source={"serpapi": "SERPAPI"},
    )

    assert leg["verified"] is True
    assert leg["stops"] == 1
    assert leg["arrival_date"] == "2027-03-11"
    assert leg["depart"] == "11:45"
    assert leg["arrive"] == "11:15"
    # Der Schaetzpreis ist ersetzt, also auch sein Originalpreis.
    assert "price_native" not in leg


def test_a_verified_leg_keeps_its_own_original_currency():
    day = date(2027, 3, 10)
    combo = Combination(dates=(day,), total=Money(61200, "EUR"))
    live = VerifiedItinerary(
        combination=combo, offers=[long_haul_offer(native=Money(10098000, "JPY"))]
    )

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(65000, "EUR")}], combo, 0, day, live,
        by_source={"serpapi": "SERPAPI"},
    )

    assert leg["price_native"] == {"amount": 100980.0, "currency": "JPY"}


@pytest.mark.asyncio
async def test_the_runner_loads_exchange_rates_once_per_job(tmp_path, monkeypatch):
    from flightopt.jobs import runner as runner_mod

    seen: dict = {"rates_calls": 0}

    async def fake_rates(conn, **kw):
        seen["rates_calls"] += 1
        return Rates(base="EUR", rates={"JPY": 165.0},
                     fetched_at=datetime(2027, 3, 1))

    async def fake_build_grid(job_spec, sources, **kw):
        seen["rates"] = kw.get("rates")
        raise RuntimeError("hier ist Schluss")

    monkeypatch.setattr(runner_mod.fx_store, "current_rates", fake_rates)
    monkeypatch.setattr(runner_mod, "build_grid", fake_build_grid)
    monkeypatch.setattr(runner_mod, "build_catalogue", lambda wanted, **kw: [])

    job_runner = JobRunner(str(tmp_path / "jobs.db"))
    job_spec = spec("BER")
    job_id = job_runner.create(job_spec)
    await job_runner._run(job_id, job_spec)

    assert seen["rates_calls"] == 1
    assert seen["rates"].rates["JPY"] == 165.0
    assert job_runner.result(job_id)["status"] == "failed"


@pytest.mark.asyncio
async def test_the_variant_path_hands_the_rates_to_the_grid(tmp_path, monkeypatch):
    """Der Gruppenweg ist ein eigener Codepfad und braucht die Kurse genauso."""
    seen = job_probe(monkeypatch)

    job_runner = JobRunner(str(tmp_path / "jobs.db"))
    specs = [spec("BER"), spec("LEJ")]
    job_id = job_runner.create(specs)
    await job_runner._run_many(job_id, specs, airlines=[])

    # Die Kurse gehoeren zum Job, nicht zur Routenvariante: sonst preist die
    # zweite Variante gegen einen anderen Kurs als die erste.
    assert seen["rates_calls"] == 1
    assert len(seen["handed"]) == 2
    assert all(handed is seen["handed"][0] for handed in seen["handed"])
    assert seen["handed"][0].rates["JPY"] == 165.0
    assert job_runner.result(job_id)["status"] == "done"


@pytest.mark.asyncio
async def test_the_catalogue_is_built_once_for_the_whole_job(tmp_path, monkeypatch):
    """Quellen tragen Ratenbremse und Sicherung; pro Variante neu heisst: keine."""
    seen = job_probe(monkeypatch)

    job_runner = JobRunner(str(tmp_path / "jobs.db"))
    specs = [spec("BER"), spec("LEJ"), spec("DRS")]
    job_id = job_runner.create(specs)
    await job_runner._run_many(job_id, specs, airlines=[])

    assert seen["catalogue_calls"] == 1
    assert len(seen["handed"]) == 3
    assert job_runner.result(job_id)["status"] == "done"


# --- Umstiege, wenn niemand die Fluege kennt ---------------------------------


def test_an_offer_without_segments_knows_no_stops():
    """Null Umstiege waere eine Aussage; wir haben schlicht keine."""
    offer = Offer(source="kiwi", origin="BER", destination="NRT",
                  travel_date=date(2027, 3, 10), price=Money(29394, "EUR"))

    assert offer.stops is None
    assert long_haul_offer().stops == 1


def test_a_verified_leg_without_segments_reports_unknown_stops():
    """Kiwi bestaetigt einen Preis ohne Flugplan: dann bleibt es unbekannt."""
    day = date(2027, 3, 10)
    combo = Combination(dates=(day,), total=Money(29394, "EUR"))
    live = VerifiedItinerary(
        combination=combo,
        offers=[Offer(source="kiwi", origin="BER", destination="NRT",
                      travel_date=day, price=Money(29394, "EUR"))],
    )

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(65000, "EUR")}], combo, 0, day, live,
        by_source={"kiwi": "KIWI"},
    )

    assert leg["verified"] is True
    assert leg["stops"] is None
    assert leg["carriers"] == ["KIWI"]


def test_an_itinerary_sums_only_the_stops_it_knows():
    day = date(2027, 3, 10)
    blind = Offer(source="kiwi", origin="NRT", destination="BER",
                  travel_date=day, price=Money(29394, "EUR"))
    itinerary = Itinerary(
        spec_route="BER-NRT-BER",
        dates=(day, day),
        offers=(long_haul_offer(), blind),
    )

    assert itinerary.stops == 1
