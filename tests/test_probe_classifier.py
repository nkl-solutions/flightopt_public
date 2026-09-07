"""Die Kategorisierung des Probe-Werkzeugs.

Sechzehn Airline-Entscheidungen haengen an diesen drei Kategorien, deshalb sind
sie gepinnt. Der wichtigste Fall ist der letzte: Cloudflare vor einer Quelle
heisst nicht, dass die Quelle blockiert ist - Eurowings liefert hinter
Cloudflare seit Monaten Tagespreise.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

SPIKE = Path(__file__).resolve().parent.parent / "spike" / "probe_calendar.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("probe_calendar", SPIKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_placeholders_cover_the_whole_month(probe):
    values = probe.placeholders("BER", "AYT", date(2026, 11, 1))

    assert values["{origin}"] == "BER"
    assert values["{month_start}"] == "2026-11-01"
    assert values["{month_end}"] == "2026-11-30"
    assert values["{month_start_compact}"] == "20261101"
    assert values["{month}"] == "2026-11"


def test_placeholders_are_filled_in_nested_structures(probe):
    values = probe.placeholders("BER", "AYT", date(2026, 11, 1))
    filled = probe.fill(
        {"url": "https://x/{origin}", "body": {"d": ["{destination}", "{month}"]}},
        values,
    )

    assert filled == {"url": "https://x/BER", "body": {"d": ["AYT", "2026-11"]}}


def test_priced_days_finds_dates_next_to_amounts(probe):
    data = {"data": [{"date": "2026-11-01", "price": 99.0},
                     {"date": "2026-11-02", "price": {"amount": "88.50"}},
                     {"date": "2026-11-03", "price": None}]}

    assert probe.priced_days(data) == {"2026-11-01", "2026-11-02"}


def test_priced_days_finds_a_date_keyed_map(probe):
    data = {"2026-11-01": {"bestPrice": 462.64}, "2026-11-02": {"bestPrice": 0}}

    assert probe.priced_days(data) == {"2026-11-01"}


def test_priced_days_ignores_a_page_without_prices(probe):
    assert probe.priced_days({"dates": [{"date": "2026-11-01", "available": True}]}) == set()


def test_waf_markers_name_the_family(probe):
    markers = probe.waf_markers({"set-cookie": "_abck=xyz; Path=/"}, {}, "")

    assert "akamai:_abck" in markers


def test_waf_markers_read_response_headers(probe):
    markers = probe.waf_markers({"CF-Ray": "8abc", "Server": "cloudflare"}, {}, "")

    assert "cloudflare:cf-ray" in markers
    assert "cloudflare:server" in markers


def test_a_403_is_blocked(probe):
    assert probe.classify(403, [], None, "") == "blockiert"


def test_a_challenge_page_is_blocked_even_with_status_200(probe):
    body = "<html><title>Just a moment...</title></html>"

    assert probe.classify(200, ["cloudflare:cf-ray"], None, body) == "blockiert"


def test_an_answer_without_prices_is_no_calendar(probe):
    assert probe.classify(200, [], {"dates": []}, "{}") == "kein-kalender"


def test_a_missing_key_is_no_calendar_not_a_block(probe):
    # 401 heisst "Schluessel noetig", nicht "Bot-Schutz".
    assert probe.classify(401, [], None, "unauthorized") == "kein-kalender"


def test_prices_behind_cloudflare_are_still_open(probe):
    # Eurowings-Fall: Cloudflare davor, Tagespreise trotzdem da.
    data = {"data": [{"date": "2026-11-01", "price": 99.0},
                     {"date": "2026-11-02", "price": 89.0},
                     {"date": "2026-11-03", "price": 79.0}]}

    assert probe.classify(200, ["cloudflare:cf-ray"], data, "{}") == "offen"


def test_every_candidate_in_the_target_file_is_shaped_alike(probe):
    import json

    targets = json.loads(
        (SPIKE.with_name("probe_targets.json")).read_text(encoding="utf-8")
    )
    airlines = targets["airlines"]

    assert len(airlines) == 16
    for code, entry in airlines.items():
        assert entry["airline"], code
        assert 2 <= len(entry["candidates"]) <= 4, code
        for candidate in entry["candidates"]:
            assert candidate["name"], code
            assert candidate["url"].startswith("https://"), code
            assert candidate["method"] in {"GET", "POST"}, code
            if candidate["method"] == "POST":
                assert "body" in candidate, code
