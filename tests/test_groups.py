"""Flughafengruppen und das Variantenbudget.

Gruppen sind Daten, keine Konstanten im Code. Diese Datei prueft die Form der
Datei und - ab Task 5 - den Guard gegen Variantenexplosion.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "flightopt" / "data"


def groups() -> dict:
    return json.loads((DATA / "airport_groups.json").read_text(encoding="utf-8"))


def aliases() -> dict:
    return json.loads((DATA / "airport_aliases_de.json").read_text(encoding="utf-8"))


def test_group_file_has_the_expected_shape():
    rows = groups()

    assert len(rows) >= 35
    for code, row in rows.items():
        assert code == code.upper(), code
        assert row["name"], code
        assert row["airports"], code
        assert all(len(a) == 3 and a.isupper() for a in row["airports"]), code
        assert len(row["airports"]) == len(set(row["airports"])), code
        assert isinstance(row.get("aliases", []), list), code


def test_metro_groups_cover_the_far_east_and_the_us():
    rows = groups()

    assert rows["TYO"]["airports"] == ["NRT", "HND"]
    assert rows["SEL"]["airports"] == ["ICN", "GMP"]
    assert rows["OSA"]["airports"] == ["KIX", "ITM"]
    assert rows["NYC"]["airports"] == ["JFK", "EWR", "LGA"]
    assert rows["BJS"]["airports"] == ["PEK", "PKX"]
    assert "MOW" not in rows


def test_country_groups_stay_under_eight_airports():
    # "DE" ist ausgenommen: die Gruppe gab es vorher schon mit 13 Flughaefen
    # und bleibt unveraendert (siehe test_existing_german_groups_are_unchanged).
    rows = groups()
    country_codes = ["JP", "KR", "US-EAST", "US-WEST", "TH", "VN", "TW", "SG",
                     "MY", "ID", "AE", "TR", "GR", "ES", "IT", "PT"]

    for code in country_codes:
        assert code in rows, code
        assert len(rows[code]["airports"]) <= 8, code
    assert "DE" in rows


def test_us_is_only_an_alias_never_a_group():
    rows = groups()

    assert "US" not in rows
    assert "usa" in rows["US-EAST"]["aliases"]
    assert "usa" in rows["US-WEST"]["aliases"]


def test_existing_german_groups_are_unchanged():
    rows = groups()

    assert rows["DE-OST"]["name"] == "Berlin, Leipzig/Halle, Dresden"
    assert rows["DE-OST"]["city"] == "Ostflughäfen"
    assert rows["DE-OST"]["airports"] == ["BER", "LEJ", "DRS"]
    assert rows["DE"]["name"] == "Alle deutschen Flughäfen"
    assert rows["DE"]["airports"] == ["BER", "LEJ", "DRS", "HAM", "FRA", "DUS",
                                      "CGN", "STR", "MUC", "NUE", "BRE", "HAJ", "NRN"]


def test_alias_file_keeps_the_old_hand_written_aliases():
    rows = aliases()

    assert "malle" in [a.lower() for a in rows["PMI"]]
    assert "kreta" in [a.lower() for a in rows["HER"]]
    assert "saloniki" in [a.lower() for a in rows["SKG"]]
    assert "muenchen" in [a.lower() for a in rows["MUC"]]


def test_alias_file_covers_the_important_long_haul_cities():
    rows = aliases()
    wanted = {
        "NRT": "tokio", "ICN": "seoul", "PEK": "peking", "DEL": "neu-delhi",
        "CPT": "kapstadt", "JFK": "new york", "LAX": "los angeles",
        "MEX": "mexiko-stadt", "HAV": "havanna", "GRU": "sao paulo",
        "CAI": "kairo", "RAK": "marrakesch", "NBO": "nairobi",
        "JNB": "johannesburg", "KEF": "reykjavik", "SIN": "singapur",
        "HKG": "hongkong", "TPE": "taipeh", "SYD": "sydney", "YYZ": "toronto",
    }

    assert len(rows) >= 100
    for code, alias in wanted.items():
        assert code in rows, code
        assert alias in [a.lower() for a in rows[code]], code
    for code, names in rows.items():
        assert len(code) == 3 and code.isupper(), code
        assert names and all(isinstance(n, str) and n.strip() for n in names), code


# --- Gruppencodes kollidieren nicht mit Flughafencodes ------------------------

from flightopt.domain import airports as registry  # noqa: E402

METRO_GROUPS_WITH_A_NAMESAKE_AIRPORT = {
    "IST-ALL": ("IST", "SAW"),
    "BKK-ALL": ("BKK", "DMK"),
    "DXB-ALL": ("DXB", "DWC"),
    "SHA-ALL": ("PVG", "SHA"),
}


def test_no_group_code_shadows_a_different_airport():
    """Ein Code darf nicht zwei Dinge bedeuten.

    Solange die Gruppe 'SHA' hiess, suchte `expand_code("SHA")` still auch in
    Pudong, und die Vorschlagsliste zeigte zweimal dieselbe Zeile. Eine Gruppe
    darf den Code eines Flughafens nur tragen, wenn sie genau diesen einen
    Flughafen enthaelt (BER).
    """
    clashes = {
        code: group.airports
        for code, group in registry.GROUPS.items()
        if registry.by_code(code) is not None and group.airports != (code,)
    }

    assert not clashes, f"Gruppencodes ueberdecken Flughaefen: {clashes}"


def test_a_code_that_is_also_an_airport_stays_the_airport():
    """Der nackte Code meint den Flughafen, die Gruppe heisst '-ALL'.

    Wer beide Flughaefen der Metro-Region will, tippt IST-ALL; wer IST tippt,
    bekommt den einen Flughafen und nicht heimlich Sabiha Gokcen dazu.
    """
    for group_code, members in METRO_GROUPS_WITH_A_NAMESAKE_AIRPORT.items():
        airport_code = group_code.removesuffix("-ALL")

        assert registry.expand_code(airport_code) == (airport_code,), airport_code
        assert registry.expand_code(group_code) == members, group_code
        assert registry.resolve(airport_code) == airport_code, airport_code
        assert registry.resolve(group_code) == group_code, group_code


def test_the_type_ahead_offers_istanbul_once_as_a_group_and_once_as_an_airport():
    hits = registry.search("Istanbul", 5)
    codes = [h.code for h in hits]

    assert codes[0] == "IST-ALL"
    assert hits[0].as_dict()["kind"] == "group"
    assert codes.count("IST") == 1
    assert codes.index("IST-ALL") < codes.index("IST")


# --- Variantenbudget ---------------------------------------------------------

from datetime import date  # noqa: E402

import pytest  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from flightopt.api import main  # noqa: E402

W = {"window_start": date(2027, 3, 1), "window_end": date(2027, 4, 30)}


def test_variant_counts_multiplies_group_sizes():
    per_leg, total = registry.variant_counts(["BER", "TYO", "SEL", "BER"])

    assert per_leg == [2, 4, 2]
    assert total == 4


def test_variant_budget_allows_the_japan_korea_route():
    registry.check_variant_budget(["BER", "TYO", "SEL", "BER"])


def test_variant_budget_rejects_a_leg_with_too_many_pairs():
    with pytest.raises(registry.TooManyVariants, match="Flughafenkombinationen"):
        # DE (13) x TR (6) = 78 Paare auf einem Leg.
        registry.check_variant_budget(["DE", "TR"])


def test_variant_budget_names_the_leg_and_the_way_out():
    """Die Meldung landet unveraendert im Formular, also muss sie tragen."""
    with pytest.raises(registry.TooManyVariants) as exc:
        # LON (6) x GR (8) = 48 Paare, das erste Leg bleibt unter der Grenze.
        registry.check_variant_budget(["BER", "LON", "GR"])

    assert str(exc.value) == (
        "Leg 2 (LON nach GR) ergibt 48 Flughafenkombinationen, erlaubt sind 24. "
        "Ersetze die Gruppe GR oder LON durch einen einzelnen Flughafen."
    )


def test_variant_budget_rejects_too_many_routes_overall():
    with pytest.raises(registry.TooManyVariants, match="Routenvarianten") as exc:
        # 3 x 8 x 3 = 72 Routenvarianten, jedes einzelne Leg bleibt erlaubt.
        registry.check_variant_budget(["DE-OST", "GR", "DE-OST"])

    assert str(exc.value) == (
        "Die Route DE-OST-GR-DE-OST ergibt 72 Routenvarianten, erlaubt sind 64. "
        "Ersetze die Gruppe GR oder DE-OST durch einen einzelnen Flughafen."
    )


def test_search_request_rejects_a_group_explosion():
    with pytest.raises(registry.TooManyVariants):
        main.SearchRequest(airports=["DE", "TR"], trip="one_way", **W).to_specs()


@pytest.mark.asyncio
async def test_estimate_answers_422_on_a_group_explosion():
    req = main.SearchRequest(airports=["DE", "TR"], trip="one_way", **W)

    with pytest.raises(HTTPException) as exc:
        await main.estimate(req)

    assert exc.value.status_code == 422
    assert "Flughafenkombinationen" in exc.value.detail


@pytest.mark.asyncio
async def test_search_answers_422_on_a_group_explosion():
    req = main.SearchRequest(airports=["DE", "TR"], trip="one_way", **W)

    with pytest.raises(HTTPException) as exc:
        await main.start_search(req)

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_estimate_still_answers_400_on_a_broken_route():
    req = main.SearchRequest(airports=["BER"], trip="return", **W)

    with pytest.raises(HTTPException) as exc:
        await main.estimate(req)

    assert exc.value.status_code == 400


def test_every_group_airport_exists_in_base():
    """Eine Gruppe, die einen unbekannten Code nennt, sucht ins Leere.

    Frueher war das ein Handgriff nach jedem Neubau der Basis; als Test faellt
    es beim naechsten Lauf auf statt beim naechsten Nutzer.
    """
    missing = {
        code: [a for a in group.airports if registry.by_code(a) is None]
        for code, group in registry.GROUPS.items()
    }
    missing = {code: gone for code, gone in missing.items() if gone}

    assert not missing, f"Gruppen mit unbekannten Flughaefen: {missing}"
