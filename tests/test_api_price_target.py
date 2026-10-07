"""Profile price targets are validated in minor units, including explicit removal."""

import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from flightopt.api import main
from flightopt.jobs.runner import JobRunner
from flightopt.jobs import price_targets


@pytest.fixture
def profile_api(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "profiles.db")))
    monkeypatch.delenv("FLIGHTOPT_BASIC_USER", raising=False)
    monkeypatch.delenv("FLIGHTOPT_BASIC_PASSWORD", raising=False)


def request(target=25099):
    return {"name": "Athen", "airports": ["BER", "ATH"], "trip": "one_way",
            "window_start": date.today().isoformat(),
            "window_end": (date.today() + timedelta(days=10)).isoformat(),
            "price_target_minor": target}


@pytest.mark.asyncio
async def test_target_can_be_created_patched_and_explicitly_removed(profile_api):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/profiles", json=request())
        assert response.status_code == 200
        profile_id = response.json()["profile_id"]
        rows = (await client.get("/api/profiles")).json()["profiles"]
        assert rows[0]["price_target_minor"] == 25099
        response = await client.patch(f"/api/profiles/{profile_id}", json={"price_target_minor": 18029})
        assert response.status_code == 200
        assert response.json()["profile"]["price_target_minor"] == 18029
        response = await client.patch(f"/api/profiles/{profile_id}", json={"name": "Neu"})
        assert response.json()["profile"]["price_target_minor"] == 18029
        response = await client.patch(f"/api/profiles/{profile_id}", json={"price_target_minor": None})
        assert response.status_code == 200
        assert response.json()["profile"]["price_target_minor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [0, -10, 250.99, "25099", True, 100000001])
async def test_target_rejects_invalid_json_amounts(profile_api, target):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.post("/api/profiles", json=request(target))).status_code == 422
        profile_id = (await client.post("/api/profiles", json=request(None))).json()["profile_id"]
        assert (await client.patch(f"/api/profiles/{profile_id}", json={"price_target_minor": target})).status_code == 422


@pytest.mark.asyncio
async def test_price_target_alert_routes_are_protected(profile_api, monkeypatch):
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.setenv("FLIGHTOPT_BASIC_PASSWORD", "test-only-password")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/api/profile-alerts")).status_code == 401
        assert (await client.post("/api/profile-alerts/1/acknowledge")).status_code == 401


def seed_alert():
    conn = main.runner._conn()
    price_targets.ensure_schema(conn)
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        "INSERT INTO price_target_event(profile_id,profile_name,job_id,price_minor,target_minor,"
        "currency,route,dates,legs,created_at,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (1, "Athen", 1, 5000, 6000, "EUR", "BER-ATH",
         json.dumps([date.today().isoformat()]), json.dumps([]), now, now),
    )
    conn.commit()
    alert_id = cursor.lastrowid
    conn.close()
    return alert_id


@pytest.mark.asyncio
async def test_alert_listing_and_idempotent_acknowledgement(profile_api):
    alert_id = seed_alert()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/profile-alerts?only_open=true&limit=1")
        assert response.status_code == 200
        original = response.json()["alerts"][0]
        assert original["delivery"] == "dry_run"
        response = await client.post(f"/api/profile-alerts/{alert_id}/acknowledge")
        assert response.status_code == 200
        acknowledged = response.json()["alert"]
        assert acknowledged["acknowledged_at"]
        assert {k: v for k, v in acknowledged.items() if k != "acknowledged_at"} == {
            k: v for k, v in original.items() if k != "acknowledged_at"
        }
        assert (await client.post(f"/api/profile-alerts/{alert_id}/acknowledge")).json()["alert"] == acknowledged
        assert (await client.get("/api/profile-alerts?only_open=true")).json()["alerts"] == []
        assert len((await client.get("/api/profile-alerts")).json()["alerts"]) == 1
        assert (await client.post("/api/profile-alerts/99999/acknowledge")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["0", "501", "bad"])
async def test_alert_list_limit_is_bounded(profile_api, limit):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/api/profile-alerts", params={"limit": limit})).status_code == 422


@pytest.mark.asyncio
async def test_open_filter_precedes_limit_even_after_many_acknowledged_events(profile_api):
    oldest = seed_alert()
    conn = main.runner._conn()
    row = conn.execute("SELECT * FROM price_target_event WHERE id=?", (oldest,)).fetchone()
    for i in range(50):
        conn.execute(
            "INSERT INTO price_target_event(profile_id,profile_name,job_id,price_minor,target_minor,"
            "currency,route,dates,legs,created_at,checked_at,acknowledged_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, row["profile_name"], i + 2, 5000, 6000, "EUR", row["route"],
             row["dates"], row["legs"], row["created_at"], row["checked_at"], row["created_at"]),
        )
    conn.commit()
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        all_rows = (await client.get("/api/profile-alerts?limit=50")).json()["alerts"]
        assert all(row["acknowledged_at"] for row in all_rows)
        open_rows = (await client.get("/api/profile-alerts?limit=50&only_open=true")).json()["alerts"]
        assert [row["id"] for row in open_rows] == [oldest]
