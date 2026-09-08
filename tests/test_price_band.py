"""Die Preislage: was ein Preis gegenueber seiner eigenen Historie taugt.

Kein Test fasst hier ein Netz an. Der Detektor rechnet gegen `price_baseline`,
und die Zeilen dort werden im Test selbst gesetzt: nur so laesst sich pruefen,
dass ohne Baseline wirklich nichts behauptet wird.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, Money, SearchSpec, StayRange
from flightopt.jobs import runner as runner_module
from flightopt.jobs.runner import (
    JobRunner,
    apply_price_bands,
    leg_entity_key,
    price_band,
)
from flightopt.search.dp import Combination
from flightopt.search.grid import GridReport
from flightopt.search.verify import VerifyReport
from flightopt.storage import db

OUT = date(2026, 10, 1)
BACK = date(2026, 10, 5)
OBSERVED = datetime(2026, 9, 1, 12, 0, 0)


def conn_at(tmp_path, name: str = "band.db"):
    return db.connect(str(tmp_path / name))


def leg(price: float = 99.0, day: str = "2026-10-01") -> dict:
    return {"origin": "BER", "destination": "ATH", "date": day, "price": price}


def put_baseline(conn, entity_key: str, *, median_minor: int, mad_minor: int = 1000,
                 weekday: int = 3, bucket: str = "30-59", n: int = 12) -> None:
    """Eine fertige Baseline setzen, ohne den Umweg ueber die Beobachtungen."""
    conn.execute(
        "INSERT INTO price_baseline("
        "entity_type, entity_key, weekday, leadtime_bucket, currency, "
        "median_minor, mad_minor, n, computed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("flight", entity_key, weekday, bucket, "EUR", median_minor, mad_minor, n,
         OBSERVED.isoformat(timespec="seconds")),
    )
    conn.commit()


def test_the_entity_key_is_the_leg_not_the_route():
    assert leg_entity_key(leg()) == "BER|ATH"
    assert leg_entity_key({}) == "|"


def test_without_a_baseline_nothing_is_claimed(tmp_path):
    conn = conn_at(tmp_path)

    band = price_band(conn, leg(), observed_at=OBSERVED)

    assert band["tier"] == "unknown"
    assert "median" not in band
    assert "deviation_pct" not in band


def test_a_cheap_leg_is_named_cheap_against_its_own_baseline(tmp_path):
    conn = conn_at(tmp_path)
    # 01.10.2026 ist ein Donnerstag, beobachtet am 01.09.2026: 30 Tage Vorlauf.
    put_baseline(conn, "BER|ATH", median_minor=20000, mad_minor=1000)

    band = price_band(conn, leg(price=99.0), observed_at=OBSERVED)

    assert band["tier"] == "cheap"
    assert band["median"] == 200.0
    assert band["deviation_pct"] == -50.5
    assert band["n"] == 12


def test_a_dear_leg_is_named_dear_and_a_normal_one_normal(tmp_path):
    conn = conn_at(tmp_path)
    put_baseline(conn, "BER|ATH", median_minor=20000, mad_minor=1000)

    assert price_band(conn, leg(price=400.0), observed_at=OBSERVED)["tier"] == "expensive"
    assert price_band(conn, leg(price=201.0), observed_at=OBSERVED)["tier"] == "normal"


def test_the_fourth_tier_never_shows_up_for_a_flight(tmp_path):
    """`error` gehoert den Hotels. Fuer Fluege gibt es genau drei Stufen."""
    conn = conn_at(tmp_path)
    put_baseline(conn, "BER|ATH", median_minor=20000, mad_minor=1000)

    tiers = {
        price_band(conn, leg(price=price), observed_at=OBSERVED)["tier"]
        for price in (1.0, 99.0, 201.0, 4000.0)
    }

    assert tiers <= {"cheap", "normal", "expensive"}
    assert "error" not in tiers


def test_a_leg_without_price_or_date_stays_unknown(tmp_path):
    conn = conn_at(tmp_path)
    put_baseline(conn, "BER|ATH", median_minor=20000)

    assert price_band(conn, {"origin": "BER", "destination": "ATH", "price": 99.0},
                      observed_at=OBSERVED)["tier"] == "unknown"
    assert price_band(conn, leg(price=0.0), observed_at=OBSERVED)["tier"] == "unknown"


def test_every_leg_of_a_row_gets_its_own_band(tmp_path):
    """Die Baseline gilt je Teilstrecke, also wird auch je Teilstrecke gerechnet."""
    conn = conn_at(tmp_path)
    put_baseline(conn, "BER|ATH", median_minor=20000, mad_minor=1000)

    rows = [
        {
            "currency": "EUR",
            "legs": [
                leg(price=99.0),
                {"origin": "ATH", "destination": "BER", "date": "2026-10-05",
                 "price": 120.0},
            ],
        }
    ]
    apply_price_bands(conn, rows, observed_at=OBSERVED)

    assert rows[0]["legs"][0]["band"]["tier"] == "cheap"
    # Fuer den Rueckweg gibt es keine Baseline, also auch keine Aussage.
    assert rows[0]["legs"][1]["band"]["tier"] == "unknown"


# ---------------------------------------------------------------------------
# Der Runner haengt die Preislage an beide Ereignisse: Schaetzung und Pruefung.


def spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "ATH"), LegSpec("ATH", "BER")),
        stays=(StayRange(3, 5),),
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 12),
    )


@pytest.fixture
def offline(monkeypatch):
    async def no_routes(sources, legs):
        return None

    async def fake_build_grid(s, sources, **kwargs):
        grid = {0: {OUT: Money(9900)}, 1: {BACK: Money(12000)}}
        report = GridReport(filled={0: 1, 1: 1}, calls=2,
                            winner={(0, OUT): "FR", (1, BACK): "FR"})
        return grid, report

    def fake_solve(s, grid, **kwargs):
        return [Combination(dates=(OUT, BACK), total=Money(21900))]

    async def fake_verify(s, best, sources, **kwargs):
        return [], VerifyReport()

    async def fake_rates(conn, **kwargs):
        return Rates()

    monkeypatch.setattr(runner_module, "preload_routes", no_routes)
    monkeypatch.setattr(runner_module, "build_grid", fake_build_grid)
    monkeypatch.setattr(runner_module, "solve", fake_solve)
    monkeypatch.setattr(runner_module, "verify", fake_verify)
    monkeypatch.setattr(runner_module.fx_store, "current_rates", fake_rates)


async def test_the_runner_bands_both_the_estimate_and_the_verified_row(offline, tmp_path):
    runner = JobRunner(str(tmp_path / "run.db"))
    conn = runner._conn()
    put_baseline(conn, "BER|ATH", median_minor=20000, mad_minor=1000,
                 weekday=OUT.weekday(), bucket="120+")
    conn.close()

    job_id = runner.create(spec())
    await runner._run(job_id, spec())

    partial = next(p for p in runner._history[job_id] if p.phase == "partial")
    first = partial.detail["results"][0]["legs"][0]
    assert first["band"]["tier"] in {"cheap", "normal", "expensive", "unknown"}

    verified = [p for p in runner._history[job_id] if p.phase == "verified"]
    assert all("band" in row_leg
               for p in verified
               for row_leg in p.detail["result"]["legs"])


async def test_a_run_without_any_baseline_says_so_on_every_leg(offline, tmp_path):
    """Bei duenner Historie steht ueberall "keine Basis". Das ist in Ordnung."""
    runner = JobRunner(str(tmp_path / "empty.db"))
    job_id = runner.create(spec())

    await runner._run(job_id, spec())

    done = runner._history[job_id][-1]
    assert done.phase == "done"
    tiers = [l["band"]["tier"] for row in done.detail["results"] for l in row["legs"]]
    assert tiers and set(tiers) == {"unknown"}


async def test_the_stored_row_keeps_the_band_it_was_measured_with(offline, tmp_path):
    """Ein gespeicherter Scan zeigt spaeter dieselbe Preislage wie am Scan-Tag."""
    path = str(tmp_path / "keep.db")
    runner = JobRunner(path)
    job_id = runner.create(spec())

    await runner._run(job_id, spec())

    # Ein zweiter Runner kennt den Verlauf nicht und liest nur die Datenbank.
    stored = JobRunner(path).result(job_id)
    assert stored["status"] == "done"
    assert all("band" in leg_row
               for row in stored["results"] for leg_row in row["legs"])
