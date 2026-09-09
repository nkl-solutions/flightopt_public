"""Der Hoteldurchlauf als Job: Fortschritt je Tag, Abbruch, Wiederaufnahme.

Kein Test fasst hier ein Netz an: der Quellen-Katalog ist eine Attrappe, genau
wie in `tests/test_api_cancel.py` fuer die Fluege.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.scan import load_scan
from flightopt.hotels.sources.base import HotelBatch, HotelSource
from flightopt.hotels.store import HOTEL_BASELINE_SPLITS_POPULATIONS, signal_for
from flightopt.jobs import hotel_runner as hotel_runner_module
from flightopt.jobs import runner as flight_runner_module
from flightopt.jobs.hotel_runner import HotelJobRunner, hotel_row, stored_rows
from flightopt.jobs.runner import JobRunner
from flightopt.storage import db


class StubSource(HotelSource):
    """Zwei Haeuser je Tag, ohne Netz. Zaehlt mit, wonach gefragt wurde."""

    name = "stub"

    def __init__(self, fail_from: date | None = None) -> None:
        super().__init__()
        self.asked: list[date] = []
        self.closed = 0
        self.fail_from = fail_from

    async def search(self, query: HotelQuery) -> HotelBatch:
        self.asked.append(query.arrival)
        if self.fail_from is not None and query.arrival >= self.fail_from:
            raise RuntimeError("Quelle antwortet nicht")
        return HotelBatch(
            offers=[
                HotelOffer(
                    source=self.name,
                    property_key=f"stub:{index}",
                    name=f"Hotel {index}",
                    arrival=query.arrival,
                    departure=query.departure,
                    price_total=Money(9000 + index * 3000, "EUR"),
                    stars=4 + index,
                    city="Athens",
                    country="Greece",
                    review_rating=8.0 + index,
                    url="https://example.invalid/hotel",
                    party_size=query.party_size,
                )
                for index in range(2)
            ]
        )

    def close(self) -> None:
        self.closed += 1


class WideStub(StubSource):
    """Dieselbe Attrappe mit zwei Plaetzen, damit ein Fenster zwei Tage fasst."""

    concurrency = 2

    async def search(self, query: HotelQuery) -> HotelBatch:
        await asyncio.sleep(0.02)
        return await super().search(query)


def query(nights: int = 1) -> HotelQuery:
    return HotelQuery(destination="Athen", arrival=date(2026, 11, 10), nights=nights)


def window() -> tuple[date, date]:
    return date(2026, 11, 10), date(2026, 11, 12)


async def test_the_run_reports_every_day_and_ends_done(tmp_path):
    runner = HotelJobRunner(str(tmp_path / "run.db"))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)
    source = StubSource()

    await runner._run(
        scan_id, query(), window_start=start, window_end=end, sources=[source]
    )

    history = runner._history[scan_id]
    assert [p.phase for p in history] == ["planning", "day", "day", "day", "done"]
    assert [(p.done, p.total) for p in history if p.phase == "day"] == [
        (1, 3),
        (2, 3),
        (3, 3),
    ]
    assert [p.detail["date"] for p in history if p.phase == "day"] == [
        "2026-11-10",
        "2026-11-11",
        "2026-11-12",
    ]
    # Teilergebnisse laufen einzeln ein, das Ende traegt trotzdem alles.
    assert all(len(p.detail["rows"]) == 2 for p in history if p.phase == "day")
    assert len(history[-1].detail["rows"]) == 6
    assert runner.status(scan_id) == "done"
    assert source.asked == [date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)]
    assert source.closed == 1


async def test_a_cancel_mid_run_ends_cancelled_and_keeps_the_finished_days(tmp_path):
    runner = HotelJobRunner(str(tmp_path / "stop.db"))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)
    source = StubSource()

    emit = runner._emit

    def emit_then_cancel(sid, progress):
        emit(sid, progress)
        if progress.phase == "day" and progress.done == 1:
            runner.cancel(sid)

    runner._emit = emit_then_cancel  # type: ignore[method-assign]

    await runner._run(
        scan_id, query(), window_start=start, window_end=end, sources=[source]
    )

    phases = [p.phase for p in runner._history[scan_id]]
    assert phases[-1] == "cancelled"
    assert "done" not in phases
    assert runner.status(scan_id) == "cancelled"
    # Genau ein Tag war fertig, und die Wiederaufnahme merkt sich ihn.
    assert source.asked == [date(2026, 11, 10)]
    assert load_scan(runner._conn(), scan_id)["current_day"] == "2026-11-10"
    assert runner.result(scan_id)["status"] == "cancelled"
    assert len(runner.result(scan_id)["rows"]) == 2
    assert scan_id not in runner._cancelled


async def test_a_cancel_inside_the_window_stops_the_fan_at_once(tmp_path):
    """Ein Abbruch mitten im Faecher darf nicht erst nach dem Fenster wirken."""
    runner = HotelJobRunner(str(tmp_path / "wide.db"))
    start, end = date(2026, 11, 10), date(2026, 11, 15)
    scan_id = runner.create(query(), window_start=start, window_end=end)
    source = WideStub()

    emit = runner._emit

    def emit_then_cancel(sid, progress):
        emit(sid, progress)
        if progress.phase == "day" and progress.done == 1:
            runner.cancel(sid)

    runner._emit = emit_then_cancel  # type: ignore[method-assign]

    await runner._run(
        scan_id, query(), window_start=start, window_end=end, sources=[source]
    )

    phases = [p.phase for p in runner._history[scan_id]]
    assert phases[-1] == "cancelled", phases
    assert "done" not in phases
    assert runner.status(scan_id) == "cancelled"
    # Das laufende Fenster ist zwei Tage breit; der dritte wird nie gefragt.
    assert set(source.asked) <= {date(2026, 11, 10), date(2026, 11, 11)}
    assert date(2026, 11, 12) not in source.asked
    assert load_scan(runner._conn(), scan_id)["current_day"] == "2026-11-10"
    assert len(runner.result(scan_id)["rows"]) == 2
    assert source.closed == 1


async def test_an_error_after_the_abort_keeps_the_run_cancelled(tmp_path, monkeypatch):
    runner = HotelJobRunner(str(tmp_path / "boom.db"))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)

    async def cancel_then_break(*_args, **_kwargs):
        runner.cancel(scan_id)
        raise RuntimeError("Quelle weggebrochen")

    monkeypatch.setattr(hotel_runner_module, "run_scan", cancel_then_break)

    await runner._run(
        scan_id, query(), window_start=start, window_end=end, sources=[StubSource()]
    )

    phases = [p.phase for p in runner._history[scan_id]]
    assert "failed" not in phases, phases
    assert phases[-1] == "cancelled", phases
    assert runner.status(scan_id) == "cancelled"
    assert scan_id not in runner._cancelled


async def test_a_failing_source_ends_the_run_as_failed(tmp_path):
    runner = HotelJobRunner(str(tmp_path / "fail.db"))
    start, end = date(2026, 11, 10), date(2026, 11, 20)
    scan_id = runner.create(query(), window_start=start, window_end=end)

    await runner._run(
        scan_id,
        query(),
        window_start=start,
        window_end=end,
        sources=[StubSource(fail_from=start)],
        max_errors=2,
    )

    history = runner._history[scan_id]
    assert history[-1].phase == "failed"
    assert "2 Fehler in Folge" in history[-1].message
    assert runner.status(scan_id) == "failed"


async def test_a_resumed_run_starts_after_the_last_finished_day(tmp_path):
    runner = HotelJobRunner(str(tmp_path / "resume.db"))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)
    conn = runner._conn()
    conn.execute(
        "UPDATE hotel_scan SET current_day=?, days_done=1 WHERE id=?",
        ("2026-11-10", scan_id),
    )
    conn.commit()
    conn.close()
    source = StubSource()

    await runner._run(
        scan_id, query(), window_start=start, window_end=end, sources=[source]
    )

    assert source.asked == [date(2026, 11, 11), date(2026, 11, 12)]
    assert runner.status(scan_id) == "done"


async def test_a_resumed_run_drops_the_verlauf_of_the_previous_attempt(tmp_path):
    """Sonst spielt der Strom dem Browser zuerst das alte Ende vor."""
    runner = HotelJobRunner(str(tmp_path / "again.db"))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)
    runner.cancel(scan_id)
    await runner._run(
        scan_id, query(), window_start=start, window_end=end, sources=[StubSource()]
    )
    assert [p.phase for p in runner._history[scan_id]] == ["cancelled"]

    runner.start(
        scan_id, query(), window_start=start, window_end=end, sources=[StubSource()]
    )
    await runner._tasks[scan_id]

    phases = [p.phase for p in runner._history[scan_id]]
    assert phases[0] == "planning", phases
    assert phases[-1] == "done", phases
    assert runner.result(scan_id)["status"] == "done"


def test_a_cancel_leaves_a_finished_run_alone(tmp_path):
    runner = HotelJobRunner(str(tmp_path / "late.db"))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)
    conn = runner._conn()
    conn.execute("UPDATE hotel_scan SET status='done' WHERE id=?", (scan_id,))
    conn.commit()
    conn.close()

    assert runner.cancel(scan_id) == "done"
    assert runner.cancel(9999) is None


async def test_a_run_stopped_twice_still_shows_the_rows_of_its_second_attempt(tmp_path):
    """Ein Lauf, der wieder laeuft, ist nicht fertig.

    Der Endzeitpunkt des ersten Anlaufs blieb stehen: `cancel` und die
    Fehlerwege nehmen ihn per COALESCE in Schutz. `stored_rows` grenzt die
    Beobachtungen aber genau darauf ein, und damit fiel jede Zeile des zweiten
    Anlaufs aus dem Fenster - sie wurde nach dem "Ende" beobachtet. HTTP 200,
    richtige Metadaten, zu wenige Zeilen.
    """
    path = tmp_path / "resume.db"
    runner = HotelJobRunner(str(path))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)

    # Anlauf eins endete vorzeitig und hat seinen Endzeitpunkt gesetzt.
    conn = db.connect(path)
    conn.execute(
        "UPDATE hotel_scan SET status='cancelled', created_at=?, finished_at=? "
        "WHERE id=?",
        ("2026-09-01T10:00:00", "2026-09-01T10:05:00", scan_id),
    )
    conn.close()

    # Anlauf zwei laeuft an und wird nach dem ersten Tag wieder gestoppt.
    emit = runner._emit

    def emit_then_cancel(sid, progress):
        emit(sid, progress)
        if progress.phase == "day" and progress.done == 1:
            runner.cancel(sid)

    runner._emit = emit_then_cancel  # type: ignore[method-assign]

    await runner._run(
        scan_id, query(), window_start=start, window_end=end, sources=[StubSource()]
    )

    conn = db.connect(path)
    try:
        rows = stored_rows(conn, load_scan(conn, scan_id))
    finally:
        conn.close()

    assert {row["name"] for row in rows} == {"Hotel 0", "Hotel 1"}


class WobblySource(StubSource):
    """Antwortet erst nach dem Nachfassen. Der Zaehler haengt am Ergebnis."""

    async def search(self, query: HotelQuery) -> HotelBatch:
        batch = await super().search(query)
        batch.retries = 1
        return batch


class BrittleSource(StubSource):
    """Gibt nach zwei vergeblichen Nachfragen auf - und sagt, was es kostete."""

    async def search(self, query: HotelQuery) -> HotelBatch:
        self.asked.append(query.arrival)
        failure = RuntimeError("Quelle antwortet nicht")
        failure.retries = 2
        raise failure


async def test_a_run_that_only_went_green_by_asking_again_says_so(tmp_path):
    """Der Zaehler entstand im Adapter und endete im Speicher.

    Weder der Strom noch die Datenbank noch die API haben ihn je gesehen: ein
    Endpunkt, der schleichend unzuverlaessig wird, verschwand damit in lauter
    erfolgreichen Laeufen.
    """
    path = tmp_path / "wobble.db"
    runner = HotelJobRunner(str(path))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)

    await runner._run(
        scan_id, query(), window_start=start, window_end=end,
        sources=[WobblySource()],
    )

    history = runner._history[scan_id]
    assert [p.detail["retries"] for p in history if p.phase == "day"] == [1, 1, 1]
    assert history[-1].detail["retries"] == 3

    conn = db.connect(path)
    try:
        assert load_scan(conn, scan_id)["retries"] == 3
    finally:
        conn.close()


async def test_a_day_that_was_lost_anyway_still_shows_what_it_cost(tmp_path):
    """Gerade der vergebliche Neuversuch sagt etwas ueber die Quelle."""
    path = tmp_path / "brittle.db"
    runner = HotelJobRunner(str(path))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)

    await runner._run(
        scan_id, query(), window_start=start, window_end=end,
        sources=[BrittleSource()],
    )

    conn = db.connect(path)
    try:
        # Drei Tage, je zwei vergebliche Nachfragen.
        assert load_scan(conn, scan_id)["retries"] == 6
    finally:
        conn.close()


def test_the_row_carries_every_field_the_signal_answers_with(tmp_path):
    """Die neue Ehrlichkeit darf nicht nur im Satz stehen.

    `reason` ist Text fuer Menschen und aendert seine Formulierung. Welche
    Regel entschieden hat (`evidence`), wie duenn die Vergleichsgruppe war
    (`thin`) und gegen welche Grundgesamtheit ueberhaupt gerechnet wurde
    (`population`, `population_split`) sind Kennungen - danach kann eine
    Oberflaeche sortieren und filtern, nach einem Satz nicht.
    """
    conn = db.connect(tmp_path / "zeile.db")
    try:
        # Ohne jede Historie traegt nur die Plausibilitaetsschranke: zwoelf
        # Euro fuer drei Sterne sind in keinem europaeischen Markt ein Angebot.
        offer = HotelOffer(
            source="stub",
            property_key="stub:0",
            name="Hotel Ohne Historie",
            arrival=date(2026, 11, 10),
            departure=date(2026, 11, 11),
            price_total=Money(1200, "EUR"),
            price_eur=Money(1200, "EUR"),
            stars=3,
            city="Athens",
            country="Greece",
            party_size=2,
            indicative=True,
        )
        signal = signal_for(conn, offer)
        row = hotel_row(conn, offer)

        assert row["tier"] == "error"
        assert row["evidence"] == "schranke"
        assert row["thin"] is False
        assert row["population"] == "estimate"
        # Die Zeile sagt auch, ob die Baseline die Grundgesamtheiten ueberhaupt
        # trennt. Ohne das liest sich ein Urteil gegen eine gemischte Gruppe
        # wie eines gegen Haendlerpreise.
        assert row["population_split"] == HOTEL_BASELINE_SPLITS_POPULATIONS
        # Und zwar Feld fuer Feld dasselbe, was der Detektor geantwortet hat.
        for field in ("tier", "reason", "basis", "n", "evidence", "thin",
                      "population", "population_split"):
            assert row[field] == signal[field], field
    finally:
        conn.close()


def test_the_flight_runner_stays_free_of_hotels():
    """Die Flugsuche darf von diesem Umbau nichts mitbekommen."""
    source = Path(flight_runner_module.__file__).read_text(encoding="utf-8")

    assert "hotel" not in source.lower()
    # Der Hotellauf leiht sich nur Fortschritt und Abbruch, nichts weiter.
    assert hotel_runner_module.Progress is flight_runner_module.Progress
    assert hotel_runner_module.JobCancelled is flight_runner_module.JobCancelled
    for name in ("create", "start", "cancel", "status", "result", "subscribe"):
        assert callable(getattr(JobRunner, name))


class BlockingStub(StubSource):
    """Haengt im ersten Abruf, bis der Test sie loslaesst."""

    def __init__(self) -> None:
        super().__init__()
        self.reached = asyncio.Event()

    async def search(self, query: HotelQuery) -> HotelBatch:
        self.reached.set()
        await asyncio.sleep(30)
        return await super().search(query)


async def test_a_second_start_stops_the_first_run(tmp_path, monkeypatch):
    """Zwei Laeufe auf derselben Kennung schrieben dieselben Tage doppelt.

    `start()` hat den alten Task nur aus `_tasks` verdraengt; er lief weiter,
    fragte dieselben Tage noch einmal ab und setzte den Fortschritt des neuen
    Laufs zurueck.
    """
    async def no_network(conn, **kwargs):
        return Rates(base="EUR", rates={}, fetched_at=datetime.now())

    monkeypatch.setattr(hotel_runner_module.fx_store, "current_rates", no_network)
    runner = HotelJobRunner(str(tmp_path / "doppelt.db"))
    start, end = window()
    scan_id = runner.create(query(), window_start=start, window_end=end)
    blocked, second_source = BlockingStub(), StubSource()

    runner.start(
        scan_id, query(), window_start=start, window_end=end, sources=[blocked]
    )
    first = runner._tasks[scan_id]
    await asyncio.wait_for(blocked.reached.wait(), timeout=5)

    runner.start(
        scan_id, query(), window_start=start, window_end=end, sources=[second_source]
    )
    second = runner._tasks[scan_id]
    await asyncio.wait_for(second, timeout=10)

    assert second is not first
    assert first.cancelled()
    # Der abgebrochene Lauf gibt seine Quelle frei, statt sie liegen zu lassen.
    assert blocked.closed == 1
    assert runner.status(scan_id) == "done"
    assert second_source.asked == [
        date(2026, 11, 10),
        date(2026, 11, 11),
        date(2026, 11, 12),
    ]
