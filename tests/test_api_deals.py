"""Die Deals-Ansicht zeigt den besten Treffer je Profil-Scan mit Baseline-Abstand."""

from __future__ import annotations

import json
from datetime import date, datetime

from flightopt.api import main
from flightopt.domain.models import LegSpec, SearchSpec, StayRange
from flightopt.jobs.daily import collect_deals, dispatch_due_profiles, save_profile
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


def add_job(conn, spec_json: str, *, finished_at: str, status: str = "done",
            profile_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO search_job(spec, status, created_at, finished_at, profile_id) "
        "VALUES(?,?,?,?,?)",
        (spec_json, status, finished_at, finished_at, profile_id),
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


def observe(conn, entity_key: str, travel_date: date, prices) -> None:
    for price in prices:
        conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, party_size, "
            "currency, price_total_minor, is_estimate) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                OBSERVED.isoformat(timespec="seconds"), "test", "flight", entity_key,
                travel_date.isoformat(), 1, "EUR", price, 1,
            ),
        )


def seed_baseline(conn) -> None:
    """Nur die Hinstrecke bekommt eine Baseline, der Rueckflug keine."""
    observe(conn, "BER|ATH", OUT, (21000, 22000, 23000, 22500, 21500))
    assert refresh_baselines(conn, now=OBSERVED, min_samples=5) == 1


def seed_both_baselines(conn) -> None:
    """Beide Teilstrecken. Mediane 220,00 und 180,00, zusammen 400,00."""
    observe(conn, "BER|ATH", OUT, (21000, 22000, 23000, 22500, 21500))
    observe(conn, "ATH|BER", BACK, (17000, 18000, 19000, 18500, 17500))
    assert refresh_baselines(conn, now=OBSERVED, min_samples=5) == 2


def test_collect_deals_returns_rank_one_with_profile_and_signal(tmp_path):
    conn = db.connect(tmp_path / "deals.db")
    spec_json = json.dumps(specs_to_dict([spec()]))
    profile_id = save_profile(conn, "Athen Oktober", [spec()], now=OBSERVED)
    job_id = add_job(conn, spec_json, finished_at="2026-09-06T07:00:00",
                     profile_id=profile_id)
    add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))
    add_result(conn, job_id, 2, 19000, legs(95.0, 95.0))
    seed_both_baselines(conn)
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
    # 220,00 fuer die Hinstrecke plus 180,00 fuer den Rueckflug. Vorher stand
    # hier 220,00: der Preis der ganzen Kette gegen die Baseline einer
    # Teilstrecke.
    assert deals[0]["median"] == 400.0
    assert deals[0]["deviation_pct"] == -60.0
    assert deals[0]["signal"] == "cheap"
    assert deals[0]["approximate"] is True


def test_a_manual_search_matching_a_saved_profile_is_not_a_profile_deal(tmp_path):
    conn = db.connect(tmp_path / "manual.db")
    try:
        save_profile(conn, "Athen Oktober", [spec()], now=OBSERVED)
        job_id = add_job(
            conn, json.dumps(specs_to_dict([spec()])),
            finished_at="2026-09-06T07:00:00",
        )
        add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))

        assert collect_deals(conn, now=OBSERVED) == []
    finally:
        conn.close()


def test_identical_profiles_keep_their_own_jobs_and_ignore_manual_searches(tmp_path):
    path = tmp_path / "identical.db"
    conn = db.connect(path)
    try:
        first = save_profile(conn, "Athen FR", [spec()], airlines=["FR"], now=OBSERVED)
        second = save_profile(conn, "Athen A3", [spec()], airlines=["A3"], now=OBSERVED)
        started = []

        class Runner(JobRunner):
            def start(self, job_id, specs, *, airlines=None):
                started.append((job_id, airlines))
                conn.execute(
                    "UPDATE search_job SET status='done', finished_at=? WHERE id=?",
                    ("2026-09-06T07:00:00", job_id),
                )
                add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))

        runner = Runner(str(path))
        jobs = dispatch_due_profiles(conn, runner, now=OBSERVED)
        manual = runner.create([spec()])
        conn.execute(
            "UPDATE search_job SET status='done', finished_at=? WHERE id=?",
            ("2026-09-06T08:00:00", manual),
        )
        add_result(conn, manual, 1, 15000, legs(75.0, 75.0))

        deals = collect_deals(conn, now=OBSERVED)

        assert len(deals) == 2
        assert {(deal["job_id"], deal["profile_id"]) for deal in deals} == {
            (jobs[0]["job_id"], first), (jobs[1]["job_id"], second),
        }
        assert [airlines for _, airlines in started] == [["FR"], ["A3"]]
        assert conn.execute(
            "SELECT profile_id FROM search_job WHERE id=?", (manual,)
        ).fetchone()["profile_id"] is None
        assert collect_deals(conn, limit=1, now=OBSERVED)[0]["profile_id"] == second

        # Editing a profile's parameters does not change the job's origin.
        conn.execute("UPDATE search_profile SET spec='{}' WHERE id=?", (first,))
        assert {deal["profile_id"] for deal in collect_deals(conn, now=OBSERVED)} == {
            first, second,
        }
    finally:
        conn.close()


