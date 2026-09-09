"""Der Ereignisstrom darf nicht an einem Lauf haengen, den niemand fuehrt.

Ein Job aus einem frueheren Prozess steht in der Datenbank auf `running`. Sein
Task ist mit dem Prozess gestorben, es kommt kein einziges Ereignis mehr, und
der Strom wartete darauf endlos - alle zwanzig Sekunden ein Keepalive, im
Browser fuer immer "laeuft".
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from flightopt.api import main
from flightopt.domain.models import LegSpec, SearchSpec
from flightopt.hotels.models import HotelQuery
from flightopt.jobs.hotel_runner import HotelJobRunner
from flightopt.jobs.runner import JobRunner
from flightopt.storage import db

TIMEOUT = 1.0
"""Deutlich unter den 20 Sekunden Keepalive: was laenger braucht, haengt."""


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"),),
        stays=(),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 3),
    )


def query() -> HotelQuery:
    return HotelQuery(destination="Athen", arrival=date(2026, 10, 1), nights=2)


async def drain(response) -> list[str]:
    chunks: list[str] = []

    async def read() -> None:
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    await asyncio.wait_for(read(), timeout=TIMEOUT)
    return chunks


def payload_of(chunk: str) -> dict:
    return json.loads(chunk.removeprefix("data: ").strip())


@pytest.mark.asyncio
async def test_job_stream_ends_for_a_run_from_a_dead_process(monkeypatch, tmp_path):
    runner = JobRunner(str(tmp_path / "zombie.db"))
    monkeypatch.setattr(main, "runner", runner)
    job_id = runner.create(spec())
    conn = runner._conn()
    conn.execute("UPDATE search_job SET status='running' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()

    chunks = await drain(await main.events(job_id))

    assert len(chunks) == 1
    payload = payload_of(chunks[0])
    assert payload["phase"] == "failed"
    assert payload["message"] == db.INTERRUPTED


@pytest.mark.asyncio
async def test_hotel_stream_ends_for_a_run_from_a_dead_process(monkeypatch, tmp_path):
    hotels = HotelJobRunner(str(tmp_path / "zombie-hotels.db"))
    monkeypatch.setattr(main, "hotel_runner", hotels)
    scan_id = hotels.create(
        query(), window_start=date(2026, 10, 1), window_end=date(2026, 10, 2)
    )
    conn = hotels._conn()
    conn.execute("UPDATE hotel_scan SET status='running' WHERE id=?", (scan_id,))
    conn.commit()
    conn.close()

    chunks = await drain(await main.hotel_scan_events(scan_id))

    assert len(chunks) == 1
    assert payload_of(chunks[0])["phase"] == "failed"


@pytest.mark.asyncio
async def test_a_live_job_still_waits_for_its_events(monkeypatch, tmp_path):
    """Die Gegenprobe: ein Lauf dieses Prozesses darf nicht abgewuergt werden."""
    runner = JobRunner(str(tmp_path / "live.db"))
    monkeypatch.setattr(main, "runner", runner)
    job_id = runner.create(spec())
    conn = runner._conn()
    conn.execute("UPDATE search_job SET status='running' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()

    async def never() -> None:
        await asyncio.sleep(30)

    runner._tasks[job_id] = asyncio.create_task(never())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await drain(await main.events(job_id))
    finally:
        runner._tasks[job_id].cancel()
