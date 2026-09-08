"""Jeder Kalenderadapter holt so viel je Abruf, wie sein Endpunkt hergibt.

Die Suche kostet ihre Zeit in Abrufen, nicht in Rechenzeit. Ein Adapter, der
ein Fenster in Stuecken holt, die kleiner sind als noetig, verlaengert jede
Suche um genau diese ueberzaehligen Anfragen. Der Test haelt je Quelle fest,
wie viele Abrufe ein Fenster kosten darf - und dokumentiert damit zugleich,
wo der Endpunkt selbst die Grenze zieht.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from flightopt.domain.models import Money
from flightopt.sources.aegean import AegeanSource
from flightopt.sources.airbaltic import AirBalticSource
from flightopt.sources.britishairways import BritishAirwaysSource
from flightopt.sources.condor import DAYS_PER_CALL, CondorSource
from flightopt.sources.eurowings import EurowingsSource
from flightopt.sources.icelandair import MAX_PERIOD_DAYS, IcelandairSource
from flightopt.sources.jetblue import JetBlueSource
from flightopt.sources.kiwi import CHUNK_DAYS, KiwiSource

# Ein Fenster, das absichtlich mitten im Monat beginnt: genau da schlug die
# Monatsschleife zu und kostete einen Abruf mehr als das Fenster braucht.
START = date(2026, 10, 15)
END = START + timedelta(days=59)  # 60 Tage, drei Kalendermonate


def stub_fetch(src, monkeypatch, payload, seen: list):
    async def fake(url, **kw):
        seen.append(kw)
        return payload

    monkeypatch.setattr(src, "fetch_json", fake)


# -- Punkt 7a: ein Abruf deckt alles ------------------------------------------


async def test_airbaltic_covers_a_year_in_one_call(monkeypatch):
    src = AirBalticSource()
    seen: list = []
    stub_fetch(src, monkeypatch, {"success": True, "data": []}, seen)

    await src.calendar_range("BER", "RIX", START, START + timedelta(days=300))

    assert len(seen) == 1
    assert seen[0]["params"]["startDate"] == START.isoformat()


async def test_icelandair_asks_for_the_whole_window_in_one_call(monkeypatch):
    src = IcelandairSource()
    seen: list = []
    stub_fetch(src, monkeypatch, {}, seen)

    await src.calendar_range("BER", "KEF", START, END)

    assert len(seen) == 1
    assert seen[0]["params"]["period"] == 60


async def test_icelandair_stops_at_the_endpoints_own_ceiling(monkeypatch):
    src = IcelandairSource()
    seen: list = []
    stub_fetch(src, monkeypatch, {}, seen)

    await src.calendar_range("BER", "KEF", START, START + timedelta(days=500))

    assert len(seen) == 1
    assert seen[0]["params"]["period"] == MAX_PERIOD_DAYS


async def test_eurowings_reads_fifteen_months_in_one_call(monkeypatch):
    src = EurowingsSource()
    calls: list = []

    async def fake_fetch(origin, destination):
        calls.append((origin, destination))
        return {"sections": []}

    monkeypatch.setattr(src, "_fetch", fake_fetch)

    await src.calendar_range("DUS", "PMI", START, START + timedelta(days=400))

    assert calls == [("DUS", "PMI")]


async def test_kiwi_fits_a_normal_window_into_one_chunk(monkeypatch):
    src = KiwiSource()
    seen: list = []
    stub_fetch(
        src, monkeypatch,
        {"data": {"itineraryPricesCalendar": {"__typename": "ItineraryPricesCalendar",
                                              "currency": {"code": "EUR"},
                                              "calendar": []}}},
        seen,
    )

    await src.calendar_range("BER", "AYT", START, END)

    assert len(seen) == 1, "60 Tage passen in einen 90-Tage-Chunk"


async def test_kiwi_only_splits_past_its_own_cap(monkeypatch):
    src = KiwiSource()
    seen: list = []
    stub_fetch(
        src, monkeypatch,
        {"data": {"itineraryPricesCalendar": {"__typename": "ItineraryPricesCalendar",
                                              "currency": {"code": "EUR"},
                                              "calendar": []}}},
        seen,
    )

    await src.calendar_range("BER", "AYT", START, START + timedelta(days=CHUNK_DAYS))

    assert len(seen) == 2


# -- Punkt 7b: Condor kachelt in Tagen statt in Monaten ------------------------


def condor_payload(anchor: date) -> dict:
    """Der Endpunkt antwortet grosszuegig: ab dem Tag nach dem Anker, 45 Tage."""
    return {
        "data": [
            [
                {
                    "date": (anchor + timedelta(days=offset)).strftime("%Y%m%d"),
                    "price": 25199,
                    "offer": True,
                }
                for offset in range(1, 46)
            ]
        ]
    }


async def test_condor_tiles_in_days_not_in_calendar_months(monkeypatch):
    src = CondorSource()
    anchors: list[str] = []

    async def fake(url, **kw):
        anchor_raw = kw["params"]["outboundDate"]
        anchors.append(anchor_raw)
        assert kw["params"]["numberOfFlightDays"] == DAYS_PER_CALL
        return condor_payload(date(int(anchor_raw[:4]), int(anchor_raw[4:6]),
                                   int(anchor_raw[6:])))

    monkeypatch.setattr(src, "fetch_json", fake)

    prices = await src.calendar_range("FRA", "HER", START, END)

    # Drei Kalendermonate, aber nur zwei 31-Tage-Kacheln.
    assert len(anchors) == 2
    assert anchors[0] == (START - timedelta(days=1)).strftime("%Y%m%d")
    assert anchors[1] == (START - timedelta(days=1) + timedelta(days=DAYS_PER_CALL)).strftime(
        "%Y%m%d"
    )
    # Und das Fenster ist lueckenlos abgedeckt, inklusive erstem und letztem Tag.
    assert set(prices) == {START + timedelta(days=i) for i in range(60)}
    assert prices[START] == Money(25199, "EUR")


async def test_condor_needs_a_single_call_for_a_short_window(monkeypatch):
    """Ein 20-Tage-Fenster ueber einen Monatswechsel kostete vorher zwei Abrufe."""
    src = CondorSource()
    anchors: list[str] = []

    async def fake(url, **kw):
        anchors.append(kw["params"]["outboundDate"])
        raw = kw["params"]["outboundDate"]
        return condor_payload(date(int(raw[:4]), int(raw[4:6]), int(raw[6:])))

    monkeypatch.setattr(src, "fetch_json", fake)

    short_start = date(2026, 10, 25)
    prices = await src.calendar_range("FRA", "HER", short_start,
                                      short_start + timedelta(days=19))

    assert len(anchors) == 1
    assert len(prices) == 20


async def test_condor_does_not_pay_for_a_window_it_already_covered(monkeypatch):
    """Genau 31 Tage sind genau ein Abruf - nicht zwei.

    Der Abruf deckt anchor+1 bis anchor+31 ab. Wer nur den Cursor
    weiterschiebt und ihn gegen das Fensterende prueft, bestellt fuer den
    letzten bereits gelieferten Tag noch einen zweiten Abruf.
    """
    src = CondorSource()
    anchors: list[str] = []

    async def fake(url, **kw):
        raw = kw["params"]["outboundDate"]
        anchors.append(raw)
        return condor_payload(date(int(raw[:4]), int(raw[4:6]), int(raw[6:])))

    monkeypatch.setattr(src, "fetch_json", fake)

    exact = date(2026, 10, 1)
    prices = await src.calendar_range("FRA", "HER", exact,
                                      exact + timedelta(days=DAYS_PER_CALL - 1))

    assert len(anchors) == 1
    assert len(prices) == DAYS_PER_CALL
    assert min(prices) == exact
    assert max(prices) == exact + timedelta(days=DAYS_PER_CALL - 1)


async def test_condor_calendar_still_answers_for_one_month(monkeypatch):
    """Das Kalenderprotokoll bleibt, damit nichts anderes daran haengen bleibt."""
    src = CondorSource()
    seen: list = []
    stub_fetch(src, monkeypatch, condor_payload(date(2026, 11, 1)), seen)

    prices = await src.calendar("FRA", "HER", date(2026, 11, 17))

    assert len(seen) == 1
    assert seen[0]["params"]["outboundDate"] == "20261101"
    assert seen[0]["params"]["numberOfFlightDays"] == DAYS_PER_CALL
    assert prices[date(2026, 11, 2)] == Money(25199, "EUR")


# -- Punkt 7c: monatsgebundene Endpunkte -------------------------------------


@pytest.mark.parametrize(
    "factory, payload",
    [
        (AegeanSource, {}),
        (BritishAirwaysSource, {"response": {"docs": []}}),
        (JetBlueSource, {}),
    ],
)
async def test_month_bound_sources_cost_one_call_per_month(factory, payload, monkeypatch):
    """Kein Fehler, sondern die Grenze des Endpunkts - festgehalten, nicht behoben.

    Aegean nimmt `YYYY-MM`, British Airways `month_year:(YYYYMM)`, JetBlue
    `"NOVEMBER 2026"`. Keiner davon kennt einen Zeitraum.
    """
    src = factory()
    seen: list = []
    stub_fetch(src, monkeypatch, payload, seen)

    await src.calendar_range("BER", "ATH", START, END)

    assert len(seen) == 3, "Oktober, November, Dezember"


async def test_ryanair_costs_one_call_per_month(monkeypatch):
    from flightopt.sources.ryanair import RyanairSource

    src = RyanairSource()
    seen: list = []

    async def fake(url, **kw):
        seen.append(url)
        return {"outbound": {"fares": []}}

    monkeypatch.setattr(src, "fetch_json", fake)

    await src.calendar_range("BER", "ATH", START, END)

    assert len(seen) == 3
    assert "outboundMonthOfDate=2026-10-01" in seen[0]
