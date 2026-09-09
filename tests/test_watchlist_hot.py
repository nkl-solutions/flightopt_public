"""Der Takt einer einzelnen Strecke: taeglich oder heiss."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from flightopt.hunt import cadence
from flightopt.storage import db
from flightopt.storage.watchlist import (
    add_route,
    due_routes,
    get_route,
    hot_routes,
    mark_hot_ran,
    mark_ran,
    set_cadence,
    set_enabled,
    watchlist_report,
)

NOW = datetime(2026, 9, 9, 12, 0, 0)


def test_a_new_route_starts_on_the_daily_pace(tmp_path):
    """Der normale Fall bleibt der normale Fall."""
    conn = db.connect(tmp_path / "new.db")
    route_id = add_route(conn, "BER", "ATH")

    route = get_route(conn, route_id)
    assert route.cadence == cadence.DAILY
    assert route.hot is False
    assert route.last_hot_run_at is None
    assert route.as_dict()["cadence"] == cadence.DAILY


def test_a_route_can_be_switched_to_the_hot_pace(tmp_path):
    conn = db.connect(tmp_path / "hot.db")
    route_id = add_route(conn, "BER", "ATH")

    route = set_cadence(conn, route_id, cadence.HOT)

    assert route.hot is True
    assert hot_routes(conn, now=NOW) == [route]


def test_an_unknown_pace_is_refused(tmp_path):
    """Ein Zwischenwert waere eine Zahl, die keine Obergrenze mehr haelt."""
    conn = db.connect(tmp_path / "bogus.db")
    route_id = add_route(conn, "BER", "ATH")

    with pytest.raises(ValueError, match="Takt"):
        set_cadence(conn, route_id, "stuendlich")


def test_a_wide_window_may_not_run_hot(tmp_path):
    """Ein groesseres Fenster kostet mehr Abrufe, als das Budget verbucht.

    Bei sechzig Tagen sind es drei Kalendermonate und damit drei Abrufe. Wer
    das Fenster aufzieht, verbucht weiter drei und bezahlt vier.
    """
    conn = db.connect(tmp_path / "wide.db")
    route_id = add_route(conn, "BER", "ATH", lead_min_days=0,
                         lead_max_days=cadence.MAX_HOT_WINDOW_DAYS)

    with pytest.raises(ValueError, match="Fenster"):
        set_cadence(conn, route_id, cadence.HOT)


def test_a_hot_route_comes_up_again_after_the_interval(tmp_path):
    conn = db.connect(tmp_path / "interval.db")
    route_id = add_route(conn, "BER", "ATH")
    set_cadence(conn, route_id, cadence.HOT)

    mark_hot_ran(conn, route_id, now=NOW)

    assert hot_routes(conn, now=NOW + timedelta(minutes=5)) == []
    # Die Streuung wirkt nur nach hinten, also ist die Strecke spaetestens
    # nach Takt plus Streuung wieder dran.
    later = NOW + timedelta(
        seconds=cadence.HOT_INTERVAL_SECONDS + cadence.HOT_JITTER_SECONDS + 1
    )
    assert [r.id for r in hot_routes(conn, now=later)] == [route_id]


def test_a_hot_pass_counts_as_the_daily_pass(tmp_path):
    """Sonst zahlt eine heisse Strecke ihren Tageslauf ein zweites Mal."""
    conn = db.connect(tmp_path / "double.db")
    route_id = add_route(conn, "BER", "ATH")
    set_cadence(conn, route_id, cadence.HOT)

    mark_hot_ran(conn, route_id, now=NOW)

    assert due_routes(conn, now=NOW) == []
    assert get_route(conn, route_id).last_run_at == NOW.isoformat(timespec="seconds")


def test_the_longest_waiting_route_goes_first(tmp_path):
    """Reihenfolge nach Wartezeit, damit kein Eintrag hinten liegen bleibt.

    Das Budget verschiebt Strecken, wenn zu viele heiss sind. Ohne feste
    Reihenfolge verschoebe es immer dieselben.
    """
    conn = db.connect(tmp_path / "fair.db")
    first = add_route(conn, "BER", "ATH")
    second = add_route(conn, "BER", "FCO")
    third = add_route(conn, "BER", "MAD")
    for route_id in (first, second, third):
        set_cadence(conn, route_id, cadence.HOT)
    mark_hot_ran(conn, second, now=NOW - timedelta(hours=2))
    mark_hot_ran(conn, first, now=NOW - timedelta(hours=1))

    # Die nie gelaufene zuerst, dann die am laengsten wartende.
    assert [r.id for r in hot_routes(conn, now=NOW)] == [third, second, first]


def test_a_disabled_route_is_never_hot(tmp_path):
    conn = db.connect(tmp_path / "off.db")
    route_id = add_route(conn, "BER", "ATH")
    set_cadence(conn, route_id, cadence.HOT)
    set_enabled(conn, route_id, False)

    assert hot_routes(conn, now=NOW) == []


def test_the_report_names_how_many_routes_run_hot(tmp_path):
    """Null heisse Strecken sieht sonst genauso aus wie eine stille Jagd."""
    conn = db.connect(tmp_path / "report.db")
    add_route(conn, "BER", "ATH")
    hot = add_route(conn, "BER", "FCO")
    set_cadence(conn, hot, cadence.HOT)

    report = watchlist_report(conn, now=NOW)

    assert report["hot"] == 1
    assert report["hot_due"] == 1
    assert report["hot_interval_seconds"] == cadence.HOT_INTERVAL_SECONDS
    assert report["max_hot_routes"] == cadence.MAX_HOT_ROUTES


def test_an_existing_route_keeps_its_pace_when_the_window_is_reset(tmp_path):
    """Eine erneute Eintragung meint das Fenster, nicht den Takt."""
    conn = db.connect(tmp_path / "again.db")
    route_id = add_route(conn, "BER", "ATH")
    set_cadence(conn, route_id, cadence.HOT)

    add_route(conn, "BER", "ATH", lead_min_days=7, lead_max_days=40)

    route = get_route(conn, route_id)
    assert route.hot is True
    assert route.lead_min_days == 7


def test_a_route_re_added_with_a_wide_window_falls_back_to_daily(tmp_path):
    """Sonst laeuft eine heisse Strecke mit einem Fenster, das sie nicht darf."""
    conn = db.connect(tmp_path / "shrink.db")
    route_id = add_route(conn, "BER", "ATH")
    set_cadence(conn, route_id, cadence.HOT)

    add_route(conn, "BER", "ATH", lead_min_days=0,
              lead_max_days=cadence.MAX_HOT_WINDOW_DAYS + 10)

    assert get_route(conn, route_id).hot is False


def test_marking_a_daily_run_leaves_the_hot_stamp_alone(tmp_path):
    """Der Tageslauf ist kein heisser Durchgang und darf keiner werden."""
    conn = db.connect(tmp_path / "stamp.db")
    route_id = add_route(conn, "BER", "ATH")
    set_cadence(conn, route_id, cadence.HOT)

    mark_ran(conn, route_id, now=NOW)

    assert get_route(conn, route_id).last_hot_run_at is None
