"""Was der Healthcheck sagt, und was er nicht sagen darf."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

from flightopt.api import main
from flightopt.jobs.runner import JobRunner
from flightopt.storage import db


def client() -> TestClient:
    # Ohne `with` laeuft der Lifespan nicht, der Tages-Scanner startet also nicht.
    return TestClient(main.app)


def test_half_configured_basic_auth_is_unusable_and_says_so(monkeypatch, tmp_path):
    """Nutzer ohne Passwort: der Container war gesund und trotzdem unbenutzbar.

    Vorher warf `basic_auth_config` einen RuntimeError mitten in der
    Middleware, jede Route antwortete mit 500, und `/api/health` blieb gruen,
    weil es die Pruefung gar nicht erst anfasste.
    """
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "auth.db")))
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.delenv("FLIGHTOPT_BASIC_PASSWORD", raising=False)
    c = client()

    health = c.get("/api/health")
    page = c.get("/")

    assert health.status_code == 503
    assert health.json() == {"ok": False}
    assert page.status_code == 503


def test_health_stays_open_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "open.db")))
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.setenv("FLIGHTOPT_BASIC_PASSWORD", "secret")
    c = client()

    assert c.get("/").status_code == 401
    assert c.get("/api/health").status_code == 200


def test_public_health_says_nothing_but_the_verdict(monkeypatch, tmp_path):
    """Der Endpunkt ist ohne Anmeldung erreichbar, also gehoert nichts hinein.

    Keine Quellennamen, keine Pfade, keine Zaehlerstaende: alles davon liesse
    einen Fremden auf Aufbau und Nutzung schliessen.
    """
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "lean.db")))

    response = client().get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_health_turns_red_when_the_database_is_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "gone.db")))

    def broken() -> None:
        raise OSError("Datei nicht lesbar")

    monkeypatch.setattr(main.runner, "_conn", broken)

    response = client().get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"ok": False}


def test_detail_needs_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "detail-auth.db")))
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.setenv("FLIGHTOPT_BASIC_PASSWORD", "secret")
    c = client()

    assert c.get("/api/health/detail").status_code == 401

    encoded = base64.b64encode(b"dev:secret").decode("ascii")
    allowed = c.get(
        "/api/health/detail", headers={"Authorization": f"Basic {encoded}"}
    )

    assert allowed.status_code == 200


def test_detail_lists_the_adapters_that_really_exist(monkeypatch, tmp_path):
    """Zehn Adapter, nicht elf: Marabu faehrt auf dem Condor-Adapter mit."""
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "detail.db")))
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    body = client().get("/api/health/detail").json()
    names = [source["name"] for source in body["sources"]["flight"]]

    assert len(names) == 10
    assert "condor" in names
    # `DI` ist eine Airline im Katalog, aber keine eigene Quelle.
    assert "DI" not in names
    assert [s for s in body["sources"]["flight"] if s["name"] == "condor"][0][
        "carriers"
    ] == ["DE", "DI"]


def test_detail_shows_serpapi_once_a_key_is_configured(monkeypatch, tmp_path):
    """Die bezahlte Quelle stand nie im Bericht, weil sie keine Airline ist."""
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "serpapi.db")))
    monkeypatch.setenv("SERPAPI_KEY", "test-key")

    body = client().get("/api/health/detail").json()

    assert "serpapi" in [source["name"] for source in body["sources"]["flight"]]


def test_detail_lists_the_hotel_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "hotels.db")))

    body = client().get("/api/health/detail").json()

    assert [source["name"] for source in body["sources"]["hotel"]] == [
        "trivago",
        "booking",
    ]


def test_detail_reports_when_a_source_last_wrote(monkeypatch, tmp_path):
    """Die eigentliche Luecke: eine Quelle, die seit Tagen nichts liefert."""
    runner = JobRunner(str(tmp_path / "freshness.db"))
    monkeypatch.setattr(main, "runner", runner)
    conn = runner._conn()
    fresh = datetime.now() - timedelta(hours=2)
    silent = datetime.now() - timedelta(days=9)
    for source, stamp in (("ryanair", fresh), ("condor", silent)):
        conn.execute(
            "INSERT INTO price_observation(observed_at, source, entity_type, "
            "entity_key, travel_date, currency, price_total_minor) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                stamp.isoformat(timespec="seconds"),
                source,
                "flight",
                "BER|ATH",
                "2026-10-01",
                "EUR",
                9900,
            ),
        )
    conn.commit()
    conn.close()

    body = client().get("/api/health/detail").json()
    by_name = {source["name"]: source for source in body["sources"]["flight"]}

    assert by_name["ryanair"]["silent"] is False
    assert by_name["ryanair"]["silent_days"] == 0
    assert by_name["condor"]["silent"] is True
    assert by_name["condor"]["silent_days"] == 9
    # Eine Quelle, die noch nie geschrieben hat, ist kein leerer Eintrag.
    assert by_name["wizz"]["last_observation"] is None
    assert by_name["wizz"]["silent"] is True


def test_detail_names_writers_that_left_the_catalogue(monkeypatch, tmp_path):
    """Eine umbenannte oder entfernte Quelle verschwindet sonst lautlos."""
    runner = JobRunner(str(tmp_path / "unknown.db"))
    monkeypatch.setattr(main, "runner", runner)
    conn = runner._conn()
    conn.execute(
        "INSERT INTO price_observation(observed_at, source, entity_type, "
        "entity_key, travel_date, currency, price_total_minor) "
        "VALUES(?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), "sunexpress", "flight",
         "BER|AYT", "2026-10-01", "EUR", 9900),
    )
    conn.commit()
    conn.close()

    body = client().get("/api/health/detail").json()

    assert body["unknown_writers"] == ["sunexpress"]


def test_detail_reports_the_scheduler(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "sched.db")))

    body = client().get("/api/health/detail").json()

    assert body["scheduler"]["running"] is False
    assert body["scheduler"]["interval_seconds"] == main.daily_scheduler.interval_seconds
    assert body["database"]["reachable"] is True


@pytest.mark.parametrize(
    "last_seen, expected_days, expected_silent",
    [
        (None, None, True),
        (timedelta(hours=1), 0, False),
        (timedelta(days=2), 2, False),
        (timedelta(days=4), 4, True),
    ],
)
def test_silence_is_counted_in_whole_days(last_seen, expected_days, expected_silent):
    now = datetime(2026, 9, 9, 12, 0, 0)
    stamps = {} if last_seen is None else {
        "ryanair": (now - last_seen).isoformat(timespec="seconds")
    }

    row = main.source_freshness(["ryanair"], stamps, now=now)[0]

    assert row["silent_days"] == expected_days
    assert row["silent"] is expected_silent


def test_startup_purges_the_expired_cache(monkeypatch, tmp_path):
    """Der Tagesplaner laesst sich abschalten, dann raeumt sonst niemand."""
    monkeypatch.setenv("FLIGHTOPT_DAILY_SCANS", "0")
    runner = JobRunner(str(tmp_path / "startup-purge.db"))
    monkeypatch.setattr(main, "runner", runner)
    conn = runner._conn()
    conn.execute(
        "INSERT INTO price_cache(cache_key, source, payload, fetched_at, expires_at) "
        "VALUES(?,?,?,?,?)",
        ("alt", "ryanair", "{}", db.now(), db.expires(timedelta(hours=-1))),
    )
    conn.commit()
    conn.close()

    with client():
        pass

    conn = runner._conn()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM price_cache").fetchone()["c"] == 0
    finally:
        conn.close()


def test_detail_releases_the_sources_it_built(monkeypatch, tmp_path):
    """Der Bericht baut den Katalog, also gibt er ihn auch wieder frei.

    Scheitert das Lesen mittendrin, blieben die Sitzungen der schon gebauten
    Quellen offen - der Endpunkt laesst sich von aussen aufrufen, das haette
    sich aufsummiert.
    """
    monkeypatch.setattr(main, "runner", JobRunner(str(tmp_path / "leak.db")))
    closed: list[str] = []

    class Stub:
        def __init__(self, name: str, boom: bool = False) -> None:
            self.name = name
            self.boom = boom

        @property
        def carriers(self):
            if self.boom:
                raise RuntimeError("Katalog kaputt")
            return ()

        def close(self) -> None:
            closed.append(self.name)

    monkeypatch.setattr(
        main, "build_sources", lambda *a, **k: [Stub("heil"), Stub("kaputt", boom=True)]
    )

    with pytest.raises(RuntimeError):
        client().get("/api/health/detail")

    assert closed == ["heil", "kaputt"]
