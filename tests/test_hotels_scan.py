"""Der Zeitraum-Durchlauf: Faecher je Fenster, Wiederaufnahme, Dedup, Abbruch."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.scan import (
    ScanProgress,
    create_scan,
    day_cache_key,
    fan_width,
    load_scan,
    run_scan,
    scan_days,
)
from flightopt.hotels.sources.base import HotelBatch, HotelSource, SourceBlocked
from flightopt.storage import db

RATES = Rates(base="EUR", rates={"USD": 1.10})
WINDOW = (date(2026, 11, 10), date(2026, 11, 14))


class FakeSource(HotelSource):
    """Antwortet aus einer Liste, ohne Netz. `fail_on` laesst Tage scheitern.

    `concurrency` und `delays` sind fuer den Faecher da: die Spitze der
    gleichzeitig laufenden Abrufe steht in `peak`, und wer die Antwortzeiten
    ungleich verteilt, laesst die Tage absichtlich in falscher Reihenfolge
    fertig werden.
    """

    def __init__(
        self,
        name: str = "fake",
        *,
        fail_on: set[date] | None = None,
        concurrency: int = 1,
        delays: dict[date, float] | None = None,
    ) -> None:
        super().__init__(concurrency=concurrency)
        self.name = name
        self.fail_on = fail_on or set()
        self.delays = delays or {}
        self.asked: list[date] = []
        self.finished: list[date] = []
        self.open = 0
        self.peak = 0

    async def search(self, query: HotelQuery) -> HotelBatch:
        self.asked.append(query.arrival)
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            delay = self.delays.get(query.arrival, 0.0)
            if delay:
                await asyncio.sleep(delay)
            if query.arrival in self.fail_on:
                raise RuntimeError("Quelle antwortet nicht")
            self.finished.append(query.arrival)
            return HotelBatch(
                offers=[
                    HotelOffer(
                        source=self.name,
                        property_key=f"{self.name}:abc",
                        name="Melia Athens",
                        arrival=query.arrival,
                        departure=query.departure,
                        price_total=Money(11800, "EUR"),
                        stars=4,
                        country="Greece",
                        party_size=query.party_size,
                    )
                ]
            )
        finally:
            self.open -= 1


class BlockedSource(FakeSource):
    """Antwortet mit einer Sperre. Danach faechert diese Quelle nichts mehr.

    Vermerkt wird sie wie im echten Adapter, also vor dem Werfen. Die Schwelle
    steht auf eins, weil hier der Fall geprueft wird, in dem die Sicherung
    wirklich zugeht - eine einzelne Abweisung kostet nur ihren eigenen Tag.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.breaker.threshold = 1

    async def search(self, query: HotelQuery) -> HotelBatch:
        self.asked.append(query.arrival)
        self.breaker.record_block()
        raise SourceBlocked("wir sind gesperrt")


class Stop(RuntimeError):
    """Was ein Abbruch aus der Fortschrittsmeldung heraus wirft."""


def slower_first(days: list[date]) -> dict[date, float]:
    """Der erste Tag braucht am laengsten, der letzte gar nichts."""
    return {day: 0.02 * (len(days) - index) for index, day in enumerate(days)}


def athens(**kwargs) -> HotelQuery:
    return HotelQuery(destination="Athen", arrival=WINDOW[0], **kwargs)


def conn_for(tmp_path):
    return db.connect(tmp_path / "scan.db")


def test_the_window_becomes_one_arrival_day_per_night():
    assert scan_days(*WINDOW) == [date(2026, 11, d) for d in (10, 11, 12, 13, 14)]
    # Der letzte fertige Tag wird nicht wiederholt.
    assert scan_days(*WINDOW, resume_after=date(2026, 11, 12)) == [
        date(2026, 11, 13), date(2026, 11, 14)
    ]
    with pytest.raises(ValueError):
        scan_days(WINDOW[1], WINDOW[0])


