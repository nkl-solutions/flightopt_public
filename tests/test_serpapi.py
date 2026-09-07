"""SerpApi (Google Flights) als Verify-Quelle.

Die einzige Quelle mit weltweiter Tagessuche, aber mit 250 Abrufen im Monat im
Free Tier. Der Budget-Guard ist deshalb kein Komfort, sondern die Bedingung
dafuer, dass die Quelle ueberhaupt eingebaut werden darf.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from flightopt.domain.models import Cabin, Money, Pax
from flightopt.sources import serpapi_google as serp
from flightopt.sources.base import SourceBlocked, SourceError
from flightopt.storage import db
from flightopt.storage.budget import MonthlyBudget, month_key

FIXTURES = Path(__file__).parent / "fixtures"
DAY = date(2027, 3, 10)


def payload() -> dict:
    return json.loads(
        (FIXTURES / "serpapi_google_flights_BER_NRT.json").read_text(encoding="utf-8")
    )


def source(monkeypatch, budget=None) -> serp.SerpApiGoogleFlights:
    src = serp.SerpApiGoogleFlights(api_key="k-123", budget=budget)

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)
    return src


# --- Parser ------------------------------------------------------------------


def test_parser_reads_best_and_other_flights():
    offers = serp.parse_offers(payload(), "BER", "NRT", DAY, currency="EUR")

    assert [o.price for o in offers] == [
        Money(61200, "EUR"), Money(73800, "EUR"), Money(84500, "EUR")
    ]
    assert all(o.source == "serpapi" for o in offers)
    assert all(o.origin == "BER" and o.destination == "NRT" for o in offers)
    assert all(o.travel_date == DAY for o in offers)
    assert all(o.is_estimate is False for o in offers)


def test_parser_builds_segments_with_carrier_codes():
    best = serp.parse_offers(payload(), "BER", "NRT", DAY)[0]

    assert best.stops == 1
    assert [s.carrier for s in best.segments] == ["TK", "TK"]
    assert [s.flight_number for s in best.segments] == ["TK 1724", "TK 198"]
    assert best.segments[0].origin == "BER"
    assert best.segments[0].destination == "IST"
    assert best.segments[0].departure == datetime(2027, 3, 10, 11, 45)
    assert best.segments[-1].arrival == datetime(2027, 3, 11, 11, 15)
    assert best.deep_link.startswith("https://www.google.com/travel/flights")


def test_parser_skips_itineraries_without_a_price_or_flights():
    broken = {
        "search_metadata": {"google_flights_url": "https://example.invalid"},
        "best_flights": [
            {"flights": [], "price": 500},
            {"flights": [{"departure_airport": {"id": "BER", "time": "2027-03-10 08:00"},
                          "arrival_airport": {"id": "NRT", "time": "2027-03-11 08:00"},
                          "flight_number": "XX 1"}]},
        ],
        "other_flights": [],
    }

    assert serp.parse_offers(broken, "BER", "NRT", DAY) == []


# --- Budget ------------------------------------------------------------------


def test_budget_counts_up_and_blocks_at_the_cap(tmp_path):
    conn = db.connect(tmp_path / "budget.db")
    budget = MonthlyBudget(conn, "serpapi", cap=2)
    now = datetime(2027, 3, 10, 9, 0, 0)

    assert budget.used(now=now) == 0
    assert budget.consume(now=now) is True
    assert budget.consume(now=now) is True
    assert budget.used(now=now) == 2
    assert budget.consume(now=now) is False
    assert budget.used(now=now) == 2


def test_budget_starts_over_in_a_new_month(tmp_path):
    conn = db.connect(tmp_path / "budget.db")
    budget = MonthlyBudget(conn, "serpapi", cap=1)

    assert budget.consume(now=datetime(2027, 3, 31, 23, 0, 0)) is True
    assert budget.consume(now=datetime(2027, 3, 31, 23, 30, 0)) is False
    assert budget.consume(now=datetime(2027, 4, 1, 0, 30, 0)) is True
    assert month_key(datetime(2027, 4, 1)) == "2027-04"


# --- Adapter -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_leg_sends_the_documented_parameters(monkeypatch):
    src = source(monkeypatch)
    seen: dict = {}

    async def fake(url, *, params=None, **kw):
        seen["url"] = url
        seen["params"] = params
        return payload()

    monkeypatch.setattr(src, "fetch_json", fake)
    offers = await src.search_leg("BER", "NRT", DAY, pax=Pax(adults=2),
                                  cabin=Cabin.ECONOMY, currency="EUR")

    assert seen["url"] == serp.ENDPOINT
    assert seen["params"]["engine"] == "google_flights"
    assert seen["params"]["departure_id"] == "BER"
    assert seen["params"]["arrival_id"] == "NRT"
    assert seen["params"]["outbound_date"] == "2027-03-10"
    assert seen["params"]["type"] == "2"
    assert seen["params"]["currency"] == "EUR"
    assert seen["params"]["adults"] == 2
    assert seen["params"]["api_key"] == "k-123"
    assert offers[0].price == Money(61200, "EUR")


@pytest.mark.asyncio
async def test_search_leg_blocks_instead_of_burning_the_budget(monkeypatch, tmp_path):
    conn = db.connect(tmp_path / "budget.db")
    src = source(monkeypatch, budget=MonthlyBudget(conn, "serpapi", cap=1))
    calls = 0

    async def fake(url, **kw):
        nonlocal calls
        calls += 1
        return payload()

    monkeypatch.setattr(src, "fetch_json", fake)

    await src.search_leg("BER", "NRT", DAY)
    with pytest.raises(SourceBlocked, match="Budget"):
        await src.search_leg("BER", "NRT", DAY)

    assert calls == 1


@pytest.mark.asyncio
async def test_an_open_circuit_costs_no_budget(monkeypatch, tmp_path):
    """Ein Abruf, der ohnehin nicht rausgeht, darf keinen Monatsabruf kosten."""
    conn = db.connect(tmp_path / "budget.db")
    budget = MonthlyBudget(conn, "serpapi", cap=5)
    src = source(monkeypatch, budget=budget)
    calls = 0

    async def fake(url, **kw):
        nonlocal calls
        calls += 1
        return payload()

    monkeypatch.setattr(src, "fetch_json", fake)
    for _ in range(src.breaker.threshold):
        src.breaker.record_block()

    with pytest.raises(SourceBlocked, match="circuit"):
        await src.search_leg("BER", "NRT", DAY)

    assert calls == 0
    assert budget.used() == 0


@pytest.mark.asyncio
async def test_an_api_error_is_a_source_error(monkeypatch):
    src = source(monkeypatch)

    async def fake(url, **kw):
        return {"error": "Invalid API key"}

    monkeypatch.setattr(src, "fetch_json", fake)

    with pytest.raises(SourceError, match="Invalid API key"):
        await src.search_leg("BER", "NRT", DAY)


def test_the_adapter_needs_a_key(tmp_path):
    conn = db.connect(tmp_path / "budget.db")

    assert serp.from_env({}, conn=conn) is None
    assert serp.from_env({"SERPAPI_KEY": "   "}, conn=conn) is None

    built = serp.from_env({"SERPAPI_KEY": "abc", "SERPAPI_MONTHLY_CAP": "42"}, conn=conn)
    assert built is not None
    assert built.api_key == "abc"
    assert built.budget.cap == 42
    assert serp.from_env({"SERPAPI_KEY": "abc"}, conn=conn).budget.cap == serp.DEFAULT_CAP


# --- Sparsam mit einer bezahlten API -----------------------------------------


@pytest.mark.asyncio
async def test_a_paid_call_is_not_retried_blindly(monkeypatch):
    """Jeder Versuch kostet einen Abruf aus dem Monatsbudget."""
    src = source(monkeypatch)
    seen: dict = {}

    async def fake(url, *, params=None, retries=3, **kw):
        seen["retries"] = retries
        return payload()

    monkeypatch.setattr(src, "fetch_json", fake)
    await src.search_leg("BER", "NRT", DAY)

    assert seen["retries"] == 1


def test_a_key_without_a_connection_is_refused():
    """Ohne Verbindung gibt es kein Budget, und ohne Budget keine bezahlte Quelle."""
    with pytest.raises(ValueError, match="conn"):
        serp.from_env({"SERPAPI_KEY": "abc"}, conn=None)

    # Ohne Schluessel gibt es nichts zu schuetzen.
    assert serp.from_env({}, conn=None) is None

    with pytest.raises(TypeError):
        serp.from_env({"SERPAPI_KEY": "abc"})


@pytest.mark.asyncio
async def test_an_empty_result_is_not_an_error(monkeypatch):
    src = source(monkeypatch)

    async def fake(url, **kw):
        return {
            "search_metadata": {"status": "Success"},
            "error": "Google Flights hasn't returned any results for this query.",
        }

    monkeypatch.setattr(src, "fetch_json", fake)

    assert await src.search_leg("BER", "NRT", DAY) == []


@pytest.mark.asyncio
async def test_a_failed_search_status_is_a_source_error(monkeypatch):
    src = source(monkeypatch)

    async def fake(url, **kw):
        data = payload()
        data["search_metadata"]["status"] = "Error"
        return data

    monkeypatch.setattr(src, "fetch_json", fake)

    with pytest.raises(SourceError, match="Error"):
        await src.search_leg("BER", "NRT", DAY)


@pytest.mark.asyncio
async def test_the_documented_success_status_passes(monkeypatch):
    src = source(monkeypatch)

    async def fake(url, **kw):
        return payload()

    monkeypatch.setattr(src, "fetch_json", fake)

    assert len(await src.search_leg("BER", "NRT", DAY)) == 3


def test_a_segment_without_a_flight_number_falls_back_to_the_airline():
    data = {
        "search_metadata": {"status": "Success"},
        "best_flights": [
            {
                "price": 500,
                "flights": [
                    {
                        "departure_airport": {"id": "BER", "time": "2027-03-10 08:00"},
                        "arrival_airport": {"id": "IST", "time": "2027-03-10 12:00"},
                        "airline": "Turkish Airlines",
                    },
                    {
                        "departure_airport": {"id": "IST", "time": "2027-03-10 14:00"},
                        "arrival_airport": {"id": "NRT", "time": "2027-03-11 08:00"},
                        "airline": "Nauru Airlines",
                    },
                ],
            }
        ],
        "other_flights": [],
    }

    best = serp.parse_offers(data, "BER", "NRT", DAY)[0]

    # Bekannte Namen werden zum Code, unbekannte bleiben stehen statt zu raten.
    assert [s.carrier for s in best.segments] == ["TK", "Nauru Airlines"]
    assert best.stops == 1


def test_an_itinerary_without_any_airline_is_still_skipped():
    data = {
        "best_flights": [
            {
                "price": 500,
                "flights": [
                    {
                        "departure_airport": {"id": "BER", "time": "2027-03-10 08:00"},
                        "arrival_airport": {"id": "NRT", "time": "2027-03-11 08:00"},
                    }
                ],
            }
        ]
    }

    assert serp.parse_offers(data, "BER", "NRT", DAY) == []


# --- Budget: eine Anweisung, ein Monat ---------------------------------------


def test_the_month_comes_from_utc_not_from_the_local_clock(monkeypatch):
    """Wer in UTC+13 sitzt, buchte sonst am Monatsersten aus dem Vormonat."""
    from datetime import timezone

    from flightopt.storage import budget as budget_mod

    seen: dict = {}

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            seen["tz"] = tz
            return datetime(2027, 4, 1, 0, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(budget_mod, "datetime", FakeDatetime)

    assert budget_mod.month_key() == "2027-04"
    assert seen["tz"] is timezone.utc


def test_the_cap_holds_even_when_consume_is_called_in_a_row(tmp_path):
    """Zaehlen und Pruefen passieren in einer Anweisung, nicht nacheinander."""
    conn = db.connect(tmp_path / "budget.db")
    budget = MonthlyBudget(conn, "serpapi", cap=3)
    now = datetime(2027, 3, 10, 9, 0, 0)

    granted = [budget.consume(now=now) for _ in range(6)]

    assert granted == [True, True, True, False, False, False]
    assert budget.used(now=now) == 3
    assert budget.remaining(now=now) == 0


def test_a_cap_of_zero_never_books_a_call(tmp_path):
    conn = db.connect(tmp_path / "budget.db")
    budget = MonthlyBudget(conn, "serpapi", cap=0)
    now = datetime(2027, 3, 10, 9, 0, 0)

    assert budget.consume(now=now) is False
    assert budget.used(now=now) == 0
    assert conn.execute("SELECT COUNT(*) FROM api_budget").fetchone()[0] == 0


def test_consume_books_a_call_in_a_single_statement(tmp_path):
    """Erst lesen, dann schreiben hiesse: zwei Jobs buchen dasselbe Kontingent."""
    conn = db.connect(tmp_path / "budget.db")
    budget = MonthlyBudget(conn, "serpapi", cap=2)
    statements: list[str] = []

    conn.set_trace_callback(statements.append)
    try:
        budget.consume(now=datetime(2027, 3, 10, 9, 0, 0))
    finally:
        conn.set_trace_callback(None)

    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("INSERT")


# --- Katalog und Verify-Reihenfolge -----------------------------------------

from flightopt.domain.models import LegSpec, Offer, SearchSpec  # noqa: E402
from flightopt.jobs.runner import build_catalogue  # noqa: E402
from flightopt.search.dp import Combination  # noqa: E402
from flightopt.search.verify import verify  # noqa: E402


def test_catalogue_has_no_serpapi_without_a_key(tmp_path):
    conn = db.connect(tmp_path / "cat.db")
    names = [s.name for s in build_catalogue(set(), env={}, conn=conn)]

    assert "serpapi" not in names
    assert "kiwi" in names
    assert len(names) == len(set(names))


def test_catalogue_adds_serpapi_with_a_key(tmp_path):
    conn = db.connect(tmp_path / "cat.db")
    sources = build_catalogue(set(), env={"SERPAPI_KEY": "abc"}, conn=conn)

    assert "serpapi" in [s.name for s in sources]
    assert [s for s in sources if s.name == "serpapi"][0].budget is not None


def test_an_airline_filter_drops_the_collectors_it_cannot_narrow(tmp_path):
    conn = db.connect(tmp_path / "cat.db")
    sources = build_catalogue({"FR"}, env={"SERPAPI_KEY": "abc"}, conn=conn)
    names = [s.name for s in sources]

    assert names.count("kiwi") == 1
    assert "ryanair" in names
    assert "wizz" not in names
    # SerpApi kennt keinen Airline-Filter, also darf es einen nicht vortaeuschen.
    assert "serpapi" not in names


class Airline:
    name = "airline"
    carrier = "JL"
    carriers = ("JL",)
    supports_calendar = False
    supports_search = True

    def __init__(self, *, serves: bool = True, answers: bool = True) -> None:
        self.serves = serves
        self.answers = answers
        self.calls = 0

    def supports_route(self, origin, destination):
        return self.serves

    async def search_leg(self, origin, destination, day, *, pax=Pax(),
                         cabin=Cabin.ECONOMY, currency="EUR"):
        self.calls += 1
        if not self.answers:
            return []
        return [Offer(source=self.name, origin=origin, destination=destination,
                      travel_date=day, price=Money(50000, currency))]


class Collector:
    name = "collector"
    carrier = ""
    carriers = ()
    supports_calendar = False
    supports_search = True

    def __init__(self) -> None:
        self.calls = 0

    def supports_route(self, origin, destination):
        return True

    async def search_leg(self, origin, destination, day, *, pax=Pax(),
                         cabin=Cabin.ECONOMY, currency="EUR"):
        self.calls += 1
        return [Offer(source=self.name, origin=origin, destination=destination,
                      travel_date=day, price=Money(40000, currency))]


def verify_spec() -> SearchSpec:
    return SearchSpec(legs=(LegSpec("BER", "NRT"),), stays=(),
                      window_start=DAY, window_end=DAY)


@pytest.mark.asyncio
async def test_the_collector_stays_unused_when_an_airline_answers():
    airline, collector = Airline(), Collector()
    combo = Combination(dates=(DAY,), total=Money(50000, "EUR"))

    verified, _ = await verify(verify_spec(), [combo], [collector, airline])

    assert airline.calls == 1
    assert collector.calls == 0
    assert verified[0].offers[0].source == "airline"


@pytest.mark.asyncio
async def test_the_collector_answers_when_no_airline_serves_the_route():
    airline, collector = Airline(serves=False), Collector()
    combo = Combination(dates=(DAY,), total=Money(50000, "EUR"))

    verified, _ = await verify(verify_spec(), [combo], [airline, collector])

    assert airline.calls == 0
    assert collector.calls == 1
    assert verified[0].offers[0].source == "collector"


@pytest.mark.asyncio
async def test_the_collector_answers_when_the_airline_finds_nothing():
    """Kein Flug ist keine Antwort: dann darf die Sammelquelle doch ran."""
    airline, collector = Airline(answers=False), Collector()
    combo = Combination(dates=(DAY,), total=Money(50000, "EUR"))

    verified, _ = await verify(verify_spec(), [combo], [airline, collector])

    assert airline.calls == 1
    assert collector.calls == 1
    assert verified[0].offers[0].source == "collector"


# --- Fehlerliste: einmal sagen reicht ----------------------------------------


class BrokenCollector:
    """Steht fuer SerpApi mit leerem Monatsbudget: jeder Abruf scheitert gleich."""

    name = "serpapi"
    carrier = ""
    carriers = ()
    supports_calendar = False
    supports_search = True

    def supports_route(self, origin, destination):
        return True

    async def search_leg(self, origin, destination, day, *, pax=Pax(),
                         cabin=Cabin.ECONOMY, currency="EUR"):
        raise SourceBlocked(
            "serpapi: Budget von 200 Abrufen fuer diesen Monat aufgebraucht"
        )


@pytest.mark.asyncio
async def test_one_exhausted_budget_is_one_error_line():
    """Sechs Leg-Tage am selben leeren Budget sind eine Nachricht, nicht sechs."""
    spec = SearchSpec(
        legs=(LegSpec("BER", "NRT"),),
        stays=(),
        window_start=DAY,
        window_end=date(2027, 3, 15),
    )
    combos = [
        Combination(dates=(date(2027, 3, 10 + n),), total=Money(50000, "EUR"))
        for n in range(6)
    ]

    _, report = await verify(spec, combos, [BrokenCollector()], limit=10)

    assert len(report.errors) == 1
    assert "Budget" in report.errors[0]


# --- Deployment: ohne durchgereichten Schluessel gibt es die Quelle nicht -----

ROOT = Path(__file__).resolve().parent.parent


def test_the_stack_hands_the_key_and_the_cap_to_the_container():
    """from_env liest os.environ des Containers, nicht das des Hosts."""
    compose = (ROOT / "docker-compose.portainer.yml").read_text(encoding="utf-8")

    assert "SERPAPI_KEY: ${SERPAPI_KEY:-}" in compose
    assert "SERPAPI_MONTHLY_CAP: ${SERPAPI_MONTHLY_CAP:-}" in compose


def test_the_env_example_names_both_variables_without_a_value():
    example = (ROOT / "deploy" / "portainer.env.example").read_text(encoding="utf-8")

    assert "SERPAPI_KEY=" in example  # sync-public: ok
    assert "SERPAPI_MONTHLY_CAP=" in example
    # Ein echter Schluessel im Beispiel waere ein Leck, kein Beispiel.
    for line in example.splitlines():
        if line.startswith("SERPAPI_"):
            assert line.endswith("=")
