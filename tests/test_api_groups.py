"""API behavior for flexible airport groups."""

from __future__ import annotations

from datetime import date

import pytest

from flightopt.api import main


class RecordingRunner:
    def __init__(self) -> None:
        self.created = None
        self.started = None
        self.airlines = None

    def create(self, specs):
        self.created = specs
        return 4242

    def start(self, job_id, specs, *, airlines=None):
        self.started = (job_id, specs)
        self.airlines = airlines


@pytest.mark.asyncio
async def test_search_starts_every_route_variant_for_airport_groups(monkeypatch):
    runner = RecordingRunner()
    monkeypatch.setattr(main, "runner", runner)
    req = main.SearchRequest(
        airports=["DE-OST", "ATH"],
        trip="one_way",
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 3),
    )

    response = await main.start_search(req)

    assert response == {"job_id": 4242, "route": "3 Routenvarianten"}
    assert sorted(s.route for s in runner.created) == ["BER-ATH", "DRS-ATH", "LEJ-ATH"]
    assert runner.started[0] == 4242
    assert sorted(s.route for s in runner.started[1]) == ["BER-ATH", "DRS-ATH", "LEJ-ATH"]
