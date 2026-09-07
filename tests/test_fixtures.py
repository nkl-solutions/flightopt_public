"""Die Fixtures fuer Teilprojekt A haben die Form, die die Parser erwarten.

Ein stiller Formatdrift in einer Fixture faelscht jeden Test, der auf ihr
aufbaut. Diese Datei prueft nur die Form, nicht das Parsen selbst.
"""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

OURAIRPORTS_COLUMNS = [
    "id", "ident", "type", "name", "latitude_deg", "longitude_deg",
    "elevation_ft", "continent", "iso_country", "iso_region", "municipality",
    "scheduled_service", "gps_code", "iata_code", "local_code", "home_link",
    "wikipedia_link", "keywords",
]


def test_ourairports_fixture_has_the_upstream_columns():
    text = (FIXTURES / "ourairports_sample.csv").read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))

    assert reader.fieldnames == OURAIRPORTS_COLUMNS
    rows = list(reader)
    assert len(rows) == 30
    assert {r["type"] for r in rows} >= {"large_airport", "medium_airport",
                                         "small_airport", "heliport", "closed"}


def test_ecb_fixture_carries_ten_daily_rates():
    root = ET.fromstring((FIXTURES / "ecb_eurofxref_daily.xml").read_text(encoding="utf-8"))
    days = [e for e in root.iter() if e.get("time")]
    rates = [e for e in root.iter() if e.get("currency")]

    assert [e.get("time") for e in days] == ["2026-09-05"]
    assert len(rates) == 10
    assert {e.get("currency") for e in rates} >= {"USD", "JPY", "GBP", "TRY"}


def test_serpapi_fixture_has_best_and_other_flights():
    data = json.loads(
        (FIXTURES / "serpapi_google_flights_BER_NRT.json").read_text(encoding="utf-8")
    )

    assert data["search_metadata"]["google_flights_url"].startswith("https://")
    assert len(data["best_flights"]) == 2
    assert len(data["other_flights"]) == 1
    first = data["best_flights"][0]
    assert isinstance(first["price"], int)
    assert first["flights"][0]["departure_airport"]["id"] == "BER"
    assert first["flights"][-1]["arrival_airport"]["id"] == "NRT"
    assert data["price_insights"]["lowest_price"] == 612
