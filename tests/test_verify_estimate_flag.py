"""Ein Angebot sagt selbst, ob es geprueft ist.

`verify` und `JobRunner._leg_payload` setzten `is_estimate` fest auf False:
wer die Nachpruefung durchlaufen hatte, galt als geprueft - unabhaengig davon,
was die Quelle ueber ihr eigenes Ergebnis sagte.

Warum das zaehlt: `is_estimate` steht im Schluessel von `flight_baseline`
(`docs/PRICE_HISTORY.md`, Abschnitt 3). Ein falsch als geprueft gebuchter Preis
landet in der falschen Grundgesamtheit und weicht die Trennung wieder auf, die
Kalenderpreise und Live-Preise auseinanderhaelt.

Die Tests hier halten genau eine Regel fest: durchreichen statt setzen. Sie
sagen nichts darueber, wann eine Quelle ihr Ergebnis als Schaetzung einstuft -
das ist ihre eigene Entscheidung und steht in ihrem Adapter.
"""

from __future__ import annotations

from datetime import date

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, Offer, SearchSpec
from flightopt.jobs import runner as runner_module
from flightopt.jobs.runner import JobRunner, price_band
from flightopt.search.dp import Combination
from flightopt.search.grid import GridReport
from flightopt.search.verify import VerifiedItinerary, VerifyReport, verify
from flightopt.storage import db
from flightopt.storage.cache import SqliteCache, SqliteHistory
from tests.test_price_band import OBSERVED, put_baseline
from tests.test_verify_order import combo, make_spec

OUT = date(2026, 10, 1)
LINK = "https://example.invalid/buchen"


def one_leg_spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"),),
        stays=(),
        window_start=OUT,
        window_end=OUT,
    )


def day_price(day: date = OUT, *, is_estimate: bool,
              native: Money | None = None) -> Offer:
    """Eine Tagessuche ohne Flugplan - Preis ja, Verbindung nein."""
    return Offer(
        source="tagessuche",
        origin="BER",
        destination="ATH",
        travel_date=day,
        price=Money(5000, "EUR"),
        deep_link=LINK,
        is_estimate=is_estimate,
        price_native=native,
    )


class DaySearchSource:
    """Eine Quelle, die ihr eigenes Ergebnis einstuft."""

    supports_calendar = False
    supports_search = True
    name = "tagessuche"
    carrier = "TS"
    indicative = False

    def __init__(self, *, is_estimate: bool, native: Money | None = None) -> None:
        self.is_estimate = is_estimate
        self.native = native
        self.calls = 0

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def search_leg(self, origin, destination, day, *, pax=None, cabin=None,
                         currency: str = "EUR"):
        self.calls += 1
        return [day_price(day, is_estimate=self.is_estimate, native=self.native)]


# --- Historienschreiber ------------------------------------------------------


@pytest.mark.parametrize("says_estimate, written", [(True, 1), (False, 0)])
async def test_the_history_writes_what_the_source_said(tmp_path, says_estimate, written):
    """Die Beobachtung traegt das Kennzeichen der Quelle, nicht das der Phase.

    Vorher stand an jeder Zeile aus der Nachpruefung `is_estimate=0`, allein
    weil sie aus der Nachpruefung kam.
    """
    conn = db.connect(str(tmp_path / "verify.db"))
    source = DaySearchSource(is_estimate=says_estimate)

    await verify(
        make_spec(1), [combo((2026, 10, 1))], [source],
        history=SqliteHistory(conn), limit=10,
    )

    rows = conn.execute("SELECT is_estimate FROM price_observation").fetchall()
    assert [row["is_estimate"] for row in rows] == [written]


# --- Zwischenspeicher --------------------------------------------------------


async def test_the_cache_does_not_turn_an_estimate_into_a_checked_price(tmp_path):
    """Der Umweg ueber den Cache darf das Kennzeichen nicht verlieren.

    `_offer_to_cache` legte es gar nicht erst ab, `_offer_from_cache` schrieb
    beim Lesen fest False hin. Ein zweiter Lauf innerhalb der TTL machte damit
    aus jeder Schaetzung einen geprueften Preis.
    """
    conn = db.connect(str(tmp_path / "cache.db"))
    cache = SqliteCache(conn)
    spec = make_spec(1)
    shortlist = [combo((2026, 10, 1))]
    source = DaySearchSource(is_estimate=True)

    await verify(spec, shortlist, [source], cache=cache, limit=10)
    verified, report = await verify(spec, shortlist, [source], cache=cache, limit=10)

    assert (source.calls, report.cache_hits) == (1, 1), "der zweite Lauf kam nicht aus dem Cache"
    assert verified[0].offers[0].is_estimate is True


