"""Was in eine Flug-Baseline darf und was nicht.

Zwei Regeln, beide sagen dasselbe: Gleiches gehoert zu Gleichem. Ein Richtwert
eines Vergleichsportals ist kein Tarif, und eine Kalenderschaetzung ist kein
gepruefter Preis. Wo nach beiden Regeln zu wenig uebrig bleibt, gibt es keine
Baseline - und `unknown` ist dann die richtige Antwort und keine Luecke.

Kein Test fasst hier ein Netz an: die Beobachtungen werden gesetzt, die
Quellen sind Attrappen.
"""

from __future__ import annotations

from datetime import date, datetime

from flightopt.domain.models import LegSpec, Money, Offer, SearchSpec
from flightopt.jobs.runner import price_band
from flightopt.search import grid as grid_mod
from flightopt.search.dp import Combination
from flightopt.search.verify import verify
from flightopt.storage import db
from flightopt.storage.baseline import detect_price_signal, refresh_baselines
from flightopt.storage.cache import SqliteHistory

TRAVEL = date(2026, 11, 10)
OBSERVED = datetime(2026, 9, 8, 12, 0)
ROUTE = "BER|ATH"


def observe(conn, prices, *, source="ryanair", indicative=False, estimate=True,
            entity_key=ROUTE) -> None:
    for price in prices:
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
            "travel_date, party_size, currency, price_total_minor, is_estimate, "
            "is_indicative) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), source, "flight", entity_key,
             TRAVEL.isoformat(), 1, "EUR", int(price), int(estimate), int(indicative)),
        )


def baselines(conn) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT entity_key, is_estimate, median_minor, n FROM flight_baseline "
            "ORDER BY entity_key, is_estimate"
        )
    ]


def signal(conn, price_minor: int, *, estimate: bool = True, key: str = ROUTE) -> dict:
    return detect_price_signal(
        conn, key, TRAVEL, price_minor, observed_at=OBSERVED, is_estimate=estimate
    )


# --- Regel eins: Richtwerte bleiben draussen --------------------------------


def test_an_indicative_source_moves_neither_the_median_nor_the_count(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [20000, 20000, 20000, 20000, 20000], source="aegean")
    # Dieselbe Strecke, derselbe Tag, aber ein Portal mit bekanntem Aufschlag.
    observe(conn, [9000] * 20, source="kiwi", indicative=True)

    assert refresh_baselines(conn, now=OBSERVED) == 1
    rows = baselines(conn)

    # Der Median steht dort, wo ihn die fuenf echten Tarife hinsetzen, und `n`
    # zaehlt fuenf. Zwanzig Richtwerte haben ihn nicht um einen Cent bewegt.
    assert (rows[0]["median_minor"], rows[0]["n"]) == (20000, 5)
    conn.close()


def test_a_route_that_only_a_portal_prices_gets_no_baseline_at_all(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [9000] * 30, source="kiwi", indicative=True)

    assert refresh_baselines(conn, now=OBSERVED) == 0
    assert baselines(conn) == []
    # Dreissig Beobachtungen, und trotzdem keine Aussage. Genau so ist es
    # gemeint: eine Baseline aus Richtwerten misst den Aufschlag, nicht den Markt.
    assert signal(conn, 5000)["status"] == "unknown"
    conn.close()


async def test_the_grid_writes_down_what_the_catalogue_knew(tmp_path):
    """Das Kennzeichen entsteht beim Schreiben, nicht beim Rechnen."""

    class Portal:
        name = "portal"
        carrier = ""
        carriers = ()
        supports_calendar = True
        supports_search = False
        indicative = True

        def supports_route(self, origin, destination):
            return True

        async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
            return {TRAVEL: Money(9900, "EUR")}

    conn = db.connect(tmp_path / "f.db")
    spec = SearchSpec(legs=(LegSpec("BER", "ATH"),), stays=(),
                      window_start=TRAVEL, window_end=TRAVEL)

    await grid_mod.build_grid(spec, [Portal()], history=SqliteHistory(conn))

    row = conn.execute(
        "SELECT source, is_estimate, is_indicative FROM price_observation"
    ).fetchone()
    assert (row["source"], row["is_estimate"], row["is_indicative"]) == ("portal", 1, 1)
    conn.close()


# --- Regel zwei: Schaetzung und Pruefung sind zwei Grundgesamtheiten --------


def test_each_population_keeps_its_own_median(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [10000] * 6, estimate=True)
    observe(conn, [30000] * 6, estimate=False)

    assert refresh_baselines(conn, now=OBSERVED) == 2
    rows = {row["is_estimate"]: row["median_minor"] for row in baselines(conn)}

    assert rows == {1: 10000, 0: 30000}
    # Derselbe Preis, zwei Urteile - weil zwei verschiedene Fragen gestellt sind.
    assert signal(conn, 30000, estimate=False)["status"] == "normal"
    assert signal(conn, 30000, estimate=True)["status"] == "expensive"
    conn.close()


def test_a_verified_price_is_never_held_against_estimates(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [10000] * 20, estimate=True)

    refresh_baselines(conn, now=OBSERVED)
    verdict = signal(conn, 30000, estimate=False)

    # Zwanzig Schaetzungen liegen bereit, und trotzdem bleibt die Antwort
    # `unknown`: sie beschreiben ein anderes Produkt.
    assert verdict["status"] == "unknown"
    assert verdict["population"] == "verified"
    assert verdict["n"] == 0
    conn.close()