def test_the_cache_key_separates_every_parameter_that_changes_the_answer():
    base = day_cache_key("trivago", athens())

    assert base != day_cache_key("booking", athens())
    assert base != day_cache_key("trivago", athens(nights=2))
    assert base != day_cache_key("trivago", athens(adults=3))
    assert base != day_cache_key("trivago", athens(rooms=2))
    assert base != day_cache_key("trivago", athens(stars=(4, 5)))
    assert base != day_cache_key("trivago", athens(children=(6,)))
    assert base == day_cache_key("trivago", athens())


async def test_every_day_is_searched_once_and_every_row_is_written(tmp_path):
    conn = conn_for(tmp_path)
    source = FakeSource()
    seen: list[ScanProgress] = []

    result = await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[source], rates=RATES, on_progress=seen.append,
    )

    assert result.status == "done"
    assert (result.days_done, result.days_total) == (5, 5)
    assert source.asked == scan_days(*WINDOW)
    assert len(result.offers) == 5
    rows = conn.execute("SELECT travel_date FROM price_observation ORDER BY 1").fetchall()
    assert [row["travel_date"] for row in rows] == [d.isoformat() for d in scan_days(*WINDOW)]
    # Fortschritt je Tag plus die Schlussmeldung.
    assert [p.days_done for p in seen] == [1, 2, 3, 4, 5, 5]
    assert seen[-1].status == "done"
    conn.close()


async def test_a_resumed_run_starts_at_the_day_after_the_last_finished_one(tmp_path):
    conn = conn_for(tmp_path)
    first = FakeSource(fail_on={date(2026, 11, 12)})
    scan_id = create_scan(conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1])

    await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[first], rates=RATES, scan_id=scan_id, max_errors=1,
    )
    stopped = load_scan(conn, scan_id)

    assert stopped["status"] == "failed"
    assert stopped["current_day"] == "2026-11-11"

    second = FakeSource()
    result = await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[second], rates=RATES, scan_id=scan_id,
    )

    # Der Lauf macht am 12. weiter statt am 10. anzufangen.
    assert second.asked == [date(2026, 11, d) for d in (12, 13, 14)]
    assert result.days_done == 5
    assert load_scan(conn, scan_id)["status"] == "done"
    conn.close()


async def test_a_day_no_source_answered_is_asked_again_on_resume(tmp_path):
    """`current_day` ist der Stand der Wiederaufnahme, kein Hochwasserstand.

    Ein Tag, an dem keine Quelle geantwortet hat, wurde uebersprungen - und
    der naechste gelungene Tag schrieb die Marke trotzdem fort. Der Fehltag
    lag danach dahinter und wurde nie wieder gefragt. Weil `already` ihn beim
    naechsten Mal als erledigt mitzaehlt, erreichte der Balken am Ende sauber
    "alle Tage": der Ausfall verschwand restlos.
    """
    conn = conn_for(tmp_path)
    lost = date(2026, 11, 11)
    scan_id = create_scan(conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1])

    await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[FakeSource(fail_on={lost})], rates=RATES, scan_id=scan_id,
    )

    assert load_scan(conn, scan_id)["current_day"] == "2026-11-10"

    second = FakeSource()
    result = await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[second], rates=RATES, scan_id=scan_id,
    )

    # Nur der Fehltag geht wirklich ins Netz, der Rest liegt im Tages-Cache.
    assert second.asked == [lost]
    assert result.days_done == 5
    conn.close()


async def test_the_same_combination_is_not_fetched_twice_on_the_same_day(tmp_path):
    conn = conn_for(tmp_path)
    source = FakeSource()

    await run_scan(conn, athens(), window_start=WINDOW[0], window_end=WINDOW[0],
                   sources=[source], rates=RATES)
    again = await run_scan(conn, athens(), window_start=WINDOW[0], window_end=WINDOW[0],
                           sources=[source], rates=RATES)

    assert source.asked == [WINDOW[0]]
    # Die Zeilen sind trotzdem da, sie kommen aus dem Cache statt aus dem Netz.
    assert len(again.offers) == 1
    assert any("heute schon geholt" in note for note in again.skipped)
    conn.close()


