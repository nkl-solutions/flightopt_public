"""Der Hoteldurchlauf als Job: Fortschritt je Tag, Abbruch, Wiederaufnahme.

Kein Test fasst hier ein Netz an: der Quellen-Katalog ist eine Attrappe, genau
wie in `tests/test_api_cancel.py` fuer die Fluege.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.scan import load_scan
from flightopt.hotels.sources.base import HotelBatch, HotelSource
from flightopt.jobs import hotel_runner as hotel_runner_module
from flightopt.jobs import runner as flight_runner_module
from flightopt.jobs.hotel_runner import HotelJobRunner
from flightopt.jobs.runner import JobRunner


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


def test_the_flight_runner_stays_free_of_hotels():
    """Die Flugsuche darf von diesem Umbau nichts mitbekommen."""
    source = Path(flight_runner_module.__file__).read_text(encoding="utf-8")

    assert "hotel" not in source.lower()
    # Der Hotellauf leiht sich nur Fortschritt und Abbruch, nichts weiter.
    assert hotel_runner_module.Progress is flight_runner_module.Progress
    assert hotel_runner_module.JobCancelled is flight_runner_module.JobCancelled
    for name in ("create", "start", "cancel", "status", "result", "subscribe"):
        assert callable(getattr(JobRunner, name))
