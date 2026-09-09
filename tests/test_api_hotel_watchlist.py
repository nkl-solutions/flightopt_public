"""Die Hotelbeobachtung ueber HTTP: eintragen, ansehen, schalten, laufen lassen.

Zugeschnitten wie `/api/watchlist` und `/api/hunt/*`, und zwar mit Absicht:
die Oberflaeche soll fuer Fluege und Hotels dasselbe Muster lesen und nicht
zwei. Deshalb steht hier auch der Test, dass eine einzelne Zeile genauso
aussieht wie eine aus der Liste - zwei Formen fuer dieselbe Sache waeren zwei
Fallunterscheidungen im Browser.

Kein Test fasst ein Netz an.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from flightopt.api import main
from flightopt.hotels import browser as browser_module
from flightopt.hotels import watch as hotel_watch
from flightopt.jobs import scheduler as scheduler_module
from flightopt.jobs.runner import JobRunner
from flightopt.storage import db


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    runner = JobRunner(str(tmp_path / "hotel-watch-api.db"))
    monkeypatch.setattr(main, "runner", runner)
    monkeypatch.setattr(main.daily_scheduler, "runner", runner)
    # Ohne `with` laeuft der Lifespan nicht, der Tagesplaner startet also nicht.
    return TestClient(main.app)


ATHEN = {"destination": "Athen", "nights": 2, "adults": 2}


# -- Eintragen und ansehen -------------------------------------------------


def test_an_empty_hotel_watchlist_answers_with_zero_not_with_nothing(client):
    """Null Beobachtungen ist eine Aussage.

    Sie sieht sonst genauso aus wie eine Beobachtung, die nichts findet - und
    das ist ein ganz anderer Befund.
    """
    response = client.get("/api/hotels/watchlist")

    assert response.status_code == 200
    body = response.json()
    assert body["watches"] == []
    assert body["summary"]["watches"] == 0
    assert body["summary"]["due"] == 0
    assert body["summary"]["max_per_run"] == hotel_watch.MAX_WATCHES_PER_RUN


def test_a_watch_can_be_added_and_read_back(client):
    created = client.post("/api/hotels/watchlist", json=ATHEN)

    assert created.status_code == 200
    row = created.json()
    assert row["destination"] == "Athen"
    assert row["nights"] == 2
    assert row["enabled"] is True
    assert row["lead_min_days"] == hotel_watch.DEFAULT_LEAD_MIN
    assert row["lead_max_days"] == hotel_watch.DEFAULT_LEAD_MAX
    # Ohne Historie ist nichts messbar, und das steht da auch.
    assert row["observations"] == 0
    assert row["ready"] is False
    assert row["due"] is True
    assert len(row["window"]) == 2

    listed = client.get("/api/hotels/watchlist").json()
    assert [w["id"] for w in listed["watches"]] == [row["id"]]
    assert listed["summary"]["watches"] == 1


def test_a_single_row_looks_exactly_like_a_row_from_the_list(client):
    """Sonst muesste die Oberflaeche zwei Formen derselben Sache lesen."""
    created = client.post("/api/hotels/watchlist", json=ATHEN).json()

    listed = client.get("/api/hotels/watchlist").json()["watches"][0]

    assert set(created) == set(listed)


def test_the_same_search_twice_stays_one_row(client):
    """Zweimal dieselbe Frage waeren zweimal dieselben Abrufe am selben Tag."""
    first = client.post("/api/hotels/watchlist", json=ATHEN).json()
    second = client.post("/api/hotels/watchlist", json=ATHEN).json()

    assert first["id"] == second["id"]
    assert client.get("/api/hotels/watchlist").json()["summary"]["watches"] == 1


def test_a_window_nobody_can_serve_is_a_400_and_not_a_500(client):
    """Gekuerzt wird nichts stillschweigend, und abgestuerzt wird auch nicht."""
    too_wide = client.post(
        "/api/hotels/watchlist",
        json={**ATHEN, "lead_min_days": 1,
              "lead_max_days": hotel_watch.MAX_WINDOW_DAYS + 5},
    )
    upside_down = client.post(
        "/api/hotels/watchlist",
        json={**ATHEN, "lead_min_days": 40, "lead_max_days": 10},
    )

    assert too_wide.status_code == 400
    assert upside_down.status_code == 400
    assert client.get("/api/hotels/watchlist").json()["watches"] == []


# -- Schalten --------------------------------------------------------------


def test_a_watch_can_be_switched_off_and_on_again(client):
    watch_id = client.post("/api/hotels/watchlist", json=ATHEN).json()["id"]

    off = client.patch(f"/api/hotels/watchlist/{watch_id}", json={"enabled": False})

    assert off.status_code == 200
    assert off.json()["enabled"] is False
    # Abgeschaltet ist nie faellig, aber auch nicht geloescht.
    assert off.json()["due"] is False
    assert client.get("/api/hotels/watchlist").json()["summary"]["watches"] == 1

    on = client.patch(f"/api/hotels/watchlist/{watch_id}", json={"enabled": True})

    assert on.json()["enabled"] is True


def test_switching_an_unknown_watch_is_a_404(client):
    assert client.patch(
        "/api/hotels/watchlist/404", json={"enabled": False}
    ).status_code == 404


# -- Von Hand laufen lassen ------------------------------------------------


def test_the_hotel_pass_can_be_triggered_by_hand(client, monkeypatch):
    """Damit der erste Eintrag nicht bis zum naechsten Durchgang wartet."""
    calls: list[str] = []

    async def fake_run(conn, *, now=None):
        calls.append(str(now))
        return {
            "watches": 1, "days": 7, "observations": 14, "errors": [], "due_left": 0
        }

    monkeypatch.setattr(scheduler_module, "run_hotel_watches", fake_run)

    response = client.post("/api/hotels/watchlist/run-once")

    assert response.status_code == 200
    assert response.json()["observations"] == 14
    assert len(calls) == 1


# -- Betriebsbericht -------------------------------------------------------


def test_health_detail_says_whether_any_hotel_is_being_watched(client):
    empty = client.get("/api/health/detail").json()["hotels"]

    assert empty["watches"] == 0
    assert empty["active"] == 0
    assert empty["due"] == 0
    assert empty["observations"] == 0
    assert empty["days_recorded"] == 0
    assert empty["max_per_run"] == hotel_watch.MAX_WATCHES_PER_RUN
    assert empty["min_days"] == hotel_watch.MIN_RECORDING_DAYS
    # Der Zahlenteil des Dauerbetriebs, wie beim `hunt`-Block: was gilt, nicht
    # nur was gerade ist.
    assert empty["browser"]["idle_seconds"] > 0
    assert empty["baseline_splits_populations"] in (True, False)

    client.post("/api/hotels/watchlist", json=ATHEN)
    filled = client.get("/api/health/detail").json()["hotels"]

    assert filled["watches"] == 1
    assert filled["active"] == 1
    assert filled["due"] == 1


def test_the_observation_count_is_the_number_of_observations(client):
    """Zwei Beobachtungen derselben Belegung zaehlten eine Zeile doppelt.

    `watch_stats` fragt die Historie nach Naechten und Belegung ab, nicht nach
    dem Ziel - die Beobachtung traegt gar kein Ziel. Zwei Suchen mit demselben
    Zuschnitt sehen deshalb beide dieselben Zeilen, und wer die Zeilen der
    einzelnen Suchen aufaddiert, zaehlt jede so oft, wie es Suchen gibt.

    Der Betriebsbericht will die Zahl der Beobachtungen wissen, und die steht
    genau einmal in der Tabelle.
    """
    client.post("/api/hotels/watchlist", json={"destination": "Athen", "nights": 2})
    client.post("/api/hotels/watchlist", json={"destination": "Paris", "nights": 2})
    conn = db.connect(main.runner.db_path)
    conn.execute(
        "INSERT INTO price_observation("
        "observed_at, source, entity_type, entity_key, travel_date, "
        "return_or_nights, party_size, currency, price_total_minor, is_estimate) "
        "VALUES('2026-09-05T08:00:00','trivago','hotel','trivago:melia',"
        "'2026-11-10','2',2,'EUR',9000,1)"
    )
    conn.commit()
    conn.close()

    hotels = client.get("/api/health/detail").json()["hotels"]

    assert hotels["watches"] == 2
    assert hotels["observations"] == 1
    assert hotels["days_recorded"] == 1


def test_the_effective_interval_is_the_one_the_scheduler_really_uses(client):
    """Die Schleife laeuft nur so oft, wie der Tagesplaner sie aufruft."""
    hotels = client.get("/api/health/detail").json()["hotels"]

    assert hotels["interval_seconds"] == main.daily_scheduler.interval_seconds


# -- Herunterfahren --------------------------------------------------------


class NeverLaunches:
    async def __call__(self):  # pragma: no cover - kein Test oeffnet eine Seite
        raise AssertionError("dieser Test startet keinen Browser")


def test_shutdown_closes_the_shared_browsers(monkeypatch, tmp_path):
    """Ein sauberes Ende ist billiger als ein Zombie-Chromium.

    Die Leerlaufwache erledigt es sonst auch - zwei Minuten nach der letzten
    Seite. Beim Herunterfahren ist das zwei Minuten zu spaet: der Prozess
    geht, und ein Chromium ohne Elternprozess haelt seine paar hundert
    Megabyte weiter.
    """
    runner = JobRunner(str(tmp_path / "shutdown.db"))
    monkeypatch.setattr(main, "runner", runner)
    monkeypatch.setattr(main.daily_scheduler, "runner", runner)
    monkeypatch.setenv("FLIGHTOPT_DAILY_SCANS", "0")
    browser_module.reset_shared_pools()
    browser_module.shared_pool("booking", NeverLaunches())
    assert browser_module._POOLS

    try:
        with TestClient(main.app) as running:
            running.get("/api/health")
        assert browser_module._POOLS == {}
    finally:
        browser_module.reset_shared_pools()