def test_too_few_points_in_the_matching_population_stay_unknown(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [10000] * 20, estimate=True)
    observe(conn, [30000] * 4, estimate=False)

    assert refresh_baselines(conn, now=OBSERVED) == 1
    assert [row["is_estimate"] for row in baselines(conn)] == [1]
    assert signal(conn, 30000, estimate=False)["status"] == "unknown"
    conn.close()


def test_the_count_says_how_thin_the_basis_is(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [10000, 10100, 10200, 10300, 10400], estimate=True)

    refresh_baselines(conn, now=OBSERVED)
    verdict = signal(conn, 10200)

    assert verdict["n"] == 5
    assert verdict["thin"] is True
    assert verdict["population"] == "estimate"
    conn.close()


# --- Der Weg bis in die Oberflaeche ----------------------------------------


def leg(price: float, *, verified: bool) -> dict:
    return {
        "origin": "BER", "destination": "ATH",
        "date": TRAVEL.isoformat(), "price": price, "verified": verified,
    }


def test_the_band_of_a_leg_follows_its_own_kind_of_price(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [10000] * 6, estimate=True)
    observe(conn, [30000] * 6, estimate=False)
    refresh_baselines(conn, now=OBSERVED)

    estimated = price_band(conn, leg(300.0, verified=False), observed_at=OBSERVED)
    checked = price_band(conn, leg(300.0, verified=True), observed_at=OBSERVED)

    assert estimated["tier"] == "expensive"
    assert estimated["population"] == "estimate"
    assert checked["tier"] == "normal"
    assert checked["population"] == "verified"
    # `n` reist mit, damit die Oberflaeche sagen kann, worauf die Stufe steht.
    assert checked["n"] == 6
    conn.close()


def test_a_verified_leg_without_a_verified_history_says_nothing(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    observe(conn, [10000] * 20, estimate=True)
    refresh_baselines(conn, now=OBSERVED)

    band = price_band(conn, leg(300.0, verified=True), observed_at=OBSERVED)

    assert band["tier"] == "unknown"
    assert band["n"] == 0
    conn.close()


async def test_a_confirmed_flight_becomes_a_verified_observation(tmp_path):
    """Ohne diesen Schritt bliebe die gepruefte Grundgesamtheit fuer immer leer."""

    class Airline:
        name = "attrappe"
        carrier = "XX"
        carriers = ("XX",)
        supports_calendar = False
        supports_search = True
        indicative = False

        def supports_route(self, origin, destination):
            return True

        async def search_leg(self, origin, destination, day, **kwargs):
            return [Offer(source=self.name, origin=origin, destination=destination,
                          travel_date=day, price=Money(18500, "EUR"))]

    conn = db.connect(tmp_path / "f.db")
    spec = SearchSpec(legs=(LegSpec("BER", "ATH"),), stays=(),
                      window_start=TRAVEL, window_end=TRAVEL)
    combos = [Combination(dates=(TRAVEL,), total=Money(18500, "EUR"))]

    await verify(spec, combos, [Airline()], history=SqliteHistory(conn))

    row = conn.execute(
        "SELECT entity_key, price_total_minor, is_estimate, is_indicative "
        "FROM price_observation"
    ).fetchone()
    assert row["entity_key"] == ROUTE
    assert (row["price_total_minor"], row["is_estimate"], row["is_indicative"]) == (
        18500, 0, 0
    )
    conn.close()


# --- Die Hotelseite bleibt, wie sie ist ------------------------------------


def test_hotels_keep_their_own_table_their_counts_and_their_fourth_tier(tmp_path):
    """Fluege haben drei Stufen, Hotels vier. Daran aendert die Trennung nichts."""
    conn = db.connect(tmp_path / "h.db")
    conn.execute(
        "INSERT INTO hotel_property(property_key, source, name, city, country, "
        "country_code, stars, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        ("trivago:melia", "trivago", "Melia Athens", "Athens", "Greece", "GR", 4,
         "2026-09-01T10:00:00", "2026-09-08T10:00:00"),
    )
    for price in (8600, 8800, 9000, 9200, 9400):
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
            "travel_date, return_or_nights, party_size, currency, price_total_minor, "
            "is_estimate) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), "trivago", "hotel",
             "GR|trivago:melia", TRAVEL.isoformat(), "1", 2, "EUR", price, 1),
        )

    # Derselbe Rueckgabewert wie eh und je: eine Zeile in `price_baseline`.
    assert refresh_baselines(conn, entity_type="hotel", now=OBSERVED) == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM price_baseline WHERE entity_type='hotel'"
    ).fetchone()["c"] == 1
    # Und keine einzige Hotelzeile in der Flugtabelle.
    assert baselines(conn) == []

    verdict = detect_price_signal(
        conn, "GR|trivago:melia", TRAVEL, 900, observed_at=OBSERVED,
        entity_type="hotel", party_size=2, nights=1,
    )
    assert verdict["tier"] == "error"
    assert verdict["basis"] in {"own", "peer"}
    conn.close()
