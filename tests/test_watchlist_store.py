"""Die Beobachtungsliste als Tabelle: eintragen, faellig werden, abschalten."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from flightopt.storage import db
from flightopt.storage.watchlist import (
    DEFAULT_LEAD_MAX,
    DEFAULT_LEAD_MIN,
    MAX_WINDOW_DAYS,
    MIN_RECORDING_DAYS,
    add_route,
    due_routes,
    get_route,
    list_routes,
    mark_ran,
    route_stats,
    set_enabled,
    watchlist_report,
)


def observation(conn, entity_key: str, *, observed_at: datetime,
                travel_date: date, price_minor: int = 12000,
                source: str = "ryanair") -> None:
    conn.execute(
        "INSERT INTO price_observation("
        "observed_at, source, entity_type, entity_key, travel_date, party_size, "
        "currency, price_total_minor, is_estimate) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            observed_at.isoformat(timespec="seconds"),
            source,
            "flight",
            entity_key,
            travel_date.isoformat(),
            1,
            "EUR",
            price_minor,
            1,
        ),
    )


def test_a_new_route_is_stored_and_listed(tmp_path):
    conn = db.connect(tmp_path / "watch.db")
    now = datetime(2026, 9, 5, 8, 0, 0)

    route_id = add_route(conn, "ber", "ath", now=now)

    routes = list_routes(conn)
    assert [r.id for r in routes] == [route_id]
    assert routes[0].origin == "BER"
    assert routes[0].destination == "ATH"
    assert routes[0].entity_key == "BER|ATH"
    assert routes[0].lead_min_days == DEFAULT_LEAD_MIN
    assert routes[0].lead_max_days == DEFAULT_LEAD_MAX
    assert routes[0].currency == "EUR"
    assert routes[0].enabled is True
    assert routes[0].last_run_at is None


def test_the_window_rolls_with_the_day(tmp_path):
    """Ein festes Datum hoert nach dem Reisetag auf, Sinn zu ergeben.

    Deshalb steht in der Zeile ein Vorlauf und kein Datum: derselbe Eintrag
    fragt morgen dasselbe Vorlauf-Fenster ab, nur einen Tag weiter.
    """
    conn = db.connect(tmp_path / "roll.db")
    add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=40)

    route = list_routes(conn)[0]

    assert route.window(date(2026, 9, 5)) == (date(2026, 9, 15), date(2026, 10, 15))
    assert route.window(date(2026, 9, 6)) == (date(2026, 9, 16), date(2026, 10, 16))


def test_the_same_route_is_not_watched_twice(tmp_path):
    """Zweimal dieselbe Strecke waere zweimal derselbe Abruf am selben Tag."""
    conn = db.connect(tmp_path / "twice.db")
    first = add_route(conn, "BER", "ATH", lead_min_days=10, lead_max_days=40)

    second = add_route(conn, "ber", "ath", lead_min_days=20, lead_max_days=60)

    assert second == first
    routes = list_routes(conn)
    assert len(routes) == 1
    # Der zweite Eintrag ist keine neue Zeile, sondern die Ansage, dass das
    # Fenster jetzt anders lauten soll.
    assert (routes[0].lead_min_days, routes[0].lead_max_days) == (20, 60)


def test_a_nonsense_route_is_refused(tmp_path):
    conn = db.connect(tmp_path / "nonsense.db")

    with pytest.raises(ValueError):
        add_route(conn, "BER", "BER")
    with pytest.raises(ValueError):
        add_route(conn, "BERLIN", "ATH")
    with pytest.raises(ValueError):
        add_route(conn, "BER", "ATH", lead_min_days=40, lead_max_days=10)
    with pytest.raises(ValueError):
        add_route(conn, "BER", "ATH", lead_min_days=-1, lead_max_days=10)
    with pytest.raises(ValueError):
        add_route(
            conn, "BER", "ATH", lead_min_days=0, lead_max_days=MAX_WINDOW_DAYS + 5
        )

    assert list_routes(conn) == []


def test_a_route_runs_once_a_day(tmp_path):
    """Die Quellen sind dieselben wie in der Suche, eine Sperre traefe beide."""
    conn = db.connect(tmp_path / "due.db")
    route_id = add_route(conn, "BER", "ATH")
    morning = datetime(2026, 9, 5, 8, 0, 0)

    assert [r.id for r in due_routes(conn, now=morning)] == [route_id]

    mark_ran(conn, route_id, now=morning)

    assert due_routes(conn, now=morning + timedelta(hours=6)) == []
    assert due_routes(conn, now=morning + timedelta(hours=14)) == []
    assert [r.id for r in due_routes(conn, now=morning + timedelta(days=1))] == [route_id]
    assert get_route(conn, route_id).last_run_at == morning.isoformat(timespec="seconds")


def test_a_disabled_route_is_never_due(tmp_path):
    conn = db.connect(tmp_path / "off.db")
    route_id = add_route(conn, "BER", "ATH")
    now = datetime(2026, 9, 5, 8, 0, 0)

    set_enabled(conn, route_id, False)

    assert get_route(conn, route_id).enabled is False
    assert due_routes(conn, now=now) == []

    set_enabled(conn, route_id, True)

    assert [r.id for r in due_routes(conn, now=now)] == [route_id]


def test_switching_an_unknown_route_says_so(tmp_path):
    conn = db.connect(tmp_path / "unknown.db")

    with pytest.raises(ValueError):
        set_enabled(conn, 404, False)


def test_stats_count_what_the_recording_brought(tmp_path):
    """Beobachtungen und Aufzeichnungstage, nicht nur ein Haekchen.

    Eine Aussage traegt erst ab `min_samples` je Kombination aus Strecke,
    Wochentag und Vorlauf-Fenster. Je Tag kommt genau eine Beobachtung je
    Kombination dazu, die Zahl der Aufzeichnungstage ist deshalb das Mass,
    an dem sich ablesen laesst, wann es soweit ist.
    """
    conn = db.connect(tmp_path / "stats.db")
    add_route(conn, "BER", "ATH")
    route = list_routes(conn)[0]
    for day in range(3):
        stamp = datetime(2026, 9, 5 + day, 8, 0, 0)
        for offset in range(4):
            observation(
                conn, "BER|ATH", observed_at=stamp,
                travel_date=date(2026, 10, 1) + timedelta(days=offset),
            )
    # Eine fremde Strecke faellt nicht in die Zahlen dieser hier.
    observation(
        conn, "BER|FCO", observed_at=datetime(2026, 9, 9, 8, 0, 0),
        travel_date=date(2026, 10, 1),
    )

    stats = route_stats(conn, route)

    assert stats["observations"] == 12
    assert stats["days_recorded"] == 3
    assert stats["last_observation"] == "2026-09-07T08:00:00"
    assert stats["ready"] is False
    assert stats["min_days"] == MIN_RECORDING_DAYS


def test_a_route_without_observations_reads_as_zero_not_as_missing(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    add_route(conn, "BER", "ATH")

    stats = route_stats(conn, list_routes(conn)[0])

    assert stats["observations"] == 0
    assert stats["days_recorded"] == 0
    assert stats["last_observation"] is None
    assert stats["ready"] is False


def test_the_report_says_whether_anything_is_being_recorded(tmp_path):
    """Eine leere Liste ist eine Aussage und muss als solche lesbar sein."""
    conn = db.connect(tmp_path / "report.db")
    now = datetime(2026, 9, 5, 8, 0, 0)

    empty = watchlist_report(conn, now=now)
    assert empty["routes"] == 0
    assert empty["active"] == 0
    assert empty["due"] == 0
    assert empty["observations"] == 0
    assert empty["last_run_at"] is None

    route_id = add_route(conn, "BER", "ATH", now=now)
    add_route(conn, "BER", "FCO", now=now)
    set_enabled(conn, route_id, False)
    observation(
        conn, "BER|FCO", observed_at=now, travel_date=date(2026, 10, 1),
    )
    mark_ran(conn, list_routes(conn)[1].id, now=now)

    report = watchlist_report(conn, now=now)

    assert report["routes"] == 2
    assert report["active"] == 1
    assert report["due"] == 0
    assert report["observations"] == 1
    assert report["last_run_at"] == now.isoformat(timespec="seconds")
