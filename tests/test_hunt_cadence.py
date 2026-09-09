"""Der Takt der Jagd und die Obergrenze, die ihn haelt.

Hier steht die Messung, aus der der Standardtakt abgeleitet ist, und zwar als
Test und nicht als Kommentar: wer eine Quelle einbaut, die ein Fenster in acht
Stuecken holt statt in dreien, faellt hier auf und nicht erst im Sperrvermerk
einer Airline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from flightopt.hunt import budget, cadence
from flightopt.sources.registry import build_sources
from flightopt.storage import db

START = date(2026, 10, 15)
"""Mitten im Monat, damit ein 60-Tage-Fenster drei Kalendermonate beruehrt.

Genau das ist der teuerste Fall fuer die monatsgebundenen Endpunkte, und nur
der teuerste Fall darf die Obergrenze bestimmen.
"""

EMPTY_PAYLOAD = {
    "ryanair": {"outbound": {"fares": []}},
    "wizz": {"outboundFlights": []},
    "aegean": {},
    "condor": {"data": [[]]},
    "eurowings": {"sections": []},
    "britishairways": {"response": {"docs": []}},
    "icelandair": {},
    "airbaltic": {"success": True, "data": []},
    "jetblue": {},
    "kiwi": {
        "data": {
            "itineraryPricesCalendar": {
                "__typename": "ItineraryPricesCalendar",
                "currency": {"code": "EUR"},
                "calendar": [],
            }
        }
    },
}


def calendar_sources() -> list:
    return [s for s in build_sources(None) if getattr(s, "supports_calendar", False)]


async def count_requests(source, start: date, end: date) -> int:
    """Wie viele HTTP-Abrufe ein Fenster bei dieser Quelle kostet."""
    seen: list[str] = []

    async def fake_fetch(url, **kwargs):
        seen.append(url)
        return EMPTY_PAYLOAD.get(source.name, {})

    source.fetch_json = fake_fetch
    if hasattr(source, "_fetch"):
        # Eurowings holt seinen Kalender ueber eine eigene Schicht; sie zaehlt
        # genauso, weil dahinter derselbe fremde Server steht.
        async def fake_inner(origin, destination):
            seen.append(f"{origin}-{destination}")
            return {"sections": []}

        source._fetch = fake_inner
    await source.calendar_range("BER", "ATH", start, end)
    return len(seen)


@pytest.mark.parametrize("source", calendar_sources(), ids=lambda s: s.name)
async def test_a_watch_window_costs_no_more_than_the_charged_calls(source):
    """Die Messung, auf der das Budget steht.

    Verrechnet wird der schlechteste Fall, nicht der gemessene Einzelwert:
    eine Quelle, die mit einem Abruf auskommt, wird zu teuer verbucht, und der
    Fehler geht damit in die vorsichtige Richtung. Reisst eine Quelle den
    schlechtesten Fall, stimmt die Verrechnung nicht mehr und das Budget
    schuetzt nichts.
    """
    end = START + timedelta(days=cadence.WATCH_WINDOW_DAYS - 1)

    assert await count_requests(source, START, end) <= cadence.CALLS_PER_PASS


def test_the_cap_stays_below_the_slowest_source_own_pace():
    """Die Obergrenze ist abgeleitet, nicht geraten.

    Sie haengt an der langsamsten Kalenderquelle. Kommt eine noch langsamere
    dazu, ist die Ableitung falsch und dieser Test rot.
    """
    slowest = min(source.per_minute for source in calendar_sources())

    assert slowest == cadence.SLOWEST_SOURCE_PER_MINUTE
    assert cadence.MAX_CALLS_PER_SOURCE_HOUR == int(slowest * 60 * cadence.CAP_SHARE)


def test_the_default_cadence_costs_one_search_per_hour():
    """Eine heisse Strecke kostet je Quelle so viel wie eine Suche in der Stunde."""
    per_hour = cadence.CALLS_PER_PASS * cadence.passes_per_hour()

    assert per_hour == cadence.SEARCH_CALLS_PER_SOURCE
    assert cadence.HOT_INTERVAL_SECONDS == 1200


def test_the_cap_bounds_how_many_routes_may_run_hot():
    """Zwanzig heisse Strecken duerfen die Obergrenze nicht reissen koennen."""
    fits = cadence.MAX_HOT_ROUTES

    assert fits * cadence.CALLS_PER_PASS * cadence.passes_per_hour() <= (
        cadence.MAX_CALLS_PER_SOURCE_HOUR
    )
    assert (fits + 1) * cadence.CALLS_PER_PASS * cadence.passes_per_hour() > (
        cadence.MAX_CALLS_PER_SOURCE_HOUR
    )


def test_a_hot_pass_must_not_read_its_own_last_answer():
    """Ein Takt kuerzer als die Cache-Frist fragt sich selbst statt die Quelle."""
    assert cadence.MAX_CACHE_AGE < timedelta(seconds=cadence.HOT_INTERVAL_SECONDS)


# -- Das Hauptbuch ------------------------------------------------------------


def test_an_untouched_source_has_its_whole_budget(tmp_path):
    conn = db.connect(tmp_path / "budget.db")
    now = datetime(2026, 9, 9, 12, 0, 0)

    assert budget.remaining(conn, "ryanair", now=now) == (
        cadence.MAX_CALLS_PER_SOURCE_HOUR
    )
    assert budget.affordable(conn, ["ryanair", "wizz"], cadence.CALLS_PER_PASS, now=now)


def test_charging_takes_the_budget_down_and_blocks_the_pass(tmp_path):
    conn = db.connect(tmp_path / "charge.db")
    now = datetime(2026, 9, 9, 12, 0, 0)
    passes = cadence.MAX_CALLS_PER_SOURCE_HOUR // cadence.CALLS_PER_PASS

    for step in range(passes):
        assert budget.affordable(conn, ["ryanair"], cadence.CALLS_PER_PASS, now=now)
        budget.charge(conn, ["ryanair"], cadence.CALLS_PER_PASS,
                      now=now + timedelta(minutes=step))

    assert budget.remaining(conn, "ryanair", now=now + timedelta(minutes=passes)) == 0
    assert not budget.affordable(
        conn, ["ryanair"], cadence.CALLS_PER_PASS, now=now + timedelta(minutes=passes)
    )


def test_one_exhausted_source_blocks_the_whole_pass(tmp_path):
    """Ein Durchgang fragt alle Quellen. Eine ohne Budget reicht zum Verschieben."""
    conn = db.connect(tmp_path / "one.db")
    now = datetime(2026, 9, 9, 12, 0, 0)
    budget.charge(conn, ["aegean"], cadence.MAX_CALLS_PER_SOURCE_HOUR, now=now)

    assert budget.affordable(conn, ["ryanair"], cadence.CALLS_PER_PASS, now=now)
    assert not budget.affordable(
        conn, ["ryanair", "aegean"], cadence.CALLS_PER_PASS, now=now
    )


def test_the_budget_window_rolls_instead_of_jumping_at_the_full_hour(tmp_path):
    """Rollende Stunde, keine Kalenderstunde.

    Eine Kalenderstunde erlaubt das volle Budget um 11:59 und noch einmal um
    12:00. Genau dieser Doppelschlag ist das, was eine Quelle sieht.
    """
    conn = db.connect(tmp_path / "roll.db")
    start = datetime(2026, 9, 9, 11, 30, 0)
    budget.charge(conn, ["ryanair"], cadence.MAX_CALLS_PER_SOURCE_HOUR, now=start)

    assert budget.remaining(conn, "ryanair", now=datetime(2026, 9, 9, 12, 0, 0)) == 0
    assert budget.remaining(conn, "ryanair", now=datetime(2026, 9, 9, 12, 29, 0)) == 0
    assert budget.remaining(conn, "ryanair", now=datetime(2026, 9, 9, 12, 31, 0)) == (
        cadence.MAX_CALLS_PER_SOURCE_HOUR
    )


def test_old_ledger_rows_are_swept(tmp_path):
    """Das Hauptbuch ist Betriebsmittel und kein Archiv."""
    conn = db.connect(tmp_path / "sweep.db")
    old = datetime(2026, 9, 8, 12, 0, 0)
    budget.charge(conn, ["ryanair"], 3, now=old)

    removed = budget.purge(conn, now=datetime(2026, 9, 9, 12, 0, 0))

    assert removed == 1
    assert budget.report(conn, ["ryanair"], now=datetime(2026, 9, 9, 12, 0, 0)) == [
        {
            "source": "ryanair",
            "used": 0,
            "remaining": cadence.MAX_CALLS_PER_SOURCE_HOUR,
            "cap": cadence.MAX_CALLS_PER_SOURCE_HOUR,
        }
    ]


# -- Die Sicherung ------------------------------------------------------------


def test_without_a_block_the_hunt_is_allowed(tmp_path):
    conn = db.connect(tmp_path / "open.db")

    assert budget.paused_sources(conn, now=datetime(2026, 9, 9, 12, 0, 0)) == []


def test_a_blocked_source_stops_the_hot_pace_for_the_cooldown(tmp_path):
    """Eine zugegangene Sicherung nimmt die ganze Jagd vom Takt.

    Nicht nur die eine Quelle: ein Block ist der Hinweis, dass unser
    Fussabdruck auffaellt. Die anderen Quellen weiter dreimal die Stunde zu
    fragen waere genau die Gier, die den naechsten Block holt.
    """
    conn = db.connect(tmp_path / "block.db")
    now = datetime(2026, 9, 9, 12, 0, 0)

    budget.pause(conn, ["aegean"], reason="HTTP 429", now=now)

    still = budget.paused_sources(conn, now=now + timedelta(minutes=5))
    assert [row["source"] for row in still] == ["aegean"]
    assert still[0]["reason"] == "HTTP 429"
    assert budget.paused_sources(
        conn, now=now + budget.PAUSE_COOLDOWN + timedelta(seconds=1)
    ) == []
