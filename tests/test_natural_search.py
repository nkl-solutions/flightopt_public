"""Natural-language route input stays a thin layer over the normal search."""

from __future__ import annotations

import pytest

from flightopt.api import main
from flightopt.domain.natural_search import parse_search_text


def test_natural_search_extracts_multi_stop_route_with_specific_origin():
    parsed = parse_search_text(
        "Flug von Deutschland aus Berlin nach Istanbul, "
        "von Istanbul nach Athen und von Athen wieder nach Berlin"
    )

    assert parsed.trip == "multi"
    assert parsed.airports == ["BER", "IST", "ATH", "BER"]
    assert parsed.labels == ["Berlin", "Istanbul", "Athen", "Berlin"]


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
