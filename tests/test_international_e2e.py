"""Die Zielsuche des Teilprojekts, komplett mit Fake-Quellen.

BER - Tokio - Seoul - BER: Gruppen werden aufgeloest, der Kalender kommt in
JPY, die Summe steht in EUR, zwei Legs werden mit echten Fluegen bestaetigt und
das Ergebnis nennt Umstiege und Ankunftsdatum. Kein Netz.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from flightopt.api.main import SearchRequest
from flightopt.domain.fx import Rates
from flightopt.domain.models import Cabin, Money, Offer, Pax, Segment
from flightopt.jobs import runner as runner_mod
from flightopt.jobs.runner import JobRunner

WINDOW = {"window_start": date(2027, 3, 1), "window_end": date(2027, 4, 30)}
RATES = Rates(base="EUR", rates={"JPY": 165.0, "KRW": 1500.0},
              fetched_at=datetime(2027, 3, 1))


class GlobalCalendar:
    """Steht fuer Kiwi: deckt jede Strecke ab, preist Japan in JPY."""

    name = "fakekiwi"
    carrier = ""
    carriers = ()
    supports_calendar = True
    supports_search = False
    indicative = True
    accepts_max_stops = True

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int | None]] = []

    def supports_route(self, origin, destination):
        return True

    async def calendar_range(self, origin, destination, start, end, *,
                             currency="EUR", max_stops=None):
        self.seen.append((origin, destination, max_stops))
        prices: dict[date, Money] = {}
        day = start
        while day <= end:
            if origin == "BER":
                prices[day] = Money(4850000 + day.day * 1000, "JPY")
            else:
                prices[day] = Money(45000 + day.day * 100, "EUR")
            day += timedelta(days=1)
        return prices


class FarEastAirline:
    """Steht fuer eine Airline mit echter Tagessuche - aber nicht ab BER."""

    name = "fakeair"
    carrier = "JL"
    carriers = ("JL",)
    supports_calendar = False
    supports_search = True
    indicative = False

    def supports_route(self, origin, destination):
        return origin != "BER"

    async def search_leg(self, origin, destination, day, *, pax=Pax(),
                         cabin=Cabin.ECONOMY, currency="EUR"):
        departure = datetime.combine(day, time(11, 45))
        stopover = departure + timedelta(hours=5)
        arrival = departure + timedelta(hours=20)
        return [
            Offer(
                source=self.name,
                origin=origin,
                destination=destination,
                travel_date=day,
                price=Money(40000, "EUR"),
                segments=(
                    Segment(carrier="JL", flight_number="JL 100", origin=origin,
                            destination="HKG", departure=departure, arrival=stopover),
                    Segment(carrier="JL", flight_number="JL 200", origin="HKG",
                            destination=destination,
                            departure=stopover + timedelta(hours=2), arrival=arrival),
                ),
            )
        ]


def request() -> SearchRequest:
    return SearchRequest(
        airports=["BER", "TYO", "SEL", "BER"],
        trip="multi",
        stays=[[7, 14], [4, 8]],
        **WINDOW,
    )


async def run_job(tmp_path, monkeypatch) -> tuple[JobRunner, int, GlobalCalendar]:
    calendar = GlobalCalendar()

    async def fake_rates(conn, **kw):
        return RATES

    monkeypatch.setattr(runner_mod.fx_store, "current_rates", fake_rates)
    monkeypatch.setattr(
        runner_mod, "build_catalogue",
        lambda wanted, **kw: [calendar, FarEastAirline()],
    )

    job_runner = JobRunner(str(tmp_path / "e2e.db"))
    specs = request().to_specs()
    job_id = job_runner.create(specs)
    await job_runner._run_many(job_id, specs, airlines=[])
    return job_runner, job_id, calendar


def test_the_group_route_fans_out_into_four_variants():
    specs = request().to_specs()

    assert sorted(s.route for s in specs) == [
        "BER-HND-GMP-BER", "BER-HND-ICN-BER",
        "BER-NRT-GMP-BER", "BER-NRT-ICN-BER",
    ]
    assert all(len(s.legs) == 3 for s in specs)
    assert [(s.min_nights, s.max_nights) for s in specs[0].stays] == [(7, 14), (4, 8)]


@pytest.mark.asyncio
async def test_the_search_runs_end_to_end_and_totals_in_euro(tmp_path, monkeypatch):
    job_runner, job_id, _ = await run_job(tmp_path, monkeypatch)
    stored = job_runner.result(job_id)

    assert stored["status"] == "done", stored["error"]
    assert stored["results"], "die Suche hat keinen Kandidaten geliefert"

    top = stored["results"][0]
    assert top["rank"] == 1
    assert top["currency"] == "EUR"
    assert top["route"] in {"BER-NRT-ICN-BER", "BER-NRT-GMP-BER",
                            "BER-HND-ICN-BER", "BER-HND-GMP-BER"}
    assert len(top["legs"]) == 3
    assert len(top["dates"]) == 3
    assert top["total"] == pytest.approx(sum(leg["price"] for leg in top["legs"]), abs=0.02)
    assert [r["rank"] for r in stored["results"]] == list(
        range(1, len(stored["results"]) + 1)
    )


@pytest.mark.asyncio
async def test_the_first_leg_stays_an_estimate_in_its_own_currency(tmp_path, monkeypatch):
    job_runner, job_id, _ = await run_job(tmp_path, monkeypatch)
    first = job_runner.result(job_id)["results"][0]["legs"][0]

    assert first["origin"] == "BER"
    assert first["verified"] is False
    assert first["indicative"] is True
    assert first["stops"] is None
    assert first["price_native"]["currency"] == "JPY"
    assert first["price_native"]["amount"] > 40000
    # 4.850.000+ JPY-Minor zu 165 sind knapp 300 EUR.
    assert 280 < first["price"] < 320


@pytest.mark.asyncio
async def test_the_verified_legs_report_stops_and_arrival(tmp_path, monkeypatch):
    job_runner, job_id, _ = await run_job(tmp_path, monkeypatch)
    top = job_runner.result(job_id)["results"][0]
    second = top["legs"][1]

    assert second["verified"] is True
    assert second["stops"] == 1
    assert second["price"] == 400.0
    assert second["depart"] == "11:45"
    assert second["arrival_date"] == (
        date.fromisoformat(top["dates"][1]) + timedelta(days=1)
    ).isoformat()
    assert "price_native" not in second


@pytest.mark.asyncio
async def test_the_long_haul_leg_is_asked_with_two_stops(tmp_path, monkeypatch):
    _, _, calendar = await run_job(tmp_path, monkeypatch)
    asked = dict(((origin, destination), stops) for origin, destination, stops in calendar.seen)

    assert asked[("BER", "NRT")] == 2
    assert asked[("ICN", "BER")] == 2
    assert set(asked) >= {("BER", "NRT"), ("BER", "HND"), ("NRT", "ICN"),
                          ("ICN", "BER")}
