"""Die Beobachtungsliste ueber HTTP: eintragen, ansehen, abschalten."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from flightopt.api import main
from flightopt.jobs import scheduler as scheduler_module
from flightopt.jobs.runner import JobRunner
from flightopt.storage import db
from flightopt.storage.watchlist import DEFAULT_LEAD_MAX, DEFAULT_LEAD_MIN


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    runner = JobRunner(str(tmp_path / "watch-api.db"))
    monkeypatch.setattr(main, "runner", runner)
    monkeypatch.setattr(main.daily_scheduler, "runner", runner)
    # Ohne `with` laeuft der Lifespan nicht, der Tagesplaner startet also nicht.
    return TestClient(main.app)


def test_an_empty_watchlist_answers_with_zero_not_with_nothing(client):
    response = client.get("/api/watchlist")

    assert response.status_code == 200
    body = response.json()
    assert body["routes"] == []
    assert body["summary"]["routes"] == 0
    assert body["summary"]["active"] == 0
    assert body["summary"]["observations"] == 0


def test_a_route_can_be_added_and_read_back(client):
    created = client.post("/api/watchlist", json={"origin": "ber", "destination": "ath"})

    assert created.status_code == 200
    route = created.json()
    assert route["origin"] == "BER"
    assert route["destination"] == "ATH"
    assert route["route"] == "BER-ATH"
    assert route["lead_min_days"] == DEFAULT_LEAD_MIN
    assert route["lead_max_days"] == DEFAULT_LEAD_MAX
    assert route["enabled"] is True
    assert route["observations"] == 0
    assert route["days_recorded"] == 0

    listed = client.get("/api/watchlist").json()
    assert [r["id"] for r in listed["routes"]] == [route["id"]]
    assert listed["summary"]["active"] == 1


def test_a_place_name_is_resolved_like_in_the_search_form(client):
    created = client.post(
        "/api/watchlist", json={"origin": "Berlin", "destination": "Athen"}
    )

    assert created.status_code == 200
    assert created.json()["route"] == "BER-ATH"


def test_a_route_nobody_can_read_is_refused(client):
    bad_place = client.post(
        "/api/watchlist", json={"origin": "Gibtsnicht", "destination": "ATH"}
    )
    same_twice = client.post(
        "/api/watchlist", json={"origin": "BER", "destination": "BER"}
    )
    upside_down = client.post(
        "/api/watchlist",
        json={"origin": "BER", "destination": "ATH",
              "lead_min_days": 40, "lead_max_days": 10},
    )

    assert bad_place.status_code == 400
    assert same_twice.status_code == 400
    assert upside_down.status_code == 400
    assert client.get("/api/watchlist").json()["routes"] == []


def test_a_route_can_be_switched_off_and_on_again(client):
    route_id = client.post(
        "/api/watchlist", json={"origin": "BER", "destination": "ATH"}
    ).json()["id"]

    off = client.patch(f"/api/watchlist/{route_id}", json={"enabled": False})

    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert client.get("/api/watchlist").json()["summary"]["active"] == 0

    on = client.patch(f"/api/watchlist/{route_id}", json={"enabled": True})

    assert on.json()["enabled"] is True
    # Abgeschaltet heisst nicht geloescht: die Zeile und ihre Historie bleiben.
    assert client.get("/api/watchlist").json()["summary"]["routes"] == 1


def test_switching_an_unknown_route_is_a_404(client):
    assert client.patch("/api/watchlist/404", json={"enabled": False}).status_code == 404


def test_the_list_carries_what_the_recording_brought(client, tmp_path):
    client.post("/api/watchlist", json={"origin": "BER", "destination": "ATH"})
    conn = db.connect(main.runner.db_path)
    for day in range(2):
        conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, party_size, "
            "currency, price_total_minor, is_estimate) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                datetime(2026, 9, 5 + day, 8, 0, 0).isoformat(timespec="seconds"),
                "ryanair", "flight", "BER|ATH",
                (date(2026, 10, 1) + timedelta(days=day)).isoformat(),
                1, "EUR", 12000, 1,
            ),
        )
    conn.commit()
    conn.close()

    row = client.get("/api/watchlist").json()["routes"][0]

    assert row["observations"] == 2
    assert row["days_recorded"] == 2
    assert row["last_observation"] == "2026-09-06T08:00:00"
    assert row["ready"] is False


def test_the_recording_can_be_triggered_by_hand(client, monkeypatch):
    """Damit der erste Eintrag nicht bis zum naechsten Durchgang wartet."""
    calls: list[int] = []

    async def fake_run(conn, *, now=None):
        calls.append(1)
        return {"routes": 1, "observations": 60, "calls": 1, "errors": [], "due_left": 0}

    monkeypatch.setattr(scheduler_module, "run_watchlist", fake_run)

    response = client.post("/api/watchlist/run-once")

    assert response.status_code == 200
    assert response.json()["observations"] == 60
    assert calls == [1]


def test_health_detail_says_whether_anything_is_being_recorded(client):
    empty = client.get("/api/health/detail").json()

    assert empty["watchlist"]["routes"] == 0
    assert empty["watchlist"]["active"] == 0

    client.post("/api/watchlist", json={"origin": "BER", "destination": "ATH"})
    filled = client.get("/api/health/detail").json()

    assert filled["watchlist"]["routes"] == 1
    assert filled["watchlist"]["active"] == 1
    assert filled["watchlist"]["due"] == 1
    assert filled["watchlist"]["observations"] == 0


def test_a_group_code_names_the_airports_it_stands_for(client):
    """Abweisen ohne Vorschlag laesst den Nutzer raten, was er tippen soll."""
    response = client.post(
        "/api/watchlist", json={"origin": "BER", "destination": "Rom"}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "FCO" in detail and "CIA" in detail
