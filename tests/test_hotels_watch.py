"""Die Hotel-Beobachtungsliste: was von selbst laeuft und was dabei entsteht.

Zugeschnitten wie die Beobachtungsliste der Fluege (`storage/watchlist.py`),
und zwar bewusst: ein anderer Zuschnitt haette zwei Schleifen mit zwei
Faelligkeitsbegriffen ergeben, und die zweite waere die gewesen, an die
niemand denkt.

Kein Test fasst ein Netz an. Die Quelle ist eine Attrappe, die vorgegebene
Angebote zurueckgibt.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.sources.base import HotelBatch, HotelSource
from flightopt.hotels.watch import (
    HOTEL_WATCH_SCHEMA,
    MAX_WATCHES_PER_RUN,
    MAX_WINDOW_DAYS,
    HotelWatch,
    add_watch,
    due_watches,
    ensure_hotel_watch,
    get_watch,
    hotel_findings,
    list_watches,
    mark_ran,
    run_hotel_watches,
    set_enabled,
    watch_report,
    watch_stats,
)
from flightopt.storage import db

RATES = Rates(base="EUR", rates={"USD": 1.10})
TODAY = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 6, 0)


def connect(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    ensure_hotel_watch(conn)
    return conn


class FakeSource(HotelSource):
    """Liefert je Anreisetag ein Angebot und zaehlt, wonach gefragt wurde."""

    name = "trivago"

    def __init__(self, price_minor: int = 9000) -> None:
        super().__init__()
        self.price_minor = price_minor
        self.asked: list[date] = []

    async def search(self, query: HotelQuery) -> HotelBatch:
        self.asked.append(query.arrival)
        return HotelBatch(
            offers=[
                HotelOffer(
                    source=self.name,
                    property_key="trivago:melia",
                    name="Melia Athens",
                    arrival=query.arrival,
                    departure=query.departure,
                    price_total=Money(self.price_minor, "EUR"),
                    stars=4,
                    city="Athens",
                    country="Greece",
                    party_size=query.party_size,
                )
            ]
        )


# -- Stammdaten ------------------------------------------------------------


def test_a_watch_becomes_a_query_and_a_rolling_window(tmp_path):
    """Ein rollendes Vorlauf-Fenster, kein festes Datum.

    Eine gespeicherte Suche mit festem Reisetag hoert nach dem Reisetag auf,
    Sinn zu ergeben. Eine Beobachtung laeuft weiter, bis jemand sie abschaltet.
    """
    conn = connect(tmp_path)
    watch_id = add_watch(
        conn, "Athen", nights=2, adults=2, lead_min_days=14, lead_max_days=20, now=NOW
    )

    watch = get_watch(conn, watch_id)
    assert watch is not None
    assert watch.window(TODAY) == (date(2026, 11, 15), date(2026, 11, 21))
    query = watch.query(date(2026, 11, 15))
    assert (query.destination, query.nights, query.adults) == ("Athen", 2, 2)
    assert query.departure == date(2026, 11, 17)
    conn.close()


def test_the_same_search_twice_is_one_row(tmp_path):
    """Zweimal dasselbe Ziel mit derselben Belegung waeren zweimal dieselben
    Abrufe am selben Tag bei denselben Quellen."""
    conn = connect(tmp_path)

    first = add_watch(conn, "Athen", nights=2, adults=2, now=NOW)
    second = add_watch(conn, "Athen", nights=2, adults=2, now=NOW)

    assert first == second
    assert len(list_watches(conn)) == 1
    conn.close()


def test_a_different_occupancy_is_a_different_watch(tmp_path):
    """Ein Familienzimmer ist eine andere Frage als ein Doppelzimmer."""
    conn = connect(tmp_path)

    first = add_watch(conn, "Athen", nights=2, adults=2, now=NOW)
    second = add_watch(conn, "Athen", nights=2, adults=4, now=NOW)

    assert first != second
    conn.close()


def test_a_window_beyond_the_cap_is_refused_instead_of_shortened(tmp_path):
    """Gekuerzt wird nichts stillschweigend."""
    conn = connect(tmp_path)

    with pytest.raises(ValueError):
        add_watch(conn, "Athen", lead_min_days=1, lead_max_days=MAX_WINDOW_DAYS + 5, now=NOW)
    conn.close()


def test_a_reversed_window_is_refused(tmp_path):
    conn = connect(tmp_path)

    with pytest.raises(ValueError):
        add_watch(conn, "Athen", lead_min_days=30, lead_max_days=10, now=NOW)
    conn.close()


def test_a_watch_without_a_destination_is_refused(tmp_path):
    conn = connect(tmp_path)

    with pytest.raises(ValueError):
        add_watch(conn, "   ", now=NOW)
    conn.close()


# -- Faelligkeit -----------------------------------------------------------


def test_due_is_a_calendar_day_and_not_an_interval(tmp_path):
    """"Einmal am Tag" soll nicht driften, und ein Neustart nichts ausloesen."""
    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", now=NOW)

    assert [w.id for w in due_watches(conn, now=NOW)] == [watch_id]
    mark_ran(conn, watch_id, now=NOW)
    assert due_watches(conn, now=datetime(2026, 11, 1, 23, 59)) == []
    assert [w.id for w in due_watches(conn, now=datetime(2026, 11, 2, 0, 1))] == [watch_id]
    conn.close()


def test_a_disabled_watch_is_never_due(tmp_path):
    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", now=NOW)

    set_enabled(conn, watch_id, False)

    assert due_watches(conn, now=NOW) == []
    conn.close()


# -- Der Durchgang ---------------------------------------------------------


async def test_a_run_writes_observations_and_marks_the_watch(tmp_path):
    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", lead_min_days=10, lead_max_days=12, now=NOW)
    source = FakeSource()

    report = await run_hotel_watches(conn, sources=[source], rates=RATES, now=NOW)

    assert report["watches"] == 1
    assert report["days"] == 3
    assert report["observations"] == 3
    assert source.asked == [date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)]
    written = conn.execute(
        "SELECT count(*) AS n FROM price_observation WHERE entity_type='hotel'"
    ).fetchone()
    assert written["n"] == 3
    assert due_watches(conn, now=NOW) == []
    assert get_watch(conn, watch_id).last_run_at is not None
    conn.close()


async def test_a_failing_watch_still_counts_as_run(tmp_path):
    """Gelaufen heisst gelaufen. Sonst laeuft dieselbe kaputte Suche im Takt
    des Planers immer wieder gegen dieselbe Wand."""
    conn = connect(tmp_path)
    add_watch(conn, "Athen", lead_min_days=10, lead_max_days=10, now=NOW)

    class Broken(FakeSource):
        async def search(self, query):
            raise RuntimeError("Quelle kaputt")

    report = await run_hotel_watches(conn, sources=[Broken()], rates=RATES, now=NOW)

    assert report["watches"] == 1
    assert report["errors"]
    assert due_watches(conn, now=NOW) == []
    conn.close()


async def test_only_a_few_watches_run_per_pass(tmp_path):
    """Ein Hotelfenster kostet ein Vielfaches eines Flugkalenders.

    Alle faelligen auf einmal zu fahren hiesse, den Takt der Quellen ueber
    Minuten zu halten, waehrend nebenan die Flugsuche warten muss.
    """
    conn = connect(tmp_path)
    for index in range(MAX_WATCHES_PER_RUN + 2):
        add_watch(conn, f"Stadt {index}", lead_min_days=10, lead_max_days=10, now=NOW)

    report = await run_hotel_watches(conn, sources=[FakeSource()], rates=RATES, now=NOW)

    assert report["watches"] == MAX_WATCHES_PER_RUN
    assert report["due_left"] == 2
    conn.close()


async def test_a_pass_without_due_watches_asks_nobody(tmp_path):
    conn = connect(tmp_path)
    source = FakeSource()

    report = await run_hotel_watches(conn, sources=[source], rates=RATES, now=NOW)

    assert report == {
        "watches": 0, "days": 0, "observations": 0, "errors": [], "due_left": 0
    }
    assert source.asked == []
    conn.close()


async def test_a_run_refreshes_the_baselines_once(tmp_path):
    """Ohne Auffrischung waechst die Historie, aber kein Urteil folgt ihr."""
    conn = connect(tmp_path)
    add_watch(conn, "Athen", lead_min_days=14, lead_max_days=27, now=NOW)

    for day in range(3):
        await run_hotel_watches(
            conn,
            sources=[FakeSource(9000 + day)],
            rates=RATES,
            now=NOW + timedelta(days=day),
        )

    rows = conn.execute("SELECT count(*) AS n FROM hotel_baseline").fetchone()
    assert rows["n"] > 0
    conn.close()


async def test_a_narrow_window_needs_weeks_and_not_days(tmp_path):
    """Die unbequeme Rechnung, und sie steht hier, damit sie niemand vergisst.

    Die Baseline gruppiert je Haus, Wochentag, Vorlauf-Stufe, Belegung und
    Waehrung. Ein Fenster von einem einzigen Anreisetag liefert je Durchgang
    genau eine Beobachtung in genau eine Gruppe - und die naechste in dieselbe
    Gruppe erst eine Woche spaeter, weil der Wochentag mitwandert. Fuenf
    Beobachtungen je Gruppe heissen dann fuenf Wochen, nicht fuenf Tage.

    Ein breites Fenster deckt denselben Wochentag mehrfach in derselben
    Vorlauf-Stufe ab und fuellt die Gruppe deshalb um ein Vielfaches schneller.
    """
    conn = connect(tmp_path)
    add_watch(conn, "Athen", lead_min_days=10, lead_max_days=10, now=NOW)

    for day in range(6):
        await run_hotel_watches(
            conn,
            sources=[FakeSource(9000 + day)],
            rates=RATES,
            now=NOW + timedelta(days=day),
        )

    observations = conn.execute(
        "SELECT count(*) AS n FROM price_observation WHERE entity_type='hotel'"
    ).fetchone()
    baselines = conn.execute("SELECT count(*) AS n FROM hotel_baseline").fetchone()

    assert observations["n"] == 6
    assert baselines["n"] == 0
    conn.close()


# -- Was gemeldet wird -----------------------------------------------------


async def test_findings_return_only_the_rows_a_human_should_see(tmp_path):
    """Der Anschluss fuer die Meldung: eine Zeile je Fund, fertig gerechnet."""
    conn = connect(tmp_path)
    add_watch(conn, "Athen", lead_min_days=10, lead_max_days=10, now=NOW)
    await run_hotel_watches(conn, sources=[FakeSource(9000)], rates=RATES, now=NOW)
    await run_hotel_watches(
        conn, sources=[FakeSource(900)], rates=RATES, now=NOW + timedelta(days=1)
    )

    found = hotel_findings(conn, now=NOW + timedelta(days=1))

    assert found
    first = found[0]
    # Die Feldnamen sind die von `hunt.alerts.Find`, damit Fluege und Hotels
    # denselben Melder fuellen koennen.
    assert first["tier"] == "error"
    assert first["entity_key"] == "trivago:melia"
    assert first["travel_date"] == "2026-11-12"
    assert first["price_minor"] == 900
    assert first["currency"] == "EUR"
    assert first["source"] == "trivago"
    assert first["reason"]
    assert first["population"] == "estimate"
    assert first["thin"] is False
    # Alles, was nur Hotels haben, steht unter `detail`.
    assert first["detail"]["name"] == "Melia Athens"
    assert first["detail"]["price"] == 9.0
    assert first["detail"]["verified"] is False
    assert first["detail"]["kind"] == "hotel"
    conn.close()


async def test_findings_stay_empty_when_nothing_is_conspicuous(tmp_path):
    conn = connect(tmp_path)
    add_watch(conn, "Athen", lead_min_days=10, lead_max_days=10, now=NOW)
    await run_hotel_watches(conn, sources=[FakeSource(9000)], rates=RATES, now=NOW)

    assert hotel_findings(conn, now=NOW) == []
    conn.close()


def test_the_report_says_what_is_watched_and_since_when(tmp_path):
    conn = connect(tmp_path)
    add_watch(conn, "Athen", now=NOW)

    report = watch_report(conn, now=NOW)

    assert report["watches"] == 1
    assert report["due"] == 1
    assert report["rows"][0]["destination"] == "Athen"
    # Ohne Historie ist nichts messbar, und das steht da auch.
    assert report["rows"][0]["ready"] is False
    conn.close()


def test_a_watch_row_survives_a_round_trip(tmp_path):
    conn = connect(tmp_path)
    watch_id = add_watch(
        conn,
        "Athen",
        nights=3,
        adults=2,
        children=(4, 9),
        rooms=2,
        stars=(4, 5),
        min_review_score=8.0,
        currency="EUR",
        country="DE",
        now=NOW,
    )

    watch = get_watch(conn, watch_id)

    assert isinstance(watch, HotelWatch)
    assert watch.children == (4, 9)
    assert watch.stars == (4, 5)
    assert watch.min_review_score == 8.0
    query = watch.query(TODAY)
    assert query.children == (4, 9) and query.stars == (4, 5)
    conn.close()


@pytest.mark.parametrize(
    "other",
    [
        {"destination": "Berlin"},
        {"rooms": 2},
        {"stars": (5,)},
        {"min_review_score": 8.5},
        {"min_review_score": 8.04},
        {"children": (9,)},
        {"adults": 1, "children": (4, 9)},
        {"currency": "USD"},
        {"country": "US"},
    ],
)
async def test_watch_history_belongs_to_the_exact_search(tmp_path, other):
    conn = connect(tmp_path)
    settings = {
        "destination": "Athen", "nights": 2, "adults": 2, "children": (4,),
        "stars": (4,), "min_review_score": 8.0,
        "lead_min_days": 10, "lead_max_days": 10, "now": NOW,
    }
    first = add_watch(conn, **settings)
    second = add_watch(conn, **(settings | other))
    assert first != second
    set_enabled(conn, second, False)

    for day in range(5):
        await run_hotel_watches(
            conn, sources=[FakeSource()], rates=RATES, now=NOW + timedelta(days=day)
        )

    first_stats = watch_stats(conn, get_watch(conn, first))
    second_stats = watch_stats(conn, get_watch(conn, second))
    assert first_stats["observations"] == first_stats["days_recorded"] == 5
    assert first_stats["ready"] is True
    assert first_stats["last_observation"] == "2026-11-05T06:00:00"
    assert second_stats["observations"] == second_stats["days_recorded"] == 0
    assert second_stats["last_observation"] is None
    assert second_stats["ready"] is False

    set_enabled(conn, second, True)
    report = await run_hotel_watches(
        conn, sources=[FakeSource()], rates=RATES, now=NOW + timedelta(days=4)
    )
    assert report["observations"] == 1
    assert watch_stats(conn, get_watch(conn, first)) == first_stats
    assert watch_stats(conn, get_watch(conn, second))["observations"] == 1
    conn.close()


async def test_legacy_and_manual_history_stays_global_and_unattributed(tmp_path):
    from flightopt.hotels.store import record_offers
    from flightopt.storage.baseline import refresh_baselines

    conn = db.connect(tmp_path / "h.db")
    query = HotelQuery(destination="Athen", arrival=TODAY + timedelta(days=20))
    # Identical property, dates, party and prices still prove no watch ownership.
    for day in range(5):
        offers = (await FakeSource().search(query)).offers
        await record_offers(conn, offers, rates=RATES, observed_at=NOW + timedelta(days=day))
    refresh_baselines(conn, entity_type="hotel", now=NOW + timedelta(days=4))
    baselines = [tuple(row) for row in conn.execute("SELECT * FROM hotel_baseline")]
    assert baselines

    watch_id = add_watch(conn, "Athen", lead_min_days=14, lead_max_days=14, now=NOW)
    ensure_hotel_watch(conn)
    ensure_hotel_watch(conn)
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 0
    assert [tuple(row) for row in conn.execute("SELECT * FROM hotel_baseline")] == baselines

    await run_hotel_watches(conn, sources=[FakeSource()], rates=RATES, now=NOW)
    await record_offers(conn, offers, rates=RATES, observed_at=NOW + timedelta(days=6))
    ensure_hotel_watch(conn)
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 1
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 7
    conn.close()


@pytest.mark.parametrize("failure", [False, True])
async def test_failed_and_empty_runs_add_no_watch_history(tmp_path, failure):
    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", lead_min_days=10, lead_max_days=10, now=NOW)

    class NoOffers(FakeSource):
        async def search(self, query):
            if failure:
                raise RuntimeError("offline failure")
            return HotelBatch(offers=[])

    report = await run_hotel_watches(conn, sources=[NoOffers()], rates=RATES, now=NOW)
    stats = watch_stats(conn, get_watch(conn, watch_id))
    assert report["observations"] == stats["observations"] == stats["days_recorded"] == 0
    assert stats["last_observation"] is None
    assert stats["ready"] is False
    assert due_watches(conn, now=NOW) == []
    conn.close()


async def test_failed_scan_keeps_global_prices_without_counting_watch_history(tmp_path):
    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", lead_min_days=10, lead_max_days=15, now=NOW)

    class PartialFailure(FakeSource):
        async def search(self, query):
            if query.arrival > TODAY + timedelta(days=10):
                raise RuntimeError("offline failure")
            return await super().search(query)

    report = await run_hotel_watches(conn, sources=[PartialFailure()], rates=RATES, now=NOW)
    assert report["errors"]
    assert conn.execute("SELECT status FROM hotel_scan").fetchone()[0] == "failed"
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 1
    assert report["observations"] == 0
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 0
    conn.close()


async def test_ownership_migration_is_idempotent_and_survives_reconnection(tmp_path):
    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", lead_min_days=10, lead_max_days=11, now=NOW)
    await run_hotel_watches(conn, sources=[FakeSource()], rates=RATES, now=NOW)
    expected = watch_stats(conn, get_watch(conn, watch_id))
    observations = [tuple(row) for row in conn.execute("SELECT * FROM price_observation")]
    ownership = [tuple(row) for row in conn.execute("SELECT * FROM hotel_watch_observation")]
    scans = [tuple(row) for row in conn.execute("SELECT * FROM hotel_watch_scan")]

    for _ in range(3):
        ensure_hotel_watch(conn)
    assert [tuple(row) for row in conn.execute("SELECT * FROM price_observation")] == observations
    assert [tuple(row) for row in conn.execute("SELECT * FROM hotel_watch_observation")] == ownership
    assert [tuple(row) for row in conn.execute("SELECT * FROM hotel_watch_scan")] == scans
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()

    conn = db.connect(tmp_path / "h.db")
    assert watch_report(conn, now=NOW)["rows"][0]["observations"] == 2
    assert watch_stats(conn, get_watch(conn, watch_id)) == expected
    await run_hotel_watches(
        conn, sources=[FakeSource()], rates=RATES, now=NOW + timedelta(days=1)
    )
    stats = watch_stats(conn, get_watch(conn, watch_id))
    assert stats["observations"] == 4
    assert stats["days_recorded"] == 2
    conn.close()


async def test_schema_initialization_does_not_commit_pending_observations(tmp_path):
    from flightopt.hotels.store import record_offers

    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", now=NOW)
    offers = (await FakeSource().search(get_watch(conn, watch_id).query(TODAY))).offers
    conn.execute("BEGIN")
    await record_offers(conn, offers, rates=RATES, observed_at=NOW)

    ensure_hotel_watch(conn)
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 0
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 0
    conn.close()


async def test_observation_and_ownership_insert_fail_atomically(tmp_path):
    from flightopt.hotels.store import record_offers

    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", lead_min_days=10, lead_max_days=10, now=NOW)
    conn.execute(
        "CREATE TEMP TRIGGER reject_watch_ownership "
        "BEFORE INSERT ON hotel_watch_observation "
        "BEGIN SELECT RAISE(ABORT, 'ownership unavailable'); END"
    )
    report = await run_hotel_watches(conn, sources=[FakeSource()], rates=RATES, now=NOW)
    assert report["errors"]
    assert report["observations"] == 0
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 0
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 0

    # A later manual write proves failed scans cleared their capture scope.
    offers = (await FakeSource().search(get_watch(conn, watch_id).query(TODAY))).offers
    await record_offers(conn, offers, rates=RATES, observed_at=NOW)
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 1
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 0
    conn.close()


async def test_cancellation_excludes_partial_history_and_clears_capture_scope(tmp_path):
    from flightopt.hotels.store import record_offers

    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", lead_min_days=10, lead_max_days=11, now=NOW)
    blocked = asyncio.Event()

    class WaitOnSecondDay(FakeSource):
        async def search(self, query):
            if query.arrival == TODAY + timedelta(days=11):
                blocked.set()
                await asyncio.Event().wait()
            return await super().search(query)

    task = asyncio.create_task(
        run_hotel_watches(conn, sources=[WaitOnSecondDay()], rates=RATES, now=NOW)
    )
    await asyncio.wait_for(blocked.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 1
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 0

    offers = (await FakeSource().search(get_watch(conn, watch_id).query(TODAY))).offers
    await record_offers(conn, offers, rates=RATES, observed_at=NOW)
    assert conn.execute("SELECT count(*) FROM hotel_watch_observation").fetchone()[0] == 1
    await run_hotel_watches(
        conn, sources=[FakeSource()], rates=RATES, now=NOW + timedelta(days=1)
    )
    stats = watch_stats(conn, get_watch(conn, watch_id))
    assert stats["observations"] == 2
    assert stats["days_recorded"] == 1
    conn.close()


async def test_concurrent_manual_history_on_same_connection_is_unattributed(tmp_path):
    from flightopt.hotels.store import record_offers

    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", lead_min_days=10, lead_max_days=10, now=NOW)
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingSource(FakeSource):
        async def search(self, query):
            entered.set()
            await release.wait()
            return await super().search(query)

    task = asyncio.create_task(
        run_hotel_watches(conn, sources=[WaitingSource()], rates=RATES, now=NOW)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    offers = (await FakeSource().search(get_watch(conn, watch_id).query(TODAY))).offers
    await record_offers(conn, offers, rates=RATES, observed_at=NOW)
    release.set()
    report = await asyncio.wait_for(task, timeout=2)

    assert report["observations"] == 1
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 2
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 1
    conn.close()


async def test_migration_of_existing_watch_leaves_legacy_history_unattributed(tmp_path):
    from flightopt.hotels.store import record_offers

    conn = db.connect(tmp_path / "h.db")
    conn.execute(HOTEL_WATCH_SCHEMA)
    conn.execute(
        "INSERT INTO hotel_watch(id, fingerprint, destination, lead_min_days, "
        "lead_max_days, created_at, last_run_at) VALUES(1, 'legacy', 'Athen', 10, 10, ?, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    offers = (await FakeSource().search(HotelQuery(destination="Athen", arrival=TODAY))).offers
    await record_offers(conn, offers, rates=RATES, observed_at=NOW)
    original = tuple(conn.execute("SELECT * FROM hotel_watch").fetchone())

    ensure_hotel_watch(conn)
    ensure_hotel_watch(conn)
    assert tuple(conn.execute("SELECT * FROM hotel_watch").fetchone()) == original
    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM hotel_watch_scan").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM hotel_watch_observation").fetchone()[0] == 0
    assert watch_stats(conn, get_watch(conn, 1))["observations"] == 0
    conn.close()


async def test_rollback_removes_observation_and_ownership_together(tmp_path):
    from flightopt.hotels.scan import create_scan
    from flightopt.hotels.store import record_offers
    from flightopt.hotels.watch_history import capture_watch_scan

    conn = connect(tmp_path)
    watch_id = add_watch(conn, "Athen", now=NOW)
    query = get_watch(conn, watch_id).query(TODAY)
    scan_id = create_scan(conn, query, window_start=TODAY, window_end=TODAY)
    offers = (await FakeSource().search(query)).offers
    conn.execute("BEGIN")
    with capture_watch_scan(conn, watch_id, scan_id):
        await record_offers(conn, offers, rates=RATES, observed_at=NOW)
        ensure_hotel_watch(conn)
        assert conn.execute("SELECT count(*) FROM hotel_watch_observation").fetchone()[0] == 1
    conn.rollback()

    assert conn.execute("SELECT count(*) FROM price_observation").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM hotel_watch_observation").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM hotel_watch_scan").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    await record_offers(conn, offers, rates=RATES, observed_at=NOW)
    assert watch_stats(conn, get_watch(conn, watch_id))["observations"] == 0
    conn.close()