def test_deleting_a_profile_keeps_its_job_without_attributing_its_deal(tmp_path):
    conn = db.connect(tmp_path / "deleted-profile.db")
    try:
        profile_id = save_profile(conn, "Athen", [spec()], now=OBSERVED)
        job_id = add_job(
            conn, json.dumps(specs_to_dict([spec()])),
            finished_at="2026-09-06T07:00:00", profile_id=profile_id,
        )
        add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))

        conn.execute("DELETE FROM search_profile WHERE id=?", (profile_id,))

        assert conn.execute(
            "SELECT profile_id FROM search_job WHERE id=?", (job_id,)
        ).fetchone()["profile_id"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM itinerary_result WHERE job_id=?", (job_id,)
        ).fetchone()[0] == 1
        assert collect_deals(conn, now=OBSERVED) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_a_chain_is_never_judged_against_a_single_leg(tmp_path):
    """Der Kettenpreis liegt zwischen den beiden Leg-Medianen.

    Gegen die Baseline der ersten Teilstrecke allein sieht er nach "teuer"
    aus; gegen die Preislage der ganzen Kette ist er normal. Genau dieser
    Groessenfehler liess bei drei Legs zwangslaeufig "rund plus 100 Prozent"
    entstehen.
    """
    conn = db.connect(tmp_path / "deals.db")
    spec_json = json.dumps(specs_to_dict([spec()]))
    profile_id = save_profile(conn, "Athen Oktober", [spec()], now=OBSERVED)
    job_id = add_job(conn, spec_json, finished_at="2026-09-06T07:00:00",
                     profile_id=profile_id)
    add_result(conn, job_id, 1, 39000, legs(210.0, 180.0))
    seed_both_baselines(conn)

    deal = collect_deals(conn, limit=50, now=OBSERVED)[0]

    assert deal["median"] == 400.0
    assert deal["signal"] == "normal"


def test_a_chain_without_a_baseline_for_every_leg_says_nothing(tmp_path):
    """Eine fehlende Teilsumme setzt die Vergleichsgroesse zu tief an."""
    conn = db.connect(tmp_path / "deals.db")
    spec_json = json.dumps(specs_to_dict([spec()]))
    profile_id = save_profile(conn, "Athen Oktober", [spec()], now=OBSERVED)
    job_id = add_job(conn, spec_json, finished_at="2026-09-06T07:00:00",
                     profile_id=profile_id)
    add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))
    seed_baseline(conn)

    deal = collect_deals(conn, limit=50, now=OBSERVED)[0]

    assert deal["signal"] == "unknown"
    assert deal["median"] is None
    assert deal["deviation_pct"] is None


def test_a_single_leg_chain_is_judged_exactly(tmp_path):
    """Eine Kette aus einem Leg ist keine Naeherung, sondern genau die Baseline."""
    conn = db.connect(tmp_path / "deals.db")
    one_way = SearchSpec(
        legs=(LegSpec("BER", "ATH"),),
        stays=(),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 10),
    )
    spec_json = json.dumps(specs_to_dict([one_way]))
    profile_id = save_profile(conn, "Nur hin", [one_way], now=OBSERVED)
    job_id = add_job(conn, spec_json, finished_at="2026-09-06T07:00:00",
                     profile_id=profile_id)
    conn.execute(
        "INSERT INTO itinerary_result("
        "job_id, rank, dates, price_total_minor, currency, is_estimate, detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            job_id, 1, json.dumps([OUT.isoformat()]), 21500, "EUR", 1,
            json.dumps([{"origin": "BER", "destination": "ATH",
                         "date": OUT.isoformat(), "price": 215.0,
                         "verified": False}]),
        ),
    )
    seed_baseline(conn)

    deal = collect_deals(conn, limit=50, now=OBSERVED)[0]

    assert deal["median"] == 220.0
    assert deal["approximate"] is False
    assert deal["signal"] == "normal"


async def test_deals_endpoint_reads_the_runner_database(monkeypatch, tmp_path):
    path = tmp_path / "endpoint.db"
    conn = db.connect(path)
    spec_json = json.dumps(specs_to_dict([spec()]))
    profile_id = save_profile(conn, "Athen Oktober", [spec()], now=OBSERVED)
    job_id = add_job(conn, spec_json, finished_at="2026-09-06T07:00:00",
                     profile_id=profile_id)
    add_result(conn, job_id, 1, 16000, legs(79.0, 81.0))
    conn.close()
    monkeypatch.setattr(main, "runner", JobRunner(str(path)))

    response = await main.deals(limit=50)

    assert [d["profile"] for d in response["deals"]] == ["Athen Oktober"]
    # Ohne Baseline wird nichts behauptet.
    assert response["deals"][0]["signal"] == "unknown"
    assert response["deals"][0]["median"] is None
    assert response["deals"][0]["deviation_pct"] is None
