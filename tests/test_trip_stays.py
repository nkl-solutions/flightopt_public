"""Gesamtreise-Kosten: Aufenthalte ableiten, entdoppeln, bepreisen, summieren.

Kein Test fasst hier ein Netz an. Die Quelle ist eine Attrappe nach demselben
Muster wie in `tests/test_hotel_jobs.py`, und sie zaehlt mit, wonach gefragt
wurde: nur so laesst sich zeigen, dass das Entdoppeln wirklich greift und dass
ohne Schalter kein einziger Abruf stattfindet.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, SearchSpec, StayRange
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.sources.base import HotelBatch, HotelSource
from flightopt.jobs import runner as runner_module
from flightopt.jobs.runner import JobRunner
from flightopt.search.dp import Combination
from flightopt.search.grid import GridReport
from flightopt.search.verify import VerifyReport
from flightopt.trip.stays import (
    Stay,
    StayOptions,
    apply_stay_costs,
    city_of,
    price_stays,
    stays_of_legs,
    stays_of_rows,
    unique_stays,
)

OUT = date(2026, 10, 1)
BACK_A = date(2026, 10, 5)
BACK_B = date(2026, 10, 6)


class StubStaySource(HotelSource):
    """Ein Haus je Anfrage, ohne Netz. Merkt sich jede gestellte Frage."""

    name = "stub"

    def __init__(self, *, minor: int = 8400, silent_for: set[str] | None = None) -> None:
        super().__init__()
        self.asked: list[HotelQuery] = []
        self.closed = 0
        self.minor = minor
        self.silent_for = silent_for or set()

    async def search(self, query: HotelQuery) -> HotelBatch:
        self.asked.append(query)
        if query.destination in self.silent_for:
            return HotelBatch(empty=True)
        return HotelBatch(
            offers=[
                HotelOffer(
                    source=self.name,
                    property_key=f"stub:{query.destination}:{index}",
                    name=f"Haus {index}",
                    arrival=query.arrival,
                    departure=query.departure,
                    # Der zweite Treffer ist teurer: gewaehlt wird der erste.
                    price_total=Money(self.minor + index * 2000, "EUR"),
                    city=query.destination,
                    country="Greece",
                    party_size=query.party_size,
                    rooms=query.rooms,
                )
                for index in range(2)
            ]
        )

    def close(self) -> None:
        self.closed += 1


def leg(origin: str, destination: str, day: date, price: float = 100.0) -> dict:
    return {
        "origin": origin,
        "destination": destination,
        "date": day.isoformat(),
        "price": price,
        "verified": False,
    }


def row(legs: list[dict], total: float = 200.0) -> dict:
    return {
        "route": "-".join([legs[0]["origin"]] + [l["destination"] for l in legs]),
        "dates": [l["date"] for l in legs],
        "total": total,
        "currency": "EUR",
        "verified": False,
        "legs": legs,
    }


# --- Ableiten ---------------------------------------------------------------


def test_a_return_trip_has_exactly_one_stay():
    stays = stays_of_legs([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)])

    assert len(stays) == 1
    assert stays[0].code == "ATH"
    assert stays[0].arrival == OUT
    assert stays[0].nights == 4
    assert stays[0].departure == BACK_A
    # Die Quelle sucht nach dem Ort, nicht nach dem Terminal.
    assert stays[0].city == "Athen"


def test_a_one_way_flight_has_no_stay_at_all():
    assert stays_of_legs([leg("BER", "ATH", OUT)]) == []
    assert stays_of_legs([]) == []


def test_three_stops_give_three_stays_in_order():
    legs = [
        leg("BER", "IST", date(2026, 10, 1)),
        leg("IST", "ATH", date(2026, 10, 4)),
        leg("ATH", "FCO", date(2026, 10, 9)),
        leg("FCO", "BER", date(2026, 10, 11)),
    ]

    stays = stays_of_legs(legs)

    assert [s.code for s in stays] == ["IST", "ATH", "FCO"]
    assert [s.nights for s in stays] == [3, 5, 2]
    # Beim letzten Leg endet die Reise, dort wird nicht mehr uebernachtet.
    assert all(s.code != "BER" for s in stays)


def test_a_same_day_connection_is_no_stay():
    legs = [leg("BER", "IST", OUT), leg("IST", "ATH", OUT)]

    assert stays_of_legs(legs) == []


def test_an_unknown_airport_keeps_its_code_instead_of_a_guess():
    assert city_of("ZZZZ") == "ZZZZ"
    assert city_of("") == ""
    assert city_of("ATH") == "Athen"


def test_the_same_triple_appears_only_once():
    here = Stay(code="ATH", city="Athen", arrival=OUT, nights=4)
    twin = Stay(code="ATH", city="Athen", arrival=OUT, nights=4)
    other = Stay(code="ATH", city="Athen", arrival=OUT, nights=5)

    assert len(unique_stays([here, twin, other])) == 2
    # Zwei Flughaefen derselben Stadt fallen zusammen: gesucht wird ein Bett.
    assert here.key == twin.key


# --- Abfragen ---------------------------------------------------------------


async def test_a_shared_stay_is_asked_only_once():
    """Der eigentliche Spar-Hebel: gleiche Tripel, eine Frage."""
    rows = [
        row([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)]),
        row([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)], total=240.0),
        row([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_B)], total=260.0),
    ]
    source = StubStaySource()

    quote = await price_stays(stays_of_rows(rows), [source])

    # Drei Zeilen, aber nur zwei verschiedene Aufenthalte.
    assert len(source.asked) == 2
    assert quote.asked == 2
    assert quote.calls == 2
    assert sorted(q.nights for q in source.asked) == [4, 5]


async def test_twenty_candidates_need_fewer_lookups_after_dedup():
    """Das gemessene Beispiel fuer die Rechnung "40 Abfragen, oder eben nicht".

    Das Feld hat den Zuschnitt, den `solve(top_k=20, max_per_start=3)` liefert:
    hoechstens drei Kandidaten je Abflugtag. Dieselben Zwischenstopp-Termine
    tauchen dabei in mehreren Zeilen auf, und genau daran spart der Schritt.
    """
    rows = []
    for out_offset in range(7):
        for back_offset in range(3):
            first = OUT + timedelta(days=out_offset)
            middle = first + timedelta(days=3 + back_offset)
            last = middle + timedelta(days=4)
            rows.append(
                row([
                    leg("BER", "IST", first),
                    leg("IST", "ATH", middle),
                    leg("ATH", "BER", last),
                ])
            )
    rows = rows[:20]

    source = StubStaySource()
    quote = await price_stays(stays_of_rows(rows), [source])

    # 40 Aufenthalte roh, 28 nach dem Entdoppeln: 12 Abrufe gespart, und alles
    # bleibt unter dem Budget von 40.
    assert sum(len(stays_of_legs(r["legs"])) for r in rows) == 40
    assert len(source.asked) == quote.asked
    assert quote.asked == 28
    assert quote.calls == 28
    assert quote.skipped == []


async def test_the_budget_stops_the_run_and_says_so():
    stays = [
        Stay(code="ATH", city="Athen", arrival=OUT + timedelta(days=i), nights=3)
        for i in range(6)
    ]
    source = StubStaySource()

    quote = await price_stays(stays, [source], options=StayOptions(budget=4))

    assert len(source.asked) == 4
    assert quote.asked == 4
    assert len(quote.skipped) == 2
    assert quote.notes and "abgefragt werden 4" in quote.notes[0]


async def test_the_cheapest_offer_wins_and_names_its_source():
    stays = stays_of_legs([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)])

    quote = await price_stays(stays, [StubStaySource(minor=8400)])

    price = quote.prices[stays[0].key]
    assert price.minor == 8400
    assert price.source == "stub"
    assert price.currency == "EUR"


async def test_the_occupancy_from_the_form_reaches_the_query():
    stays = stays_of_legs([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)])
    source = StubStaySource()

    await price_stays(stays, [source], options=StayOptions(adults=3, rooms=2))

    assert source.asked[0].adults == 3
    assert source.asked[0].rooms == 2


async def test_the_default_occupancy_is_two_adults_in_one_room():
    stays = stays_of_legs([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)])
    source = StubStaySource()

    await price_stays(stays, [source])

    assert source.asked[0].adults == 2
    assert source.asked[0].rooms == 1


# --- Summieren --------------------------------------------------------------


async def test_the_total_is_flights_plus_stays():
    rows = [row([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)], total=200.0)]
    quote = await price_stays(stays_of_rows(rows), [StubStaySource(minor=8400)])

    apply_stay_costs(rows, quote)

    assert rows[0]["stay_total"] == 84.0
    assert rows[0]["grand_total"] == 284.0
    assert rows[0]["stays"][0]["city"] == "Athen"
    assert rows[0]["stays"][0]["nights"] == 4
    assert rows[0]["stays"][0]["price"] == 84.0
    assert rows[0]["stays"][0]["source"] == "stub"


async def test_a_missing_stay_price_leaves_the_total_empty():
    """Eine Zeile mit Luecke darf nicht billiger aussehen als eine ganze."""
    rows = [
        row([
            leg("BER", "IST", date(2026, 10, 1)),
            leg("IST", "ATH", date(2026, 10, 4)),
            leg("ATH", "BER", date(2026, 10, 9)),
        ], total=300.0)
    ]
    # Fuer Istanbul antwortet die Quelle mit nichts.
    source = StubStaySource(silent_for={"Istanbul"})

    quote = await price_stays(stays_of_rows(rows), [source])
    apply_stay_costs(rows, quote)

    assert rows[0]["grand_total"] is None
    assert rows[0]["stay_total"] is None
    # Die Aufenthalte stehen trotzdem da, sonst ist die leere Summe raetselhaft.
    assert [s["city"] for s in rows[0]["stays"]] == ["Istanbul", "Athen"]
    assert rows[0]["stays"][0]["price"] is None
    assert rows[0]["stays"][1]["price"] == 84.0


async def test_a_one_way_row_gets_a_total_without_any_stay():
    rows = [row([leg("BER", "ATH", OUT)], total=99.0)]

    quote = await price_stays(stays_of_rows(rows), [StubStaySource()])
    apply_stay_costs(rows, quote)

    assert rows[0]["stays"] == []
    assert rows[0]["grand_total"] == 99.0


async def test_a_foreign_currency_offer_is_left_out_instead_of_added():
    """287 in fremder Waehrung zur Euro-Summe zu addieren waere schlicht falsch."""

    class ForeignSource(StubStaySource):
        async def search(self, query: HotelQuery) -> HotelBatch:
            self.asked.append(query)
            return HotelBatch(
                offers=[
                    HotelOffer(
                        source=self.name, property_key="x", name="Haus",
                        arrival=query.arrival, departure=query.departure,
                        price_total=Money(2870000, "JPY"),
                    )
                ]
            )

    rows = [row([leg("BER", "ATH", OUT), leg("ATH", "BER", BACK_A)])]
    quote = await price_stays(stays_of_rows(rows), [ForeignSource()])
    apply_stay_costs(rows, quote)

    assert quote.prices == {}
    assert rows[0]["grand_total"] is None
    assert any("JPY" in note for note in quote.notes)


# --- Im Lauf ----------------------------------------------------------------


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 12),
    )


@pytest.fixture
def offline(monkeypatch):
    """Keine Flugquelle wird angefasst. Geprueft wird der Aufenthaltsschritt."""

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


async def test_without_the_switch_not_a_single_lookup_happens(offline, tmp_path):
    runner = JobRunner(str(tmp_path / "off.db"))
    source = StubStaySource()
    job_id = runner.create(spec())

    await runner._run(job_id, spec())

    assert source.asked == []
    phases = [p.phase for p in runner._history[job_id]]
    assert "staying" not in phases
    assert "stays" not in phases
    done = runner._history[job_id][-1]
    assert done.phase == "done"
    assert all("grand_total" not in r for r in done.detail["results"])


async def test_the_flight_rows_arrive_before_the_stay_costs(offline, tmp_path):
    runner = JobRunner(str(tmp_path / "order.db"))
    source = StubStaySource()
    job_id = runner.create(spec())

    await runner._run(job_id, spec(), stays=StayOptions(sources=[source]))

    phases = [p.phase for p in runner._history[job_id]]
    assert phases.index("partial") < phases.index("verified")
    assert phases.index("verified") < phases.index("staying")
    assert phases.index("staying") < phases.index("stays")
    assert phases.index("stays") < phases.index("done")

    stays = next(p for p in runner._history[job_id] if p.phase == "stays")
    assert [r["grand_total"] for r in stays.detail["results"]] == [294.0, 314.0]
    # Zwei Kandidaten, zwei verschiedene Rueckflugtage, also zwei Abfragen.
    assert len(source.asked) == 2
    assert source.closed == 1

    done = runner._history[job_id][-1]
    assert done.phase == "done"
    assert done.detail["results"][0]["grand_total"] == 294.0


async def test_a_cancel_during_the_stay_phase_ends_cancelled(offline, tmp_path):
    runner = JobRunner(str(tmp_path / "stop.db"))
    source = StubStaySource()
    job_id = runner.create(spec())

    emit = runner._emit

    def emit_then_cancel(jid, progress):
        emit(jid, progress)
        if progress.phase == "staying" and progress.done == 1:
            runner.cancel(jid)

    runner._emit = emit_then_cancel  # type: ignore[method-assign]

    await runner._run(job_id, spec(), stays=StayOptions(sources=[source]))

    phases = [p.phase for p in runner._history[job_id]]
    assert phases[-1] == "cancelled", phases
    assert "done" not in phases
    assert runner.status(job_id) == "cancelled"
    # Genau ein Fenster war fertig, das zweite wird nie gefragt.
    assert len(source.asked) == 1
    # Auch ein abgebrochener Lauf gibt seine Quellen frei.
    assert source.closed == 1


def test_the_api_only_builds_a_stay_order_when_the_switch_is_on():
    from flightopt.api.main import SearchRequest

    base = dict(
        airports=["BER", "ATH"],
        trip="return",
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 20),
    )

    assert SearchRequest(**base).stay_options() is None

    options = SearchRequest(**base, with_hotels=True, hotel_adults=3,
                            hotel_rooms=2).stay_options()
    assert options is not None
    assert options.adults == 3
    assert options.rooms == 2
    assert options.currency == "EUR"
    assert callable(options.sources)

    # Ohne eigene Angabe: zwei Erwachsene, ein Zimmer.
    plain = SearchRequest(**base, with_hotels=True).stay_options()
    assert (plain.adults, plain.rooms) == (2, 1)


async def test_a_catalogue_factory_is_only_called_when_the_switch_is_on(offline, tmp_path):
    made: list[StubStaySource] = []

    def catalogue():
        source = StubStaySource()
        made.append(source)
        return [source]

    runner = JobRunner(str(tmp_path / "factory.db"))
    job_id = runner.create(spec())

    await runner._run(job_id, spec(), stays=StayOptions(sources=catalogue))

    assert len(made) == 1
    assert made[0].closed == 1


async def test_route_variants_share_one_stay_pass_after_the_merge(offline, tmp_path):
    """Flughafen-Gruppen laufen ueber `_run_many`. Auch dort kommt der Schritt
    zum Schluss, und zwar einmal fuer die zusammengefuehrte Bestenliste."""
    runner = JobRunner(str(tmp_path / "many.db"))
    source = StubStaySource()
    specs = [spec(), spec()]
    job_id = runner.create(specs)

    await runner._run_many(job_id, specs, stays=StayOptions(sources=[source]))

    phases = [p.phase for p in runner._history[job_id]]
    assert phases.index("verified") < phases.index("staying")
    assert phases[-1] == "done"
    # Zwei gleiche Varianten fuehren zu denselben zwei Aufenthalten, nicht vier.
    assert len(source.asked) == 2
    done = runner._history[job_id][-1]
    assert [r["grand_total"] for r in done.detail["results"]] == [294.0, 314.0]