async def test_the_history_of_a_cached_run_keeps_the_flag_too(tmp_path):
    """Beide Wege in die Historie muessen dasselbe sagen, sonst zerfaellt sie."""
    conn = db.connect(str(tmp_path / "cached-history.db"))
    cache = SqliteCache(conn)
    spec = make_spec(1)
    shortlist = [combo((2026, 10, 1))]
    source = DaySearchSource(is_estimate=True)

    await verify(spec, shortlist, [source], cache=cache,
                 history=SqliteHistory(conn), limit=10)
    await verify(spec, shortlist, [source], cache=cache,
                 history=SqliteHistory(conn), limit=10)

    rows = conn.execute("SELECT is_estimate FROM price_observation").fetchall()
    assert [row["is_estimate"] for row in rows] == [1, 1]


async def test_the_cache_keeps_the_native_price(tmp_path):
    """Dieselbe Luecke eine Zeile weiter: `price_native` fiel im Cache aus.

    Der zweite Lauf innerhalb der TTL bekam ein Angebot ohne Nativwaehrung
    zurueck, und `_leg_payload` loescht das Feld dann aus der Zeile. Der Nutzer
    sah je nach Cache-Zustand mal den Originalpreis der Airline und mal nicht,
    ohne dass sich etwas geaendert haette.
    """
    conn = db.connect(str(tmp_path / "native.db"))
    cache = SqliteCache(conn)
    spec = make_spec(1)
    shortlist = [combo((2026, 10, 1))]
    source = DaySearchSource(is_estimate=False, native=Money(1990000, "HUF"))

    await verify(spec, shortlist, [source], cache=cache, limit=10)
    verified, report = await verify(spec, shortlist, [source], cache=cache, limit=10)

    assert (source.calls, report.cache_hits) == (1, 1), "der zweite Lauf kam nicht aus dem Cache"
    assert verified[0].offers[0].price_native == Money(1990000, "HUF")


# --- Ergebniszeile -----------------------------------------------------------


def test_a_leg_counts_as_checked_only_when_the_offer_says_so():
    day = OUT
    dates = Combination(dates=(day,), total=Money(6500, "EUR"))
    live = VerifiedItinerary(
        combination=dates, offers=[day_price(day, is_estimate=True)]
    )

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(6500, "EUR")}], dates, 0, day, live,
        by_source={"tagessuche": "TS"},
    )

    assert leg["verified"] is False


def test_such_a_leg_keeps_its_live_price_its_link_and_its_source():
    """Es ist ein Preis, keine Verbindung - aber ein buchbarer Preis.

    Das Angebot darf deshalb nicht verschwinden: der Nutzer bekommt weiter den
    live geholten Preis und den Link, unter dem er ihn buchen kann.
    """
    day = OUT
    dates = Combination(dates=(day,), total=Money(6500, "EUR"))
    live = VerifiedItinerary(
        combination=dates, offers=[day_price(day, is_estimate=True)]
    )

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(6500, "EUR")}], dates, 0, day, live,
        by_source={"tagessuche": "TS"},
    )

    assert leg["price"] == 50.0
    assert leg["deep_link"] == LINK
    assert leg["source"] == "tagessuche"
    # Ohne Segmente gibt es keine Umstiegszahl. Null waere eine Aussage ueber
    # einen Direktflug, den niemand gesehen hat.
    assert leg["stops"] is None


def test_a_live_price_from_a_comparison_site_stays_a_guide():
    """Dieselbe Bauart wie `verified`: `indicative` stand fest auf False.

    Heute faellt es nicht auf, weil keine Quelle mit `supports_search` einen
    Aufschlag traegt - Kiwi ist ein Richtwert, sucht aber keine Tage. Bekaeme
    ein Vergleichsportal je eine Tagessuche, wuerde sein Aufschlag ab dann als
    Tarif ausgegeben. Die Quelle sagt es, die Zeile reicht es durch.
    """
    day = OUT
    dates = Combination(dates=(day,), total=Money(6500, "EUR"))
    live = VerifiedItinerary(
        combination=dates, offers=[day_price(day, is_estimate=False)]
    )

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(6500, "EUR")}], dates, 0, day, live,
        by_source={"tagessuche": "TS"},
        indicative_sources={"tagessuche"},
    )

    assert leg["indicative"] is True

    firm = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(6500, "EUR")}], dates, 0, day, live,
        by_source={"tagessuche": "TS"},
    )

    assert firm["indicative"] is False