async def test_five_failures_in_a_row_end_the_run(tmp_path):
    conn = conn_for(tmp_path)
    source = FakeSource(fail_on=set(scan_days(date(2026, 11, 10), date(2026, 11, 20))))

    result = await run_scan(
        conn, athens(), window_start=date(2026, 11, 10), window_end=date(2026, 11, 20),
        sources=[source], rates=RATES,
    )

    assert result.status == "failed"
    assert "5 Fehler in Folge" in (result.error or "")
    assert len(source.asked) == 5
    assert load_scan(conn, result.scan_id)["status"] == "failed"
    conn.close()


def test_the_window_is_as_wide_as_the_widest_source():
    """Fest verdrahtet ist nichts: die Nebenlaeufigkeit gibt das Mass."""
    assert fan_width([FakeSource()]) == 1
    assert fan_width([FakeSource(concurrency=2)]) == 2
    assert fan_width([FakeSource(), FakeSource("zwei", concurrency=2)]) == 2
    assert fan_width([]) == 1


async def test_two_slots_really_fetch_two_days_at_once(tmp_path):
    conn = conn_for(tmp_path)
    days = scan_days(*WINDOW)
    wide = FakeSource("wide", concurrency=2, delays={day: 0.02 for day in days})

    result = await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[wide], rates=RATES,
    )

    assert result.status == "done"
    assert result.days_done == 5
    assert sorted(wide.asked) == days
    # Der Beweis, dass es wirklich gleichzeitig lief und nicht nur schnell war.
    assert wide.peak == 2
    conn.close()


async def test_a_source_with_one_slot_still_walks_day_by_day(tmp_path):
    """Trivago hat genau einen Platz. Fuer sie darf sich nichts aendern."""
    conn = conn_for(tmp_path)
    days = scan_days(*WINDOW)
    lone = FakeSource("lone", delays={day: 0.01 for day in days})

    await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[lone], rates=RATES,
    )

    assert lone.asked == days
    assert lone.peak == 1
    conn.close()


async def test_only_the_missing_days_go_into_the_fan(tmp_path):
    conn = conn_for(tmp_path)
    days = scan_days(*WINDOW)
    first = FakeSource("wide", concurrency=2)

    await run_scan(
        conn, athens(), window_start=days[0], window_end=days[1],
        sources=[first], rates=RATES,
    )
    second = FakeSource("wide", concurrency=2)
    again = await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[second], rates=RATES,
    )

    # Die ersten beiden Tage liegen im Cache und werden gar nicht erst gefaechert.
    assert sorted(second.asked) == days[2:]
    assert again.days_done == 5
    assert len(again.offers) == 5
    assert sum("heute schon geholt" in note for note in again.skipped) == 2
    conn.close()


async def test_the_progress_comes_per_day_and_in_date_order(tmp_path):
    conn = conn_for(tmp_path)
    days = scan_days(*WINDOW)
    wide = FakeSource("wide", concurrency=2, delays=slower_first(days))
    seen: list[ScanProgress] = []

    result = await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[wide], rates=RATES, on_progress=seen.append,
    )

    # Der Faecher wird absichtlich in falscher Reihenfolge fertig ...
    assert wide.finished != days
    # ... die Meldungen kommen trotzdem je Tag und nach Datum.
    assert [p.day for p in seen if p.status == "running"] == days
    assert [p.days_done for p in seen if p.status == "running"] == [1, 2, 3, 4, 5]
    assert all(len(p.offers) == 1 for p in seen if p.status == "running")
    # Und die Tabelle springt nicht: die Zeilen stehen nach Datum.
    assert [offer.arrival for offer in result.offers] == days
    conn.close()


