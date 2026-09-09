"""Die Jagd ueber HTTP: Funde ansehen, abhaken, Takt stellen, Kurve lesen.

Die Oberflaeche baut ein anderer Agent nach diesen Endpunkten. Die Tests
halten deshalb nicht nur fest, dass sie antworten, sondern auch **womit** -
ein Feld, das spaeter anders heisst, bricht eine Oberflaeche, die es niemand
mehr fragen kann.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from flightopt.api import main
from flightopt.hunt import alerts, budget, cadence
from flightopt.jobs.runner import JobRunner

NOW = datetime(2026, 9, 9, 12, 0, 0)
TRAVEL = date(2026, 11, 20)


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    runner = JobRunner(str(tmp_path / "hunt-api.db"))
    monkeypatch.setattr(main, "runner", runner)
    monkeypatch.setattr(main.daily_scheduler, "runner", runner)
    return TestClient(main.app)


def a_find(**overrides) -> alerts.Find:
    base = {
        "entity_key": "BER|BKK",
        "travel_date": TRAVEL,
        "tier": "error",
        "price_minor": 3900,
        "source": "ryanair",
        "median_minor": 50000,
        "n": 12,
        "reason": "unter 25 Prozent des Medians von 500,00 Euro (n=12)",
        "distance_km": 8622.0,
    }
    base.update(overrides)
    return alerts.Find(**base)


def with_conn(client, work):
    conn = main.runner._conn()
    try:
        result = work(conn)
        conn.commit()
        return result
    finally:
        conn.close()


# -- Liste und Schalter -------------------------------------------------------


def test_an_empty_hunt_answers_with_zero_not_with_nothing(client):
    response = client.get("/api/hunt/finds")

    assert response.status_code == 200
    body = response.json()
    assert body["finds"] == []
    assert body["summary"]["events"] == 0
    assert body["summary"]["channel_configured"] is False


def test_a_find_is_listed_with_everything_the_view_needs(client):
    with_conn(client, lambda conn: alerts.record(
        conn, a_find(), delivery=alerts.DRY_RUN, now=NOW))

    body = client.get("/api/hunt/finds").json()

    find = body["finds"][0]
    assert find["route"] == "BER-BKK"
    assert find["travel_date"] == TRAVEL.isoformat()
    assert find["tier"] == "error"
    assert find["price"] == 39.0
    assert find["median"] == 500.0
    assert find["n"] == 12
    assert find["delivery"] == alerts.DRY_RUN
    assert find["reason"].startswith("unter 25 Prozent")
    assert find["booking_url"].startswith("https://")
    assert find["acknowledged_at"] is None


def test_a_find_can_be_ticked_off_and_reopened(client):
    event_id = with_conn(client, lambda conn: alerts.record(
        conn, a_find(), delivery=alerts.DRY_RUN, now=NOW))

    done = client.patch(f"/api/hunt/finds/{event_id}", json={"acknowledged": True})
    assert done.status_code == 200
    assert done.json()["acknowledged_at"] is not None

    still_there = client.get("/api/hunt/finds?open_only=true").json()
    assert still_there["finds"] == []

    again = client.patch(f"/api/hunt/finds/{event_id}", json={"acknowledged": False})
    assert again.json()["acknowledged_at"] is None
    assert len(client.get("/api/hunt/finds?open_only=true").json()["finds"]) == 1


def test_ticking_off_an_unknown_find_is_a_404(client):
    response = client.patch("/api/hunt/finds/4711", json={"acknowledged": True})

    assert response.status_code == 404


def test_the_list_can_be_narrowed_to_one_tier(client):
    def seed(conn):
        alerts.record(conn, a_find(), delivery=alerts.DRY_RUN, now=NOW)
        alerts.record(conn, a_find(tier="cheap", travel_date=TRAVEL + timedelta(days=1)),
                      delivery=alerts.DRY_RUN, now=NOW)

    with_conn(client, seed)

    body = client.get("/api/hunt/finds?tier=error").json()

    assert [f["tier"] for f in body["finds"]] == ["error"]


# -- Takt einer Strecke -------------------------------------------------------


def test_a_route_can_be_switched_to_the_hot_pace(client):
    created = client.post("/api/watchlist", json={"origin": "BER", "destination": "BKK"})
    route_id = created.json()["id"]

    response = client.patch(f"/api/watchlist/{route_id}", json={"cadence": "hot"})

    assert response.status_code == 200
    body = response.json()
    assert body["cadence"] == "hot"
    assert body["hot"] is True
    assert client.get("/api/watchlist").json()["summary"]["hot"] == 1


def test_the_switch_still_takes_a_plain_on_off(client):
    """Die bestehende Form bleibt gueltig: die Oberflaeche schickt sie so."""
    created = client.post("/api/watchlist", json={"origin": "BER", "destination": "ATH"})
    route_id = created.json()["id"]

    response = client.patch(f"/api/watchlist/{route_id}", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["cadence"] == "daily"


def test_an_unknown_pace_is_refused_with_a_readable_reason(client):
    created = client.post("/api/watchlist", json={"origin": "BER", "destination": "ATH"})
    route_id = created.json()["id"]

    response = client.patch(f"/api/watchlist/{route_id}", json={"cadence": "stuendlich"})

    assert response.status_code == 400
    assert "Takt" in response.json()["detail"]


def test_a_wide_window_may_not_go_hot(client):
    created = client.post(
        "/api/watchlist",
        json={"origin": "BER", "destination": "ATH",
              "lead_min_days": 0, "lead_max_days": 100},
    )
    route_id = created.json()["id"]

    response = client.patch(f"/api/watchlist/{route_id}", json={"cadence": "hot"})

    assert response.status_code == 400
    assert "Fenster" in response.json()["detail"]


def test_switching_an_unknown_route_is_a_404(client):
    assert client.patch("/api/watchlist/4711", json={"cadence": "hot"}).status_code == 404


# -- Preisverlauf -------------------------------------------------------------


def observe(conn, *, day: datetime, travel: date, price_minor: int,
            source: str = "ryanair", indicative: int = 0) -> None:
    conn.execute(
        "INSERT INTO price_observation(observed_at, source, entity_type, entity_key, "
        "travel_date, party_size, currency, price_total_minor, is_estimate, "
        "is_indicative) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (day.isoformat(timespec="seconds"), source, "flight", "BER|ATH",
         travel.isoformat(), 1, "EUR", price_minor, 1, indicative),
    )


def test_the_curve_groups_by_observation_day_by_default(client):
    def seed(conn):
        for offset, price in ((0, 12000), (0, 9000), (1, 15000)):
            observe(conn, day=datetime.now() - timedelta(days=offset),
                    travel=TRAVEL, price_minor=price)

    with_conn(client, seed)

    body = client.get("/api/hunt/history/BER/ATH").json()

    assert body["route"] == "BER-ATH"
    assert body["entity_key"] == "BER|ATH"
    assert body["by"] == "observed"
    assert len(body["points"]) == 2
    today = body["points"][-1]
    assert today["min"] == 90.0
    assert today["max"] == 120.0
    assert today["median"] == 105.0
    assert today["n"] == 2


def test_the_curve_can_be_read_by_travel_day(client):
    def seed(conn):
        observe(conn, day=datetime.now(), travel=TRAVEL, price_minor=12000)
        observe(conn, day=datetime.now(), travel=TRAVEL + timedelta(days=1),
                price_minor=9000)

    with_conn(client, seed)

    body = client.get("/api/hunt/history/BER/ATH?by=travel").json()

    assert [p["day"] for p in body["points"]] == [
        TRAVEL.isoformat(), (TRAVEL + timedelta(days=1)).isoformat()
    ]


def test_the_curve_leaves_indicative_prices_out(client):
    """Sonst springt die Linie zwischen Direkttarif und Ein-Stopp-Verbindung."""
    def seed(conn):
        observe(conn, day=datetime.now(), travel=TRAVEL, price_minor=12000)
        observe(conn, day=datetime.now(), travel=TRAVEL, price_minor=4000,
                source="kiwi", indicative=1)

    with_conn(client, seed)

    body = client.get("/api/hunt/history/BER/ATH").json()

    assert body["points"][0]["min"] == 120.0


def test_the_curve_carries_the_pace_and_the_stats_of_a_watched_route(client):
    client.post("/api/watchlist", json={"origin": "BER", "destination": "ATH"})

    body = client.get("/api/hunt/history/BER/ATH").json()

    assert body["cadence"] == "daily"
    assert body["hot"] is False
    assert body["stats"]["observations"] == 0
    assert body["stats"]["min_days"] > 0


def test_an_unwatched_route_still_answers_with_its_history(client):
    with_conn(client, lambda conn: observe(
        conn, day=datetime.now(), travel=TRAVEL, price_minor=12000))

    body = client.get("/api/hunt/history/BER/ATH").json()

    assert body["cadence"] is None
    assert body["stats"] is None
    assert body["points"][0]["min"] == 120.0


def test_a_nonsense_grouping_is_refused(client):
    response = client.get("/api/hunt/history/BER/ATH?by=mondphase")

    assert response.status_code == 400


def test_a_place_name_leads_to_the_same_curve_as_its_code(client):
    """Dieselbe Aufloesung wie beim Eintragen: "Berlin" ist BER."""
    with_conn(client, lambda conn: observe(
        conn, day=datetime.now(), travel=TRAVEL, price_minor=12000))

    body = client.get("/api/hunt/history/Berlin/Athen").json()

    assert body["entity_key"] == "BER|ATH"
    assert body["points"][0]["min"] == 120.0


def test_an_unknown_place_is_refused(client):
    response = client.get("/api/hunt/history/Atlantis/ATH")

    assert response.status_code == 400
    assert "Atlantis" in response.json()["detail"]


# -- Betriebsbericht ----------------------------------------------------------


def test_the_health_detail_names_the_pace_and_the_channel(client):
    body = client.get("/api/health/detail").json()

    hunt = body["hunt"]
    assert hunt["interval_seconds"] == cadence.HOT_INTERVAL_SECONDS
    assert hunt["calls_per_pass"] == cadence.CALLS_PER_PASS
    assert hunt["cap_per_source_hour"] == cadence.MAX_CALLS_PER_SOURCE_HOUR
    assert hunt["max_hot_routes"] == cadence.MAX_HOT_ROUTES
    assert hunt["hot_routes"] == 0
    assert hunt["paused"] == []
    assert hunt["channel"] == {
        "kind": "discord",
        "configured": False,
        "env": "FLIGHTOPT_DISCORD_WEBHOOK",
    }
    assert hunt["alerts"]["events"] == 0


def test_the_health_detail_shows_a_spent_budget_and_an_open_fuse(client):
    def seed(conn):
        budget.charge(conn, ["ryanair"], cadence.CALLS_PER_PASS)
        budget.pause(conn, ["aegean"], reason="HTTP 429")

    with_conn(client, seed)

    hunt = client.get("/api/health/detail").json()["hunt"]

    spent = {row["source"]: row for row in hunt["budget"]}
    assert spent["ryanair"]["used"] == cadence.CALLS_PER_PASS
    assert spent["ryanair"]["remaining"] == (
        cadence.MAX_CALLS_PER_SOURCE_HOUR - cadence.CALLS_PER_PASS
    )
    assert [row["source"] for row in hunt["paused"]] == ["aegean"]
    assert hunt["paused"][0]["reason"] == "HTTP 429"


def test_the_health_detail_says_when_the_scheduler_slows_the_hunt_down(client,
                                                                      monkeypatch):
    """Der Planer taktet die Jagd, nicht umgekehrt.

    Steht sein Intervall hoeher als der heisse Takt, laeuft die Jagd
    langsamer als gedacht - und das soll man sehen und nicht ausrechnen.
    """
    monkeypatch.setattr(main.daily_scheduler, "interval_seconds", 3600)

    hunt = client.get("/api/health/detail").json()["hunt"]

    assert hunt["effective_interval_seconds"] == 3600


def test_a_hot_pass_can_be_triggered_by_hand(client, monkeypatch):
    seen: list = []

    async def fake_hunt(conn, *, now=None):
        seen.append(now)
        return {"routes": 0, "finds": 0, "alerts": [], "paused": []}

    monkeypatch.setattr("flightopt.jobs.scheduler.run_hunt", fake_hunt)

    response = client.post("/api/hunt/run-once")

    assert response.status_code == 200
    assert response.json()["routes"] == 0
    assert len(seen) == 1


def test_the_watchlist_row_carries_its_pace(client):
    created = client.post("/api/watchlist", json={"origin": "BER", "destination": "BKK"})

    assert created.json()["cadence"] == "daily"
    assert created.json()["hot"] is False
    listed = client.get("/api/watchlist").json()
    assert listed["routes"][0]["cadence"] == "daily"
    assert listed["summary"]["hot_interval_seconds"] == cadence.HOT_INTERVAL_SECONDS


def test_the_window_limits_the_observations_not_the_travel_days(client):
    """Sonst zeigt der Preiskalender Preise von vor drei Monaten als heutige."""
    def seed(conn):
        observe(conn, day=datetime.now() - timedelta(days=40), travel=TRAVEL,
                price_minor=5000)
        observe(conn, day=datetime.now(), travel=TRAVEL, price_minor=12000)

    with_conn(client, seed)

    body = client.get("/api/hunt/history/BER/ATH?by=travel&days=7").json()

    assert len(body["points"]) == 1
    assert body["points"][0]["min"] == 120.0