def test_a_leg_from_a_source_that_says_checked_stays_checked():
    """Die Gegenprobe: durchgereicht wird in beide Richtungen."""
    day = OUT
    dates = Combination(dates=(day,), total=Money(6500, "EUR"))
    live = VerifiedItinerary(
        combination=dates, offers=[day_price(day, is_estimate=False)]
    )

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(6500, "EUR")}], dates, 0, day, live,
        by_source={"tagessuche": "TS"},
    )

    assert leg["verified"] is True
    assert leg["deep_link"] == LINK


# --- Preislage ---------------------------------------------------------------


def test_the_band_of_such_a_leg_is_measured_against_the_estimates(tmp_path):
    """Gemessen wird gegen die eigene Grundgesamtheit, nicht gegen die andere.

    Beide Baselines stehen bereit und sagen Verschiedenes. Nur die richtige
    darf zaehlen, sonst misst die Zeile den Unterschied der beiden Messarten
    statt den Preis.
    """
    conn = db.connect(str(tmp_path / "band.db"))
    put_baseline(conn, "BER|ATH", median_minor=5100, mad_minor=100,
                 is_estimate=True)
    put_baseline(conn, "BER|ATH", median_minor=20000, mad_minor=100,
                 is_estimate=False)
    day = OUT
    dates = Combination(dates=(day,), total=Money(6500, "EUR"))
    live = VerifiedItinerary(
        combination=dates, offers=[day_price(day, is_estimate=True)]
    )

    leg = JobRunner._leg_payload(
        one_leg_spec(), [{day: Money(6500, "EUR")}], dates, 0, day, live,
        by_source={"tagessuche": "TS"},
    )
    band = price_band(conn, leg, observed_at=OBSERVED)

    assert band["population"] == "estimate"
    assert band["median"] == 51.0
    assert band["tier"] == "normal"


# --- Zeile und gespeicherter Lauf --------------------------------------------


def offline(monkeypatch, offer: Offer) -> None:
    """Ein Lauf ohne Netz, dessen Nachpruefung genau dieses Angebot liefert."""

    async def no_routes(sources, legs):
        return None

    async def fake_build_grid(spec, sources, **kwargs):
        return ({0: {OUT: Money(6500)}},
                GridReport(filled={0: 1}, calls=1, winner={(0, OUT): "TS"}))

    def fake_solve(spec, grid, **kwargs):
        return [Combination(dates=(OUT,), total=Money(6500))]

    async def fake_verify(spec, best, sources, **kwargs):
        item = VerifiedItinerary(combination=best[0], offers=[offer])
        return [item], VerifyReport(confirmed=1)

    async def fake_rates(conn, **kwargs):
        return Rates()

    monkeypatch.setattr(runner_module, "preload_routes", no_routes)
    monkeypatch.setattr(runner_module, "build_grid", fake_build_grid)
    monkeypatch.setattr(runner_module, "solve", fake_solve)
    monkeypatch.setattr(runner_module, "verify", fake_verify)
    monkeypatch.setattr(runner_module, "build_catalogue", lambda wanted, **kw: [])
    monkeypatch.setattr(runner_module.fx_store, "current_rates", fake_rates)


@pytest.mark.parametrize("says_estimate, row_verified, stored", [
    (True, False, 1),
    (False, True, 0),
])
async def test_a_row_is_checked_only_when_every_leg_is(
    tmp_path, monkeypatch, says_estimate, row_verified, stored
):
    """Die Zeile behauptet nicht mehr, als ihre Teilstrecken hergeben.

    `verified` an der Zeile wird zur Spalte `is_estimate` in
    `itinerary_result` und von dort in jede gespeicherte Ansicht. Haengt sie
    allein daran, dass jedes Bein aufgeloest wurde, steht "geprueft" auch ueber
    einer Zeile aus lauter Schaetzpreisen.
    """
    offline(monkeypatch, day_price(is_estimate=says_estimate))
    path = str(tmp_path / "row.db")
    runner = JobRunner(path)
    spec = one_leg_spec()
    job_id = runner.create(spec)

    await runner._run(job_id, spec)

    done = runner._history[job_id][-1]
    assert done.phase == "done"
    row = done.detail["results"][0]
    assert row["verified"] is row_verified
    assert row["legs"][0]["verified"] is row_verified

    conn = db.connect(path)
    kept = conn.execute(
        "SELECT is_estimate FROM itinerary_result WHERE job_id=?", (job_id,)
    ).fetchone()
    assert kept["is_estimate"] == stored
