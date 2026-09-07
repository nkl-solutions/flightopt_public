"""Natural-language route input stays a thin layer over the normal search."""

from __future__ import annotations

import pytest

from flightopt.api import main
from flightopt.domain import airports
from flightopt.domain.natural_search import parse_search_text


def test_natural_search_extracts_multi_stop_route_with_specific_origin():
    """Istanbul heisst beide Flughaefen, Berlin und Athen je genau einen.

    Der Satz nennt eine Stadt, keinen Flughafen: fuer eine Preissuche ist die
    Metro-Gruppe die Antwort, sonst faellt Sabiha Gokcen still unter den Tisch.
    Staedte ohne Gruppe bleiben beim Flughafen.
    """
    parsed = parse_search_text(
        "Flug von Deutschland aus Berlin nach Istanbul, "
        "von Istanbul nach Athen und von Athen wieder nach Berlin"
    )

    assert parsed.trip == "multi"
    assert parsed.airports == ["BER", "IST-ALL", "ATH", "BER"]
    assert parsed.labels == ["Berlin", "Istanbul", "Athen", "Berlin"]


def test_natural_search_uses_the_same_resolver_as_the_form():
    """CLI, Sprache und Formular duerfen nicht auseinanderlaufen."""
    parsed = parse_search_text("Von Tokio nach London")

    assert parsed.airports == [airports.resolve("Tokio"), airports.resolve("London")]
    assert parsed.airports == ["TYO", "LON"]


def test_natural_search_keeps_airport_group_when_no_specific_origin_follows():
    parsed = parse_search_text("Von Ostflughäfen nach Athen und zurück")

    assert parsed.trip == "return"
    assert parsed.airports == ["DE-OST", "ATH"]
    assert parsed.labels == ["Ostflughäfen", "Athen"]


@pytest.mark.asyncio
async def test_parse_search_endpoint_returns_prefill_payload():
    response = await main.parse_search_text_endpoint(
        main.NaturalSearchRequest(text="Nur hin von Berlin nach Athen")
    )

    assert response["trip"] == "one_way"
    assert response["airports"] == ["BER", "ATH"]
    assert response["hops"][0] == {"code": "BER", "label": "Berlin"}