async def test_an_abort_inside_the_window_stops_the_remaining_days(tmp_path):
    conn = conn_for(tmp_path)
    days = scan_days(date(2026, 11, 10), date(2026, 11, 15))
    wide = FakeSource("wide", concurrency=2, delays={day: 0.02 for day in days})

    def stop_after_the_first_day(progress: ScanProgress) -> None:
        if progress.days_done == 1:
            raise Stop("Durchlauf abgebrochen")

    with pytest.raises(Stop):
        await run_scan(
            conn, athens(), window_start=days[0], window_end=days[-1],
            sources=[wide], rates=RATES, on_progress=stop_after_the_first_day,
        )

    # Der Abbruch wirkt sofort und nicht erst nach dem Fenster: der dritte Tag
    # wurde nie gefragt, obwohl das Fenster nur zwei Tage breit ist.
    assert set(wide.asked) <= set(days[:2])
    assert days[2] not in wide.asked
    # Und der abgerissene Abruf haengt nicht mehr in der Luft.
    assert wide.open == 0
    conn.close()


async def test_five_failing_days_end_the_run_even_with_a_wide_fan(tmp_path):
    conn = conn_for(tmp_path)
    days = scan_days(date(2026, 11, 10), date(2026, 11, 20))
    wide = FakeSource("wide", concurrency=2, fail_on=set(days))

    result = await run_scan(
        conn, athens(), window_start=days[0], window_end=days[-1],
        sources=[wide], rates=RATES,
    )

    assert result.status == "failed"
    assert "5 Fehler in Folge" in (result.error or "")
    # Das laufende Fenster wird noch voll, der Rest des Zeitraums nicht.
    assert 5 <= len(wide.asked) <= 6
    assert len(wide.asked) < len(days)
    # Gemeldet sind die fuenf Tage, an denen der Lauf endete.
    assert len(result.errors) == 5
    assert result.days_done == 0
    assert not result.offers
    conn.close()


async def test_a_day_nobody_started_counts_as_a_failure_not_as_empty(tmp_path):
    """Ein Tag, den der Faecher abgebrochen hat, ist kein leeres Ergebnis.

    Die schmale Quelle wird auf dem ersten Tag gesperrt, also faechert sie den
    zweiten gar nicht erst auf. Er darf nicht als "nichts gefunden" in die
    Buecher gehen, sondern als das, was er ist: nie gefragt.
    """
    conn = conn_for(tmp_path)
    days = scan_days(WINDOW[0], WINDOW[0] + timedelta(days=1))
    blocked = BlockedSource("narrow")
    healthy = FakeSource("wide", concurrency=2)

    result = await run_scan(
        conn, athens(), window_start=days[0], window_end=days[-1],
        sources=[blocked, healthy], rates=RATES,
    )

    # Der zweite Tag wurde nie gestartet, steht aber trotzdem im Bericht.
    assert blocked.asked == [days[0]]
    assert [note.split(":")[0] for note in result.errors] == [
        days[0].isoformat(), days[1].isoformat()
    ]
    assert "gesperrt" in result.errors[0]
    assert "beendet" in result.errors[1]
    # Die gesunde Quelle traegt beide Tage, der Lauf ist deshalb fertig.
    assert result.status == "done"
    assert result.days_done == 2
    assert len(result.offers) == 2
    conn.close()


async def test_one_failing_source_does_not_end_a_day_the_other_answered(tmp_path):
    conn = conn_for(tmp_path)
    broken = FakeSource("broken", fail_on=set(scan_days(*WINDOW)))
    healthy = FakeSource("healthy")

    result = await run_scan(
        conn, athens(), window_start=WINDOW[0], window_end=WINDOW[1],
        sources=[broken, healthy], rates=RATES,
    )

    assert result.status == "done"
    assert result.days_done == 5
    assert len(result.errors) == 5
    assert len(result.offers) == 5
    conn.close()
