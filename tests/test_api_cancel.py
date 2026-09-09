"""Eine laufende Suche muss abbrechbar sein, ohne dass eine Quelle weiter befragt wird."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from flightopt.api import main
from flightopt.domain.models import (
    Cabin,
    LegSpec,
    Money,
    Offer,
    Pax,
    SearchSpec,
    StayRange,
)
from flightopt.jobs import runner as runner_module
from flightopt.jobs.runner import JobRunner


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 10),
    )


def impossible_spec() -> SearchSpec:
    """Ein Fenster, in das kein Aufenthalt passt: der Lauf endet in einem ValueError."""
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 2),
    )


def test_cancel_marks_the_job_and_arms_the_flag(tmp_path):
    runner = JobRunner(str(tmp_path / "cancel.db"))
    job_id = runner.create(spec())

    assert runner.cancel(job_id) == "cancelled"
    assert runner.status(job_id) == "cancelled"
    assert runner.result(job_id) == {"status": "cancelled", "error": None, "results": []}


def test_cancel_is_idempotent_and_keeps_a_finished_job_finished(tmp_path):
    runner = JobRunner(str(tmp_path / "done.db"))
    job_id = runner.create(spec())
    conn = runner._conn()
    conn.execute("UPDATE search_job SET status='done' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()

    assert runner.cancel(job_id) == "done"
    assert runner.status(job_id) == "done"


@pytest.mark.asyncio
async def test_cancelled_job_stops_before_any_source_is_touched(tmp_path):
    runner = JobRunner(str(tmp_path / "stop.db"))
    job_id = runner.create(spec())
    runner.cancel(job_id)

    # Wuerde der Runner weiterlaufen, kaeme hier ein echter HTTP-Abruf.
    await runner._run(job_id, spec())

    assert [p.phase for p in runner._history[job_id]] == ["cancelled"]
    assert runner._history[job_id][0].message == "Suche abgebrochen"
    assert runner.status(job_id) == "cancelled"


def test_cancel_endpoint_reports_the_new_status(monkeypatch, tmp_path):
    runner = JobRunner(str(tmp_path / "endpoint.db"))
    monkeypatch.setattr(main, "runner", runner)

    with TestClient(main.app) as client:
        # Erst nach dem Start anlegen: der Start schliesst Laeufe ab, die noch
        # auf `pending` stehen, denn die gehoeren einem toten Prozess.
        job_id = runner.create(spec())
        response = client.post(f"/api/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"job_id": job_id, "status": "cancelled"}


def test_cancel_endpoint_rejects_an_unknown_job(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "unknown.db")))

    with TestClient(main.app) as client:
        response = client.post("/api/jobs/999/cancel")

    assert response.status_code == 404
    assert response.json() == {"detail": "Unbekannter Job"}


def test_event_stream_replays_a_cancelled_job_without_history(monkeypatch, tmp_path):
    """Nach einem Neustart hat der Job keinen Verlauf mehr, nur noch seinen Status."""
    db_path = str(tmp_path / "replay.db")
    first = JobRunner(db_path)
    job_id = first.create(spec())
    first.cancel(job_id)
    monkeypatch.setattr(main, "runner", JobRunner(db_path))

    with TestClient(main.app) as client:
        body = client.get(f"/api/jobs/{job_id}/events").text

    assert '"phase": "cancelled"' in body
    assert "Suche abgebrochen" in body
    assert "fertig" not in body


class _FakeSource:
    """Liefert Preise und Fluege ohne Netz, damit der Runner echt durchlaeuft."""

    supports_calendar = True
    supports_search = True
    name = "fake"
    carrier = "FA"
    carriers = ("FA",)
    indicative = False

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
        day = start
        out = {}
        while day <= end:
            out[day] = Money(10000, currency)
            day += timedelta(days=1)
        return out

    async def search_leg(self, origin, destination, day, *, pax=Pax(),
                         cabin=Cabin.ECONOMY, currency="EUR"):
        return [
            Offer(
                source=self.name,
                origin=origin,
                destination=destination,
                travel_date=day,
                price=Money(9000, currency),
                fetched_at=datetime(2026, 1, 1),
            )
        ]


class _DeadSource:
    supports_calendar = False
    supports_search = False
    name = "dead"
    carrier = ""
    carriers = ()
    indicative = False

    def supports_route(self, origin: str, destination: str) -> bool:
        return False


def _only_fake_sources(monkeypatch) -> None:
    # Der Katalog entsteht seit der Registry dort, nicht mehr im Runner.
    from flightopt.sources import registry as registry_module

    for name in ("WizzSource", "AegeanSource", "CondorSource", "EurowingsSource",
                 "BritishAirwaysSource", "IcelandairSource", "KiwiSource"):
        monkeypatch.setattr(registry_module, name, _DeadSource)
    monkeypatch.setattr(registry_module, "RyanairSource", _FakeSource)


async def test_cancel_during_verify_ends_cancelled(tmp_path, monkeypatch):
    """Ein Abbruch waehrend der Pruefphase darf nicht als 'fertig' enden."""
    _only_fake_sources(monkeypatch)
    db_path = str(tmp_path / "verify.db")
    runner = JobRunner(db_path)
    job_id = runner.create(spec())

    emit = runner._emit
    pressed = []

    def emit_then_cancel(jid, progress):
        emit(jid, progress)
        if progress.phase == "verifying" and not pressed:
            pressed.append(True)
            runner.cancel(jid)

    monkeypatch.setattr(runner, "_emit", emit_then_cancel, raising=False)

    await runner._run(job_id, spec())

    phases = [p.phase for p in runner._history[job_id]]
    assert pressed, phases
    assert phases[-1] == "cancelled"
    assert "done" not in phases
    assert runner.status(job_id) == "cancelled"
    assert runner.result(job_id) == {"status": "cancelled", "error": None, "results": []}
    # Auch ein frischer Runner (Neustart) darf den Job nicht als fertig sehen.
    assert JobRunner(db_path).result(job_id)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_an_error_after_the_abort_keeps_the_job_cancelled(tmp_path, monkeypatch):
    """Ein Folgefehler nach dem Abbruch darf den Status nicht auf 'failed' drehen."""
    runner = JobRunner(str(tmp_path / "boom.db"))
    bad = impossible_spec()
    job_id = runner.create(bad)

    emit = runner._emit
    pressed = []

    def emit_then_cancel(jid, progress):
        emit(jid, progress)
        if progress.phase == "planning" and not pressed:
            pressed.append(True)
            runner.cancel(jid)

    monkeypatch.setattr(runner, "_emit", emit_then_cancel, raising=False)

    await runner._run(job_id, bad)

    phases = [p.phase for p in runner._history[job_id]]
    assert pressed, phases
    assert "failed" not in phases, phases
    # Der Strom braucht ein Endereignis, sonst wartet ein spaeter Abonnent ewig.
    assert phases[-1] == "cancelled", phases
    assert runner.status(job_id) == "cancelled"
    assert runner.result(job_id) == {"status": "cancelled", "error": None, "results": []}
    assert job_id not in runner._cancelled


@pytest.mark.asyncio
async def test_run_many_keeps_a_cancelled_job_cancelled_on_a_later_error(tmp_path, monkeypatch):
    runner = JobRunner(str(tmp_path / "many.db"))
    job_id = runner.create(spec())

    async def cancel_then_break(jid, _conn, _spec, **_kwargs):
        runner.cancel(jid)
        raise RuntimeError("Quelle weggebrochen")

    monkeypatch.setattr(runner, "_run_variant_payloads", cancel_then_break, raising=False)

    await runner._run_many(job_id, [spec()])

    phases = [p.phase for p in runner._history[job_id]]
    assert "failed" not in phases, phases
    assert phases[-1] == "cancelled", phases
    assert runner.status(job_id) == "cancelled"
    assert runner.result(job_id)["status"] == "cancelled"
    assert job_id not in runner._cancelled
