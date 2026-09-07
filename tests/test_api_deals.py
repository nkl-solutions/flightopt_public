"""Die Deals-Ansicht zeigt den besten Treffer je Profil-Scan mit Baseline-Abstand."""

from __future__ import annotations

import json
from datetime import date, datetime

from flightopt.api import main
from flightopt.domain.models import LegSpec, SearchSpec, StayRange
from flightopt.jobs.daily import collect_deals, save_profile
from flightopt.jobs.runner import JobRunner, specs_to_dict
from flightopt.storage import db
from flightopt.storage.baseline import refresh_baselines

OBSERVED = datetime(2026, 9, 5, 8, 0, 0)
OUT = date(2026, 10, 1)
BACK = date(2026, 10, 6)


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 10),
    )


def legs(price_out: float, price_back: float) -> list[dict]:
    return [
        {"origin": "BER", "destination": "ATH", "date": OUT.isoformat(),
         "price": price_out, "verified": False},
        {"origin": "ATH", "destination": "BER", "date": BACK.isoformat(),
         "price": price_back, "verified": False},
    ]


def add_job(conn, spec_json: str, *, finished_at: str, status: str = "done") -> int:
    cur = conn.execute(
        "INSERT INTO search_job(spec, status, created_at, finished_at) VALUES(?,?,?,?)",
        (spec_json, status, finished_at, finished_at),
    )
    return int(cur.lastrowid)


def add_result(conn, job_id: int, rank: int, minor: int, detail: list[dict]) -> None:
    conn.execute(
        "INSERT INTO itinerary_result("
        "job_id, rank, dates, price_total_minor, currency, is_estimate, detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            job_id,
            rank,
            json.dumps([OUT.isoformat(), BACK.isoformat()]),
            minor,
            "EUR",
            1,
            json.dumps(detail),
        ),
    )


def seed_baseline(conn) -> None:
    for price in (21000, 22000, 23000, 22500, 21500):
        conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, party_size, "
            "currency, price_total_minor, is_estimate) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                OBSERVED.isoformat(timespec="seconds"), "test", "flight", "BER|ATH",
                OUT.isoformat(), 1, "EUR", price, 1,
            ),
        )
    assert refresh_baselines(conn, now=OBSERVED, min_samples=5) == 1


def test_collect_deals_returns_rank_one_with_profile_and_signal(tmp_path):
    conn = db.connect(tmp_path / "deals.db")
    spec_json = json.dumps(specs_to_dict([spec()]))
    save_profile(conn, "Athen Oktober", [spec()], now=OBSERVED)
    job_id = add_job(conn, spec_json, finished_at="2026-09-06T07:00:00")
    add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))
    add_result(conn, job_id, 2, 19000, legs(95.0, 95.0))
    seed_baseline(conn)
    # Ein Lauf ohne Profil gehoert nicht in die Liste.
    orphan = add_job(conn, json.dumps({"airports": ["BER", "ATH"]}),
                     finished_at="2026-09-06T08:00:00")
    add_result(conn, orphan, 1, 15000, legs(75.0, 75.0))

    deals = collect_deals(conn, limit=50, now=OBSERVED)

    assert len(deals) == 1
    assert deals[0]["job_id"] == job_id
    assert deals[0]["profile"] == "Athen Oktober"
    assert deals[0]["route"] == "BER-ATH-BER"
    assert deals[0]["scanned_at"] == "2026-09-06T07:00:00"
    assert deals[0]["price"] == 160.0
    assert deals[0]["median"] == 220.0
    assert deals[0]["deviation_pct"] == -27.3
    assert deals[0]["signal"] == "cheap"


async def test_deals_endpoint_reads_the_runner_database(monkeypatch, tmp_path):
    path = tmp_path / "endpoint.db"
    conn = db.connect(path)
    spec_json = json.dumps(specs_to_dict([spec()]))
    save_profile(conn, "Athen Oktober", [spec()], now=OBSERVED)
    job_id = add_job(conn, spec_json, finished_at="2026-09-06T07:00:00")
    add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))
    conn.close()
    monkeypatch.setattr(main, "runner", JobRunner(str(path)))

    response = await main.deals(limit=50)

    assert [d["profile"] for d in response["deals"]] == ["Athen Oktober"]
    # Ohne Baseline wird nichts behauptet.
    assert response["deals"][0]["signal"] == "unknown"
    assert response["deals"][0]["median"] is None
    assert response["deals"][0]["deviation_pct"] is None
