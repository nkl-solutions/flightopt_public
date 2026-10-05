"""Profile management uses existing auth and rejects invalid scan requests."""

from datetime import date, timedelta

import httpx
import pytest

from flightopt.api import main
from flightopt.jobs.runner import JobRunner


@pytest.fixture
def profile_runner(monkeypatch, tmp_path):
    class Runner(JobRunner):
        def start(self, job_id, specs, *, airlines=None):
            pass

    runner = Runner(str(tmp_path / "profiles.db"))
    monkeypatch.setattr(main, "runner", runner)
    monkeypatch.delenv("FLIGHTOPT_BASIC_USER", raising=False)
    monkeypatch.delenv("FLIGHTOPT_BASIC_PASSWORD", raising=False)
    return runner


async def create_profile():
    return await main.save_profile_endpoint(main.ProfileRequest(
        name="Athen", airports=["DE-OST", "ATH"], trip="one_way",
        window_start=date.today(), window_end=date.today() + timedelta(days=3),
    ))


@pytest.mark.asyncio
async def test_profile_list_edit_pause_and_targeted_run(profile_runner):
    saved = await create_profile()
    profile_id = saved["profile_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/profiles")
        assert response.status_code == 200
        row = response.json()["profiles"][0]
        assert row["variants"] == 3
        assert row["status"] == "due"
        response = await client.patch(f"/api/profiles/{profile_id}", json={
            "name": "Ost nach Athen", "enabled": False, "cadence_days": 7,
        })
        assert response.status_code == 200
        assert response.json()["profile"]["status"] == "paused"
        assert (await client.post(f"/api/profiles/{profile_id}/run-once")).status_code == 409
        assert (await client.patch(f"/api/profiles/{profile_id}", json={"enabled": True})).status_code == 200
        response = await client.post(f"/api/profiles/{profile_id}/run-once")
        assert response.status_code == 200
        assert response.json()["job"]["profile_id"] == profile_id
        assert response.json()["profile"]["active_job_id"] == response.json()["job"]["job_id"]
        assert (await client.post(f"/api/profiles/{profile_id}/run-once")).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"enabled": None}, {"name": " "}, {"cadence_days": 0}, {"name": "x" * 121}, {"spec": {}}, {"enabled": "false"}])
async def test_profile_patch_rejects_invalid_changes(profile_runner, payload):
    saved = await create_profile()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.patch(f"/api/profiles/{saved['profile_id']}", json=payload)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_missing_profile_is_404(profile_runner):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.patch("/api/profiles/999", json={"enabled": False})).status_code == 404
        assert (await client.post("/api/profiles/999/run-once")).status_code == 404


@pytest.mark.asyncio
async def test_profile_management_remains_protected(profile_runner, monkeypatch):
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.setenv("FLIGHTOPT_BASIC_PASSWORD", "test-only-password")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/api/profiles")).status_code == 401
        assert (await client.patch("/api/profiles/1", json={"enabled": False})).status_code == 401
        assert (await client.post("/api/profiles/1/run-once")).status_code == 401
