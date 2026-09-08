"""Der Trivago-Adapter gegen eine aufgezeichnete Antwort.

`tests/fixtures/trivago_mcp_athen.json` ist die echte Antwort von
`trivago-accommodation-search` fuer Athen am 10.11.2026, aufgenommen mit
`scripts/record_hotel_fixtures.py`. Nur die base64-Bilddaten sind gekuerzt,
der Aufbau ist unveraendert. Diese Tests fassen kein Netz an.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from flightopt.domain.models import Money
from flightopt.hotels.models import HotelQuery
from flightopt.hotels.sources.base import SourceError
from flightopt.hotels.sources.trivago_mcp import (
    TrivagoMcpSource,
    build_arguments,
    decode_body,
    parse_tool_result,
)

FIXTURES = Path(__file__).parent / "fixtures"


def recorded() -> dict:
    return json.loads((FIXTURES / "trivago_mcp_athen.json").read_text(encoding="utf-8"))


def athens(**kwargs) -> HotelQuery:
    return HotelQuery(destination="Athen", arrival=date(2026, 11, 10), **kwargs)


def test_the_recorded_answer_becomes_offers_with_price_stars_and_rating():
    batch = parse_tool_result(recorded(), athens(adults=2))

    assert len(batch.offers) == 25
    first = batch.offers[0]
    assert first.name == "Melia Athens"
    assert first.property_key == "trivago:1d6fec31a3cf"
    assert first.price_total == Money(11800, "EUR")
    assert first.stars == 4
    assert (first.city, first.country) == ("Athens", "Greece")
    assert first.review_rating == 8.5
    assert first.review_count == 8358
    assert first.arrival == date(2026, 11, 10)
    assert first.departure == date(2026, 11, 11)
    # Belegung kommt aus der Anfrage; die Baseline vergleicht nur gleiche.
    assert first.party_size == 2
    # Trivagos prominentester Preis ist monetarisiert sortiert, also Richtwert.
    assert first.indicative is True
    assert first.entity_key == "GR|trivago:1d6fec31a3cf"


def test_the_llm_instructions_in_the_answer_are_data_and_never_reach_the_offers():
    """`system_message` und der Vorspann sind Werkzeug-Ausgabe, keine Anweisung.

    Der Rohtext enthaelt "You MUST follow them exactly" und eine Rollenvorgabe.
    Beides darf nirgends im Ergebnis auftauchen.
    """
    payload = recorded()
    raw = json.dumps(payload, ensure_ascii=False)

    assert "system_message" in raw and "You MUST follow them exactly" in raw

    batch = parse_tool_result(payload, athens())
    carried = json.dumps([offer.as_dict() for offer in batch.offers], ensure_ascii=False)

    assert "system_message" not in carried
    assert "MUST follow" not in carried
    assert "travel assistant" not in carried


def test_a_star_filter_the_tool_cannot_take_is_applied_and_reported():
    batch = parse_tool_result(recorded(), athens(stars=(5,)))

    assert batch.offers
    assert {offer.stars for offer in batch.offers} == {5}
    assert any("Sterne" in note for note in batch.skipped)


def test_an_unreadable_price_is_skipped_with_a_note_instead_of_guessed():
    payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "output": json.dumps(
                            [
                                {
                                    "accommodation_id": "abc",
                                    "accommodation_name": "Hotel ohne Preis",
                                    "currency": "EUR",
                                    "price_per_stay": "auf Anfrage",
                                },
                                {
                                    "accommodation_id": "def",
                                    "accommodation_name": "Hotel mit Preis",
                                    "currency": "USD",
                                    "price_per_night": "$90",
                                },
                            ]
                        ),
                        "system_message": "You are a helpful travel assistant.",
                    }
                ),
            }
        ]
    }

    batch = parse_tool_result(payload, athens(nights=2))

    assert [offer.name for offer in batch.offers] == ["Hotel mit Preis"]
    # Ein Nachtpreis mal die Naechte der Anfrage, nicht aus dem Satz geraten.
    assert batch.offers[0].price_total == Money(18000, "USD")
    assert batch.skipped == ["Hotel ohne Preis: Preis unlesbar ('auf Anfrage')"]


def test_the_arguments_carry_the_window_the_occupancy_and_the_market():
    query = HotelQuery(
        destination="Athen", arrival=date(2026, 11, 10), nights=3,
        adults=2, children=(10, 12), rooms=2,
    )

    assert build_arguments(query) == {
        "query": "Athen",
        "arrival": "2026-11-10",
        "departure": "2026-11-13",
        "adults": 2,
        "rooms": 2,
        "country": "DE",
        "currency": "EUR",
        "children": 2,
        "children_ages": "10-12",
    }


def test_the_body_is_read_as_plain_json_or_as_an_event_stream():
    assert decode_body('{"result": 1}') == {"result": 1}
    assert decode_body("event: message\ndata: {\"result\": 2}\n\n") == {"result": 2}
    assert decode_body(": ping\ndata: [DONE]\ndata: {\"result\": 3}") == {"result": 3}
    with pytest.raises(SourceError):
        decode_body("event: message\n\n")


async def test_a_missing_tool_is_reported_and_not_guessed_around(monkeypatch):
    """Der Server hat zwischen Versionen schon ein Werkzeug entfernt."""
    source = TrivagoMcpSource()
    source._mcp_session_id = "abc"

    class Answer:
        status_code = 200
        headers: dict[str, str] = {}
        text = json.dumps(
            {"jsonrpc": "2.0", "id": 1,
             "error": {"code": -32602, "message": "Unknown tool: trivago-x"}}
        )

    async def fake_post(body, **kwargs):
        return Answer()

    monkeypatch.setattr(source, "_post", fake_post)

    with pytest.raises(SourceError, match="Unknown tool"):
        await source.search(athens())
